import os
import itertools
import numpy as np
import torch
from PIL import Image, ImageOps
import cv2
import psutil

import folder_paths
from comfy.utils import common_upscale, ProgressBar
from .logger import logger
from .utils import BIGMAX, DIMMAX, calculate_file_hash, get_sorted_dir_files_from_directory, get_audio, lazy_eval, hash_path, validate_path


video_extensions = ['webm', 'mp4', 'mkv', 'gif']


def is_gif(filename) -> bool:
    file_parts = filename.split('.')
    return len(file_parts) > 1 and file_parts[-1] == "gif"


def target_size(width, height, force_size, custom_width, custom_height) -> tuple[int, int]:
    if force_size == "Custom":
        return (custom_width, custom_height)
    elif force_size == "Custom Height":
        force_size = "?x"+str(custom_height)
    elif force_size == "Custom Width":
        force_size = str(custom_width)+"x?"

    if force_size != "Disabled":
        force_size = force_size.split("x")
        if force_size[0] == "?":
            width = (width*int(force_size[1]))//height
            #Limit to a multple of 8 for latent conversion
            width = int(width)+4 & ~7
            height = int(force_size[1])
        elif force_size[1] == "?":
            height = (height*int(force_size[0]))//width
            height = int(height)+4 & ~7
            width = int(force_size[0])
        else:
            width = int(force_size[0])
            height = int(force_size[1])
    return (width, height)

def cv_frame_generator(video, force_rate, frame_load_cap, skip_first_frames,
                       select_every_nth, meta_batch=None, unique_id=None):
    video_cap = cv2.VideoCapture(video)
    if not video_cap.isOpened():
        raise ValueError(f"{video} could not be loaded with cv.")
    pbar = ProgressBar(frame_load_cap) if frame_load_cap > 0 else None

    # extract video metadata
    fps = video_cap.get(cv2.CAP_PROP_FPS)
    width = int(video_cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(video_cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    total_frames = int(video_cap.get(cv2.CAP_PROP_FRAME_COUNT))
    duration = total_frames / fps

    # set video_cap to look at start_index frame
    total_frame_count = 0
    total_frames_evaluated = -1
    frames_added = 0
    base_frame_time = 1 / fps
    prev_frame = None

    if force_rate == 0:
        target_frame_time = base_frame_time
    else:
        target_frame_time = 1/force_rate

    yield (width, height, fps, duration, total_frames, target_frame_time)

    time_offset=target_frame_time - base_frame_time
    while video_cap.isOpened():
        if time_offset < target_frame_time:
            is_returned = video_cap.grab()
            # if didn't return frame, video has ended
            if not is_returned:
                break
            time_offset += base_frame_time
        if time_offset < target_frame_time:
            continue
        time_offset -= target_frame_time
        # if not at start_index, skip doing anything with frame
        total_frame_count += 1
        if total_frame_count <= skip_first_frames:
            continue
        else:
            total_frames_evaluated += 1

        # if should not be selected, skip doing anything with frame
        if total_frames_evaluated%select_every_nth != 0:
            continue

        # opencv loads images in BGR format (yuck), so need to convert to RGB for ComfyUI use
        # follow up: can videos ever have an alpha channel?
        # To my testing: No. opencv has no support for alpha
        unused, frame = video_cap.retrieve()
        frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        # convert frame to comfyui's expected format
        # TODO: frame contains no exif information. Check if opencv2 has already applied
        frame = np.array(frame, dtype=np.float32)
        torch.from_numpy(frame).div_(255)
        if prev_frame is not None:
            inp  = yield prev_frame
            if inp is not None:
                #ensure the finally block is called
                return
        prev_frame = frame
        frames_added += 1
        if pbar is not None:
            pbar.update_absolute(frames_added, frame_load_cap)
        # if cap exists and we've reached it, stop processing frames
        if frame_load_cap > 0 and frames_added >= frame_load_cap:
            break
    if meta_batch is not None:
        meta_batch.inputs.pop(unique_id)
        meta_batch.has_closed_inputs = True
    if prev_frame is not None:
        yield prev_frame

def load_video_cv(video: str, force_rate: int, force_size: str,
                  custom_width: int,custom_height: int, frame_load_cap: int,
                  skip_first_frames: int, select_every_nth: int,
                  meta_batch=None, unique_id=None, memory_limit_mb=None):
    if meta_batch is None or unique_id not in meta_batch.inputs:
        gen = cv_frame_generator(video, force_rate, frame_load_cap, skip_first_frames,
                                 select_every_nth, meta_batch, unique_id)
        (width, height, fps, duration, total_frames, target_frame_time) = next(gen)

        if meta_batch is not None:
            meta_batch.inputs[unique_id] = (gen, width, height, fps, duration, total_frames, target_frame_time)

    else:
        (gen, width, height, fps, duration, total_frames, target_frame_time) = meta_batch.inputs[unique_id]

    if memory_limit_mb is not None:
        memory_limit *= 2 ** 20
    else:
        #TODO: verify if garbage collection should be performed here.
        #leaves ~128 MB unreserved for safety
        memory_limit = (psutil.virtual_memory().available + psutil.swap_memory().free) - 2 ** 27
    #space required to load as f32, exist as latent with wiggle room, decode to f32
    max_loadable_frames = int(memory_limit//(width*height*3*(4+4+1/10)))
    if meta_batch is not None:
        if meta_batch.frames_per_batch > max_loadable_frames:
            raise RuntimeError(f"Meta Batch set to {meta_batch.frames_per_batch} frames but only {max_loadable_frames} can fit in memory")
        gen = itertools.islice(gen, meta_batch.frames_per_batch)
    else:
        original_gen = gen
        gen = itertools.islice(gen, max_loadable_frames)

    #Some minor wizardry to eliminate a copy and reduce max memory by a factor of ~2
    images = torch.from_numpy(np.fromiter(gen, np.dtype((np.float32, (height, width, 3)))))
    if meta_batch is None:
        try:
            next(original_gen)
            raise RuntimeError(f"Memory limit hit after loading {len(images)} frames. Stopping execution.")
        except StopIteration:
            pass
    if len(images) == 0:
        raise RuntimeError("No frames generated")
    if force_size != "Disabled":
        new_size = target_size(width, height, force_size, custom_width, custom_height)
        if new_size[0] != width or new_size[1] != height:
            s = images.movedim(-1,1)
            s = common_upscale(s, new_size[0], new_size[1], "lanczos", "center")
            images = s.movedim(1,-1)

    #Setup lambda for lazy audio capture
    audio = lambda : get_audio(video, skip_first_frames * target_frame_time,
                               frame_load_cap*target_frame_time*select_every_nth)
    #Adjust target_frame_time for select_every_nth
    target_frame_time *= select_every_nth
    video_info = {
        "source_fps": fps,
        "source_frame_count": total_frames,
        "source_duration": duration,
        "source_width": width,
        "source_height": height,
        "loaded_fps": 1/target_frame_time,
        "loaded_frame_count": len(images),
        "loaded_duration": len(images) * target_frame_time,
        "loaded_width": images.shape[2],
        "loaded_height": images.shape[1],
    }

    return (images, len(images), lazy_eval(audio), video_info)


class LoadVideoUpload:
    @classmethod
    def INPUT_TYPES(s):
        input_dir = folder_paths.get_input_directory()
        files = []
        for f in os.listdir(input_dir):
            if os.path.isfile(os.path.join(input_dir, f)):
                file_parts = f.split('.')
                if len(file_parts) > 1 and (file_parts[-1] in video_extensions):
                    files.append(f)
        return {"required": {
                    "video": (sorted(files),),
                     "force_rate": ("INT", {"default": 0, "min": 0, "max": 60, "step": 1}),
                     "force_size": (["Disabled", "Custom Height", "Custom Width", "Custom", "256x?", "?x256", "256x256", "512x?", "?x512", "512x512"],),
                     "custom_width": ("INT", {"default": 512, "min": 0, "max": DIMMAX, "step": 8}),
                     "custom_height": ("INT", {"default": 512, "min": 0, "max": DIMMAX, "step": 8}),
                     "frame_load_cap": ("INT", {"default": 0, "min": 0, "max": BIGMAX, "step": 1}),
                     "skip_first_frames": ("INT", {"default": 0, "min": 0, "max": BIGMAX, "step": 1}),
                     "select_every_nth": ("INT", {"default": 1, "min": 1, "max": BIGMAX, "step": 1}),
                     },
                "optional": {
                    "meta_batch": ("VHS_BatchManager",)
                },
                "hidden": {
                    "unique_id": "UNIQUE_ID"
                },
                }

    CATEGORY = "Video Helper Suite 🎥🅥🅗🅢"

    RETURN_TYPES = ("IMAGE", "INT", "VHS_AUDIO", "VHS_VIDEOINFO",)
    RETURN_NAMES = ("IMAGE", "frame_count", "audio", "video_info",)

    FUNCTION = "load_video"

    def load_video(self, **kwargs):
        kwargs['video'] = folder_paths.get_annotated_filepath(kwargs['video'].strip("\""))
        return load_video_cv(**kwargs)

    @classmethod
    def IS_CHANGED(s, video, **kwargs):
        image_path = folder_paths.get_annotated_filepath(video)
        return calculate_file_hash(image_path)

    @classmethod
    def VALIDATE_INPUTS(s, video, force_size, **kwargs):
        if not folder_paths.exists_annotated_filepath(video):
            return "Invalid video file: {}".format(video)
        return True


class LoadVideoPath:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "video": ("STRING", {"default": "X://insert/path/here.mp4", "vhs_path_extensions": video_extensions}),
                "force_rate": ("INT", {"default": 0, "min": 0, "max": 60, "step": 1}),
                 "force_size": (["Disabled", "Custom Height", "Custom Width", "Custom", "256x?", "?x256", "256x256", "512x?", "?x512", "512x512"],),
                 "custom_width": ("INT", {"default": 512, "min": 0, "max": DIMMAX, "step": 8}),
                 "custom_height": ("INT", {"default": 512, "min": 0, "max": DIMMAX, "step": 8}),
                "frame_load_cap": ("INT", {"default": 0, "min": 0, "max": BIGMAX, "step": 1}),
                "skip_first_frames": ("INT", {"default": 0, "min": 0, "max": BIGMAX, "step": 1}),
                "select_every_nth": ("INT", {"default": 1, "min": 1, "max": BIGMAX, "step": 1}),
            },
            "optional": {
                "meta_batch": ("VHS_BatchManager",)
            },
            "hidden": {
                "unique_id": "UNIQUE_ID"
            },
        }

    CATEGORY = "Video Helper Suite 🎥🅥🅗🅢"

    RETURN_TYPES = ("IMAGE", "INT", "VHS_AUDIO", "VHS_VIDEOINFO",)
    RETURN_NAMES = ("IMAGE", "frame_count", "audio", "video_info",)

    FUNCTION = "load_video"

    def load_video(self, **kwargs):
        if kwargs['video'] is None or validate_path(kwargs['video']) != True:
            raise Exception("video is not a valid path: " + kwargs['video'])
        return load_video_cv(**kwargs)

    @classmethod
    def IS_CHANGED(s, video, **kwargs):
        return hash_path(video)

    @classmethod
    def VALIDATE_INPUTS(s, video, **kwargs):
        return validate_path(video, allow_none=True)

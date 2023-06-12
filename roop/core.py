#!/usr/bin/env python3

import os
import sys
import threading
# single thread doubles performance of gpu-mode - needs to be set before torch import
if any(arg.startswith('--gpu-vendor') for arg in sys.argv):
    os.environ['OMP_NUM_THREADS'] = '1'
# reduce tensorflow log level
os.environ['TF_CPP_MIN_LOG_LEVEL'] = '2'
import warnings
from typing import List
import platform
import signal
import shutil
import argparse
import psutil
import torch
import tensorflow
import multiprocessing
from opennsfw2 import predict_video_frames, predict_image
import cv2

import roop.globals
import roop.ui as ui
import roop.swapper
import roop.enhancer
import roop.events as events
from roop.utilities import has_image_extension, is_image, is_video, detect_fps, create_video, extract_frames, copy_temp_image, get_temp_frame_paths, restore_audio, create_temp, move_temp, clean_temp
from roop.analyser import get_one_face

if 'ROCMExecutionProvider' in roop.globals.providers:
    del torch

warnings.simplefilter(action='ignore', category=FutureWarning)


def parse_args() -> None:
    signal.signal(signal.SIGINT, lambda signal_number, frame: destroy())
    parser = argparse.ArgumentParser()
    parser.add_argument('-f', '--face', help='use a face image', dest='source_path')
    parser.add_argument('-t', '--target', help='replace image or video with face', dest='target_path')
    parser.add_argument('-o', '--output', help='save output to this file', dest='output_path')
    parser.add_argument('--frame-processor', help='list of frame processors to run', dest='frame_processor', default=['face-swapper'], choices=['face-swapper', 'face-enhancer'], nargs='+')
    parser.add_argument('--keep-fps', help='maintain original fps', dest='keep_fps', action='store_true', default=False)
    parser.add_argument('--keep-audio', help='maintain original audio', dest='keep_audio', action='store_true', default=True)
    parser.add_argument('--keep-frames', help='keep frames directory', dest='keep_frames', action='store_true', default=False)
    parser.add_argument('--many-faces', help='swap every face in the frame', dest='many_faces', action='store_true', default=False)
    parser.add_argument('--video-encoder', help='adjust output video encoder', dest='video_encoder', default='libx264', choices=['libx264', 'libx265'])
    parser.add_argument('--video-quality', help='adjust output video quality', dest='video_quality', type=int, default=18)
    parser.add_argument('--max-memory', help='maximum amount of RAM in GB to be used', dest='max_memory', type=int, default=suggest_max_memory())
    parser.add_argument('--cpu-cores', help='number of CPU cores to use', dest='cpu_cores', type=int, default=suggest_cpu_cores())
    parser.add_argument('--execution-provider', help='execution provider', dest='execution_provider', default='cpu', choices=['cpu', 'directml'])
    parser.add_argument('--gpu-threads', help='number of threads to be use for the GPU', dest='gpu_threads', type=int, default=suggest_gpu_threads())
    parser.add_argument('--gpu-vendor', help='select your GPU vendor', dest='gpu_vendor', choices=['apple', 'amd', 'nvidia'])

    args = parser.parse_known_args()[0]

    roop.globals.source_path = args.source_path
    roop.globals.target_path = args.target_path
    roop.globals.output_path = args.output_path
    roop.globals.frame_processors = args.frame_processor
    roop.globals.headless = args.source_path or args.target_path or args.output_path
    roop.globals.keep_fps = args.keep_fps
    roop.globals.keep_audio = args.keep_audio
    roop.globals.keep_frames = args.keep_frames
    roop.globals.many_faces = args.many_faces
    roop.globals.video_encoder = args.video_encoder
    roop.globals.video_quality = args.video_quality
    roop.globals.max_memory = args.max_memory
    roop.globals.cpu_cores = args.cpu_cores
    roop.globals.gpu_threads = args.gpu_threads

    if args.execution_provider == 'directml':
        roop.globals.providers = ['DmlExecutionProvider']
        roop.globals.gpu_vendor = 'other'
    if args.gpu_vendor:
        roop.globals.gpu_vendor = args.gpu_vendor
    else:
        roop.globals.providers = ['CPUExecutionProvider']


def suggest_max_memory() -> int:
    if platform.system().lower() == 'darwin':
        return 4
    return 16


def suggest_gpu_threads() -> int:
    if 'DmlExecutionProvider' in roop.globals.providers:
        return 1
    if 'ROCMExecutionProvider' in roop.globals.providers:
        return 2
    return 8


def suggest_cpu_cores() -> int:
    if platform.system().lower() == 'darwin':
        return 2
    return int(max(psutil.cpu_count() / 2, 1))


def limit_resources() -> None:
    # prevent tensorflow memory leak
    gpus = tensorflow.config.experimental.list_physical_devices('GPU')
    for gpu in gpus:
        tensorflow.config.experimental.set_memory_growth(gpu, True)
    if roop.globals.max_memory:
        memory = roop.globals.max_memory * 1024 ** 3
        if platform.system().lower() == 'darwin':
            memory = roop.globals.max_memory * 1024 ** 6
        if platform.system().lower() == 'windows':
            import ctypes
            kernel32 = ctypes.windll.kernel32
            kernel32.SetProcessWorkingSetSize(-1, ctypes.c_size_t(memory), ctypes.c_size_t(memory))
        else:
            import resource
            resource.setrlimit(resource.RLIMIT_DATA, (memory, memory))


def release_resources() -> None:
    if roop.globals.gpu_vendor == 'nvidia':
        torch.cuda.empty_cache()


def pre_check() -> None:
    if sys.version_info < (3, 9):
        quit('Python version is not supported - please upgrade to 3.9 or higher.')
    if not shutil.which('ffmpeg'):
        quit('ffmpeg is not installed!')
    if roop.globals.gpu_vendor == 'apple':
        if 'CoreMLExecutionProvider' not in roop.globals.providers:
            quit('You are using --gpu=apple flag but CoreML is not available or properly installed on your system.')
    if roop.globals.gpu_vendor == 'amd':
        if 'ROCMExecutionProvider' not in roop.globals.providers:
            quit('You are using --gpu=amd flag but ROCM is not available or properly installed on your system.')
    if roop.globals.gpu_vendor == 'nvidia':
        if not torch.cuda.is_available():
            quit('You are using --gpu=nvidia flag but CUDA is not available or properly installed on your system.')
        if torch.version.cuda > '11.8':
            quit(f'CUDA version {torch.version.cuda} is not supported - please downgrade to 11.8')
        if torch.version.cuda < '11.4':
            quit(f'CUDA version {torch.version.cuda} is not supported - please upgrade to 11.8')
        if torch.backends.cudnn.version() < 8220:
            quit(f'CUDNN version { torch.backends.cudnn.version()} is not supported - please upgrade to 8.9.1')
        if torch.backends.cudnn.version() > 8910:
            quit(f'CUDNN version { torch.backends.cudnn.version()} is not supported - please downgrade to 8.9.1')


def conditional_process_video(source_path: str, temp_frame_paths: List[str], process_video) -> None:
    pool_amount = len(temp_frame_paths) // roop.globals.cpu_cores
    if pool_amount > 2 and roop.globals.cpu_cores > 1 and roop.globals.gpu_vendor is None:
        POOL = multiprocessing.Pool(roop.globals.cpu_cores, maxtasksperchild=1)
        pools = []
        for i in range(0, len(temp_frame_paths), pool_amount):
            pool = POOL.apply_async(process_video, args=(source_path, temp_frame_paths[i:i + pool_amount], 'cpu'))
            pools.append(pool)
        for pool in pools:
            pool.get()
        POOL.close()
        POOL.join()
    else:
         process_video(roop.globals.source_path, temp_frame_paths, 'gpu')


def update_status(message: str) -> None:
    events.on_change_status('Status: ' + message)


@events.change_status
def console_status(message: str) -> None:
    print(message)


def start() -> None:
    if not roop.globals.source_path or not os.path.isfile(roop.globals.source_path):
        update_status('Select an image that contains a face.')
        return
    elif not roop.globals.target_path or not os.path.isfile(roop.globals.target_path):
        update_status('Select an image or video target!')
        return
    test_face = get_one_face(cv2.imread(roop.globals.source_path))
    if not test_face:
        update_status('No face detected in source image. Please try with another one!')
        return
    create_temp(roop.globals.target_path)

    # process image to image
    if has_image_extension(roop.globals.target_path):
        if predict_image(roop.globals.target_path) > 0.85:
            destroy()
        temp_image_path = copy_temp_image(roop.globals.target_path)
        if 'face-swapper' in roop.globals.frame_processors:
            update_status('Swapping in progress...')
            roop.swapper.process_image(roop.globals.source_path, temp_image_path, temp_image_path)
        if roop.globals.gpu_vendor == 'nvidia' and 'face-enhancer' in roop.globals.frame_processors:
            update_status('Enhancing in progress...')
            roop.enhancer.process_image(roop.globals.source_path, temp_image_path, temp_image_path)
        if os.path.isfile(temp_image_path):
            shutil.move(temp_image_path, roop.globals.output_path, copy_function=shutil.copy2)
        if is_image(roop.globals.target_path):
            update_status('Processing to image succeed!')
        else:
            update_status('Processing to image failed!')
        return
    # process image to videos
    seconds, probabilities = predict_video_frames(video_path=roop.globals.target_path, frame_interval=100)
    if any(probability > 0.85 for probability in probabilities):
        destroy()
    update_status('Creating temp resources...')
    create_temp(roop.globals.target_path)
    update_status('Extracting frames...')
    extract_frames(roop.globals.target_path)
    temp_frame_paths = get_temp_frame_paths(roop.globals.target_path)
    if 'face-swapper' in roop.globals.frame_processors:
        update_status('Swapping in progress...')
        conditional_process_video(roop.globals.source_path, temp_frame_paths, roop.swapper.process_video)
    release_resources()
    # limit to one gpu thread
    roop.globals.gpu_threads = 1
    if roop.globals.gpu_vendor == 'nvidia' and 'face-enhancer' in roop.globals.frame_processors:
        update_status('Enhancing in progress...')
        conditional_process_video(roop.globals.source_path, temp_frame_paths, roop.enhancer.process_video)
    release_resources()
    if roop.globals.keep_fps:
        update_status('Detecting fps...')
        fps = detect_fps(roop.globals.target_path)
        update_status(f'Creating video with {fps} fps...')
        create_video(roop.globals.target_path, fps)
    else:
        update_status('Creating video with 30.0 fps...')
        create_video(roop.globals.target_path)
    if roop.globals.keep_audio:
        if roop.globals.keep_fps:
            update_status('Restoring audio...')
        else:
            update_status('Restoring audio might cause issues as fps are not kept...')
        restore_audio(roop.globals.target_path, roop.globals.output_path)
    else:
        move_temp(roop.globals.target_path, roop.globals.output_path)
    clean_temp(roop.globals.target_path)
    if is_video(roop.globals.target_path):
        update_status('Processing to video succeed!')
    else:
        update_status('Processing to video failed!')

    clean_temp(roop.globals.target_path)


def destroy() -> None:
    if roop.globals.target_path:
        clean_temp(roop.globals.target_path)
    quit()


@events.start
def start_in_thread():
    threading.Thread(target=start).start()


def run() -> None:
    parse_args()
    pre_check()
    if 'face-swapper' in roop.globals.frame_processors:
        roop.swapper.pre_check()
    limit_resources()
    if roop.globals.headless:
        start()
    else:
        window = ui.init(destroy)
        window.mainloop()

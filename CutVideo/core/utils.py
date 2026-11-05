import os
import sys
import shutil
import subprocess
import logging
from datetime import datetime


def find_binary(binary_name):
    """Find binary_name (ffmpeg/ffprobe) in PyInstaller temp folder,
    executable directory, project folder, or system PATH."""
    exe_name = binary_name + '.exe' if sys.platform == 'win32' else binary_name
    if hasattr(sys, '_MEIPASS'):
        meipass_path = os.path.join(sys._MEIPASS, exe_name)
        if os.path.exists(meipass_path):
            return meipass_path
    if getattr(sys, 'frozen', False):
        exe_dir = os.path.dirname(sys.executable)
    else:
        exe_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    candidate = os.path.join(exe_dir, exe_name)
    if os.path.exists(candidate):
        return candidate
    path = shutil.which(binary_name)
    if path and os.path.exists(path):
        return path
    sibling_autocut = 'E:\\Tat ca code\\autocut_full-20250829T055535Z-1-001\\autocut_full'
    candidate_sibling = os.path.join(sibling_autocut, exe_name)
    if os.path.exists(candidate_sibling):
        return candidate_sibling
    return binary_name


FFMPEG_PATH = find_binary('ffmpeg')
FFPROBE_PATH = find_binary('ffprobe')


def check_h264_encoder():
    """Detect available encoder for FFmpeg (h264_nvenc, h264_amf, h264_qsv, or libx264)."""
    try:
        startupinfo = None
        if sys.platform == 'win32':
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        res = subprocess.run([FFMPEG_PATH, '-encoders'], stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True, encoding='utf-8',
                             errors='ignore', startupinfo=startupinfo)
        output = res.stdout
        if 'h264_nvenc' in output:
            return ('h264_nvenc', 'Nvidia NVENC (GPU)')
        if 'h264_amf' in output:
            return ('h264_amf', 'AMD AMF (GPU)')
        if 'h264_qsv' in output:
            return ('h264_qsv', 'Intel QSV (GPU)')
        return ('libx264', 'CPU (libx264)')
    except Exception as e:
        return ('libx264', f'CPU (libx264 - Error checking GPU: {e})')


def log_success(success_log_path, video_path, duration, segments_count, elapsed_time):
    """Log successfully processed video."""
    try:
        with open(success_log_path, 'a', encoding='utf-8') as f:
            f.write(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')},{video_path},"
                    f"{duration:.2f}s,{segments_count},{elapsed_time:.2f}s\n")
    except Exception as e:
        print(f'Error writing to success log: {e}')


def log_error(error_log_path, video_path, error_msg):
    """Log error for failed video processing."""
    try:
        with open(error_log_path, 'a', encoding='utf-8') as f:
            f.write(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')},{video_path},\"{error_msg}\"\n")
    except Exception as e:
        print(f'Error writing to error log: {e}')


def cleanup_temp_dir(temp_dir):
    """Safely remove temp audio files."""
    if os.path.exists(temp_dir):
        try:
            shutil.rmtree(temp_dir)
        except Exception:
            return

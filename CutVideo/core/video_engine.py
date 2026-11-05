import math
import os
import sys
import json
import time
import subprocess
import re
import wave
import textwrap
import tempfile
import threading
import glob
import unicodedata
import numpy as np
from .utils import FFMPEG_PATH, FFPROBE_PATH, check_h264_encoder, log_success, log_error
from .title_tools import load_protected_phrases


def read_wav_pcm16(wav_path):
    """Read 16-bit PCM WAV file using standard library wave module."""
    with wave.open(wav_path, 'rb') as wf:
        sr = wf.getframerate()
        frames = wf.readframes(wf.getnframes())
        data = np.frombuffer(frames, dtype=np.int16)
        return (sr, data)


def get_video_duration(video_path):
    """Retrieve exact duration of a video using ffprobe in seconds."""
    video_path = os.path.normpath(video_path)
    cmd = [FFPROBE_PATH, '-v', 'error', '-show_entries', 'format=duration',
           '-of', 'default=noprint_wrappers=1:nokey=1', video_path]
    startupinfo = None
    if sys.platform == 'win32':
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                         encoding='utf-8', errors='ignore', startupinfo=startupinfo, check=True)
    return float(res.stdout.strip())


def extract_audio_ffmpeg(video_path, output_wav_path, sample_rate=22050, max_seconds=None):
    """Directly extract PCM audio from video using FFmpeg."""
    video_path = os.path.normpath(video_path)
    output_wav_path = os.path.normpath(output_wav_path)
    cmd = [FFMPEG_PATH, '-ss', '0', '-i', video_path]
    if max_seconds:  # chỉ trích audio đoạn đầu -> nhanh hơn nhiều với video dài
        cmd += ['-t', f'{float(max_seconds):.3f}']
    cmd += ['-vn', '-sn', '-dn', '-ac', '1', '-ar', str(sample_rate), '-c:a', 'pcm_s16le', '-y', output_wav_path]
    startupinfo = None
    if sys.platform == 'win32':
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, startupinfo=startupinfo)
    if res.returncode != 0:
        err_detail = res.stderr.decode('utf-8', errors='ignore').strip()
        raise RuntimeError(f'Không thể trích xuất luồng âm thanh (FFmpeg code {res.returncode}): {err_detail}')
    return output_wav_path


def calculate_rms_vectorized(y, frame_length, hop_length):
    """Vectorized RMS calculation using NumPy stride tricks."""
    if len(y) < frame_length:
        return np.array([], dtype=np.float32)
    num_frames = 1 + (len(y) - frame_length) // hop_length
    shape = (num_frames, frame_length)
    strides = (y.strides[0] * hop_length, y.strides[0])
    frames = np.lib.stride_tricks.as_strided(y, shape=shape, strides=strides)
    rms = np.sqrt(np.mean(frames ** 2, axis=1))
    return rms


def find_break_points(video_path, temp_dir, min_len=220, max_len=360, sr=8000, max_segments=None):
    """Analyze video audio track and calculate silence break timestamps.
    Enforces min_len <= segment_length <= max_len for all segments.
    Falls back to equal fixed intervals if video has NO audio track.
    max_segments: nếu có -> chỉ tính điểm cắt cho N đoạn đầu (chỉ phân tích audio phần đầu video).
    Khi đó điểm cuối cùng có thể < duration (video còn phần chưa cắt)."""
    video_path = os.path.normpath(video_path)
    temp_dir = os.path.normpath(temp_dir)
    duration = get_video_duration(video_path)

    if duration <= 0:
        return (duration, [0.0, duration])
    if duration <= max_len:
        return (duration, [0.0, duration])

    os.makedirs(temp_dir, exist_ok=True)
    audio_temp = os.path.join(temp_dir, f'temp_{int(time.time() * 1000)}.wav')

    def fixed_split_fallback():
        """Fallback split when video has no audio track or audio read fails."""
        target_len = (min_len + max_len) / 2.0
        pts = [0.0]
        curr = 0.0
        while curr + target_len < duration - 10.0:
            curr += target_len
            pts.append(curr)
        pts.append(duration)
        if max_segments and len(pts) - 1 > max_segments:
            pts = pts[:max_segments + 1]
        return (duration, pts)

    try:
        try:
            limit_sec = (max_segments * max_len + 10) if max_segments else None
            extract_audio_ffmpeg(video_path, audio_temp, sample_rate=sr, max_seconds=limit_sec)
        except Exception:
            return fixed_split_fallback()

        try:
            sample_rate_read, data = read_wav_pcm16(audio_temp)
            if data.size == 0:
                return fixed_split_fallback()
        except Exception:
            return fixed_split_fallback()

        if data.ndim > 1:
            data = data[:, 0]
        if data.dtype == np.int16:
            y = data.astype(np.float32) / 32768.0
        elif data.dtype == np.int32:
            y = data.astype(np.float32) / 2147483648.0
        elif data.dtype == np.uint8:
            y = (data.astype(np.float32) - 128.0) / 128.0
        else:
            y = data.astype(np.float32)

        frame_length = int(0.1 * sr)
        hop_length = max(1, frame_length // 2)
        rms = calculate_rms_vectorized(y, frame_length, hop_length)
        if len(rms) == 0:
            return fixed_split_fallback()

        break_points = [0.0]
        curr_point = 0.0
        while curr_point < duration:
            if max_segments and len(break_points) - 1 >= max_segments:
                break
            remaining_time = duration - curr_point
            if remaining_time <= max_len:
                if remaining_time >= min_len:
                    break_points.append(duration)
                else:
                    if len(break_points) > 1:
                        last_pt = break_points.pop()
                        combined_len = duration - break_points[-1]
                        target_split = break_points[-1] + combined_len / 2.0
                        search_start_sec = max(break_points[-1] + min_len, target_split - 20.0)
                        search_end_sec = min(duration - min_len, target_split + 20.0)
                        best_split = target_split
                        if search_start_sec < search_end_sec:
                            start_idx = int(search_start_sec * sr // hop_length)
                            end_idx = int(search_end_sec * sr // hop_length)
                            start_idx = min(start_idx, len(rms) - 1)
                            end_idx = min(end_idx, len(rms))
                            if start_idx < end_idx and start_idx < len(rms):
                                rms_sub = rms[start_idx:end_idx]
                                if len(rms_sub) > 0:
                                    best_idx = int(np.argmin(rms_sub)) + start_idx
                                    best_split = best_idx * hop_length / sr
                        break_points.append(best_split)
                    break_points.append(duration)
                break

            curr_idx = int(curr_point * sr // hop_length)
            min_frames = int(min_len * sr // hop_length)
            max_frames = int(max_len * sr // hop_length)
            start_idx = curr_idx + min_frames
            end_idx = min(curr_idx + max_frames, len(rms))
            if start_idx >= end_idx or start_idx >= len(rms):
                next_point = curr_point + min_len
                if next_point < duration:
                    break_points.append(next_point)
                    curr_point = next_point
                    continue
                break_points.append(duration)
                break

            rms_slice = rms[start_idx:end_idx]
            lowest_idx = int(np.argmin(rms_slice)) + start_idx
            next_point = lowest_idx * hop_length / sr
            if next_point <= curr_point:
                next_point = curr_point + min_len
            break_points.append(next_point)
            curr_point = next_point

        truncated = bool(max_segments) and break_points[-1] < duration - 0.5
        if max_segments and len(break_points) - 1 > max_segments:
            break_points = break_points[:max_segments + 1]
            truncated = break_points[-1] < duration - 0.5
        cleaned_points = [break_points[0]]
        for p in break_points[1:]:
            if p - cleaned_points[-1] >= 1.0:
                cleaned_points.append(p)
        if cleaned_points[-1] < duration and not truncated:
            cleaned_points.append(duration)
        return (duration, cleaned_points)
    finally:
        if os.path.exists(audio_temp):
            try:
                os.remove(audio_temp)
            except Exception:
                pass


CANVAS_W = 1080
CANVAS_H = 1920
TITLE_TOP_916 = 260  # video đã cắt đủ 9:16 (lấp kín khung): tiêu đề căn giữa, đỉnh ô chữ cách mép trên khung 260px
# False: coi pixel là vuông, dùng đúng width x height của file (960x720 -> 4:3).
# True : nhân thêm SAR trong metadata (chỉ nên bật cho video anamorphic thật sự).
RESPECT_SAR = False


def _app_dir():
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def _font_search_dirs():
    """Các thư mục tìm font: cạnh code, thư mục gốc dự án, cạnh file chạy chính, thư mục làm việc hiện tại (và fonts/ trong mỗi nơi)."""
    here = os.path.dirname(os.path.abspath(__file__))
    bases = [_app_dir(), here, os.path.dirname(here)]
    if getattr(sys, '_MEIPASS', None):
        bases.append(sys._MEIPASS)
    try:
        if sys.argv and sys.argv[0]:
            bases.append(os.path.dirname(os.path.abspath(sys.argv[0])))
    except Exception:
        pass
    bases.append(os.getcwd())
    dirs, seen = [], set()
    for base in bases:
        for d in (base, os.path.join(base, 'fonts')):
            key = os.path.normcase(os.path.normpath(d))
            if key not in seen:
                seen.add(key)
                dirs.append(d)
    return dirs


def font_search_report():
    """Báo cáo các thư mục đã tìm font và file font (.ttf/.otf) thấy được trong từng thư mục - để chẩn đoán khi không tìm thấy TikTok Sans."""
    lines = []
    for d in _font_search_dirs():
        if not os.path.isdir(d):
            lines.append(f'[không tồn tại] {d}')
            continue
        fonts = sorted(f for f in os.listdir(d) if f.lower().endswith(('.ttf', '.otf')))
        lines.append(f'{d}\n    -> ' + (', '.join(fonts) if fonts else '(không có file .ttf/.otf)'))
    return '\n'.join(lines)


def _find_title_font():
    """Font tiêu đề: ưu tiên TikTok Sans (font chuẩn của TikTok, có đủ tiếng Việt).
    Thứ tự tìm: file TikTokSans-Bold.ttf / TikTokText-Bold.otf / TikTokDisplay-Bold.otf cạnh code (hoặc thư mục gốc dự án) -> font đã cài trong máy -> Arial Bold (dự phòng)."""
    dirs = _font_search_dirs()
    # Ưu tiên: TikTokSans-Bold -> TikTokText-Bold (bản chữ cho cỡ nhỏ, dùng cho phụ đề/tiêu đề) -> TikTokDisplay-Bold
    names = ('TikTokSans-Bold.ttf', 'TikTokSans.ttf', 'TikTokText-Bold.otf', 'TikTokText-Bold.ttf',
             'TikTokDisplay-Bold.otf', 'TikTokDisplay-Bold.ttf')
    candidates = [os.path.join(d, n) for d in dirs for n in names]
    # Tên file không đúng chuẩn (vd. TikTokSans[opsz,wdth,wght].ttf, TikTok_Sans-Bold.otf...): quét mọi file TikTok Sans trong các thư mục trên,
    # ưu tiên file có chữ 'bold', bỏ file italic.
    loose = []
    for d in dirs:
        for ext in ('ttf', 'otf'):
            loose += glob.glob(os.path.join(d, f'*[Tt]ik[Tt]ok*.{ext}'))
    loose = sorted({os.path.normpath(x) for x in loose},
                   key=lambda x: ('italic' in os.path.basename(x).lower(), 'bold' not in os.path.basename(x).lower(),
                                  'text' not in os.path.basename(x).lower(), x))
    candidates += loose
    fallbacks = []
    if sys.platform == 'win32':
        windir = os.environ.get('WINDIR', 'C:\\Windows')
        local = os.environ.get('LOCALAPPDATA', '')
        for base in (os.path.join(windir, 'Fonts'), os.path.join(local, 'Microsoft', 'Windows', 'Fonts')):
            candidates += sorted(glob.glob(os.path.join(base, 'TikTokSans*.ttf')))
        for name in ('arialbd.ttf', 'segoeuib.ttf', 'tahoma.ttf', 'arial.ttf'):
            fallbacks.append(os.path.join(windir, 'Fonts', name))
    else:
        fallbacks += ['/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf',
                      '/System/Library/Fonts/Supplemental/Arial Bold.ttf']
    for c in candidates + fallbacks:
        if os.path.exists(c):
            return c
    return None


def _find_cjk_fonts():
    """Font Hàn/CJK đi kèm dự án (Noto Sans KR): tìm trong thư mục fonts/ cạnh code hoặc thư mục gốc dự án.
    Ưu tiên bản tĩnh Bold; nếu chỉ có variable font thì _load_title_font sẽ ép về Bold (700)."""
    here = os.path.dirname(os.path.abspath(__file__))
    bases = [_app_dir(), here, os.path.dirname(here)]
    if getattr(sys, '_MEIPASS', None):
        bases.append(sys._MEIPASS)
    dirs = []
    for base in bases:
        for d in (os.path.join(base, 'fonts'), base):
            if d not in dirs:
                dirs.append(d)
    names = ('NotoSansKR-Bold.ttf', 'NotoSansKR-Bold.otf', 'NotoSansKR-SemiBold.ttf', 'NotoSansKR-Medium.ttf',
             'NotoSansKR-Regular.ttf', 'NotoSansKR-Regular.otf')
    found = []
    for d in dirs:
        for n in names:
            fp = os.path.join(d, n)
            if os.path.exists(fp):
                found.append(fp)
        # variable font (NotoSansKR-VariableFont_wght.ttf, NotoSansKR[wght].ttf...) và mọi file NotoSansKR* còn lại
        for ext in ('ttf', 'otf', 'ttc'):
            for fp in sorted(glob.glob(os.path.join(d, 'NotoSansKR*.' + ext)) +
                             glob.glob(os.path.join(d, 'NotoSans*KR*.' + ext))):
                if fp not in found:
                    found.append(fp)
        if found:
            break
    return found[:1]


def _load_title_font(path, px):
    """Nạp font bằng Pillow. TikTok Sans là variable font -> ép về Bold (700), width 100, optical size lớn nhất."""
    from PIL import ImageFont
    font = ImageFont.truetype(path, px)
    try:
        axes = font.get_variation_axes()
    except Exception:
        return font  # font tĩnh
    vals = []
    for ax in axes:
        name = ax['name'].decode('latin-1') if isinstance(ax['name'], bytes) else str(ax['name'])
        name = name.lower()
        if 'weight' in name:
            v = 700
        elif 'width' in name:
            v = 100
        elif 'optical' in name:
            v = ax['maximum']
        else:
            v = ax['default']
        vals.append(min(max(v, ax['minimum']), ax['maximum']))
    try:
        font.set_variation_by_axes(vals)
    except Exception:
        pass
    return font


def _hex_rgb(value, default):
    value = str(value).lstrip('#')
    if not re.fullmatch(r'[0-9a-fA-F]{6}', value):
        return default
    return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))


def _ff_escape_path(path):
    """Escape đường dẫn để dùng trong option của filter FFmpeg."""
    return path.replace('\\', '/').replace(':', '\\:')


def get_video_size(video_path):
    """Kích thước hiển thị (rộng, cao) của video, có tính cả metadata xoay."""
    cmd = [FFPROBE_PATH, '-v', 'error', '-select_streams', 'v:0', '-show_streams', '-of', 'json',
           os.path.normpath(video_path)]
    startupinfo = None
    if sys.platform == 'win32':
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                         encoding='utf-8', errors='ignore', startupinfo=startupinfo, check=True)
    info = json.loads(res.stdout)['streams'][0]
    w, h = int(info['width']), int(info['height'])
    rot = 0
    try:
        rot = int(float(info.get('tags', {}).get('rotate', 0)))
    except (TypeError, ValueError):
        pass
    for sd in info.get('side_data_list', []):
        if 'rotation' in sd:
            try:
                rot = int(float(sd['rotation']))
            except (TypeError, ValueError):
                pass
    if abs(rot) % 180 == 90:
        w, h = h, w
    # Pixel không vuông (SAR != 1): kích thước hiển thị thật = width * SAR
    sar = 1.0
    try:
        if not RESPECT_SAR:
            raise ValueError
        sn, sd = str(info.get('sample_aspect_ratio', '1:1')).split(':')
        if float(sn) > 0 and float(sd) > 0:
            sar = float(sn) / float(sd)
    except (ValueError, TypeError):
        pass
    if sar >= 1.0:
        w = int(round(w * sar))
    else:
        h = int(round(h / sar))
    return w, h


def compute_frame_layout(src_w, src_h):
    """Co video vừa khung 9:16 (giữ tỉ lệ, không crop), đặt giữa khung.
    Trả về (rộng, cao, x, y) của video trong khung 1080x1920; mọi số đều chẵn."""
    f = min(CANVAS_W / src_w, CANVAS_H / src_h)
    nw = min(CANVAS_W, max(2, int(src_w * f) // 2 * 2))
    nh = min(CANVAS_H, max(2, int(src_h * f) // 2 * 2))
    px = (CANVAS_W - nw) // 2 // 2 * 2
    py = (CANVAS_H - nh) // 2 // 2 * 2
    return nw, nh, px, py


_FONT_CMAP_CACHE = {}
_CHAR_MAP = {'\u29f8': '/', '\u2215': '/', '\u29f9': '\\', '\u2216': '\\'}


def _font_cmap(font_path):
    """Tập mã ký tự mà font hỗ trợ (cần fontTools; không có thì trả về None)."""
    if not font_path:
        return None
    if font_path not in _FONT_CMAP_CACHE:
        cmap = None
        try:
            from fontTools.ttLib import TTFont
            cmap = set(TTFont(font_path, fontNumber=0).getBestCmap().keys())
        except Exception:
            cmap = None
        _FONT_CMAP_CACHE[font_path] = cmap
    return _FONT_CMAP_CACHE[font_path]


_KEEP_INVISIBLE = {'\u200d', '\ufe0f', '\ufe0e', '\u20e3'}  # ZWJ, variation selectors, keycap: cần để ghép emoji


def sanitize_title(text, font_path=None):
    """Chuẩn hóa tiêu đề trước khi vẽ. KHÔNG xóa emoji hay ký tự đặc biệt:
    - NFC: gộp dấu tiếng Việt dạng tổ hợp về dạng dựng sẵn (không dùng NFKC để khỏi đổi ™ … ① v.v.).
    - Trả ký tự full-width mà yt-dlp hay dùng thay cho | : ? " (｜ ： ？ ＂) về ký tự ASCII thường.
    - Chỉ bỏ ký tự điều khiển / ký tự định dạng vô hình (trừ những ký tự cần để ghép emoji).
    font_path giữ lại cho tương thích, không còn dùng."""
    text = unicodedata.normalize('NFC', str(text))
    out = []
    for ch in text:
        cp = ord(ch)
        if 0xFF01 <= cp <= 0xFF5E:
            ch = chr(cp - 0xFEE0)
        elif ch == '\u3000':
            ch = ' '
        else:
            ch = _CHAR_MAP.get(ch, ch)
        cat = unicodedata.category(ch)
        if ch in _KEEP_INVISIBLE or 0xE0020 <= ord(ch) <= 0xE007F:
            out.append(ch)
        elif cat in ('Cc', 'Cs', 'Cf'):
            continue
        else:
            out.append(ch)
    return ' '.join(''.join(out).split())


def _is_ri(ch):
    return 0x1F1E6 <= ord(ch) <= 0x1F1FF


def _clusters(text):
    """Tách chuỗi thành cụm ký tự hiển thị (emoji ghép ZWJ, cờ, màu da, dấu tổ hợp... đi chung một cụm)."""
    out, i, n = [], 0, len(text)
    while i < n:
        j = i + 1
        if _is_ri(text[i]) and j < n and _is_ri(text[j]):
            j += 1
        while j < n:
            c = text[j]
            if (c in _KEEP_INVISIBLE or 0x1F3FB <= ord(c) <= 0x1F3FF or 0xE0020 <= ord(c) <= 0xE007F
                    or unicodedata.category(c) in ('Mn', 'Me')):
                j += 1
            elif c == '\u200d' and j + 1 < n:
                j += 2
            else:
                break
        out.append(text[i:j])
        i = j
    return out


def _fallback_font_specs():
    """Font dự phòng cho ký tự mà TikTok Sans không có: (đường dẫn, là font emoji màu?)."""
    specs = []
    if sys.platform == 'win32':
        d = os.path.join(os.environ.get('WINDIR', 'C:\\Windows'), 'Fonts')
        specs += [(os.path.join(d, 'seguiemj.ttf'), True), (os.path.join(d, 'seguisym.ttf'), False),
                  (os.path.join(d, 'msyhbd.ttc'), False), (os.path.join(d, 'malgunbd.ttf'), False),
                  (os.path.join(d, 'arial.ttf'), False)]
    elif sys.platform == 'darwin':
        specs += [('/System/Library/Fonts/Apple Color Emoji.ttc', True),
                  ('/System/Library/Fonts/Supplemental/Arial Unicode.ttf', False)]
    else:
        specs += [('/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf', True),
                  ('/usr/share/fonts/truetype/noto/NotoSansSymbols2-Regular.ttf', False),
                  ('/usr/share/fonts/truetype/noto/NotoSansSymbols-Bold.ttf', False),
                  ('/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf', False)]
    return [(p, c) for p, c in specs if os.path.exists(p)]


class _TitleFont:
    """Bộ font vẽ tiêu đề: TikTok Sans cho chữ thường, tự chuyển sang font emoji/ký hiệu cho ký tự TikTok Sans không có."""
    BITMAP_PX = 109  # font emoji bitmap (Noto CBDT) chỉ có đúng một cỡ này

    def __init__(self, px, primary_path):
        self.px = px
        self.specs = [(primary_path, False)] + [(p, False) for p in _find_cjk_fonts()] + _fallback_font_specs()
        self._faces = {}
        self.primary = self._face(0)['font']

    def _face(self, idx):
        if idx in self._faces:
            return self._faces[idx]
        path, color = self.specs[idx]
        face = None
        try:
            if idx == 0:
                font, scale = _load_title_font(path, self.px), 1.0
            else:
                from PIL import ImageFont
                try:
                    if color:
                        font, scale = ImageFont.truetype(path, self.px), 1.0
                    else:
                        font, scale = _load_title_font(path, self.px), 1.0  # variable font (Noto Sans KR) -> Bold
                except OSError:
                    font, scale = ImageFont.truetype(path, self.BITMAP_PX), self.px / self.BITMAP_PX
            face = {'font': font, 'scale': scale, 'color': color, 'cmap': _font_cmap(path)}
        except Exception:
            face = None
        self._faces[idx] = face
        return face

    def _covers(self, idx, face, cluster, wants_emoji):
        base = ord(cluster[0])
        if face['cmap'] is not None:
            return base in face['cmap']
        # không có fontTools -> đoán theo vùng mã ký tự
        return (not wants_emoji and base < 0x2190) if idx == 0 else True

    def _pick(self, cluster):
        base = cluster[0]
        wants_emoji = (_is_ri(base) or ord(base) >= 0x1F000 or '\ufe0f' in cluster
                       or '\u200d' in cluster or '\u20e3' in cluster)
        order = list(range(len(self.specs)))
        if wants_emoji:
            order.sort(key=lambda i: (not self.specs[i][1], i))      # font emoji màu trước
        else:
            order.sort(key=lambda i: (self.specs[i][1], i))          # font emoji màu để cuối
        for idx in order:
            face = self._face(idx)
            if face and self._covers(idx, face, cluster, wants_emoji):
                return idx
        return 0

    def runs(self, text):
        runs = []
        for cl in _clusters(text):
            idx = self._pick(cl)
            if runs and runs[-1][0] == idx:
                runs[-1][1] += cl
            else:
                runs.append([idx, cl])
        return runs

    def width(self, text):
        return sum(self._face(i)['font'].getlength(t) * self._face(i)['scale'] for i, t in self.runs(text))

    def draw(self, img, draw, x, baseline, text, fill):
        from PIL import Image, ImageDraw
        for idx, t in self.runs(text):
            face = self._face(idx)
            font, scale = face['font'], face['scale']
            if scale == 1.0:
                if face['color']:
                    draw.text((x, baseline), t, font=font, anchor='ls', embedded_color=True)
                else:
                    draw.text((x, baseline), t, font=font, fill=fill, anchor='ls')
            else:
                asc, desc = font.getmetrics()
                tmp = Image.new('RGBA', (int(font.getlength(t)) + 8, asc + desc + 8), (0, 0, 0, 0))
                ImageDraw.Draw(tmp).text((4, asc + 4), t, font=font, anchor='ls', embedded_color=True)
                tmp = tmp.resize((max(1, round(tmp.width * scale)), max(1, round(tmp.height * scale))), Image.LANCZOS)
                img.alpha_composite(tmp, (int(x - 4 * scale), int(baseline - (asc + 4) * scale)))
            x += font.getlength(t) * scale


def _protected_units(text, protected):
    """Tách tiêu đề thành các 'đơn vị' từ; các từ thuộc một cụm trong protected_phrases.txt được gộp thành một đơn vị (không bị ngắt dòng giữa chừng)."""
    words = text.split(' ')
    phrases = []
    for p in protected or []:
        pw = str(p).split()
        if len(pw) > 1:
            phrases.append([x.lower() for x in pw])
    phrases.sort(key=len, reverse=True)  # cụm dài khớp trước
    units, i = [], 0
    while i < len(words):
        hit = 1
        for pw in phrases:
            n = len(pw)
            if i + n <= len(words) and [x.lower().strip('.,!?:;"()') for x in words[i:i + n]] == pw:
                hit = n
                break
        units.append(' '.join(words[i:i + hit]))
        i += hit
    return units


def wrap_title(text, measure, max_width, protected=None):
    """Xuống dòng đơn giản theo độ rộng thật (measure(str) -> px), không giới hạn số dòng.
    Cụm trong protected_phrases.txt được giữ liền một dòng (trừ khi cụm rộng hơn cả ô thì mới tách theo từ).
    Từ quá dài thì cắt theo cụm ký tự."""
    lines, cur = [], ''
    for u in _protected_units(text, protected):
        trial = f'{cur} {u}' if cur else u
        if measure(trial) <= max_width:
            cur = trial
            continue
        if cur:
            lines.append(cur)
            cur = ''
        if measure(u) <= max_width:
            cur = u
            continue
        for w in u.split(' '):  # đơn vị quá rộng: tách lại theo từng từ
            trial = f'{cur} {w}' if cur else w
            if measure(trial) <= max_width:
                cur = trial
                continue
            if cur:
                lines.append(cur)
                cur = ''
            if measure(w) > max_width:
                chunk = ''
                for c in _clusters(w):
                    if chunk and measure(chunk + c) > max_width:
                        lines.append(chunk)
                        chunk = c
                    else:
                        chunk += c
                cur = chunk
            else:
                cur = w
    if cur:
        lines.append(cur)
    return lines or [text]


def _opt_num(opts, key, default, lo, hi):
    try:
        v = float(opts.get(key, default))
    except (TypeError, ValueError):
        v = default
    return min(max(v, lo), hi)


def render_title_image(text, opts, out_path):
    """Vẽ tiêu đề thành PNG trong suốt rộng CANVAS_W: chữ nằm trên một khối nền bo tròn liền mạch, nhiều dòng thì nền nối liền nhau.
    opts: size (px), color (chữ), halign (left/center/right) và các thông số nền:
      box_color   màu nền (mặc định #FFFFFF)
      box_opacity độ đục nền 0-100 (mặc định 100)
      box_radius  độ bo góc, % chiều cao một dòng, 0-50 (mặc định 30)
      box_pad_x   đệm ngang, % cỡ chữ (mặc định 50)
      box_pad_y   đệm dọc phía trên dòng đầu / dưới dòng cuối, % cỡ chữ (mặc định 25)
      line_height khoảng cách giữa các dòng (bước dòng), % cỡ chữ, 60-300 (mặc định 125 - sát như TikTok)
      margin_x    lề hai bên, px (mặc định 120) - quyết định độ rộng tối đa của ô
    Trả về chiều cao ảnh (px)."""
    try:
        from PIL import Image, ImageDraw, ImageChops
    except ImportError:
        raise RuntimeError('Thiếu thư viện Pillow để vẽ tiêu đề. Cài bằng: pip install pillow')
    SS = 4  # vẽ phóng to rồi thu nhỏ để viền bo tròn và chữ mượt (khử răng cưa)
    size = max(1, int(opts.get('size', 12)))
    text_rgb = _hex_rgb(opts.get('color', '#000000'), (0, 0, 0))
    box_rgb = _hex_rgb(opts.get('box_color', '#FFFFFF'), (255, 255, 255))
    opacity = _opt_num(opts, 'box_opacity', 100, 0, 100) / 100.0
    radius_pct = _opt_num(opts, 'box_radius', 30, 0, 50)
    halign = opts.get('halign', 'center')
    margin_x = int(_opt_num(opts, 'margin_x', 120, 0, CANVAS_W // 2 - 50))

    font_path = _find_title_font()
    if not font_path:
        raise RuntimeError('Không tìm thấy font tiêu đề. Hãy đặt TikTokText-Bold.otf (hoặc TikTokSans-Bold.ttf) vào thư mục fonts/')
    # Xuống dòng theo độ rộng ô (ngắt dòng thông minh đã bỏ); cụm trong protected_phrases.txt vẫn không bị tách.
    protected = load_protected_phrases()
    pad_x = int(round(size * _opt_num(opts, 'box_pad_x', 50, 0, 500) / 100.0)) * SS
    pad_y = int(round(size * _opt_num(opts, 'box_pad_y', 25, 0, 500) / 100.0)) * SS
    tf = _TitleFont(size * SS, font_path)
    avail_w = (CANVAS_W - 2 * margin_x) * SS - 2 * pad_x
    lines = wrap_title(text, tf.width, avail_w, protected)
    font = tf.primary

    n = len(lines)
    line_pct = _opt_num(opts, 'line_height', 125, 60, 300)
    pitch = max(2, int(round(size * SS * line_pct / 100.0)))      # bước dòng: ô các dòng nằm sát nhau, không còn khe hở
    cap_h = -font.getbbox('H', anchor='ls')[1]                     # chiều cao chữ hoa -> căn chữ giữa ô theo chiều dọc
    base_off = (pitch + cap_h) / 2.0                               # baseline cách đỉnh ô một dòng
    try:  # đỉnh chữ/dấu cao nhất của DÒNG ĐẦU so với baseline: chỉ chừa thêm chỗ khi dòng đầu thật sự có dấu chồng tiếng Việt, tiêu đề tiếng Anh thì ô cân đối
        dia_top = -font.getbbox(lines[0] or 'H', anchor='ls')[1]
    except Exception:
        dia_top = -font.getbbox('ẤỨỬỖ', anchor='ls')[1]
    # đệm phía TRÊN dòng đầu: thu nhỏ còn 60% đệm dọc -> mép trên của ô hạ xuống sát chữ hơn (mép dưới/giữa các dòng giữ nguyên)
    pad_top = int(round(pad_y * 0.6))
    pad_first = max(pad_top, int(math.ceil(dia_top - base_off)))   # dòng đầu: vẫn chừa chỗ cho dấu không lòi ra ngoài ô
    # Nới đáy ô dòng 1 (chỗ nối 2 dòng) sao cho chữ thường (o, w...) của dòng 1 có khoảng trắng phía TRÊN (từ mép trên ô tới đỉnh chữ)
    # bằng khoảng trắng phía DƯỚI (từ chân chữ tới mép đáy ô dòng 1)
    xh = -font.getbbox('ow', anchor='ls')[1]                       # chiều cao chữ thường
    pad_mid = max(0, int(round(pad_first + cap_h - xh)))
    ys, hs, y_cur = [], [], 0
    for i in range(n):
        h = pitch + (pad_first if i == 0 else 0) + (pad_y if i == n - 1 else 0)
        if i == 0 and n > 1:
            h += pad_mid                                           # nới phần ô trắng phía dưới dòng 1 (chỗ nối 2 dòng)
        ys.append(y_cur)
        hs.append(h)
        y_cur += h
    total_h = y_cur
    r = min(int(pitch * radius_pct / 100.0), min(hs) // 2)  # bán kính bo góc

    # Mép trái/phải của ô từng dòng (đơn vị px đã phóng to)
    x0, x1 = [], []
    for ln in lines:
        bw = tf.width(ln) + 2 * pad_x
        if halign == 'left':
            bx = margin_x * SS
        elif halign == 'right':
            bx = (CANVAS_W - margin_x) * SS - bw
        else:
            bx = (CANVAS_W * SS - bw) / 2
        x0.append(int(round(bx)))
        x1.append(int(round(bx + bw)))
    # Hai dòng lệch nhau quá ít (< 2 lần bán kính) thì gộp cho bằng nhau để viền không bị răng cưa
    for _ in range(n):
        for i in range(n - 1):
            if 0 < abs(x0[i] - x0[i + 1]) < 2 * r:
                x0[i] = x0[i + 1] = min(x0[i], x0[i + 1])
            if 0 < abs(x1[i] - x1[i + 1]) < 2 * r:
                x1[i] = x1[i + 1] = max(x1[i], x1[i + 1])

    # Vẽ hình dạng nền vào mặt nạ (L) rồi mới tô màu/độ đục -> nền trong suốt một phần vẫn đều màu, không bị chồng lớp
    mask = Image.new('L', (CANVAS_W * SS, total_h), 0)
    dm = ImageDraw.Draw(mask)
    # 1) Các ô nối liền nhau, chỉ bo những góc lộ ra ngoài (góc nằm giữa hai dòng thì để vuông)
    for i in range(n):
        y = ys[i]
        corners = (i == 0 or x0[i] < x0[i - 1],            # trên-trái
                   i == 0 or x1[i] > x1[i - 1],            # trên-phải
                   i == n - 1 or x1[i] > x1[i + 1],        # dưới-phải
                   i == n - 1 or x0[i] < x0[i + 1])        # dưới-trái
        dm.rounded_rectangle([x0[i], y, x1[i], y + hs[i]], radius=r, fill=255, corners=corners)
    # 2) Góc lõm bo tròn tại chỗ hai dòng có độ rộng khác nhau
    if r >= 1:
        for i in range(n - 1):
            yj = ys[i + 1]
            for side in ('L', 'R'):
                a_, b_ = (x0[i], x0[i + 1]) if side == 'L' else (x1[i], x1[i + 1])
                if a_ == b_:
                    continue
                upper_narrow = (a_ > b_) if side == 'L' else (a_ < b_)  # dòng hẹp hơn là dòng có mép nằm sâu vào trong
                e = a_ if upper_narrow else b_
                px, cx = (e, r) if side == 'R' else (e - r, 0)
                py, cy = (yj - r, 0) if upper_narrow else (yj, r)
                patch = Image.new('L', (r, r), 255)
                ImageDraw.Draw(patch).ellipse([cx - r, cy - r, cx + r, cy + r], fill=0)
                region = mask.crop((px, py, px + r, py + r))
                mask.paste(ImageChops.lighter(region, patch), (px, py))
    if opacity < 1.0:
        mask = mask.point(lambda v: int(v * opacity))
    img = Image.new('RGBA', (CANVAS_W * SS, total_h), box_rgb + (0,))
    img.putalpha(mask)
    draw = ImageDraw.Draw(img)
    # 3) Chữ (tự chuyển font cho emoji / ký tự đặc biệt)
    for i, ln in enumerate(lines):
        lw = tf.width(ln)
        tx = x0[i] + pad_x if halign == 'left' else (x1[i] - pad_x - lw if halign == 'right'
                                                      else (x0[i] + x1[i] - lw) / 2)
        tf.draw(img, draw, tx, ys[i] + (pad_first if i == 0 else 0) + base_off, ln, text_rgb + (255,))
    out_h = max(2, round(total_h / SS))
    img = img.resize((CANVAS_W, out_h), Image.LANCZOS)
    img.save(out_path, 'PNG')
    return out_h


# ---------------------------------------------------------------------------------------------
# XEM TRƯỚC TIÊU ĐỀ: vẽ tiêu đề nhập tay lên canvas đen theo thông số hiện tại
# ---------------------------------------------------------------------------------------------
def render_title_preview(opts, text, resolution_preset='1920x1080'):
    """Tạo ảnh xem trước: canvas CANVAS_W x CANVAS_H nền đen + tiêu đề `text`, đặt ở đúng vị trí như khi render
    video thật (cách mép trên video `gap` px, hoặc cách mép khung 260px nếu video lấp kín khung 9:16).
    Trả về PIL.Image RGB. Lưu ý: vị trí dựa vào mục Resolution; 'Original' coi video nguồn là 1920x1080."""
    from PIL import Image
    text = sanitize_title(text, _find_title_font())
    if not text:
        raise RuntimeError('Tiêu đề trống.')
    m = re.search(r'(\d{3,4})\s*x\s*(\d{3,4})', str(resolution_preset))
    tile_w, tile_h = (int(m.group(1)), int(m.group(2))) if m else (1920, 1080)
    _, _, _, py = compute_frame_layout(tile_w, tile_h)
    fd, tmp_png = tempfile.mkstemp(suffix='.png', prefix='title_preview_')
    os.close(fd)
    try:
        if py == 0:
            title_h = render_title_image(text, dict(opts, halign='center'), tmp_png)
            title_y = min(TITLE_TOP_916, max(0, CANVAS_H - title_h))
        else:
            title_h = render_title_image(text, opts, tmp_png)
            gap = max(0, int(opts.get('gap', 50)))
            title_y = max(py - gap, title_h) - title_h
        title_img = Image.open(tmp_png).convert('RGBA')
        title_img.load()
    finally:
        try:
            os.remove(tmp_png)
        except OSError:
            pass
    canvas = Image.new('RGBA', (CANVAS_W, CANVAS_H), (0, 0, 0, 255))
    canvas.alpha_composite(title_img, (0, title_y))
    return canvas.convert('RGB')


_ENC_LOCK = threading.Lock()
_GPU_FALLBACKS = {}
_GPU_FALLBACK_LIMIT = 3  # GPU lỗi rồi phải dùng libx264 3 lần liên tiếp -> các đoạn sau dùng thẳng libx264
_SIZE_CACHE = {}


def reset_encoder_state():
    with _ENC_LOCK:
        _GPU_FALLBACKS.clear()


def _effective_encoder(enc):
    with _ENC_LOCK:
        if enc != 'libx264' and _GPU_FALLBACKS.get(enc, 0) >= _GPU_FALLBACK_LIMIT:
            return 'libx264'
    return enc


def _note_gpu_fallback(enc, failed):
    with _ENC_LOCK:
        _GPU_FALLBACKS[enc] = _GPU_FALLBACKS.get(enc, 0) + 1 if failed else 0


def _cached_video_size(path):
    key = os.path.normpath(path)
    if key not in _SIZE_CACHE:
        _SIZE_CACHE[key] = get_video_size(key)
    return _SIZE_CACHE[key]


def render_segment(video_path, start_time, segment_duration, output_path, speed=1.0,
                   resolution_preset='960x720', encoder='h264_nvenc', process_callback=None,
                   frame_opts=None, is_cancelled=None, volume_db=0.0):
    """Render a single cut segment using FFmpeg fast seeking (-ss before -i)
    with instant cancellation support.
    frame_opts: nếu có -> cắt video theo tỉ lệ của resolution_preset (vd 960x720 = 4:3), rồi đặt vào
                khung 9:16 (1080x1920) nền đen, video ở giữa, tiêu đề nằm trong vùng đen ngay phía trên video.
                Keys: text, size, color, halign, gap.
    is_cancelled: callable trả về True khi người dùng bấm dừng.
    volume_db: chỉnh âm lượng theo dB (dương = tăng, âm = giảm, 0 = giữ nguyên).
    Ném RuntimeError nếu FFmpeg thất bại (trừ khi bị dừng)."""
    video_path = os.path.normpath(video_path)
    output_path = os.path.normpath(output_path)

    def cancelled():
        return callable(is_cancelled) and bool(is_cancelled())

    vf_filters = []
    title_png = None
    title_y = 0
    try:
        if frame_opts is not None:
            sq = 'scale=iw*sar:ih,setsar=1' if RESPECT_SAR else 'setsar=1'
            m = re.search(r'(\d{3,4})\s*x\s*(\d{3,4})', str(resolution_preset))
            if m:
                # Cắt (crop giữa) theo đúng tỉ lệ đã chọn ở mục Resolution rồi đặt vào khung 9:16.
                tile_w, tile_h = int(m.group(1)), int(m.group(2))
                nw, nh, px, py = compute_frame_layout(tile_w, tile_h)
                vf_filters.append(
                    f'{sq},scale={nw}:{nh}:force_original_aspect_ratio=increase,'
                    f'crop={nw}:{nh},setsar=1,pad={CANVAS_W}:{CANVAS_H}:{px}:{py}:black')
            else:
                # 'Original': giữ nguyên tỉ lệ video nguồn, co vừa khung (không crop).
                try:
                    src_w, src_h = _cached_video_size(video_path)
                except Exception:
                    src_w, src_h = 1920, 1080
                nw, nh, px, py = compute_frame_layout(src_w, src_h)
                vf_filters.append(
                    f'{sq},scale={CANVAS_W}:{CANVAS_H}:force_original_aspect_ratio=decrease:force_divisible_by=2,'
                    f'pad={CANVAS_W}:{CANVAS_H}:(ow-iw)/2:(oh-ih)/2:black')
            text = sanitize_title(frame_opts.get('text', ''), _find_title_font())
            if text:
                fd, title_png = tempfile.mkstemp(suffix='.png', prefix='title_')
                os.close(fd)
                if py == 0:
                    # Video lấp kín khung 9:16 (không còn vùng đen phía trên): căn giữa, cách lề trên 260px
                    title_opts = dict(frame_opts, halign='center')
                    title_h = render_title_image(text, title_opts, title_png)
                    title_y = min(TITLE_TOP_916, max(0, CANVAS_H - title_h))
                else:
                    title_h = render_title_image(text, frame_opts, title_png)
                    gap = max(0, int(frame_opts.get('gap', 50)))
                    title_y = max(py - gap, title_h) - title_h  # đáy ô chữ cách mép trên video `gap` px
        elif resolution_preset == '960x720':
            vf_filters.append('scale=-1:720,crop=960:720')
        elif '1080x1920' in resolution_preset:
            vf_filters.append('scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920')
        elif '1440x1080' in resolution_preset:
            vf_filters.append('scale=1440:1080:force_original_aspect_ratio=increase,crop=1440:1080')
        elif '1920x1080' in resolution_preset:
            vf_filters.append('scale=1920:1080:force_original_aspect_ratio=increase,crop=1920:1080')
        elif '1080x1080' in resolution_preset or '1:1' in resolution_preset:
            vf_filters.append('scale=1080:1080:force_original_aspect_ratio=increase,crop=1080:1080')
        elif '720x720' in resolution_preset:
            vf_filters.append('scale=720:720:force_original_aspect_ratio=increase,crop=720:720')

        pts_filter = f'setpts={1.0 / speed}*PTS' if speed != 1.0 else None
        if title_png:
            # Có tiêu đề: video là input 0, ảnh PNG ô chữ là input 1, ghép bằng overlay.
            fc = f"[0:v]{','.join(vf_filters)}[bg];[bg][1:v]overlay=0:{title_y}"
            if pts_filter:
                fc += f',{pts_filter}'
            fc += '[v]'
            filter_args = ['-filter_complex', fc, '-map', '[v]', '-map', '0:a?']
        else:
            if pts_filter:
                vf_filters.append(pts_filter)
            vf_arg = ','.join(vf_filters) if vf_filters else None
            filter_args = ['-vf', vf_arg] if vf_arg else []
        af_parts = []
        if speed != 1.0:
            af_parts.append(f'atempo={speed}')
        try:
            vol_db = float(volume_db or 0.0)
        except (TypeError, ValueError):
            vol_db = 0.0
        if abs(vol_db) > 1e-9:
            af_parts.append(f'volume={vol_db:.2f}dB')
            if vol_db > 0:
                af_parts.append('alimiter=limit=0.98')  # tăng âm lượng thì chặn đỉnh để không vỡ tiếng (clipping)
        af_arg = ','.join(af_parts) if af_parts else None

        def build_cmd(enc):
            cmd = [FFMPEG_PATH, '-hide_banner', '-loglevel', 'error']
            cmd.extend(['-ss', f'{start_time:.3f}', '-t', f'{segment_duration:.3f}', '-i', video_path])
            if title_png:
                cmd.extend(['-i', title_png])
            cmd.extend(filter_args)
            if af_arg:
                cmd.extend(['-af', af_arg])
            if 'nvenc' in enc:
                cmd.extend(['-c:v', 'h264_nvenc', '-preset', 'p4'])
            elif 'amf' in enc:
                cmd.extend(['-c:v', 'h264_amf'])
            elif 'qsv' in enc:
                cmd.extend(['-c:v', 'h264_qsv'])
            else:
                cmd.extend(['-c:v', 'libx264', '-preset', 'fast'])
            cmd.extend(['-c:a', 'aac', '-y', output_path])
            return cmd

        startupinfo = None
        if sys.platform == 'win32':
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW

        def run_proc(cmd):
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, startupinfo=startupinfo)
            if callable(process_callback):
                process_callback(proc)
            stdout, stderr = proc.communicate()
            if callable(process_callback):
                process_callback(None)
            return proc.returncode, stderr.decode('utf-8', errors='ignore')

        enc_used = _effective_encoder(encoder)
        retcode, err = run_proc(build_cmd(enc_used))
        if retcode != 0 and enc_used != 'libx264' and not cancelled():
            retcode, err = run_proc(build_cmd('libx264'))
            if retcode == 0:
                _note_gpu_fallback(enc_used, True)
        elif retcode == 0 and enc_used != 'libx264':
            _note_gpu_fallback(enc_used, False)

        if cancelled():
            try:
                if os.path.exists(output_path):
                    os.remove(output_path)  # bỏ file dở dang khi bấm dừng
            except Exception:
                pass
            return output_path
        if retcode != 0:
            try:
                if os.path.exists(output_path):
                    os.remove(output_path)
            except Exception:
                pass
            raise RuntimeError(f'FFmpeg lỗi (code {retcode}) khi xuất {os.path.basename(output_path)}: {err.strip()[-600:]}')
        return output_path
    finally:
        if title_png and os.path.exists(title_png):
            try:
                os.remove(title_png)
            except Exception:
                pass

#!/usr/bin/env python3
"""
ytdownload_gui.py  -  YouTube Channel Downloader (GUI)

Chức năng:
- Mỗi kênh PHẢI tải đủ N video (mặc định 3). Điều kiện chung: độ dài min..max phút, lượt xem > min_views
- Có thể chỉ tải video đăng trong N ngày gần đây (Cài đặt -> 'Chỉ video đăng trong', mặc định 365 = 1 năm, 0 = tắt);
  giới hạn ngày chỉ áp dụng cho Bước 1, Bước 2 (bù từ video phổ biến) chỉ xét độ dài + lượt xem
- Bước 1: quét M video gần đây đủ điều kiện, ưu tiên video nhiều view nhất
- Bước 2 (bù): nếu chưa đủ N (không đủ view, lỗi tải, members-only, đã tải từ trước...) ->
  duyệt danh sách video PHỔ BIẾN NHẤT của kênh, vẫn theo điều kiện độ dài + lượt xem, để bù cho đủ
- Video đã có trong downloaded_titles.txt -> KHÔNG tải lại, KHÔNG tính vào quota, lấy video khác bù
  (chỉ kiểm tra theo MÃ video, không so tên)
- Hết video đủ điều kiện mà vẫn thiếu -> báo "CHƯA ĐỦ" trong nhật ký
- Bấm ▶ Start mới bắt đầu theo dõi clipboard / channels.txt (và hỏi có tải các kênh đang có trong channels.txt không)
- Bấm ■ Dừng tất cả -> hủy mọi tác vụ, quay về trạng thái chờ; bấm ▶ Tiếp tục để làm nốt các kênh đang dở
  (file tải dở được tải tiếp, kênh đã xong không tải lại, không hỏi lại về channels.txt)
- Theo dõi clipboard: link kênh YouTube -> tự thêm vào channels.txt
- Theo dõi channels.txt: kênh mới xuất hiện -> quét + tải ngay
- Loại trừ: kênh trong exclusion_channels.txt (hoặc thêm ở tab "Loại trừ" trên GUI) sẽ KHÔNG được tải
- Giao diện Tkinter, tiến trình từng video, nút Dừng, cài đặt lưu vào ytdownload_gui.json
- (Tải bằng link video/playlist đã tách sang ytlink_gui.py)

Cấu trúc thư mục:

    ytdownload/
    ├── ytdownload_gui.py     (đổi đuôi .pyw nếu muốn ẩn cửa sổ console)
    ├── ytlink_gui.py
    ├── ffmpeg.exe
    ├── aria2c.exe            (tuỳ chọn)
    ├── cookies.txt / cookies.json   (tuỳ chọn - Netscape hoặc JSON, chống lỗi "not a bot")
    ├── channels.txt
    ├── exclusion_channels.txt   (kênh KHÔNG tải)
    └── downloaded_titles.txt   (mỗi dòng: #id_video|tiêu đề, vd #jghSs8653kA|10 SCARY DARKEST Videos...;
                                 dòng không có dạng #id| sẽ bị bỏ qua)

Cài yt-dlp:   pip install -U yt-dlp
Chạy:         python ytdownload_gui.py
"""

import contextlib
import glob
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import tempfile
import time
import tkinter as tk
import uuid

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import unquote
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

from yt_dlp import YoutubeDL

try:
    from yt_dlp.utils import DownloadCancelled
except ImportError:                                   # yt-dlp cũ
    class DownloadCancelled(Exception):
        pass


# ==========================================================
# CẤU HÌNH MẶC ĐỊNH
# ==========================================================

if getattr(sys, "frozen", False):
    # Đã đóng gói PyInstaller: dùng thư mục chứa file .exe thật (KHÔNG phải thư mục tạm _MEIxxxx)
    SCRIPT_DIR = Path(sys.executable).resolve().parent
else:
    SCRIPT_DIR = Path(__file__).resolve().parent
# Thư mục giải nén tạm của PyInstaller --onefile (nếu có dùng --add-binary thì tool nằm ở đây)
BUNDLE_DIR = Path(getattr(sys, "_MEIPASS", SCRIPT_DIR))
FFMPEG_PATH = SCRIPT_DIR / "ffmpeg.exe"
ARIA2C_LOCAL_PATH = SCRIPT_DIR / "aria2c.exe"
CONFIG_PATH = SCRIPT_DIR / "ytdownload_gui.json"
CHANNELS_FILE = SCRIPT_DIR / "channels.txt"
TITLES_FILE = SCRIPT_DIR / "downloaded_titles.txt"
EXCLUSION_FILE = SCRIPT_DIR / "exclusion_channels.txt"
COOKIES_FILE = SCRIPT_DIR / "cookies.txt"
COOKIES_JSON_FILE = SCRIPT_DIR / "cookies.json"
COOKIES_CONVERTED = SCRIPT_DIR / "cookies_netscape.txt"   # file tự sinh khi cookie là JSON

DEFAULT_CFG = {
    "videos_per_channel": 30,   # số video đủ điều kiện được xét (nhóm để chọn top)
    "top_videos": 3,            # số video PHẢI tải đủ cho mỗi kênh (thiếu thì bù từ video phổ biến)
    "min_views": 500000,        # video trong top phải có lượt xem LỚN HƠN ngưỡng này mới được tải
    "max_scan": 50,
    "popular_scan": 60,         # số video xét trong danh sách phổ biến nhất khi cần bù
    "min_minutes": 5,
    "max_minutes": 100,
    "max_age_days": 365,        # chỉ tải video đăng trong N ngày gần đây (0 = không giới hạn)
    "max_height": 1080,
    "workers": 5,            # video tải song song
    "scan_workers": 5,       # kênh quét song song
    "output": "downloads",
    "use_aria2c": True,
    "watch_clipboard": True,
    "auto_video_clipboard": False,   # dùng cho ytlink_gui.py, file này không dùng
    "cookies_browser": "",      # "chrome" / "firefox" / "edge" / "brave" (bỏ trống nếu dùng cookies.txt)
}

META_WORKERS = 8
POPULAR_POOL = 20            # tối đa số video đủ điều kiện gom từ danh sách phổ biến để bù
MAX_FAIL_SUBSTITUTES = 5     # số lần bù do lỗi thường tối đa mỗi kênh (tránh lặp vô hạn khi mất mạng)
CONCURRENT_FRAGMENTS = 16

# aria2c: 16 kết nối/file là mức tối đa aria2c cho phép
ARIA2_ARGS = [
    "-x16", "-s16", "-k1M",
    "--file-allocation=none",
    "--disk-cache=64M",
    "--async-dns=false",        # c-ares trên Windows hay chậm/lỗi DNS -> dùng DNS hệ thống
    "--connect-timeout=10",
    "--timeout=15",
    "--retry-wait=1",
    "--max-tries=8",
    "--summary-interval=0",
]
WATCH_INTERVAL_MS = 1000
DOWNLOAD_DELAY_S = 0            # không delay trước mỗi lần tải video
REQUEST_SLEEP_S = 1             # nghỉ giữa các request khi QUÉT metadata kênh (chống bị chặn)
DOWNLOAD_REQUEST_SLEEP_S = 0    # khi TẢI video thì không nghỉ giữa các request

# Dấu hiệu video chỉ dành cho hội viên (members-only)
MEMBERS_MARKERS = (
    "members-only",
    "members only",
    "channel's members",
    "join this channel",
    "members on level",
)


def is_members_error(err) -> bool:
    msg = str(err).lower()
    return any(m in msg for m in MEMBERS_MARKERS)


# Option dùng chung cho mọi YoutubeDL (cookies + sleep), được gán trong main()
YDL_COMMON: dict = {}

CHANNEL_RE = re.compile(
    r"^https?://(?:www\.|m\.)?youtube\.com/"
    r"(?:@[\w.\-%]+|channel/UC[\w-]{22}|c/[\w.\-%]+|user/[\w.\-%]+)"
    r"(?:/(?:videos|featured|streams|shorts|playlists|about|community))?/?"
    r"(?:[?#].*)?$",
    re.IGNORECASE,
)

_CLIP_SPLIT_RE = re.compile(r"[\s,;]+")


def extract_channel_links(text: str) -> list[str]:
    """Tách MỌI link kênh YouTube trong văn bản clipboard (không trùng nhau, giữ thứ tự).
    Chấp nhận mỗi dòng một link, hoặc nhiều link cách nhau bằng khoảng trắng / dấu phẩy / chấm phẩy,
    kể cả khi có số thứ tự, gạch đầu dòng hay ngoặc quanh link."""
    out: list[str] = []
    seen: set[str] = set()
    for tok in _CLIP_SPLIT_RE.split(text):
        tok = tok.strip("\"'()[]<>{}.")
        if not tok or not CHANNEL_RE.match(tok):
            continue
        key = channel_key(tok)
        if key in seen:
            continue
        seen.add(key)
        out.append(tok)
    return out


def load_cfg() -> dict:
    cfg = dict(DEFAULT_CFG)
    try:
        cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
    except Exception:
        pass
    return cfg


def save_cfg(cfg: dict) -> None:
    try:
        CONFIG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def cookies_to_netscape(src: Path) -> Path:
    """Nếu file cookie là JSON (EditThisCookie, Cookie-Editor...) -> chuyển sang Netscape.
    Nếu đã là Netscape thì trả về nguyên file."""
    text = src.read_text(encoding="utf-8-sig", errors="replace").strip()
    if not text or text[0] not in "[{":
        return src

    data = json.loads(text)
    if isinstance(data, dict):
        data = data.get("cookies", [])

    lines = ["# Netscape HTTP Cookie File", ""]
    for c in data:
        domain = c.get("domain", "")
        if not domain:
            continue
        include_sub = domain.startswith(".") or not c.get("hostOnly", False)
        exp = c.get("expirationDate", c.get("expires", 0)) or 0
        prefix = "#HttpOnly_" if c.get("httpOnly") else ""
        lines.append("\t".join([
            prefix + domain,
            "TRUE" if include_sub else "FALSE",
            c.get("path", "/") or "/",
            "TRUE" if c.get("secure") else "FALSE",
            str(int(exp)),
            str(c.get("name", "")),
            str(c.get("value", "")),
        ]))

    COOKIES_CONVERTED.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    return COOKIES_CONVERTED


def find_js_runtimes() -> dict:
    """Tìm deno/node/bun (cạnh file .py hoặc trong PATH) để yt-dlp giải challenge của YouTube.
    Không có JS runtime thì YouTube bóp tốc độ từng kết nối (tham số 'n')."""
    found: dict = {}
    for name in ("deno", "node", "bun"):
        local = SCRIPT_DIR / f"{name}.exe"
        bundled = BUNDLE_DIR / f"{name}.exe"
        if local.exists():
            path = local
        elif bundled.exists():
            path = bundled
        else:
            path = Path(w) if (w := shutil.which(name)) else None
        if path:
            found[name] = {"path": str(path)}
    return found


def build_common_opts(cfg: dict) -> dict:
    """Cookies (cookies.txt / cookies.json ưu tiên, sau đó lấy từ trình duyệt) + sleep giữa các request
    + JS runtime để giải mã tham số 'n' của YouTube."""
    opts: dict = {"sleep_interval_requests": REQUEST_SLEEP_S}
    runtimes = find_js_runtimes()
    if runtimes:
        opts["js_runtimes"] = runtimes
    # cho phép yt-dlp tự tải script giải challenge nếu chưa có gói yt-dlp-ejs
    opts["remote_components"] = ["ejs:github"]
    browser = str(cfg.get("cookies_browser", "")).strip().lower()
    src = next((f for f in (COOKIES_FILE, COOKIES_JSON_FILE) if f.exists()), None)
    if src:
        try:
            opts["cookiefile"] = str(cookies_to_netscape(src))
        except Exception as e:
            print(f"[COOKIES] Không đọc được {src.name}: {e}")
    if "cookiefile" not in opts and browser:
        opts["cookiesfrombrowser"] = (browser,)
    return opts


@contextlib.contextmanager
def make_ydl(opts: dict):
    """YoutubeDL với BẢN SAO riêng của file cookies.

    yt-dlp ghi lại file cookie mỗi khi đóng. Nhiều luồng cùng dùng một file sẽ
    ghi đè/đọc dở nhau -> lỗi "does not look like a Netscape format cookies file".
    Mỗi lần dùng một bản sao tạm thì file gốc không bao giờ bị ghi.
    """
    opts = dict(opts)
    tmp: Path | None = None
    base = YDL_COMMON.get("cookiefile")
    if base and Path(base).exists():
        tmp = Path(tempfile.gettempdir()) / f"ytdl_cookies_{uuid.uuid4().hex}.txt"
        try:
            shutil.copyfile(base, tmp)
            opts["cookiefile"] = str(tmp)
        except OSError:
            tmp = None
    try:
        with YoutubeDL(opts) as ydl:
            yield ydl
    finally:
        if tmp is not None:
            try:
                tmp.unlink()
            except OSError:
                pass


# ==========================================================
# TIỆN ÍCH
# ==========================================================

# Mỗi dòng trong downloaded_titles.txt có dạng:  #id_video|tên video
#   vd:  #jghSs8653kA|10 SCARY DARKEST Videos...
# Chỉ so khớp theo MÃ video (11 ký tự). Dòng không bắt đầu bằng '#id|' (file cũ) bị bỏ qua.
_ID_LINE_RE = re.compile(r"^#([A-Za-z0-9_-]{11})\|(.*)$")
_VIDEO_ID_RE = re.compile(r"(?:[?&]v=|youtu\.be/|/shorts/|/embed/|/live/)([A-Za-z0-9_-]{11})")


def video_id_from(url: str, info: dict | None = None) -> str | None:
    """Lấy ID video (11 ký tự) từ info của yt-dlp, không có thì rút từ link."""
    vid = (info or {}).get("id")
    if isinstance(vid, str) and re.fullmatch(r"[A-Za-z0-9_-]{11}", vid):
        return vid
    m = _VIDEO_ID_RE.search(url or "")
    return m.group(1) if m else None


def load_seen(path: Path) -> set[str]:
    """Đọc downloaded_titles.txt (dòng '#id|tên') -> tập mã video đã tải."""
    ids: set[str] = set()
    if not path.exists():
        return ids
    with path.open("r", encoding="utf-8") as f:
        for raw in f:
            m = _ID_LINE_RE.match(raw.strip())
            if m:
                ids.add(m.group(1))
    return ids


def find_tool(local: Path, name: str) -> Path | None:
    """Thứ tự tìm: cạnh file exe/.py -> thư mục bundle PyInstaller -> thư mục ffmpeg/ con -> PATH."""
    for cand in (local, BUNDLE_DIR / local.name, SCRIPT_DIR / name / local.name):
        if cand.exists():
            return cand
    found = shutil.which(name)
    return Path(found) if found else None


# ---------------- kiểm tra / dọn file sau khi tải ----------------

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class NoAudioError(Exception):
    pass


class YdlLogger:
    """Đưa cảnh báo của yt-dlp ra log box (lỗi đã được except bên ngoài log rồi)."""

    def __init__(self, log) -> None:
        self.log = log

    def debug(self, m): pass
    def info(self, m): pass
    def error(self, m): pass

    def warning(self, m):
        self.log(f"  [YTDLP-CẢNH BÁO] {m}")


def probe_audio(ffmpeg: Path, path: Path) -> str | None:
    """Trả về codec audio ('aac', 'opus'...) hoặc None nếu file KHÔNG có luồng audio.
    Dùng 'ffmpeg -i' nên không cần ffprobe.exe. Không chạy được ffmpeg -> 'unknown' (không chặn)."""
    try:
        r = subprocess.run([str(ffmpeg), "-hide_banner", "-i", str(path)],
                           capture_output=True, text=True, errors="replace",
                           timeout=60, creationflags=NO_WINDOW)
    except Exception:
        return "unknown"
    m = re.search(r"Stream #\d+:\d+.*?: Audio: (\w+)", r.stderr)
    return m.group(1).lower() if m else None


def reencode_audio_aac(ffmpeg: Path, path: Path) -> bool:
    """Chuyển audio (opus/vorbis) sang AAC, giữ nguyên video. True nếu thành công."""
    tmp = path.with_name(path.stem + ".aac_tmp" + path.suffix)
    try:
        r = subprocess.run(
            [str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y", "-i", str(path),
             "-map", "0:v:0", "-map", "0:a:0", "-c:v", "copy", "-c:a", "aac",
             "-b:a", "192k", "-movflags", "+faststart", str(tmp)],
            capture_output=True, timeout=1800, creationflags=NO_WINDOW)
        if r.returncode == 0 and tmp.exists():
            os.replace(tmp, path)
            return True
    except Exception:
        pass
    try:
        tmp.unlink()
    except OSError:
        pass
    return False


def purge_outputs(path: Path) -> None:
    """Xóa file đầu ra và các file trung gian .fNNN còn sót."""
    for q in [path, *path.parent.glob(glob.escape(path.stem) + ".f[0-9]*")]:
        try:
            q.unlink()
        except OSError:
            pass


def purge_by_filename(fn: str) -> None:
    """Từ tên file bất kỳ của video (vd 'Tên.f616.mp4.part') suy ra 'Tên' rồi xóa mọi 'Tên.*'."""
    f = Path(fn)
    name = re.sub(r"\.(part|ytdl)$", "", f.name)
    name = re.sub(r"\.[A-Za-z0-9]{2,4}$", "", name)      # đuôi file
    name = re.sub(r"\.f\d+$", "", name)                  # .f616 / .f140
    if not name:
        return
    for q in f.parent.glob(glob.escape(name) + ".*"):
        try:
            q.unlink()
        except OSError:
            pass


def final_path(info: dict) -> Path | None:
    rd = info.get("requested_downloads") or []
    fp = (rd[0].get("filepath") if rd else None) or info.get("filepath") or info.get("_filename")
    return Path(fp) if fp else None


def describe_formats(info: dict) -> str:
    fmts = info.get("requested_formats") or [info]
    return " + ".join(f"{f.get('format_id')}[v={f.get('vcodec')},a={f.get('acodec')}]" for f in fmts)


def fmt_speed(bps) -> str:
    if not bps:
        return ""
    if bps >= 1024 * 1024:
        return f"{bps / 1024 / 1024:.1f} MB/s"
    return f"{bps / 1024:.0f} KB/s"


def open_path(p: Path) -> None:
    try:
        if sys.platform.startswith("win"):
            os.startfile(str(p))                       # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(p)])
        else:
            subprocess.Popen(["xdg-open", str(p)])
    except Exception:
        pass


# ---------------- channel URL ----------------

def channel_url(u: str) -> str:
    u = u.strip().rstrip("/")

    if u.startswith("@"):
        u = "https://www.youtube.com/" + u
    elif re.fullmatch(r"UC[\w-]{22}", u):
        u = "https://www.youtube.com/channel/" + u

    if "watch?" in u or "playlist?" in u:
        return u

    u = u.split("?")[0].split("#")[0].rstrip("/")

    m = re.search(r"/(playlists|featured)$", u)
    if m:
        return u[: m.start()] + "/videos"

    if re.search(r"/(videos|streams|shorts)$", u):
        return u

    return u + "/videos"


def channel_tag(url: str) -> str:
    tag = re.sub(r"^https?://(www\.)?youtube\.com/", "", url)
    tag = re.sub(r"/(videos|streams|shorts)$", "", tag)
    return tag[:40]


def channel_key(u: str) -> str:
    return channel_url(u).lower()


# ---------------- channels.txt ----------------

file_lock = threading.Lock()


def parse_channels_file(cfile: Path) -> list[str]:
    if not cfile.exists():
        return []
    try:
        lines = cfile.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    return [ln.strip() for ln in lines if ln.strip() and not ln.strip().startswith("#")]


def ensure_channels_file(cfile: Path) -> None:
    if cfile.exists():
        return
    cfile.write_text(
        "# Mỗi dòng một kênh\n"
        "# Có thể dùng URL, @handle hoặc channel ID\n"
        "# Copy link kênh YouTube vào clipboard -> chương trình tự thêm vào đây\n"
        "# Ví dụ:\n"
        "# https://www.youtube.com/@example\n"
        "# @example\n"
        "# UCxxxxxxxxxxxxxxxxxxxxxx\n",
        encoding="utf-8",
    )


def add_channel_to_file(cfile: Path, raw: str) -> bool:
    """Thêm kênh vào channels.txt nếu chưa có. True nếu đã thêm."""
    url = raw.strip().split("?")[0].split("#")[0].rstrip("/")
    with file_lock:
        ensure_channels_file(cfile)
        key = channel_key(url)
        if any(channel_key(x) == key for x in parse_channels_file(cfile)):
            return False
        text = cfile.read_text(encoding="utf-8")
        prefix = "" if (not text or text.endswith("\n")) else "\n"
        with cfile.open("a", encoding="utf-8") as f:
            f.write(prefix + url + "\n")
        return True


def remove_channel_from_file(cfile: Path, key: str) -> None:
    with file_lock:
        if not cfile.exists():
            return
        lines = cfile.read_text(encoding="utf-8").splitlines()
        kept = []
        for ln in lines:
            s = ln.strip()
            if s and not s.startswith("#") and channel_key(s) == key:
                continue
            kept.append(ln)
        cfile.write_text("\n".join(kept) + "\n", encoding="utf-8")


# ---------------- exclusion_channels.txt ----------------

_EXC_HANDLE_RE = re.compile(r"(@[\w.\-%]+)")
_EXC_ID_RE = re.compile(r"(UC[\w-]{22})")
_EXC_LEGACY_RE = re.compile(r"/(c|user)/([\w.\-%]+)", re.IGNORECASE)


def exclusion_token(raw: str) -> str | None:
    """Rút định danh chuẩn hoá của kênh (từ URL / @handle / channel ID) để so khớp danh sách loại trừ.
    Ví dụ: https://www.youtube.com/@Abc/videos -> '@abc';  .../channel/UCxxx -> 'id:ucxxx'."""
    s = raw.strip().split("?")[0].split("#")[0]
    if not s:
        return None
    if m := _EXC_HANDLE_RE.search(s):
        return unquote(m.group(1)).lower()
    if m := _EXC_ID_RE.search(s):
        return "id:" + m.group(1).lower()
    if m := _EXC_LEGACY_RE.search(s):
        return f"{m.group(1).lower()}:{unquote(m.group(2)).lower()}"
    return None


def ensure_exclusion_file(efile: Path) -> None:
    if efile.exists():
        return
    efile.write_text(
        "# Kênh KHÔNG tải - mỗi dòng một kênh\n"
        "# Có thể dùng URL, @handle hoặc channel ID\n"
        "# Kênh nằm trong file này sẽ bị bỏ qua dù có trong channels.txt\n"
        "# Ví dụ:\n"
        "# https://www.youtube.com/@example\n"
        "# @example\n"
        "# UCxxxxxxxxxxxxxxxxxxxxxx\n",
        encoding="utf-8",
    )


def add_exclusion_to_file(efile: Path, raw: str) -> bool:
    """Thêm kênh vào exclusion_channels.txt nếu chưa có. True nếu đã thêm."""
    token = exclusion_token(raw)
    if not token:
        return False
    with file_lock:
        ensure_exclusion_file(efile)
        if any(exclusion_token(x) == token for x in parse_channels_file(efile)):
            return False
        text = efile.read_text(encoding="utf-8")
        prefix = "" if (not text or text.endswith("\n")) else "\n"
        with efile.open("a", encoding="utf-8") as f:
            f.write(prefix + raw.strip() + "\n")
        return True


def remove_exclusion_from_file(efile: Path, raw: str) -> None:
    token = exclusion_token(raw)
    if not token:
        return
    with file_lock:
        if not efile.exists():
            return
        kept = []
        for ln in efile.read_text(encoding="utf-8").splitlines():
            s = ln.strip()
            if s and not s.startswith("#") and exclusion_token(s) == token:
                continue
            kept.append(ln)
        efile.write_text("\n".join(kept) + "\n", encoding="utf-8")


def remove_channel_by_token(cfile: Path, token: str | None) -> None:
    """Xoá khỏi channels.txt mọi dòng có cùng định danh kênh (dùng khi vừa đưa kênh vào loại trừ)."""
    if not token:
        return
    with file_lock:
        if not cfile.exists():
            return
        kept = []
        for ln in cfile.read_text(encoding="utf-8").splitlines():
            t = ln.strip()
            if t and not t.startswith("#") and exclusion_token(t) == token:
                continue
            kept.append(ln)
        cfile.write_text("\n".join(kept) + "\n", encoding="utf-8")


# ---------------- metadata ----------------

def entry_upload_date(d: dict) -> str | None:
    """Ngày đăng dạng YYYYMMDD (từ upload_date / timestamp / release_timestamp), không có -> None."""
    ud = d.get("upload_date")
    if isinstance(ud, str) and re.fullmatch(r"\d{8}", ud):
        return ud
    for k in ("timestamp", "release_timestamp"):
        ts = d.get(k)
        if isinstance(ts, (int, float)) and ts > 0:
            return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y%m%d")
    return None


def video_meta(entry: dict, need_date: bool = False) -> tuple[str, str, int | None, int | None, str | None]:
    """Trả về (url, tiêu đề, độ dài giây, lượt xem, ngày đăng YYYYMMDD | None).
    need_date=True: nếu entry chưa có ngày đăng thì tải thêm thông tin đầy đủ để lấy."""
    vid = entry.get("id")
    url = entry.get("url")

    if not url or not url.startswith("http"):
        url = "https://www.youtube.com/watch?v=" + str(vid)

    title = entry.get("title")
    duration = entry.get("duration")
    views = entry.get("view_count")
    upload = entry_upload_date(entry)

    if title is None or duration is None or views is None or (need_date and upload is None):
        with make_ydl({"quiet": True, "no_warnings": True, "skip_download": True, **YDL_COMMON}) as ydl:
            info = ydl.extract_info(url, download=False) or {}
        title = title or info.get("title") or str(vid)
        duration = duration if duration is not None else info.get("duration")
        views = views if views is not None else info.get("view_count")
        upload = upload or entry_upload_date(info)

    return url, title, duration, views, upload


# ==========================================================
# ENGINE (backend, không đụng tới Tkinter - chỉ đẩy event vào queue)
# ==========================================================

class ChannelState:
    """
    quota  = số video PHẢI tải xong cho mỗi kênh (top_videos)
    done   = video tải xong (video đã tải từ trước KHÔNG tính)
    active = video đang tải / chờ trong hàng đợi tải

    Thứ tự ứng viên:
      1) nhóm video gần đây đủ điều kiện, xếp theo view giảm dần (chỉ lấy view > min_views)
      2) nếu vẫn thiếu -> lấy danh sách video PHỔ BIẾN NHẤT của kênh, lọc cùng điều kiện
         (độ dài min..max phút, view > min_views), xếp theo view giảm dần
    Video lỗi / members-only / đã tải từ trước -> lấy ứng viên kế tiếp để bù cho đủ quota.
    """

    def __init__(self, eng: "Engine", url: str) -> None:
        self.eng = eng
        self.url = url
        self.key = url.lower()
        self.tag = channel_tag(url)
        self.pool_size = max(1, int(eng.cfg["videos_per_channel"]))
        self.gen = eng.gen

        self.lock = threading.Lock()
        self.done = 0
        self.failed = 0
        self.active = 0
        self.scan_done = False
        self.finished = threading.Event()

        self.cands: list[dict] = []          # ứng viên chờ tải, đã xếp theo view giảm dần
        self.known_urls: set[str] = set()    # mọi video đã được xét (tránh xét lại khi bù)
        self.popular_state = "idle"          # idle | loading | done
        self.fail_subs = 0                   # số lần bù do lỗi thường
        self.give_up = False
        self.excluded = False                # kênh nằm trong exclusion_channels.txt
        self.completed = False               # đã chạy hết (không phải bị Dừng giữa chừng)
        self.ids: set[str] = set()           # định danh kênh (id/@handle) lấy được khi quét
        m = re.search(r"/channel/(UC[\w-]{22})", url)
        self.channel_id: str | None = m.group(1) if m else None

    @property
    def min_views(self) -> int:
        return int(self.eng.cfg.get("min_views", 500000))

    @property
    def quota(self) -> int:
        return max(1, int(self.eng.cfg.get("top_videos", 3)))

    def alive(self) -> bool:
        return self.gen == self.eng.gen and not self.excluded

    def status_text(self) -> str:
        if self.excluded:
            return "Loại trừ"
        if self.finished.is_set():
            return "Xong" if self.completed else "Đã dừng"
        if not self.alive():
            return "Đang dừng..."
        if self.popular_state == "loading":
            return "Đang tìm bù"
        return "Đang tải" if self.scan_done or self.active else "Đang quét"

    def push(self) -> None:
        self.eng.q.put(("channel", dict(
            key=self.key, tag=self.tag, status=self.status_text(),
            done=self.done, quota=self.quota)))

    def _check_finished(self) -> None:
        # gọi khi đang giữ self.lock
        if (self.scan_done and self.active == 0 and self.popular_state != "loading"
                and not self.finished.is_set()):
            self.finished.set()
            self.completed = self.alive()
            if self.excluded:
                return
            short = self.alive() and self.done < self.quota
            self.eng.log(f"[HẾT KÊNH] {self.tag}: {self.done}/{self.quota} video"
                         + (f", lỗi {self.failed}" if self.failed else "")
                         + (" - CHƯA ĐỦ, hết video đủ điều kiện để bù." if short else "."))

    # ---- lập kế hoạch bù video (gọi khi đang giữ self.lock) ----
    def _pick_locked(self) -> dict | None:
        """Lấy ứng viên kế tiếp chưa tải. Video đã có trong downloaded_titles -> bỏ qua, thử kế tiếp."""
        while self.cands:
            c = self.cands.pop(0)
            if c["url"] in self.eng.blocked_urls:
                continue
            dup = self.eng.check_and_reserve(c["url"], c["title"])
            if dup:
                self.eng.log(f"  [{self.tag} #{c['index']}] "
                             f"[ĐÃ TẢI (trùng {dup}) - bỏ qua, thử video kế tiếp] {c['title']}")
                continue
            self.active += 1
            return c
        return None

    def _plan_locked(self) -> tuple[list[dict], bool]:
        """Trả về (video cần bắt đầu tải, có cần nạp danh sách phổ biến không)."""
        tasks: list[dict] = []
        if not self.alive() or self.give_up:
            return tasks, False
        while self.done + self.active < self.quota:
            c = self._pick_locked()
            if c is None:
                break
            tasks.append(c)
        start_pop = False
        if (self.done + self.active < self.quota and self.scan_done
                and self.popular_state == "idle"):
            self.popular_state = "loading"
            start_pop = True
        return tasks, start_pop

    def _launch(self, tasks: list[dict], start_pop: bool, label: str) -> None:
        for c in tasks:
            rid = self.eng.new_row(f"{self.tag} #{c['index']}", c["title"], c["url"])
            self.eng.log(f"  [{self.tag} #{c['index']}] [{label} - {c.get('src', 'gần đây')}] "
                         f"{c['views']:,} view, {c['minutes']:.1f} phút - {c['title']}")
            self.eng.submit_dl(self._run, c, rid)
        if start_pop:
            self.eng.submit_scan(self._load_popular)

    # ---- API cho luồng quét ----
    def offer_many(self, pool: list[dict]) -> None:
        """Nhận nhóm video gần đây đủ điều kiện; giữ video view > min_views, xếp theo view giảm dần."""
        ranked = sorted(pool, key=lambda c: c["views"], reverse=True)
        good: list[dict] = []
        for pos, c in enumerate(ranked):
            if c["views"] > self.min_views:
                c["src"] = "gần đây"
                good.append(c)
            elif pos < self.quota:
                self.eng.log(f"  [{self.tag} #{c['index']}] [BỎ QUA - chỉ {c['views']:,} view "
                             f"<= {self.min_views:,}] {c['title']}")

        with self.lock:
            self.known_urls.update(c["url"] for c in pool)
            self.cands = good
            tasks, start_pop = self._plan_locked()
            self._check_finished()
            self.push()
        self._launch(tasks, start_pop, "NHẬN")

    def scan_finished(self) -> None:
        with self.lock:
            self.scan_done = True
            tasks, start_pop = self._plan_locked()
            self._check_finished()
            self.push()
        self._launch(tasks, start_pop, "NHẬN")

    # ---- bù từ video phổ biến nhất ----
    def _popular_sources(self) -> list[tuple[str, str]]:
        srcs: list[tuple[str, str]] = []
        if self.channel_id:
            # playlist tự động "Popular uploads" của kênh: UC... -> UULP...
            srcs.append(("playlist phổ biến (UULP)",
                         f"https://www.youtube.com/playlist?list=UULP{self.channel_id[2:]}"))
        base = self.url.split("?")[0]
        if base.endswith("/videos"):
            srcs.append(("tab Videos sắp xếp phổ biến (sort=p)", base + "?view=0&sort=p"))
        return srcs

    def _load_popular(self) -> None:
        eng = self.eng
        tag = self.tag
        found: list[dict] = []
        try:
            if not self.alive():
                return
            min_d = int(eng.cfg["min_minutes"]) * 60
            max_d = int(eng.cfg["max_minutes"]) * 60
            limit = max(1, int(eng.cfg.get("popular_scan", 60)))
            eng.log(f"[{tag}] Mới có {self.done}/{self.quota} video -> bù từ video phổ biến nhất "
                    f"(độ dài {min_d // 60}-{max_d // 60} phút, view > {self.min_views:,}, không giới hạn ngày đăng)")

            entries: list[dict] = []
            used = ""
            for label, src_url in self._popular_sources():
                try:
                    entries, cid = eng.list_channel_videos(src_url, limit=limit)
                    if cid and not self.channel_id:
                        self.channel_id = cid
                except Exception as e:
                    eng.log(f"  [{tag}] Nguồn '{label}' lỗi: {e}")
                    entries = []
                if entries:
                    used = label
                    break
                eng.log(f"  [{tag}] Nguồn '{label}' không trả về video, thử nguồn khác...")
            if not entries:
                eng.log(f"  [{tag}] Không lấy được danh sách video phổ biến.")
                return
            eng.log(f"  [{tag}] Xét {len(entries)} video từ nguồn: {used}")

            futs = [eng.meta_pool.submit(video_meta, e) for e in entries]     # bù từ phổ biến: không lọc ngày đăng
            try:
                for index, fut in enumerate(futs, start=1):
                    if not self.alive() or len(found) >= POPULAR_POOL:
                        break
                    try:
                        url, title, duration, views, _upload = fut.result()
                    except Exception as e:
                        if not is_members_error(e):
                            eng.log(f"  [{tag} P#{index}] Bỏ qua - lỗi metadata: {e}")
                        continue

                    with self.lock:
                        known = url in self.known_urls
                    if known or url in eng.blocked_urls:
                        continue
                    if duration is None or not (min_d <= duration <= max_d):
                        continue
                    if views is None or int(views) <= self.min_views:
                        continue
                    found.append({"index": f"P{index}", "url": url, "title": title,
                                  "minutes": duration / 60, "views": int(views),
                                  "src": "phổ biến"})
            finally:
                for f in futs:
                    f.cancel()

            vs = [c["views"] for c in found]
            if any(a < b for a, b in zip(vs, vs[1:])):
                eng.log(f"  [{tag}] Lưu ý: nguồn '{used}' không xếp theo view giảm dần "
                        f"(thử cập nhật yt-dlp) -> tự xếp lại theo view.")
            found.sort(key=lambda c: c["views"], reverse=True)
            eng.log(f"  [{tag}] Tìm được {len(found)} video phổ biến đủ điều kiện để bù.")

        except Exception as e:
            eng.log(f"[LỖI BÙ] {tag}: {e}")

        finally:
            with self.lock:
                self.known_urls.update(c["url"] for c in found)
                self.cands.extend(found)
                self.popular_state = "done"
                tasks, start_pop = self._plan_locked()
                self._check_finished()
                self.push()
            self._launch(tasks, start_pop, "BÙ")

    # ---- API cho luồng tải ----
    def _run(self, cand: dict, rid: str) -> None:
        result = "fail"
        try:
            if self.alive():
                result = self.eng.download_one(cand["url"], cand["title"], rid, self.gen)
                with self.eng.lock:
                    self.eng.stats["ok" if result == "ok" else "fail"] += 1
            else:
                self.eng.row(rid, status="Đã hủy", speed="")
                self.eng.release_video(cand["url"], cand["title"])
        finally:
            self.on_result(result, cand)

    def on_result(self, result: str, cand: dict | None = None) -> None:
        label = "BÙ"
        with self.lock:
            self.active -= 1
            if result == "ok":
                self.done += 1
            else:
                self.failed += 1
                if self.alive():
                    if result == "members":
                        label = "BÙ - video members-only"
                    else:
                        label = "BÙ - video lỗi"
                        self.fail_subs += 1
                        if self.fail_subs > MAX_FAIL_SUBSTITUTES:
                            self.give_up = True
                            self.eng.log(f"  [{self.tag}] Quá {MAX_FAIL_SUBSTITUTES} video lỗi liên tiếp "
                                         f"-> ngừng bù (kiểm tra mạng / cookies).")
            tasks, start_pop = self._plan_locked()
            self._check_finished()
            self.push()
        self._launch(tasks, start_pop, label)


class Engine:
    def __init__(self, cfg: dict, ffmpeg: Path, aria2c: Path | None, ui_q: queue.Queue) -> None:
        self.cfg = cfg
        self.ffmpeg = ffmpeg
        self.aria2c = aria2c
        self.q = ui_q

        self.lock = threading.Lock()
        self.seen_ids = load_seen(TITLES_FILE)
        self.stats = {"ok": 0, "fail": 0}
        self.blocked_urls: set[str] = set()   # video members-only đã gặp (bỏ qua khi quét lại)
        self.gen = 0                       # tăng lên khi bấm Dừng
        self.row_seq = 0
        self.channels: dict[str, ChannelState] = {}
        self._excl_logged: set[str] = set()   # kênh đã log "loại trừ" (tránh lặp mỗi giây)
        self.carry: dict[str, int] = {}       # kênh bị Dừng giữa chừng -> số video đã tải xong (để tiếp tục)

        self.pool_lock = threading.Lock()
        self._dl_workers = max(1, int(cfg["workers"]))
        self._scan_workers = max(1, int(cfg["scan_workers"]))
        self.dl_pool = ThreadPoolExecutor(max_workers=self._dl_workers)
        self.scan_pool = ThreadPoolExecutor(max_workers=self._scan_workers)
        self.meta_pool = ThreadPoolExecutor(max_workers=META_WORKERS)

    # ---- UI events ----
    def log(self, msg: str) -> None:
        self.q.put(("log", msg))

    # ---- chống tải trùng: chỉ kiểm tra MÃ video ----
    def check_and_reserve(self, url: str, title: str) -> str | None:
        """Trả về "mã" nếu video đã tải (hoặc đang được tải); None nếu chưa -> đồng thời giữ chỗ."""
        vid = video_id_from(url)
        if not vid:
            return None
        with self.lock:
            if vid in self.seen_ids:
                return "mã"
            self.seen_ids.add(vid)
        return None

    def release_video(self, url: str, title: str) -> None:
        """Bỏ giữ chỗ khi video bị hủy / lỗi / members-only (để lần sau còn tải lại được)."""
        vid = video_id_from(url)
        if not vid:
            return
        with self.lock:
            self.seen_ids.discard(vid)

    def submit_dl(self, fn, *args):
        with self.pool_lock:
            return self.dl_pool.submit(fn, *args)

    def submit_scan(self, fn, *args):
        with self.pool_lock:
            return self.scan_pool.submit(fn, *args)

    def apply_settings(self) -> None:
        """Áp dụng cài đặt mới ngay cho chương trình đang chạy.
        Đổi số luồng -> tạo hàng đợi mới cho các việc sau; việc đang chạy / đã xếp hàng ở hàng đợi cũ vẫn chạy nốt."""
        dl = max(1, int(self.cfg["workers"]))
        sc = max(1, int(self.cfg["scan_workers"]))
        with self.pool_lock:
            if dl != self._dl_workers:
                old, self.dl_pool, self._dl_workers = self.dl_pool, ThreadPoolExecutor(max_workers=dl), dl
                old.shutdown(wait=False)
            if sc != self._scan_workers:
                old, self.scan_pool, self._scan_workers = self.scan_pool, ThreadPoolExecutor(max_workers=sc), sc
                old.shutdown(wait=False)

    def age_cutoff(self) -> str | None:
        """Ngày sớm nhất (YYYYMMDD) còn được tải; None nếu không giới hạn tuổi video."""
        try:
            days = int(self.cfg.get("max_age_days", 0))
        except (TypeError, ValueError):
            days = 0
        if days <= 0:
            return None
        return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y%m%d")

    def new_row(self, source: str, title: str, url: str = "") -> str:
        with self.lock:
            self.row_seq += 1
            rid = f"r{self.row_seq}"
        self.q.put(("row", dict(id=rid, src=source, title=title, url=url,
                                status="Chờ", pct=0, speed="")))
        return rid

    def row(self, rid: str, **kw) -> None:
        self.q.put(("row", dict(id=rid, **kw)))

    # ---- yt-dlp ----
    def outdir(self) -> Path:
        p = Path(self.cfg["output"])
        return p if p.is_absolute() else SCRIPT_DIR / p

    def make_opts(self) -> dict:
        h = int(self.cfg["max_height"])
        opts = {
            "outtmpl": str(self.outdir() / "%(uploader)s" / "%(title)s.%(ext)s"),
            "format": (f"bv*[height<={h}][protocol!*=m3u8]+ba[ext=m4a]/"
                       f"bv*[height<={h}][protocol!*=m3u8]+ba/"
                       f"bv*[height<={h}]+ba/b[height<={h}]"),
            "format_sort": [f"res:{h}", "ext:mp4:m4a"],
            "merge_output_format": "mp4",
            "ffmpeg_location": str(self.ffmpeg),
            "windowsfilenames": True,
            "trim_file_name": 200,
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "retries": 10,
            "fragment_retries": 10,
            "file_access_retries": 5,
            "socket_timeout": 20,
            "buffersize": 1024 * 256,
            "overwrites": False,
            "continuedl": True,
            **YDL_COMMON,
            "sleep_interval_requests": DOWNLOAD_REQUEST_SLEEP_S,   # ghi đè giá trị dùng khi quét kênh
        }

        if self.cfg["use_aria2c"] and self.aria2c:
            opts["external_downloader"] = {"default": "aria2c"}
            opts["external_downloader_args"] = {"aria2c": list(ARIA2_ARGS)}
        else:
            opts["http_chunk_size"] = 10 * 1024 * 1024
        # dùng cho video dạng fragment (HLS/DASH), vô hại với video thường
        opts["concurrent_fragment_downloads"] = CONCURRENT_FRAGMENTS

        return opts

    def download_one(self, url: str, title: str, rid: str, gen: int) -> str:
        """Trả về "ok" | "members" (video chỉ dành cho hội viên) | "fail"."""
        st = {"title": title, "last": 0.0}
        touched: set[str] = set()          # mọi file yt-dlp đã đụng tới (để dọn khi lỗi)

        def hook(d: dict) -> None:
            if gen != self.gen:
                raise DownloadCancelled("Đã dừng")

            if d.get("filename"):
                touched.add(d["filename"])

            t = (d.get("info_dict") or {}).get("title")
            if t and t != st["title"]:
                st["title"] = t
                self.row(rid, title=t)

            if d.get("status") == "downloading":
                now = time.time()
                if now - st["last"] < 0.4:
                    return
                st["last"] = now
                total = d.get("total_bytes") or d.get("total_bytes_estimate")
                done = d.get("downloaded_bytes") or 0
                pct = (done / total * 100) if total else None
                self.row(rid, status="Đang tải", pct=pct, speed=fmt_speed(d.get("speed")))
            elif d.get("status") == "finished":
                self.row(rid, status="Đang xử lý...", pct=100, speed="")

        def cleanup() -> None:
            for fn in list(touched):
                purge_by_filename(fn)
            touched.clear()

        opts = self.make_opts()
        opts["progress_hooks"] = [hook]
        opts["noplaylist"] = not re.search(r"[?&/]list=|/playlist", url)
        opts["no_warnings"] = False
        opts["logger"] = YdlLogger(self.log)

        try:
            self.row(rid, status="Đang tải")
            self.log(f"[TẢI] {title}")

            time.sleep(DOWNLOAD_DELAY_S)
            if gen != self.gen:
                raise DownloadCancelled("Đã dừng")

            ok, last_err, codec, path = False, None, "unknown", None
            for attempt in (1, 2):
                try:
                    with make_ydl(opts) as ydl:
                        info = ydl.extract_info(url, download=True) or {}
                    self.log(f"  [FORMAT] {describe_formats(info)}")

                    path = final_path(info)
                    if path is None or not path.exists():
                        raise RuntimeError("Không tìm thấy file đầu ra sau khi tải/ghép")

                    self.row(rid, status="Kiểm tra audio...")
                    codec = probe_audio(self.ffmpeg, path)
                    if codec is None:
                        raise NoAudioError("File đầu ra không có audio")
                    ok = True
                    break

                except DownloadCancelled:
                    raise
                except Exception as e:
                    if is_members_error(e):
                        raise
                    last_err = e
                    cleanup()
                    if path is not None:
                        purge_outputs(path)
                    path = None
                    opts["overwrites"] = True
                    if attempt == 1:
                        self.log(f"  [LỖI lần 1 -> xóa file dở, tải lại] {title}: {e}")
                        self.row(rid, status="Tải lại...", pct=0, speed="")
                        if gen != self.gen:
                            raise DownloadCancelled("Đã dừng")

            if not ok:
                self.log(f"[BỎ] {title}: lỗi 2 lần, đã xóa file dở ({last_err})")
            else:
                if codec in ("opus", "vorbis") and path is not None:
                    self.row(rid, status="Chuyển audio sang AAC...")
                    if not reencode_audio_aac(self.ffmpeg, path):
                        self.log(f"  [CẢNH BÁO] Không chuyển được {codec} -> AAC: {path.name}")

                final = st["title"]
                vid = video_id_from(url, info)
                clean = " ".join(final.split())
                if vid:
                    line = f"#{vid}|{clean}"
                    with self.lock:
                        self.seen_ids.add(vid)
                        with TITLES_FILE.open("a", encoding="utf-8") as f:
                            f.write(line + "\n")
                    self.log(f"  [GHI] {TITLES_FILE.name}: {line}")
                else:
                    self.log(f"  [CẢNH BÁO] Không lấy được mã video từ {url} -> không ghi được vào {TITLES_FILE.name}")
                self.row(rid, status="Xong", pct=100, speed="", title=final)
                self.log(f"[XONG] {final}")
                return "ok"

        except DownloadCancelled:
            self.row(rid, status="Đã hủy", speed="")
            self.log(f"[HỦY] {title}")
            self.release_video(url, title)
            return "fail"

        except Exception as e:
            if is_members_error(e):
                cleanup()
                self.log(f"[MEMBERS-ONLY] {title} -> tìm video khác thay thế")
                with self.lock:
                    self.blocked_urls.add(url)
                self.release_video(url, title)
                self.row(rid, status="Members-only", speed="")
                return "members"
            cleanup()
            self.log(f"[LỖI] {title}: {e}")

        self.row(rid, status="Lỗi", speed="")
        self.release_video(url, title)
        return "fail"

    # ---- kênh ----
    def start_channel(self, raw: str, force: bool = False, excluded: set[str] | None = None) -> bool:
        url = channel_url(raw)
        key = url.lower()

        tok = exclusion_token(url)
        if excluded is None:
            excluded = self.exclusion_tokens()
        if tok and tok in excluded:
            if key not in self._excl_logged:
                self._excl_logged.add(key)
                self.log(f"[LOẠI TRỪ] {channel_tag(url)} nằm trong exclusion_channels.txt -> không tải.")
            return False
        self._excl_logged.discard(key)

        with self.lock:
            old = self.channels.get(key)
            if old is not None and (not force or not old.finished.is_set()):
                return False
            st = ChannelState(self, url)
            st.done = self.carry.pop(key, 0)     # tiếp tục kênh bị dừng: giữ số video đã tải xong
            self.channels[key] = st

        st.push()
        self.submit_scan(self.scan_channel, st)
        return True

    def forget_channel(self, key: str) -> None:
        with self.lock:
            self.channels.pop(key, None)

    def list_channel_videos(self, url: str, limit: int | None = None,
                            ids_out: set[str] | None = None) -> tuple[list[dict], str | None]:
        """Trả về (danh sách entry, channel_id UC... nếu biết)."""
        max_scan = int(limit or self.cfg["max_scan"])
        opts = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "extract_flat": True,
            "ignoreerrors": True,
            "playlistend": max_scan,
            **YDL_COMMON,
        }
        with make_ydl(opts) as ydl:
            info = ydl.extract_info(url, download=False)

        if not info:
            return [], None

        if ids_out is not None:                       # định danh kênh (id / @handle) để so với danh sách loại trừ
            for k in ("channel_id", "uploader_id", "channel_url", "uploader_url"):
                v = info.get(k)
                if isinstance(v, str) and (t := exclusion_token(v)):
                    ids_out.add(t)

        cid = info.get("channel_id") or info.get("uploader_id")
        cid = cid if isinstance(cid, str) and re.fullmatch(r"UC[\w-]{22}", cid) else None

        if "entries" not in info:
            info = dict(info)
            info["url"] = info.get("webpage_url") or url
            return [info], cid

        entries = [e for e in (info.get("entries") or []) if e and e.get("id")]
        return entries[:max_scan], cid

    def scan_channel(self, st: ChannelState) -> None:
        tag = st.tag
        min_d = int(self.cfg["min_minutes"]) * 60
        max_d = int(self.cfg["max_minutes"]) * 60
        cutoff = self.age_cutoff()
        age_txt = f", chỉ video đăng trong {int(self.cfg['max_age_days'])} ngày gần đây" if cutoff else ""
        self.log(f"[KÊNH] {tag} (phải tải đủ {st.quota} video: xét {st.pool_size} video gần đây đủ điều kiện, "
                 f"ưu tiên nhiều view, yêu cầu > {st.min_views:,} view{age_txt}; thiếu thì bù từ video phổ biến nhất)")
        futs = []
        eligible: list[dict] = []

        try:
            ids: set[str] = set()
            entries, cid = self.list_channel_videos(st.url, ids_out=ids)
            st.ids = ids
            if ids & self.exclusion_tokens():          # vd channels.txt ghi @handle, exclusion ghi channel ID
                st.excluded = True
                st.give_up = True
                self.log(f"[LOẠI TRỪ] {tag} nằm trong exclusion_channels.txt -> bỏ qua, không tải.")
                return
            if cid:
                st.channel_id = cid
            if not entries:
                self.log(f"  [{tag}] Không tìm thấy video.")
                return

            futs = [self.meta_pool.submit(video_meta, e, cutoff is not None) for e in entries]

            for index, fut in enumerate(futs, start=1):
                if not st.alive() or len(eligible) >= st.pool_size:
                    break

                try:
                    url, title, duration, views, upload = fut.result()
                except Exception as e:
                    if is_members_error(e):
                        self.log(f"  [{tag} #{index}] [BỎ QUA - members-only]")
                    else:
                        self.log(f"  [{tag} #{index}] Bỏ qua - lỗi metadata: {e}")
                    continue

                if url in self.blocked_urls:
                    self.log(f"  [{tag} #{index}] [BỎ QUA - members-only] {title}")
                    continue

                if duration is None:
                    self.log(f"  [{tag} #{index}] [BỎ QUA - không biết độ dài] {title}")
                    continue

                minutes = duration / 60
                if duration < min_d:
                    self.log(f"  [{tag} #{index}] [BỎ QUA - {minutes:.1f} phút < {min_d // 60}] {title}")
                    continue
                if duration > max_d:
                    self.log(f"  [{tag} #{index}] [BỎ QUA - {minutes:.1f} phút > {max_d // 60}] {title}")
                    continue
                if cutoff:
                    if upload is None:
                        self.log(f"  [{tag} #{index}] [BỎ QUA - không biết ngày đăng] {title}")
                        continue
                    if upload < cutoff:
                        self.log(f"  [{tag} #{index}] [BỎ QUA - đăng {upload[:4]}-{upload[4:6]}-{upload[6:]}, "
                                 f"quá {int(self.cfg['max_age_days'])} ngày] {title}")
                        continue

                if views is None:
                    self.log(f"  [{tag} #{index}] [không có view - coi như 0] {title}")
                    views = 0

                eligible.append({"index": index, "url": url, "title": title,
                                 "minutes": minutes, "views": int(views)})

            if eligible and st.alive():
                ranked = sorted(eligible, key=lambda c: c["views"], reverse=True)
                top = ", ".join(f"#{c['index']} ({c['views']:,})" for c in ranked[: st.quota])
                self.log(f"  [{tag}] Đã gom {len(eligible)} video -> ưu tiên theo view: {top}")
                st.offer_many(eligible)
            elif not eligible:
                self.log(f"  [{tag}] Không có video nào đủ điều kiện.")

        except Exception as e:
            self.log(f"[LỖI KÊNH] {tag}: {e}")

        finally:
            for f in futs:
                f.cancel()
            st.scan_finished()

    def sync_channels_file(self) -> None:
        excluded = self.exclusion_tokens()
        for line in parse_channels_file(CHANNELS_FILE):
            if self.start_channel(line, excluded=excluded):
                self.log(f"[KÊNH MỚI] {channel_tag(channel_url(line))}")

    def exclusion_tokens(self) -> set[str]:
        return {t for ln in parse_channels_file(EXCLUSION_FILE) if (t := exclusion_token(ln))}

    def apply_exclusions(self) -> None:
        """Áp dụng exclusion_channels.txt cho kênh đang có: kênh vừa bị loại trừ -> không tải thêm
        (video đang tải dở vẫn chạy nốt); kênh vừa được bỏ loại trừ -> được thả ra để quét lại."""
        excl = self.exclusion_tokens()
        with self.lock:
            states = list(self.channels.values())
        for st in states:
            toks = set(st.ids)
            if t := exclusion_token(st.url):
                toks.add(t)
            hit = bool(toks & excl)
            if hit and not st.excluded:
                st.excluded = True
                self.log(f"[LOẠI TRỪ] {st.tag} -> không tải thêm video của kênh này "
                         f"(video đang tải dở vẫn chạy nốt).")
                st.push()
            elif not hit and st.excluded and st.finished.is_set():
                self.forget_channel(st.key)
                self.log(f"[LOẠI TRỪ] Đã bỏ loại trừ {st.tag} -> sẽ quét lại nếu còn trong channels.txt.")

    def prepare_resume(self) -> int:
        """Sau khi Dừng: các kênh chưa chạy xong được gỡ ra để quét lại, nhưng nhớ số video đã tải xong
        (carry) nên chỉ tải nốt phần còn thiếu. Kênh đã xong / bị loại trừ giữ nguyên, không tải lại."""
        n = 0
        with self.lock:
            for key, st in list(self.channels.items()):
                if st.finished.is_set() and not st.completed and not st.excluded:
                    self.carry[key] = st.done
                    del self.channels[key]
                    n += 1
        return n

    def stop_all(self) -> None:
        self.gen += 1
        self.log("[DỪNG] Đang hủy các tác vụ... (video đang tải bằng aria2c sẽ dừng sau khi xong file hiện tại)")

    def busy(self) -> bool:
        with self.lock:
            return any(not s.finished.is_set() for s in self.channels.values())


# ==========================================================
# GUI
# ==========================================================

class App(tk.Tk):
    def __init__(self, cfg: dict, engine: Engine, ui_q: queue.Queue, aria2c: Path | None) -> None:
        super().__init__()
        self.cfg = cfg
        self.engine = engine
        self.q = ui_q

        self.title("YouTube Channel Downloader")
        self.geometry("1050x760")
        self.minsize(900, 600)

        self.clip_chan_var = tk.BooleanVar(value=cfg["watch_clipboard"])
        self.stat_var = tk.StringVar(value="")
        self._excl_cache: list[str] | None = None
        self.started = False                       # chỉ True sau khi bấm ▶ Start
        self.row_urls: dict[str, str] = {}         # id dòng trong bảng tiến trình -> link video
        self.first_start = True                    # lần Start đầu tiên mới hỏi về channels.txt

        self._build(aria2c)

        self.last_clip = ""                        # clipboard đã xử lý gần nhất (rỗng -> bấm Start sẽ xét luôn clipboard hiện tại)

        ensure_channels_file(CHANNELS_FILE)
        ensure_exclusion_file(EXCLUSION_FILE)
        self.refresh_exclusions()
        self.engine.log("Sẵn sàng. Bấm ▶ Start để bắt đầu theo dõi clipboard và tải kênh.")

        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(100, self.pump)
        self.after(WATCH_INTERVAL_MS, self.watch)

    # ---------------- hỏi khi khởi động ----------------
    def ask_resume_channels(self) -> bool:
        """Khi bấm Start: nếu channels.txt đang có kênh -> hỏi có tiếp tục quét + tải không.
        Yes: giữ nguyên, engine sẽ quét và tải như bình thường (trả về False).
        No : không tải, xóa sạch channels.txt (chỉ còn phần hướng dẫn) và trả về True."""
        chans = parse_channels_file(CHANNELS_FILE)
        if not chans:
            return False
        self.update()                                  # cho cửa sổ chính hiện ra trước khi hiện hộp thoại
        ans = messagebox.askyesno(
            "Tiếp tục tải?",
            f"Trong channels.txt đang có {len(chans)} kênh đã lưu.\n\n"
            "Có - tiếp tục quét và tải các kênh này\n"
            "Không - không tải và XÓA SẠCH danh sách kênh",
            parent=self,
        )
        if ans:
            return False
        header = (
            "# Mỗi dòng một kênh\n"
            "# Có thể dùng URL, @handle hoặc channel ID\n"
            "# Copy link kênh YouTube vào clipboard -> chương trình tự thêm vào đây\n"
            "# Ví dụ:\n"
            "# https://www.youtube.com/@example\n"
            "# @example\n"
            "# UCxxxxxxxxxxxxxxxxxxxxxx\n"
        )
        try:
            with file_lock:
                CHANNELS_FILE.write_text(header, encoding="utf-8")   # ghi đè, không phụ thuộc unlink
        except OSError as e:
            self.engine.log(f"[LỖI] Không xóa được channels.txt: {e}")
            messagebox.showerror("Xóa kênh", f"Không xóa được channels.txt:\n{e}\n\n"
                                 "Hãy đóng file này ở các chương trình khác rồi thử lại.", parent=self)
            return False
        self.engine.log(f"[KHỞI ĐỘNG] Đã xóa {len(chans)} kênh khỏi channels.txt, không tải.")
        return True

    # ---------------- build ----------------
    def _build(self, aria2c: Path | None) -> None:
        pad = {"padx": 6, "pady": 4}

        # --- thanh trên ---
        top = ttk.Frame(self)
        top.pack(fill="x", **pad)
        self.start_btn = ttk.Button(top, text="▶ Start", command=self.on_start)
        self.start_btn.pack(side="left", padx=(0, 6))
        ttk.Button(top, text="■ Dừng tất cả", command=self.on_stop).pack(side="left")
        ttk.Button(top, text="Mở thư mục tải",
                   command=lambda: (self.engine.outdir().mkdir(parents=True, exist_ok=True),
                                    open_path(self.engine.outdir()))).pack(side="left", padx=6)
        ttk.Label(top, textvariable=self.stat_var).pack(side="right")

        # --- notebook ---
        nb = ttk.Notebook(self)
        nb.pack(fill="x", **pad)
        self.nb = nb

        self._tab_channels(nb)
        self._tab_exclusions(nb)
        self._tab_settings(nb, aria2c)

        # --- tiến trình tải ---
        pw = ttk.PanedWindow(self, orient="vertical")
        pw.pack(fill="both", expand=True, **pad)

        f1 = ttk.LabelFrame(pw, text="Tiến trình tải")
        cols = ("src", "title", "status", "pct", "speed")
        self.rows = ttk.Treeview(f1, columns=cols, show="headings", height=8)
        for c, t, w in (("src", "Nguồn", 150), ("title", "Tiêu đề", 520),
                        ("status", "Trạng thái", 110), ("pct", "%", 60), ("speed", "Tốc độ", 90)):
            self.rows.heading(c, text=t)
            self.rows.column(c, width=w, anchor="w" if c in ("src", "title") else "center",
                             stretch=(c == "title"))
        sb = ttk.Scrollbar(f1, orient="vertical", command=self.rows.yview)
        self.rows.configure(yscrollcommand=sb.set)
        self.rows.bind("<ButtonRelease-1>", self.on_row_click)
        self.rows.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        pw.add(f1, weight=3)

        f2 = ttk.LabelFrame(pw, text="Nhật ký")
        self.logbox = ScrolledText(f2, height=8, state="disabled", font=("Consolas", 9), wrap="none")
        self.logbox.pack(fill="both", expand=True)
        pw.add(f2, weight=2)

    def _tab_channels(self, nb: ttk.Notebook) -> None:
        tab = ttk.Frame(nb)
        nb.add(tab, text="Kênh")

        row = ttk.Frame(tab)
        row.pack(fill="x", padx=6, pady=4)
        self.chan_entry = ttk.Entry(row)
        self.chan_entry.pack(side="left", fill="x", expand=True)
        self.chan_entry.bind("<Return>", lambda e: self.on_add_channel())
        ttk.Button(row, text="Thêm kênh", command=self.on_add_channel).pack(side="left", padx=6)

        cols = ("channel", "status", "progress")
        self.chans = ttk.Treeview(tab, columns=cols, show="headings", height=5, selectmode="extended")
        for c, t, w in (("channel", "Kênh", 480), ("status", "Trạng thái", 130), ("progress", "Tiến độ", 100)):
            self.chans.heading(c, text=t)
            self.chans.column(c, width=w, anchor="w" if c == "channel" else "center",
                              stretch=(c == "channel"))
        self.chans.pack(fill="x", padx=6)

        row2 = ttk.Frame(tab)
        row2.pack(fill="x", padx=6, pady=4)
        ttk.Button(row2, text="Quét lại kênh chọn", command=self.on_rescan).pack(side="left")
        ttk.Button(row2, text="Xóa khỏi danh sách", command=self.on_remove).pack(side="left", padx=6)
        ttk.Button(row2, text="Mở channels.txt", command=lambda: open_path(CHANNELS_FILE)).pack(side="left")
        ttk.Checkbutton(row2, text="Theo dõi clipboard (link kênh → channels.txt)",
                        variable=self.clip_chan_var, command=self.on_toggle).pack(side="right")

    def _tab_exclusions(self, nb: ttk.Notebook) -> None:
        tab = ttk.Frame(nb)
        nb.add(tab, text="Loại trừ")

        row = ttk.Frame(tab)
        row.pack(fill="x", padx=6, pady=4)
        self.excl_entry = ttk.Entry(row)
        self.excl_entry.pack(side="left", fill="x", expand=True)
        self.excl_entry.bind("<Return>", lambda e: self.on_add_exclusion())
        ttk.Button(row, text="Thêm loại trừ", command=self.on_add_exclusion).pack(side="left", padx=6)

        self.excl_list = ttk.Treeview(tab, columns=("channel",), show="headings", height=5, selectmode="extended")
        self.excl_list.heading("channel", text="Kênh KHÔNG tải (exclusion_channels.txt)")
        self.excl_list.column("channel", width=700, anchor="w", stretch=True)
        self.excl_list.pack(fill="x", padx=6)

        row2 = ttk.Frame(tab)
        row2.pack(fill="x", padx=6, pady=4)
        ttk.Button(row2, text="Bỏ khỏi loại trừ", command=self.on_remove_exclusion).pack(side="left")
        ttk.Button(row2, text="Mở exclusion_channels.txt",
                   command=lambda: open_path(EXCLUSION_FILE)).pack(side="left", padx=6)
        ttk.Label(row2, text="Nhập link kênh, @handle hoặc channel ID").pack(side="right")

    def _tab_settings(self, nb: ttk.Notebook, aria2c: Path | None) -> None:
        tab = ttk.Frame(nb)
        nb.add(tab, text="Cài đặt")

        self.vars: dict[str, tk.Variable] = {}
        specs = [
            ("videos_per_channel", "Nhóm video xét / kênh", 1, 500),
            ("top_videos", "Số video phải tải đủ / kênh", 1, 500),
            ("min_views", "Video phải trên (lượt xem)", 0, 2_000_000_000),
            ("max_scan", "Quét tối đa (video mới nhất)", 1, 500),
            ("popular_scan", "Quét video phổ biến (để bù)", 1, 500),
            ("min_minutes", "Độ dài tối thiểu (phút)", 0, 600),
            ("max_minutes", "Độ dài tối đa (phút)", 1, 600),
            ("max_age_days", "Chỉ video đăng trong (ngày, 0=tắt)", 0, 36500),
            ("workers", "Video tải song song", 1, 16),
            ("scan_workers", "Kênh quét song song", 1, 16),
        ]

        grid = ttk.Frame(tab)
        grid.pack(fill="x", padx=6, pady=6)

        for i, (k, label, lo, hi) in enumerate(specs):
            r, c = divmod(i, 2)
            ttk.Label(grid, text=label).grid(row=r, column=c * 2, sticky="w", padx=(0, 6), pady=3)
            v = tk.StringVar(value=str(self.cfg[k]))
            self.vars[k] = v
            big = (k == "min_views")
            ttk.Spinbox(grid, from_=lo, to=hi, width=11 if big else 7,
                        increment=50000 if big else 1, textvariable=v).grid(
                row=r, column=c * 2 + 1, sticky="w", padx=(0, 24))

        ttk.Label(grid, text="Độ phân giải tối đa").grid(row=5, column=0, sticky="w", pady=3)
        hv = tk.StringVar(value=str(self.cfg["max_height"]))
        self.vars["max_height"] = hv
        ttk.Combobox(grid, textvariable=hv, width=7, state="readonly",
                     values=["360", "480", "720", "1080", "1440", "2160"]).grid(row=5, column=1, sticky="w")

        av = tk.BooleanVar(value=self.cfg["use_aria2c"])
        self.vars["use_aria2c"] = av
        ttk.Checkbutton(grid, variable=av,
                        text=f"Dùng aria2c ({'có' if aria2c else 'không tìm thấy'})",
                        state="normal" if aria2c else "disabled").grid(row=5, column=2, columnspan=2, sticky="w")

        orow = ttk.Frame(tab)
        orow.pack(fill="x", padx=6, pady=4)
        ttk.Label(orow, text="Thư mục lưu").pack(side="left")
        ov = tk.StringVar(value=self.cfg["output"])
        self.vars["output"] = ov
        ttk.Entry(orow, textvariable=ov).pack(side="left", fill="x", expand=True, padx=6)
        ttk.Button(orow, text="Chọn...", command=self.on_browse).pack(side="left")

        brow = ttk.Frame(tab)
        brow.pack(fill="x", padx=6, pady=4)
        ttk.Button(brow, text="Lưu cài đặt", command=self.on_save_settings).pack(side="left")
        ttk.Label(brow, text="Áp dụng ngay cho chương trình đang chạy").pack(side="left", padx=10)

    # ---------------- handlers ----------------
    def on_start(self) -> None:
        if self.started:
            return
        if self.engine.busy():
            messagebox.showinfo("Start", "Các tác vụ vừa dừng vẫn đang kết thúc, đợi vài giây rồi bấm lại.",
                                parent=self)
            return
        self.start_btn.configure(state="disabled")
        if self.first_start:
            if self.ask_resume_channels():         # lần đầu: hỏi có tải các kênh đang có trong channels.txt không
                try:                               # đã xóa kênh cũ -> clipboard hiện tại coi như đã xử lý
                    self.last_clip = self.clipboard_get()
                except tk.TclError:
                    self.last_clip = ""
            self.first_start = False
            self.engine.log("[START] Bắt đầu theo dõi clipboard và channels.txt.")
        else:
            n = self.engine.prepare_resume()       # sau khi Dừng: tiếp tục phần dở, không hỏi lại
            self.engine.log(f"[TIẾP TỤC] Tiếp tục {n} kênh đang dở (file tải dở sẽ được tải tiếp); "
                            f"kênh đã xong không tải lại.")
        self.started = True
        self.start_btn.configure(text="▶ Đang chạy")
        self.check_clipboard_now()                 # lấy luôn link kênh đang nằm trong clipboard
        self.engine.sync_channels_file()
        self.engine.carry.clear()                  # kênh đã bị gỡ khỏi channels.txt thì không tiếp tục

    def handle_clip_text(self, text: str, to_excl: bool = False) -> None:
        """Clipboard có một hoặc NHIỀU link kênh YouTube (danh sách) -> thêm hết vào channels.txt (hoặc loại trừ)."""
        if not text or len(text) > 200_000:
            return
        links = extract_channel_links(text)
        if not links:
            return
        added = existed = 0
        for link in links:
            if to_excl:                            # đang mở tab "Loại trừ" -> KHÔNG tải, đưa vào loại trừ
                self._exclude_url(link)
            elif add_channel_to_file(CHANNELS_FILE, link):
                added += 1
                self.engine.log(f"[CLIPBOARD] Đã thêm kênh: {link}")
            else:
                existed += 1
                if len(links) == 1:
                    self.engine.log(f"[CLIPBOARD] Kênh đã có: {link}")
        if not to_excl and len(links) > 1:
            self.engine.log(f"[CLIPBOARD] Danh sách {len(links)} link kênh: thêm mới {added}, đã có {existed}.")

    def check_clipboard_now(self) -> None:
        """Gọi khi bấm Start/Tiếp tục: xử lý clipboard hiện tại (nếu chưa xử lý và đang bật theo dõi clipboard)."""
        if not self.clip_chan_var.get():
            return
        try:
            text = self.clipboard_get()
        except tk.TclError:
            text = ""
        if text and text != self.last_clip:
            self.last_clip = text
            self.handle_clip_text(text)

    def on_stop(self) -> None:
        self.engine.stop_all()
        self.started = False                       # quay về trạng thái chờ Start (ngừng theo dõi clipboard / channels.txt)
        self.start_btn.configure(state="normal", text="▶ Start" if self.first_start else "▶ Tiếp tục")

    def on_toggle(self) -> None:
        self.cfg["watch_clipboard"] = self.clip_chan_var.get()
        save_cfg(self.cfg)

    def on_add_channel(self) -> None:
        raw = self.chan_entry.get().strip()
        if not raw:
            return
        if CHANNEL_RE.match(raw):
            add_channel_to_file(CHANNELS_FILE, raw)
        else:
            # @handle / UCxxxx / URL khác
            if not (raw.startswith("@") or re.fullmatch(r"UC[\w-]{22}", raw)):
                messagebox.showwarning("Thêm kênh", "Hãy nhập link kênh, @handle hoặc channel ID.")
                return
            add_channel_to_file(CHANNELS_FILE, channel_url(raw).removesuffix("/videos"))
        self.chan_entry.delete(0, "end")
        if self.started:
            self.engine.sync_channels_file()
        else:
            self.engine.log("[KÊNH] Đã lưu vào channels.txt, sẽ tải khi bấm Start.")

    def on_add_exclusion(self) -> None:
        raw = self.excl_entry.get().strip()
        if not raw:
            return
        if CHANNEL_RE.match(raw):
            stored = raw.split("?")[0].split("#")[0].rstrip("/")
        elif raw.startswith("@") or re.fullmatch(r"UC[\w-]{22}", raw):
            stored = channel_url(raw).removesuffix("/videos")
        else:
            messagebox.showwarning("Loại trừ", "Hãy nhập link kênh, @handle hoặc channel ID.")
            return
        self._exclude_url(stored)
        self.excl_entry.delete(0, "end")

    def _exclude_url(self, stored: str) -> None:
        """Đưa kênh vào exclusion_channels.txt, gỡ khỏi channels.txt và dừng tải thêm nếu đang chạy."""
        if add_exclusion_to_file(EXCLUSION_FILE, stored):
            remove_channel_by_token(CHANNELS_FILE, exclusion_token(stored))
            self.engine.log(f"[LOẠI TRỪ] Đã thêm: {stored}")
        else:
            self.engine.log(f"[LOẠI TRỪ] Kênh đã có trong danh sách: {stored}")
        self.refresh_exclusions()
        self.engine.apply_exclusions()

    def on_remove_exclusion(self) -> None:
        for iid in self.excl_list.selection():
            vals = self.excl_list.item(iid, "values")
            if vals:
                remove_exclusion_from_file(EXCLUSION_FILE, str(vals[0]))
        self.refresh_exclusions()
        self.engine.apply_exclusions()

    def refresh_exclusions(self) -> None:
        """Đồng bộ bảng loại trừ với exclusion_channels.txt (cũng bắt được khi sửa file bằng tay)."""
        lines = parse_channels_file(EXCLUSION_FILE)
        if lines == self._excl_cache:
            return
        self._excl_cache = lines
        self.excl_list.delete(*self.excl_list.get_children())
        for ln in lines:
            self.excl_list.insert("", "end", values=(ln,))

    def on_row_click(self, event) -> None:
        """Bấm vào một dòng trong bảng tiến trình -> copy link video đó vào clipboard."""
        rid = self.rows.identify_row(event.y)
        url = self.row_urls.get(rid) if rid else None
        if not url:
            return
        self.clipboard_clear()
        self.clipboard_append(url)
        self.last_clip = url                       # để bộ theo dõi clipboard không xử lý lại
        self.engine.log(f"[COPY] Đã copy link video: {url}")

    def on_rescan(self) -> None:
        for key in self.chans.selection():
            st = self.engine.channels.get(key)
            if not st:
                continue
            if not self.engine.start_channel(st.url, force=True):
                self.engine.log(f"[KÊNH] {st.tag} đang chạy hoặc nằm trong danh sách loại trừ, chưa thể quét lại.")

    def on_remove(self) -> None:
        for key in self.chans.selection():
            remove_channel_from_file(CHANNELS_FILE, key)
            self.engine.forget_channel(key)
            self.chans.delete(key)

    def on_browse(self) -> None:
        d = filedialog.askdirectory(initialdir=str(self.engine.outdir()))
        if d:
            self.vars["output"].set(d)

    def on_save_settings(self) -> None:
        try:
            new = {k: int(self.vars[k].get()) for k in
                   ("videos_per_channel", "top_videos", "min_views", "max_scan", "popular_scan", "min_minutes", "max_minutes", "max_age_days",
                    "workers", "scan_workers", "max_height")}
        except ValueError:
            messagebox.showerror("Cài đặt", "Các giá trị số không hợp lệ.")
            return
        if any(v < 0 for v in new.values()) or new["min_minutes"] >= new["max_minutes"]:
            messagebox.showerror("Cài đặt", "Độ dài tối thiểu phải nhỏ hơn tối đa, các số phải dương.")
            return
        new["output"] = self.vars["output"].get().strip() or "downloads"
        new["use_aria2c"] = bool(self.vars["use_aria2c"].get())
        self.cfg.update(new)
        save_cfg(self.cfg)
        self.engine.apply_settings()
        self.engine.log("[CÀI ĐẶT] Đã lưu và áp dụng ngay (kênh đang quét dở dùng giá trị cũ cho lần quét hiện tại).")

    def on_close(self) -> None:
        if self.engine.busy() and not messagebox.askyesno(
                "Thoát", "Đang có tác vụ chạy. Thoát và dừng tất cả?\n(File đang tải dở sẽ tiếp tục ở lần sau)"):
            return
        self.engine.gen += 1
        save_cfg(self.cfg)
        os._exit(0)

    # ---------------- vòng lặp ----------------
    def pump(self) -> None:
        try:
            for _ in range(300):
                kind, p = self.q.get_nowait()
                if kind == "log":
                    self.append_log(p)
                elif kind == "row":
                    self.update_row(p)
                elif kind == "channel":
                    self.update_channel(p)
        except queue.Empty:
            pass

        s = self.engine.stats
        self.stat_var.set(f"Thành công: {s['ok']}   Lỗi: {s['fail']}")
        self.after(100, self.pump)

    def watch(self) -> None:
        # 1) clipboard (chỉ link kênh)
        try:
            text = self.clipboard_get()
        except tk.TclError:
            text = ""

        if text != self.last_clip:
            to_excl = self.nb.tab(self.nb.select(), "text") == "Loại trừ"
            if to_excl or self.started:
                # chưa Start thì KHÔNG đánh dấu đã xử lý -> bấm Start sẽ lấy được clipboard này
                self.last_clip = text
                if to_excl or self.clip_chan_var.get():
                    self.handle_clip_text(text, to_excl)

        # 2) exclusion_channels.txt (cập nhật bảng + áp dụng cho kênh đang chạy)
        self.refresh_exclusions()
        self.engine.apply_exclusions()

        # 3) channels.txt (chỉ sau khi bấm Start)
        if self.started:
            self.engine.sync_channels_file()
        self.after(WATCH_INTERVAL_MS, self.watch)

    # ---------------- cập nhật UI ----------------
    def append_log(self, msg: str) -> None:
        self.logbox.configure(state="normal")
        self.logbox.insert("end", msg + "\n")
        if int(self.logbox.index("end-1c").split(".")[0]) > 2000:
            self.logbox.delete("1.0", "500.0")
        self.logbox.see("end")
        self.logbox.configure(state="disabled")

    def update_row(self, p: dict) -> None:
        rid = p["id"]
        if p.get("url"):
            self.row_urls[rid] = p["url"]
        if not self.rows.exists(rid):
            self.rows.insert("", 0, iid=rid, values=("", "", "", "", ""))
        for col in ("src", "title", "status", "speed"):
            if col in p:
                self.rows.set(rid, col, p[col])
        if "pct" in p:
            v = p["pct"]
            self.rows.set(rid, "pct", "" if v is None else f"{v:.0f}%")

    def update_channel(self, p: dict) -> None:
        key = p["key"]
        vals = (p["tag"], p["status"], f"{p['done']}/{p['quota']}")
        if self.chans.exists(key):
            self.chans.item(key, values=vals)
        else:
            self.chans.insert("", "end", iid=key, values=vals)


# ==========================================================
# MAIN
# ==========================================================

def main() -> None:
    os.chdir(SCRIPT_DIR)
    cfg = load_cfg()
    YDL_COMMON.update(build_common_opts(cfg))

    ffmpeg = find_tool(FFMPEG_PATH, "ffmpeg")
    if not ffmpeg:
        r = tk.Tk()
        r.withdraw()
        messagebox.showerror("Thiếu FFmpeg",
                             f"Không tìm thấy ffmpeg.\nĐã kiểm tra: {FFMPEG_PATH}\n"
                             f"và: {BUNDLE_DIR / 'ffmpeg.exe'}\n"
                             f"Hãy đặt ffmpeg.exe cùng thư mục với file exe/Python:\n{SCRIPT_DIR}")
        sys.exit(1)

    aria2c = find_tool(ARIA2C_LOCAL_PATH, "aria2c")
    extra = [str(SCRIPT_DIR), str(ffmpeg.parent)]
    if aria2c:
        extra.append(str(aria2c.parent))
    os.environ["PATH"] = os.pathsep.join(extra + [os.environ.get("PATH", "")])

    ui_q: queue.Queue = queue.Queue()
    engine = Engine(cfg, ffmpeg, aria2c, ui_q)
    app = App(cfg, engine, ui_q, aria2c)
    engine.log(f"Danh sách đã tải: {TITLES_FILE} ({len(engine.seen_ids)} mã video)")
    engine.log(f"FFmpeg: {ffmpeg}")
    engine.log(f"aria2c: {aria2c if aria2c else 'không tìm thấy -> dùng native downloader'}")
    if "js_runtimes" in YDL_COMMON:
        engine.log("JS runtime: " + ", ".join(f"{k} ({v['path']})" for k, v in YDL_COMMON["js_runtimes"].items()))
    else:
        engine.log("CẢNH BÁO: không thấy deno/node -> YouTube có thể bóp tốc độ hoặc thiếu định dạng. "
                   "Cài: winget install DenoLand.Deno (hoặc đặt deno.exe cạnh file .py)")
    if "cookiefile" in YDL_COMMON:
        engine.log(f"Cookies: {YDL_COMMON['cookiefile']}")
    elif "cookiesfrombrowser" in YDL_COMMON:
        engine.log(f"Cookies từ trình duyệt: {YDL_COMMON['cookiesfrombrowser'][0]}")
    else:
        engine.log("Cookies: không dùng (đặt cookies.txt cạnh file .py nếu bị lỗi 'not a bot')")
    app.mainloop()


if __name__ == "__main__":
    main()
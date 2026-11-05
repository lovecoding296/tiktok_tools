#!/usr/bin/env python3
"""
ytlink_gui.py  -  YouTube Link Downloader (GUI)

Chức năng:
- Dán link video / playlist (mỗi dòng một link) -> bấm "Tải"
- Nút "Chọn thư mục" để chọn nơi lưu; thư mục được lưu lại và dùng tiếp cho các lần tải sau
- Nút "Tải" tải thẳng vào thư mục đã chọn (nếu chưa có thư mục thì sẽ hỏi chọn)
- Không áp dụng bộ lọc độ dài
- Tùy chọn: tự dán link video vào ô khi copy vào clipboard
- Dùng chung ffmpeg.exe, aria2c.exe, cookies.txt/cookies.json, downloaded_titles.txt
  với ytdownload_gui.py (cùng thư mục). downloaded_titles.txt ghi dạng 'id|tiêu đề', mỗi video 1 lần;
  file này chỉ GHI, không dùng để chặn tải (đã có trong danh sách vẫn tải)
- Cài đặt lưu vào ytlink_gui.json
- Tối ưu tốc độ kiểu IDM: aria2c 16 kết nối/file + JS runtime (deno/node) để giải mã
  tham số 'n' của YouTube (không có thì YouTube bóp tốc độ từng kết nối)

Cài yt-dlp:   pip install -U yt-dlp
Chạy:         python ytlink_gui.py
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
import tempfile
import threading
import time
import tkinter as tk
import uuid

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

from yt_dlp import YoutubeDL

try:
    import psutil                                     # pip install psutil (tùy chọn: hiện tốc độ mạng tổng)
except ImportError:
    psutil = None

try:
    from yt_dlp.utils import DownloadCancelled
except ImportError:                                   # yt-dlp cũ
    class DownloadCancelled(Exception):
        pass


# ==========================================================
# CẤU HÌNH
# ==========================================================

if getattr(sys, "frozen", False):
    # Chạy từ file exe (PyInstaller): dùng thư mục chứa file .exe
    SCRIPT_DIR = Path(sys.executable).resolve().parent
else:
    SCRIPT_DIR = Path(__file__).resolve().parent
FFMPEG_PATH = SCRIPT_DIR / "ffmpeg.exe"
ARIA2C_LOCAL_PATH = SCRIPT_DIR / "aria2c.exe"
CONFIG_PATH = SCRIPT_DIR / "ytlink_gui.json"
TITLES_FILE = SCRIPT_DIR / "downloaded_titles.txt"
COOKIES_FILE = SCRIPT_DIR / "cookies.txt"
COOKIES_JSON_FILE = SCRIPT_DIR / "cookies.json"
COOKIES_CONVERTED = SCRIPT_DIR / "cookies_netscape.txt"

DEFAULT_CFG = {
    "max_height": 1080,
    "workers": 4,               # video tải song song (áp dụng khi mở lại chương trình)
    "use_aria2c": True,
    "watch_clipboard": False,   # tự dán link video từ clipboard vào ô
    "last_dir": "",             # thư mục lưu lần trước
    "cookies_browser": "",      # "chrome" / "firefox" / "edge" / "brave" (bỏ trống nếu dùng cookies.txt)
}

CONCURRENT_FRAGMENTS = 16

# aria2c: 16 kết nối/file là mức tối đa aria2c cho phép (IDM cho tới 32, nhưng 16 đã đủ chạm băng thông)
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
DOWNLOAD_DELAY_S = 0
REQUEST_SLEEP_S = 0

YDL_COMMON: dict = {}

VIDEO_RE = re.compile(
    r"^https?://(?:(?:www\.|m\.|music\.)?youtube\.com/"
    r"(?:watch\?\S*v=|shorts/|live/|embed/|playlist\?)|youtu\.be/)\S+$",
    re.IGNORECASE,
)
URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
PLAYLIST_RE = re.compile(r"[?&]list=|/playlist", re.IGNORECASE)

titles_lock = threading.Lock()

_TITLE_LINE_RE = re.compile(r"^([A-Za-z0-9_-]{11})\|(.*)$")
_VIDEO_ID_RE = re.compile(r"(?:[?&]v=|youtu\.be/|/shorts/|/embed/|/live/)([A-Za-z0-9_-]{11})")


def _norm_title(t: str) -> str:
    return " ".join(t.split()).casefold()


def video_id_of(info: dict | None, url: str = "") -> str | None:
    """ID video 11 ký tự: lấy từ info của yt-dlp, không có thì rút từ link."""
    vid = (info or {}).get("id")
    if isinstance(vid, str) and re.fullmatch(r"[A-Za-z0-9_-]{11}", vid):
        return vid
    m = _VIDEO_ID_RE.search(url or "")
    return m.group(1) if m else None


def record_downloaded(items: list[tuple[str | None, str]]) -> int:
    """Ghi vào downloaded_titles.txt theo dạng 'id|tiêu đề' (ví dụ jghSs8653kA|10 SCARY DARKEST...).
    Mỗi video chỉ ghi 1 lần: đã có id đó trong file (hoặc dòng cũ chỉ có đúng tiêu đề đó) thì bỏ qua.
    CHỈ ghi nhận - không bao giờ dùng file này để chặn tải. Trả về số dòng đã ghi."""
    with titles_lock:
        ids: set[str] = set()
        old_titles: set[str] = set()
        if TITLES_FILE.exists():
            with TITLES_FILE.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    if m := _TITLE_LINE_RE.match(line):
                        ids.add(m.group(1))
                    else:
                        old_titles.add(_norm_title(line))

        new_lines: list[str] = []
        for vid, title in items:
            clean = " ".join((title or "").split())
            if not clean and not vid:
                continue
            if vid:
                if vid in ids or _norm_title(clean) in old_titles:
                    continue
                ids.add(vid)
                new_lines.append(f"{vid}|{clean}")
            else:
                key = _norm_title(clean)
                if key in old_titles:
                    continue
                old_titles.add(key)
                new_lines.append(clean)

        if new_lines:
            with TITLES_FILE.open("a", encoding="utf-8") as f:
                f.write("\n".join(new_lines) + "\n")
        return len(new_lines)


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


# ---------------- cookies ----------------

def cookies_to_netscape(src: Path) -> Path:
    """Cookie JSON (EditThisCookie, Cookie-Editor...) -> Netscape. Đã là Netscape thì giữ nguyên."""
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
    """Tìm deno/node/bun (cạnh file .py hoặc trong PATH) để yt-dlp giải challenge của YouTube."""
    found: dict = {}
    for name in ("deno", "node", "bun"):
        local = SCRIPT_DIR / f"{name}.exe"
        path = local if local.exists() else (Path(w) if (w := shutil.which(name)) else None)
        if path:
            found[name] = {"path": str(path)}
    return found


def build_common_opts(cfg: dict) -> dict:
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
    """YoutubeDL với bản sao cookies riêng (tránh nhiều luồng ghi đè file gốc)."""
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


# ---------------- tiện ích ----------------

def find_tool(local: Path, name: str) -> Path | None:
    if local.exists():
        return local
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


def purge_partials(fn: str) -> None:
    """Chỉ xóa file dở (.part/.ytdl/.aria2) và luồng trung gian (.fNNN) - KHÔNG đụng file hoàn chỉnh.
    Dùng cho playlist để không xóa nhầm các video đã tải xong."""
    names = [fn + ".part", fn + ".ytdl", fn + ".aria2", fn + ".part.aria2"]
    if re.search(r"\.f\d+\.[A-Za-z0-9]{2,4}$", fn):
        names.append(fn)
    for n in names:
        try:
            Path(n).unlink()
        except OSError:
            pass


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


# ==========================================================
# ENGINE
# ==========================================================

class Engine:
    def __init__(self, cfg: dict, ffmpeg: Path, aria2c: Path | None, ui_q: queue.Queue) -> None:
        self.cfg = cfg
        self.ffmpeg = ffmpeg
        self.aria2c = aria2c
        self.q = ui_q

        self.lock = threading.Lock()
        self.stats = {"ok": 0, "fail": 0}
        self.pending = 0
        self.gen = 0
        self.row_seq = 0
        self.pool = ThreadPoolExecutor(max_workers=max(1, int(cfg["workers"])))

    # ---- UI events ----
    def log(self, msg: str) -> None:
        self.q.put(("log", msg))

    def new_row(self, source: str, title: str) -> str:
        with self.lock:
            self.row_seq += 1
            rid = f"r{self.row_seq}"
        self.q.put(("row", dict(id=rid, src=source, title=title,
                                status="Chờ", pct=0, speed="")))
        return rid

    def row(self, rid: str, **kw) -> None:
        self.q.put(("row", dict(id=rid, **kw)))

    # ---- yt-dlp ----
    def make_opts(self, outdir: Path) -> dict:
        h = int(self.cfg["max_height"])
        opts = {
            "outtmpl": str(outdir / "%(title)s.%(ext)s"),
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
        }

        if self.cfg["use_aria2c"] and self.aria2c:
            opts["external_downloader"] = {"default": "aria2c"}
            opts["external_downloader_args"] = {"aria2c": list(ARIA2_ARGS)}
        else:
            opts["http_chunk_size"] = 10 * 1024 * 1024
        # dùng cho video dạng fragment (HLS/DASH), vô hại với video thường
        opts["concurrent_fragment_downloads"] = CONCURRENT_FRAGMENTS

        return opts

    def add(self, url: str, outdir: Path) -> None:
        rid = self.new_row(outdir.name or str(outdir), url)
        gen = self.gen
        with self.lock:
            self.pending += 1
        if PLAYLIST_RE.search(url):
            self.pool.submit(self._expand_run, url, outdir, rid, gen)
        else:
            self.pool.submit(self._run, url, outdir, rid, gen)

    def expand_urls(self, url: str) -> list[str]:
        """Đọc nhanh danh sách video trong playlist (không tải)."""
        opts = {
            "quiet": True, "no_warnings": True, "skip_download": True,
            "extract_flat": "in_playlist", "noplaylist": False,
            "socket_timeout": 20, **YDL_COMMON,
        }
        with make_ydl(opts) as ydl:
            info = ydl.extract_info(url, download=False)
        urls: list[str] = []
        for e in (info or {}).get("entries") or []:
            if not e:
                continue
            u = e.get("url") or e.get("webpage_url") or ""
            if u and not u.startswith("http"):
                u = f"https://www.youtube.com/watch?v={e.get('id') or u}"
            if u:
                urls.append(u)
        return urls or [url]

    def _expand_run(self, url: str, outdir: Path, rid: str, gen: int) -> None:
        try:
            if gen != self.gen:
                self.row(rid, status="Đã hủy")
                return
            self.row(rid, status="Đọc playlist")
            try:
                urls = self.expand_urls(url)
            except Exception as e:
                self.log(f"[PLAYLIST] Không tách được, tải nguyên playlist: {e}")
                urls = [url]

            if len(urls) > 1:
                self.log(f"[PLAYLIST] {len(urls)} video -> tải song song")
                self.row(rid, status=f"{len(urls)} video", pct=100)
                for u in urls:
                    self.add(u, outdir)
                return

            ok = self.download_one(url, outdir, rid, gen)
            if ok is not None:
                with self.lock:
                    self.stats["ok" if ok else "fail"] += 1
        finally:
            with self.lock:
                self.pending -= 1

    def _run(self, url: str, outdir: Path, rid: str, gen: int) -> None:
        try:
            if gen != self.gen:
                self.row(rid, status="Đã hủy")
                return
            ok = self.download_one(url, outdir, rid, gen)
            if ok is not None:
                with self.lock:
                    self.stats["ok" if ok else "fail"] += 1
        finally:
            with self.lock:
                self.pending -= 1

    def download_one(self, url: str, outdir: Path, rid: str, gen: int) -> bool | None:
        """True = xong, False = lỗi, None = đã hủy."""
        st = {"title": url, "last": 0.0}
        touched: set[str] = set()          # mọi file yt-dlp đã đụng tới (để dọn khi lỗi)
        is_playlist = bool(re.search(r"[?&/]list=|/playlist", url))

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
            # video đơn: xóa sạch mọi file của video; playlist: chỉ xóa file dở/trung gian
            for fn in list(touched):
                if is_playlist:
                    purge_partials(fn)
                else:
                    purge_by_filename(fn)
            touched.clear()

        opts = self.make_opts(outdir)
        opts["progress_hooks"] = [hook]
        opts["noplaylist"] = not is_playlist
        opts["no_warnings"] = False
        opts["logger"] = YdlLogger(self.log)

        try:
            outdir.mkdir(parents=True, exist_ok=True)
            self.row(rid, status="Đang tải")
            self.log(f"[TẢI] {url}  ->  {outdir}")

            time.sleep(DOWNLOAD_DELAY_S)
            if gen != self.gen:
                raise DownloadCancelled("Đã dừng")

            ok, last_err, checked = False, None, []
            for attempt in (1, 2):
                try:
                    with make_ydl(opts) as ydl:
                        info = ydl.extract_info(url, download=True) or {}

                    items = [e for e in (info.get("entries") or []) if e] if "entries" in info else [info]
                    if not items:
                        raise RuntimeError("Không có video nào được tải")

                    bad, checked = [], []
                    for it in items:
                        self.log(f"  [FORMAT] {describe_formats(it)}")
                        path = final_path(it)
                        if path is None or not path.exists():
                            raise RuntimeError("Không tìm thấy file đầu ra sau khi tải/ghép")
                        self.row(rid, status="Kiểm tra audio...")
                        codec = probe_audio(self.ffmpeg, path)
                        if codec is None:
                            bad.append(path)
                        else:
                            checked.append((path, codec))

                    if bad:
                        for b in bad:
                            purge_outputs(b)
                        raise NoAudioError(f"{len(bad)} file không có audio: "
                                           + ", ".join(b.name for b in bad))
                    ok = True
                    break

                except DownloadCancelled:
                    raise
                except Exception as e:
                    last_err = e
                    cleanup()
                    if not is_playlist:
                        opts["overwrites"] = True
                    if attempt == 1:
                        self.log(f"  [LỖI lần 1 -> xóa file dở, tải lại] {url}: {e}")
                        self.row(rid, status="Tải lại...", pct=0, speed="")
                        if gen != self.gen:
                            raise DownloadCancelled("Đã dừng")

            if not ok:
                self.log(f"[BỎ] {url}: lỗi 2 lần, đã xóa file dở ({last_err})")
            else:
                for path, codec in checked:
                    if codec in ("opus", "vorbis"):
                        self.row(rid, status="Chuyển audio sang AAC...")
                        if not reencode_audio_aac(self.ffmpeg, path):
                            self.log(f"  [CẢNH BÁO] Không chuyển được {codec} -> AAC: {path.name}")

                final = st["title"]
                record_downloaded([
                    (video_id_of(it, it.get("webpage_url") or it.get("original_url") or url),
                     it.get("title") or final)
                    for it in items
                ])
                self.row(rid, status="Xong", pct=100, speed="", title=final)
                self.log(f"[XONG] {final}")
                return True

        except DownloadCancelled:
            self.row(rid, status="Đã hủy", speed="")
            self.log(f"[HỦY] {st['title']}")
            return None

        except Exception as e:
            cleanup()
            self.log(f"[LỖI] {url}: {e}")

        self.row(rid, status="Lỗi", speed="")
        return False

    def stop_all(self) -> None:
        self.gen += 1
        self.log("[DỪNG] Đang hủy các tác vụ... (video đang tải bằng aria2c sẽ dừng sau khi xong file hiện tại)")

    def busy(self) -> bool:
        with self.lock:
            return self.pending > 0


# ==========================================================
# GUI
# ==========================================================

class App(tk.Tk):
    def __init__(self, cfg: dict, engine: Engine, ui_q: queue.Queue, aria2c: Path | None) -> None:
        super().__init__()
        self.cfg = cfg
        self.engine = engine
        self.q = ui_q

        self.title("YouTube Link Downloader")
        self.geometry("1000x700")
        self.minsize(850, 550)

        self.clip_var = tk.BooleanVar(value=cfg["watch_clipboard"])
        self.height_var = tk.StringVar(value=str(cfg["max_height"]))
        self.aria_var = tk.BooleanVar(value=cfg["use_aria2c"])
        self.workers_var = tk.StringVar(value=str(cfg["workers"]))
        self.stat_var = tk.StringVar(value="")
        self.dir_var = tk.StringVar(value=self._dir_text())
        self._net_prev: tuple[float, int] | None = None
        self._net_text = ""

        self._build(aria2c)

        try:
            self.last_clip = self.clipboard_get()
        except tk.TclError:
            self.last_clip = ""

        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(100, self.pump)
        self.after(WATCH_INTERVAL_MS, self.watch)

    # ---------------- build ----------------
    def _build(self, aria2c: Path | None) -> None:
        pad = {"padx": 6, "pady": 4}

        top = ttk.Frame(self)
        top.pack(fill="x", **pad)
        ttk.Button(top, text="■ Dừng tất cả", command=self.on_stop).pack(side="left")
        ttk.Button(top, text="Mở thư mục đã chọn", command=self.on_open_last).pack(side="left", padx=6)
        ttk.Label(top, textvariable=self.stat_var).pack(side="right")

        box = ttk.LabelFrame(self, text="Link video / playlist (mỗi dòng một link)")
        box.pack(fill="x", **pad)
        self.link_text = tk.Text(box, height=6, wrap="none")
        self.link_text.pack(fill="x", padx=6, pady=4)

        row = ttk.Frame(box)
        row.pack(fill="x", padx=6, pady=4)
        ttk.Button(row, text="⇩ Tải", command=self.on_download).pack(side="left", padx=6)
        ttk.Button(row, text="📁 Chọn thư mục", command=self.on_choose_dir).pack(side="left")        
        ttk.Button(row, text="Dán từ clipboard", command=self.on_paste).pack(side="left")
        ttk.Button(row, text="Xóa", command=lambda: self.link_text.delete("1.0", "end")).pack(side="left", padx=6)
        ttk.Checkbutton(row, text="Tự dán link video khi copy vào clipboard",
                        variable=self.clip_var, command=self.on_toggle).pack(side="right")

        drow = ttk.Frame(box)
        drow.pack(fill="x", padx=6, pady=(0, 4))
        ttk.Label(drow, text="Lưu vào:").pack(side="left")
        ttk.Label(drow, textvariable=self.dir_var, foreground="#0a5").pack(side="left", padx=6)

        opt = ttk.LabelFrame(self, text="Tùy chọn")
        opt.pack(fill="x", **pad)
        orow = ttk.Frame(opt)
        orow.pack(fill="x", padx=6, pady=4)
        ttk.Label(orow, text="Độ phân giải tối đa").pack(side="left")
        ttk.Combobox(orow, textvariable=self.height_var, width=7, state="readonly",
                     values=["360", "480", "720", "1080", "1440", "2160"]).pack(side="left", padx=(6, 18))
        ttk.Label(orow, text="Tải song song *").pack(side="left")
        ttk.Spinbox(orow, from_=1, to=16, width=5, textvariable=self.workers_var).pack(side="left", padx=(6, 18))
        ttk.Checkbutton(orow, variable=self.aria_var,
                        text=f"Dùng aria2c ({'có' if aria2c else 'không tìm thấy'})",
                        state="normal" if aria2c else "disabled").pack(side="left")
        ttk.Label(orow, text="* áp dụng khi mở lại chương trình").pack(side="right")

        pw = ttk.PanedWindow(self, orient="vertical")
        pw.pack(fill="both", expand=True, **pad)

        f1 = ttk.LabelFrame(pw, text="Tiến trình tải")
        cols = ("src", "title", "status", "pct", "speed")
        self.rows = ttk.Treeview(f1, columns=cols, show="headings", height=8)
        for c, t, w in (("src", "Thư mục", 150), ("title", "Tiêu đề / Link", 500),
                        ("status", "Trạng thái", 110), ("pct", "%", 60), ("speed", "Tốc độ", 90)):
            self.rows.heading(c, text=t)
            self.rows.column(c, width=w, anchor="w" if c in ("src", "title") else "center",
                             stretch=(c == "title"))
        sb = ttk.Scrollbar(f1, orient="vertical", command=self.rows.yview)
        self.rows.configure(yscrollcommand=sb.set)
        self.rows.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        pw.add(f1, weight=3)

        f2 = ttk.LabelFrame(pw, text="Nhật ký")
        self.logbox = ScrolledText(f2, height=8, state="disabled", font=("Consolas", 9), wrap="none")
        self.logbox.pack(fill="both", expand=True)
        pw.add(f2, weight=2)

    # ---------------- handlers ----------------
    def apply_options(self) -> None:
        try:
            self.cfg["max_height"] = int(self.height_var.get())
            self.cfg["workers"] = max(1, int(self.workers_var.get()))
        except ValueError:
            pass
        self.cfg["use_aria2c"] = bool(self.aria_var.get())
        self.cfg["watch_clipboard"] = self.clip_var.get()
        save_cfg(self.cfg)

    def on_toggle(self) -> None:
        self.apply_options()

    def on_stop(self) -> None:
        self.engine.stop_all()

    def on_open_last(self) -> None:
        d = self.cfg.get("last_dir")
        if d and Path(d).exists():
            open_path(Path(d))
        else:
            messagebox.showinfo("Thư mục", "Chưa chọn thư mục nào.")

    def on_paste(self) -> None:
        try:
            self.link_text.insert("end", self.clipboard_get().strip() + "\n")
        except tk.TclError:
            pass

    def _dir_text(self) -> str:
        d = self.cfg.get("last_dir")
        return d if d else "(chưa chọn thư mục)"

    def on_choose_dir(self) -> bool:
        """Chọn thư mục lưu, lưu vào cấu hình. Trả về True nếu đã chọn."""
        init = self.cfg.get("last_dir") or str(SCRIPT_DIR)
        if not Path(init).exists():
            init = str(SCRIPT_DIR)
        folder = filedialog.askdirectory(title="Chọn thư mục lưu video", initialdir=init, mustexist=False)
        if not folder:
            return False
        self.cfg["last_dir"] = folder
        self.dir_var.set(self._dir_text())
        self.apply_options()
        return True

    def on_download(self) -> None:
        urls = [u.strip().rstrip(",;)") for u in URL_RE.findall(self.link_text.get("1.0", "end"))]
        if not urls:
            messagebox.showinfo("Tải bằng link", "Không tìm thấy link hợp lệ (bắt đầu bằng http/https).")
            return

        folder = self.cfg.get("last_dir")
        if not folder:
            # chưa có thư mục -> hỏi chọn; hủy thì không tải, giữ nguyên link
            if not self.on_choose_dir():
                return
            folder = self.cfg["last_dir"]

        outdir = Path(folder)
        try:
            outdir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            messagebox.showerror("Thư mục", f"Không tạo/truy cập được thư mục:\n{outdir}\n{e}")
            return
        self.apply_options()
        for u in urls:
            self.engine.add(u, outdir)
        self.engine.log(f"[LINK] {len(urls)} link -> {outdir}")
        self.link_text.delete("1.0", "end")

    def on_close(self) -> None:
        if self.engine.busy() and not messagebox.askyesno(
                "Thoát", "Đang có tác vụ chạy. Thoát và dừng tất cả?\n(File đang tải dở sẽ tiếp tục ở lần sau)"):
            return
        self.engine.gen += 1
        self.apply_options()
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
        except queue.Empty:
            pass

        s = self.engine.stats
        self.stat_var.set(f"{self.net_speed()}   Thành công: {s['ok']}   Lỗi: {s['fail']}")
        self.after(100, self.pump)

    def net_speed(self) -> str:
        """Tốc độ tải tổng của cả máy (aria2c không báo tiến độ từng file, nên xem ở đây)."""
        if psutil is None:
            return ""
        now = time.time()
        recv = psutil.net_io_counters().bytes_recv
        if self._net_prev is None:
            self._net_prev = (now, recv)
        elif now - self._net_prev[0] >= 1.0:
            t0, b0 = self._net_prev
            self._net_text = "Mạng: " + (fmt_speed((recv - b0) / (now - t0)) or "0 KB/s")
            self._net_prev = (now, recv)
        return self._net_text

    def watch(self) -> None:
        if self.clip_var.get():
            try:
                text = self.clipboard_get()
            except tk.TclError:
                text = ""
            if text != self.last_clip:
                self.last_clip = text
                if text and len(text) <= 5000:
                    for line in text.splitlines():
                        line = line.strip()
                        if VIDEO_RE.match(line):
                            self.link_text.insert("end", line + "\n")
                            self.engine.log(f"[CLIPBOARD] Đã dán link: {line}")
        else:
            try:
                self.last_clip = self.clipboard_get()
            except tk.TclError:
                pass
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
        if not self.rows.exists(rid):
            self.rows.insert("", 0, iid=rid, values=("", "", "", "", ""))
        for col in ("src", "title", "status", "speed"):
            if col in p:
                self.rows.set(rid, col, p[col])
        if "pct" in p:
            v = p["pct"]
            self.rows.set(rid, "pct", "" if v is None else f"{v:.0f}%")


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
                             "Hãy đặt ffmpeg.exe cùng thư mục với file Python.")
        sys.exit(1)

    aria2c = find_tool(ARIA2C_LOCAL_PATH, "aria2c")
    extra = [str(SCRIPT_DIR)]
    if aria2c:
        extra.append(str(aria2c.parent))
    os.environ["PATH"] = os.pathsep.join(extra + [os.environ.get("PATH", "")])

    ui_q: queue.Queue = queue.Queue()
    engine = Engine(cfg, ffmpeg, aria2c, ui_q)
    app = App(cfg, engine, ui_q, aria2c)
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
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
YouTube Channel Monitor
- Tab Main: số kênh đang theo dõi, bảng số video mới theo kênh, nhật ký
- Tab Setting: thời gian quét, bot token, chat id, thêm kênh, danh sách kênh, chạy cùng Windows
- Mở chương trình là quét ngay; thêm kênh mới cũng quét ngay
- Mỗi kênh có file du_lieu/<tên kênh>.txt, mỗi dòng:  #id|tên video|thời gian đăng
- Có video mới -> hiện thông báo + gửi Telegram sau mỗi lượt quét

- Bấm X: ẩn xuống khay hệ thống (tray icon) và chạy ngầm; chuột phải tray icon -> Exit để thoát hẳn

Cài đặt:  pip install yt-dlp pystray pillow
"""

import html
import json
import os
import queue
import re
import socket
import sys
import threading
import time
import tkinter as tk
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from tkinter import messagebox, scrolledtext, ttk

try:
    import yt_dlp
except ImportError:
    yt_dlp = None

try:
    import winreg  # chỉ có trên Windows
except ImportError:
    winreg = None

try:
    import pystray
    from PIL import Image, ImageDraw
except ImportError:
    pystray = None

# ----------------------------------------------------------------- cấu hình
# Bản build --noconsole: stdout/stderr = None, tránh yt-dlp lỗi khi ghi log
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")

DEFAULT_INTERVAL_MIN = 5
MAX_NEW_PER_CHANNEL = 5     # tối đa số video mới ghi nhận / kênh / lượt
LOCK_PORT = 47653           # chặn mở 2 bản cùng lúc
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "YouTubeChannelMonitor"

if getattr(sys, "frozen", False):  # chạy bằng file .exe (PyInstaller)
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LIST_FILE = os.path.join(BASE_DIR, "kenh_theo_doi.txt")
DATA_DIR = os.path.join(BASE_DIR, "du_lieu")
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
CACHE_FILE = os.path.join(BASE_DIR, "cache_kenh.json")
UA = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}


# ------------------------------------------------------------------ tiện ích
def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def read_lines(path):
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return [l.strip() for l in f if l.strip()]


def write_lines(path, lines):
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + ("\n" if lines else ""))


def safe_name(name):
    return re.sub(r'[\\/:*?"<>|]', "_", name).strip(" .") or "kenh"


def fmt_duration(sec):
    if not sec:
        return "N/A"
    sec = int(sec)
    h, r = divmod(sec, 3600)
    m, s = divmod(r, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def parse_ts(s):
    try:
        return datetime.fromisoformat((s or "").strip().replace("Z", "+00:00"))
    except Exception:
        return None


def read_record(path):
    """Đọc file của kênh -> (tập id đã biết, mốc thời gian đăng lớn nhất đã ghi).
    Dòng có dạng  #id|tên video|thời gian đăng  (file cũ chỉ có #id|tên video vẫn đọc được)."""
    known, last = set(), None
    for line in read_lines(path):
        if not line.startswith("#"):
            continue
        body = line[1:]
        known.add(body.split("|", 1)[0])
        ts = parse_ts(body.rsplit("|", 1)[-1]) if body.count("|") >= 2 else None
        if ts and (last is None or ts > last):
            last = ts
    return known, last


def normalize_url(s):
    """Chuẩn hoá về dạng https://www.youtube.com/@abc | /channel/UC... | /c/... | /user/..."""
    s = s.strip()
    if not s:
        return None
    if re.fullmatch(r"UC[\w-]{22}", s):
        return f"https://www.youtube.com/channel/{s}"
    if s.startswith("@"):
        s = "https://www.youtube.com/" + s
    if not s.lower().startswith("http"):
        s = "https://" + s
    p = urllib.parse.urlparse(s)
    if "youtube.com" not in p.netloc.lower():
        return None
    path = re.sub(
        r"/(videos|shorts|streams|featured|playlists|about|community)/?$",
        "",
        p.path.rstrip("/"),
    )
    if not re.match(r"^/(@|channel/|c/|user/)", path):
        return None
    return "https://www.youtube.com" + path


# ------------------------------------------------- chạy cùng Windows / khóa
def autostart_command():
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --minimized'
    exe = sys.executable
    pyw = os.path.join(os.path.dirname(exe), "pythonw.exe")
    if os.path.exists(pyw):
        exe = pyw
    return f'"{exe}" "{os.path.abspath(__file__)}" --minimized'


def get_autostart():
    if winreg is None:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, RUN_NAME)
            return True
    except OSError:
        return False


def set_autostart(enable):
    if winreg is None:
        raise RuntimeError("Chỉ hỗ trợ trên Windows")
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if enable:
            winreg.SetValueEx(k, RUN_NAME, 0, winreg.REG_SZ, autostart_command())
        else:
            try:
                winreg.DeleteValue(k, RUN_NAME)
            except FileNotFoundError:
                pass


def acquire_lock():
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", LOCK_PORT))
        s.listen(1)
        return s
    except OSError:
        return None


# ------------------------------------------------------------------- YouTube
def resolve_channel(url):
    """Trả về (channel_id, tên kênh). Kênh thiếu tab Videos vẫn không lỗi."""
    opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": True,
        "playlist_items": "1",
        "skip_download": True,
    }
    base = url.rstrip("/")
    last_err = None

    for tab in ("/videos", "/shorts", "/streams", ""):
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(base + tab, download=False)
        except Exception as e:
            last_err = e
            continue
        cid = info.get("channel_id") or ""
        if not cid.startswith("UC"):
            for e in info.get("entries") or []:
                if ((e or {}).get("channel_id") or "").startswith("UC"):
                    cid = e["channel_id"]
                    break
        name = info.get("channel") or info.get("uploader") or info.get("title")
        if cid.startswith("UC") and name:
            return cid, name

    try:
        req = urllib.request.Request(base, headers=UA)
        with urllib.request.urlopen(req, timeout=20) as r:
            page = r.read().decode("utf-8", "ignore")
        m = (
            re.search(r'<link rel="canonical" href="https://www\.youtube\.com/channel/(UC[\w-]{22})"', page)
            or re.search(r'"externalId":"(UC[\w-]{22})"', page)
            or re.search(r'"channelId":"(UC[\w-]{22})"', page)
        )
        n = re.search(r'<meta property="og:title" content="([^"]+)"', page)
        if m and n:
            return m.group(1), html.unescape(n.group(1))
    except Exception as e:
        last_err = e

    raise RuntimeError(f"Không lấy được channel id / tên kênh ({last_err})")


class FeedNotFound(RuntimeError):
    """RSS của kênh trả HTTP 404."""


class FeedUnavailable(RuntimeError):
    """RSS lỗi phía server YouTube (HTTP 5xx / 429) hoặc lỗi mạng sau khi thử lại."""


def fetch_feed(cid):
    """Danh sách video mới nhất (gồm cả Short) từ RSS: [(id, title, published)], mới nhất đứng đầu."""
    ns = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015"}
    url = f"https://www.youtube.com/feeds/videos.xml?channel_id={cid}"
    last_err = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=20) as r:
                root = ET.fromstring(r.read())
            items = []
            for e in root.findall("a:entry", ns):
                vid = e.findtext("yt:videoId", namespaces=ns)
                title = (e.findtext("a:title", namespaces=ns) or "").replace("\n", " ").strip()
                pub = (e.findtext("a:published", namespaces=ns) or "").strip()
                if vid:
                    items.append((vid, title, pub))
            return items
        except Exception as ex:
            last_err = ex
            time.sleep(2 * (attempt + 1))
    if isinstance(last_err, urllib.error.HTTPError):
        if last_err.code == 404:
            raise FeedNotFound(f"Lỗi đọc RSS: {last_err}")
        if last_err.code >= 500 or last_err.code == 429:
            raise FeedUnavailable(f"Lỗi đọc RSS: {last_err}")
    raise RuntimeError(f"Lỗi đọc RSS: {last_err}")


_DUR_CACHE = {}  # id video -> thời lượng (đã lấy khi kiểm tra ở fallback, đỡ gọi lại)


def get_video_meta(vid):
    """Qua yt-dlp: (thời lượng, thời gian đăng, chỉ_có_ngày). Lỗi -> (None, None, False)."""
    try:
        opts = {"quiet": True, "no_warnings": True, "skip_download": True}
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(
                f"https://www.youtube.com/watch?v={vid}", download=False, process=False
            )
    except Exception:
        return None, None, False
    dur = info.get("duration")
    if dur:
        _DUR_CACHE[vid] = dur
    ts = info.get("timestamp") or info.get("release_timestamp")
    if ts:
        return dur, datetime.fromtimestamp(ts, timezone.utc), False
    ud = str(info.get("upload_date") or info.get("release_date") or "")
    if len(ud) == 8:
        try:
            return dur, datetime.strptime(ud, "%Y%m%d").replace(tzinfo=timezone.utc), True
        except ValueError:
            pass
    return dur, None, False


def fetch_feed_ytdlp(url, known, last_ts):
    """Dự phòng khi RSS lỗi. Mỗi tab (Videos/Shorts/Streams) liệt kê mới -> cũ; đi từ trên xuống,
    dừng khi gặp video đã biết hoặc video có thời gian đăng <= mốc đã ghi.
    Chỉ trả về video MỚI: [(id, title, published_iso)], mới nhất đứng đầu."""
    opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": True,
        "playlist_items": "1-8",
        "skip_download": True,
    }
    base = url.rstrip("/")
    cands = {}  # id -> (title, ts)
    ok_tabs = 0
    for tab in ("/videos", "/shorts", "/streams"):
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(base + tab, download=False)
        except Exception:
            continue
        ok_tabs += 1
        for e in (info or {}).get("entries") or []:
            vid = (e or {}).get("id") or ""
            if len(vid) != 11:
                continue
            if vid in known:
                break  # phần còn lại của tab cũ hơn video đã biết
            if vid in cands:
                continue
            _, ts, date_only = get_video_meta(vid)
            if ts is not None and last_ts is not None:
                # chỉ có ngày đăng -> coi như cuối ngày, tránh bỏ sót video mới đăng cùng ngày
                if (ts + timedelta(days=1) if date_only else ts) <= last_ts:
                    break
            title = (e.get("title") or vid).replace("\n", " ").strip()
            cands[vid] = (title, ts)
            if not known:
                break  # lần đầu chỉ cần video mới nhất của tab
    if ok_tabs == 0:
        raise RuntimeError("yt-dlp không đọc được tab nào của kênh")
    floor = datetime.min.replace(tzinfo=timezone.utc)
    order = sorted(cands.items(), key=lambda kv: kv[1][1] or floor, reverse=True)
    return [(vid, t, ts.isoformat() if ts else "") for vid, (t, ts) in order]


def get_duration(vid):
    if vid in _DUR_CACHE:
        return _DUR_CACHE[vid]
    return get_video_meta(vid)[0]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def is_short(vid, duration):
    """/shorts/ID trả 200 nếu là Short, redirect sang /watch nếu là video thường."""
    try:
        opener = urllib.request.build_opener(_NoRedirect)
        req = urllib.request.Request(
            f"https://www.youtube.com/shorts/{vid}", headers=UA, method="HEAD"
        )
        with opener.open(req, timeout=15) as r:
            return r.status == 200
    except urllib.error.HTTPError as e:
        if e.code in (301, 302, 303, 307, 308):
            return False
    except Exception:
        pass
    return bool(duration and duration <= 180)


# ------------------------------------------------------------------ Telegram
def send_telegram(token, chat_id, text):
    try:
        data = urllib.parse.urlencode(
            {
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": "true",
            }
        ).encode()
        req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data)
        with urllib.request.urlopen(req, timeout=20):
            pass
        return True, ""
    except urllib.error.HTTPError as e:
        try:
            return False, e.read().decode("utf-8", "ignore")
        except Exception:
            return False, str(e)
    except Exception as e:
        return False, str(e)


def build_messages(results):
    esc = lambda s: html.escape(str(s), quote=False)
    by_channel = {}
    for r in results:
        by_channel.setdefault(r["channel"], []).append(r)

    blocks = []
    for ch, vs in by_channel.items():
        lines = [f"📺 <b>{esc(ch)}</b> — {len(vs)} video mới"]
        for v in vs:
            kind = "Short" if v["short"] else "Video"
            link = (
                f"https://www.youtube.com/shorts/{v['id']}"
                if v["short"]
                else f"https://www.youtube.com/watch?v={v['id']}"
            )
            lines.append(
                f"🎬 {esc(v['title'])}\n⏱ {fmt_duration(v['duration'])}  |  🏷 {kind}\n🔗 {link}"
            )
        blocks.append("\n\n".join(lines))

    cur = f"🔔 <b>Có {len(results)} video mới</b>"
    chunks = []
    for b in blocks:
        if len(cur) + len(b) + 2 > 3800:
            chunks.append(cur)
            cur = b
        else:
            cur += "\n\n" + b
    chunks.append(cur)
    return chunks


# ----------------------------------------------------------------------- GUI
class App:
    def __init__(self, root, minimized=False):
        self.root = root
        self.minimized = minimized
        root.title("YouTube Channel Monitor")
        root.geometry("940x760")
        root.minsize(780, 620)

        self.q = queue.Queue()
        self.cfg = {
            "token": "",
            "chat_id": "",
            "popup": True,
            "interval_min": DEFAULT_INTERVAL_MIN,
        }
        self.cfg.update(load_json(CONFIG_FILE, {}))
        self.cache = load_json(CACHE_FILE, {})
        self.new_counts = {}      # url -> số video mới từ lúc mở chương trình
        self.channels = []
        self.thread = None
        self.stop_evt = threading.Event()
        self.wake_evt = threading.Event()
        self.tray = None
        self.phase = "wait"       # wait | scan | idle
        self.last_end = time.time()
        self.next_ts = time.time()

        self.build_ui()
        self.reload_channels()

        # Lần đầu mở: tự bật chạy cùng Windows (có thể tắt trong tab Setting)
        if winreg is not None and not self.cfg.get("autostart_init"):
            try:
                set_autostart(True)
            except Exception as e:
                self.log(f"Không bật được chạy cùng Windows: {e}", "err")
            self.cfg["autostart_init"] = True
            save_json(CONFIG_FILE, self.cfg)
        self.var_autostart.set(get_autostart())

        root.after(200, self.poll)
        root.after(1000, self.tick)
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        if minimized:
            self.hide_to_tray()

        # Mở chương trình là chạy + quét luôn
        if yt_dlp is None:
            self.var_status.set("Thiếu thư viện yt-dlp")
            self.log("Thiếu thư viện yt-dlp. Chạy:  pip install yt-dlp  rồi mở lại.", "err")
            root.after(300, lambda: messagebox.showerror("Thiếu thư viện", "Chạy:  pip install yt-dlp"))
        else:
            self.thread = threading.Thread(target=self.loop, daemon=True)
            self.thread.start()

    # ---------------------------------------------------------------- UI
    def build_ui(self):
        nb = ttk.Notebook(self.root)
        nb.pack(fill="both", expand=True, padx=6, pady=6)
        tab_main = ttk.Frame(nb)
        tab_set = ttk.Frame(nb)
        nb.add(tab_main, text="  Main  ")
        nb.add(tab_set, text="  Setting  ")
        self.build_main(tab_main)
        self.build_setting(tab_set)

    def build_main(self, tab):
        top = ttk.Frame(tab)
        top.pack(fill="x", padx=8, pady=10)
        self.var_count = tk.StringVar(value="Đang theo dõi: 0 kênh")
        ttk.Label(top, textvariable=self.var_count, font=("TkDefaultFont", 14, "bold")).pack(
            side="left"
        )
        ttk.Button(top, text="⟳ Quét ngay", command=self.scan_now).pack(side="right")
        self.var_status = tk.StringVar(value="")
        ttk.Label(top, textvariable=self.var_status).pack(side="right", padx=12)

        fs = ttk.LabelFrame(tab, text="Số video mới theo kênh (tính từ lúc mở chương trình)")
        fs.pack(fill="x", padx=8, pady=4)
        tf = ttk.Frame(fs)
        tf.pack(fill="x", padx=6, pady=6)
        sb = ttk.Scrollbar(tf)
        sb.pack(side="right", fill="y")
        self.tree = ttk.Treeview(
            tf, columns=("kenh", "moi"), show="headings", height=8, yscrollcommand=sb.set
        )
        self.tree.heading("kenh", text="Tên kênh")
        self.tree.heading("moi", text="Số video mới")
        self.tree.column("kenh", width=600, anchor="w")
        self.tree.column("moi", width=120, anchor="center")
        self.tree.tag_configure("hot", foreground="#0a7d2c")
        self.tree.pack(side="left", fill="x", expand=True)
        sb.config(command=self.tree.yview)

        fg = ttk.LabelFrame(tab, text="Nhật ký")
        fg.pack(fill="both", expand=True, padx=8, pady=(4, 8))
        self.log_box = scrolledtext.ScrolledText(fg, height=14, state="disabled", wrap="word")
        self.log_box.pack(fill="both", expand=True, padx=6, pady=6)
        self.log_box.tag_config("new", foreground="#0a7d2c", font=("TkDefaultFont", 10, "bold"))
        self.log_box.tag_config("err", foreground="#c62828")

    def build_setting(self, tab):
        pad = {"padx": 8, "pady": 4}

        f0 = ttk.LabelFrame(tab, text="Chung")
        f0.pack(fill="x", **pad)
        ttk.Label(f0, text="Thời gian quét (phút):").grid(row=0, column=0, sticky="w", padx=6, pady=4)
        self.var_interval = tk.StringVar(value=str(self.cfg.get("interval_min", DEFAULT_INTERVAL_MIN)))
        ttk.Spinbox(f0, from_=1, to=1440, width=6, textvariable=self.var_interval).grid(
            row=0, column=1, sticky="w", padx=6
        )
        self.var_autostart = tk.BooleanVar(value=False)
        cb = ttk.Checkbutton(
            f0,
            text="Chạy cùng Windows",
            variable=self.var_autostart,
            command=self.on_autostart,
        )
        cb.grid(row=1, column=0, columnspan=2, sticky="w", padx=6, pady=2)
        if winreg is None:
            cb.state(["disabled"])
        self.var_popup = tk.BooleanVar(value=self.cfg.get("popup", True))
        ttk.Checkbutton(f0, text="Hiện popup khi có video mới", variable=self.var_popup).grid(
            row=2, column=0, columnspan=2, sticky="w", padx=6, pady=2
        )

        f1 = ttk.LabelFrame(tab, text="Telegram")
        f1.pack(fill="x", **pad)
        self.var_token = tk.StringVar(value=self.cfg.get("token", ""))
        self.var_chat = tk.StringVar(value=self.cfg.get("chat_id", ""))
        ttk.Label(f1, text="Bot token:").grid(row=0, column=0, sticky="w", padx=6, pady=3)
        ttk.Entry(f1, textvariable=self.var_token, show="*").grid(
            row=0, column=1, sticky="we", padx=6
        )
        ttk.Label(f1, text="Chat ID:").grid(row=1, column=0, sticky="w", padx=6, pady=3)
        ttk.Entry(f1, textvariable=self.var_chat).grid(row=1, column=1, sticky="we", padx=6)
        bf = ttk.Frame(f1)
        bf.grid(row=0, column=2, rowspan=2, padx=6)
        ttk.Button(bf, text="Gửi thử", command=self.test_telegram).pack(fill="x", pady=1)
        f1.columnconfigure(1, weight=1)

        bar = ttk.Frame(tab)
        bar.pack(fill="x", **pad)
        ttk.Button(bar, text="💾 Lưu cài đặt", command=self.save_cfg).pack(side="left")

        mid = ttk.Frame(tab)
        mid.pack(fill="both", expand=True, **pad)

        fl = ttk.LabelFrame(mid, text="Thêm kênh (mỗi dòng 1 link / @handle)")
        fl.pack(side="left", fill="both", expand=True, padx=(0, 4))
        self.txt_add = tk.Text(fl, height=9, width=40, wrap="none")
        self.txt_add.pack(fill="both", expand=True, padx=6, pady=6)
        ttk.Button(fl, text="➕ Thêm và theo dõi ngay", command=self.add_channels).pack(
            fill="x", padx=6, pady=(0, 6)
        )

        fr = ttk.LabelFrame(mid, text="Kênh đang theo dõi")
        fr.pack(side="left", fill="both", expand=True, padx=(4, 0))
        lf = ttk.Frame(fr)
        lf.pack(fill="both", expand=True, padx=6, pady=6)
        sb = ttk.Scrollbar(lf)
        sb.pack(side="right", fill="y")
        self.listbox = tk.Listbox(lf, selectmode="extended", height=9, yscrollcommand=sb.set)
        self.listbox.pack(side="left", fill="both", expand=True)
        sb.config(command=self.listbox.yview)
        ttk.Button(fr, text="🗑 Xóa kênh đã chọn", command=self.remove_selected).pack(
            fill="x", padx=6, pady=(0, 6)
        )

    # ------------------------------------------------------------ helpers
    def log(self, msg, tag=None):
        self.q.put(("log", f"[{datetime.now():%H:%M:%S}] {msg}\n", tag))

    def _append(self, text, tag):
        b = self.log_box
        b.config(state="normal")
        b.insert("end", text, tag or "")
        if int(b.index("end-1c").split(".")[0]) > 2000:
            b.delete("1.0", "500.0")
        b.see("end")
        b.config(state="disabled")

    def poll(self):
        try:
            while True:
                kind, a, b = self.q.get_nowait()
                if kind == "log":
                    self._append(a, b)
                elif kind == "refresh":
                    self.reload_channels()
                elif kind == "show":
                    self.show_window()
                elif kind == "scan":
                    self.scan_now()
                elif kind == "quit":
                    self.quit_app()
                    return
                elif kind == "popup":
                    self.root.bell()
                    if self.var_popup.get():
                        if self.tray is not None and self.root.state() == "withdrawn":
                            try:  # đang ẩn dưới tray -> báo bằng thông báo của Windows
                                self.tray.notify(a.replace("\n\n", "\n")[:250], "Video mới")
                            except Exception:
                                pass
                        else:
                            self.root.after(50, lambda t=a: messagebox.showinfo("Video mới", t))
        except queue.Empty:
            pass
        self.root.after(200, self.poll)

    def tick(self):
        if yt_dlp is not None:
            if self.phase == "scan":
                s = "Đang quét..."
            elif self.phase == "wait":
                s = "Đang chờ mạng sẵn sàng..."
            else:
                left = max(0, int(self.next_ts - time.time()))
                s = f"Quét tiếp sau {left // 60:02d}:{left % 60:02d}"
            self.var_status.set(s)
        self.root.after(1000, self.tick)

    def display_name(self, url):
        return (self.cache.get(url) or {}).get("name") or url

    def save_cfg(self, quiet=False):
        try:
            iv = int(self.var_interval.get())
            if not 1 <= iv <= 1440:
                raise ValueError
        except ValueError:
            messagebox.showwarning("Sai giá trị", "Thời gian quét phải là số phút từ 1 đến 1440.")
            return False
        cfg = dict(self.cfg)
        cfg.update(
            token=self.var_token.get().strip(),
            chat_id=self.var_chat.get().strip(),
            popup=bool(self.var_popup.get()),
            interval_min=iv,
        )
        self.cfg = cfg
        save_json(CONFIG_FILE, cfg)
        if not quiet:
            self.log(f"Đã lưu cài đặt (quét mỗi {iv} phút).")
        return True

    def on_autostart(self):
        want = self.var_autostart.get()
        try:
            set_autostart(want)
            self.log("Đã bật chạy cùng Windows." if want else "Đã tắt chạy cùng Windows.")
        except Exception as e:
            self.var_autostart.set(not want)
            messagebox.showerror("Lỗi", str(e))

    def test_telegram(self):
        if not self.save_cfg(quiet=True):
            return
        token, chat = self.cfg["token"], self.cfg["chat_id"]
        if not token or not chat:
            messagebox.showwarning("Thiếu thông tin", "Nhập Bot token và Chat ID trước.")
            return

        def work():
            ok, err = send_telegram(token, chat, "✅ Kết nối Telegram thành công.")
            self.log(
                "Gửi thử Telegram: OK" if ok else f"Gửi thử Telegram lỗi: {err}",
                None if ok else "err",
            )

        threading.Thread(target=work, daemon=True).start()

    # ------------------------------------------------------ danh sách kênh
    def reload_channels(self):
        self.channels = read_lines(LIST_FILE)

        sel = self.listbox.curselection()
        self.listbox.delete(0, "end")
        for u in self.channels:
            name = (self.cache.get(u) or {}).get("name")
            self.listbox.insert("end", f"{name}  —  {u}" if name else u)
        if sel and max(sel) < len(self.channels):
            for i in sel:
                self.listbox.selection_set(i)

        self.tree.delete(*self.tree.get_children())
        for u in self.channels:
            n = self.new_counts.get(u, 0)
            if n > 0:  # chỉ hiện kênh có video mới
                self.tree.insert("", "end", values=(self.display_name(u), n), tags=("hot",))
        self.var_count.set(f"Đang theo dõi: {len(self.channels)} kênh")

    def add_channels(self):
        raw = self.txt_add.get("1.0", "end")
        tokens = [t for t in re.split(r"[\s,;]+", raw) if t]
        if not tokens:
            return
        current = read_lines(LIST_FILE)
        lower = {c.lower() for c in current}
        added, bad = 0, []
        for t in tokens:
            u = normalize_url(t)
            if not u:
                bad.append(t)
                continue
            if u.lower() in lower:
                continue
            current.append(u)
            lower.add(u.lower())
            added += 1
        write_lines(LIST_FILE, current)
        self.txt_add.delete("1.0", "end")
        self.reload_channels()
        self.log(f"Đã thêm {added} kênh, quét ngay.")
        for b in bad:
            self.log(f"Link không hợp lệ (bỏ qua): {b}", "err")
        if added:
            self.wake_evt.set()  # theo dõi + quét luôn

    def remove_selected(self):
        idx = set(self.listbox.curselection())
        if not idx:
            return
        remain = [c for i, c in enumerate(self.channels) if i not in idx]
        write_lines(LIST_FILE, remain)
        self.listbox.selection_clear(0, "end")
        self.reload_channels()
        self.log(f"Đã xóa {len(idx)} kênh.")

    def scan_now(self):
        if self.thread and self.thread.is_alive():
            self.wake_evt.set()

    # ------------------------------------------------------------- tray icon
    def make_tray_image(self):
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.rounded_rectangle((2, 12, 62, 52), radius=12, fill=(220, 30, 30, 255))
        d.polygon([(26, 22), (26, 42), (44, 32)], fill=(255, 255, 255, 255))
        return img

    def start_tray(self):
        if pystray is None:
            return False
        if self.tray is not None:
            return True
        try:
            put = lambda k: (lambda icon, item: self.q.put((k, None, None)))
            menu = pystray.Menu(
                pystray.MenuItem("Mở cửa sổ", put("show"), default=True),
                pystray.MenuItem("Quét ngay", put("scan")),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Exit", put("quit")),
            )
            self.tray = pystray.Icon(
                "YouTubeChannelMonitor", self.make_tray_image(), "YouTube Channel Monitor", menu
            )
            threading.Thread(target=self.tray.run, daemon=True).start()
            return True
        except Exception as e:
            self.tray = None
            self.log(f"Không tạo được tray icon: {e}", "err")
            return False

    def hide_to_tray(self):
        if self.start_tray():
            self.root.withdraw()
        else:
            self.log("Thiếu pystray/pillow (pip install pystray pillow) -> chỉ thu nhỏ xuống taskbar.", "err")
            self.root.iconify()

    def show_window(self):
        self.root.deiconify()
        self.root.state("normal")
        self.root.lift()
        self.root.focus_force()

    def on_close(self):
        self.hide_to_tray()  # bấm X: chạy ngầm, không thoát

    def quit_app(self):
        self.stop_evt.set()
        if self.tray is not None:
            try:
                self.tray.visible = False
                self.tray.stop()
            except Exception:
                pass
        self.root.destroy()

    # --------------------------------------------------------- luồng quét
    def loop(self):
        self.log("▶ Bắt đầu theo dõi.")
        if self.minimized:
            self.phase = "wait"
            self.log("Khởi động cùng Windows: chờ 20 giây cho mạng sẵn sàng...")
            self.stop_evt.wait(20)
        while not self.stop_evt.is_set():
            self.wake_evt.clear()
            try:
                self.run_cycle()
            except Exception as e:
                self.log(f"[Lỗi] Lượt quét: {e}", "err")
            self.phase = "idle"
            self.last_end = time.time()
            while not self.stop_evt.is_set() and not self.wake_evt.is_set():
                self.next_ts = self.last_end + int(self.cfg.get("interval_min", DEFAULT_INTERVAL_MIN)) * 60
                if time.time() >= self.next_ts:
                    break
                time.sleep(0.5)

    def resolve(self, url, force=False):
        c = self.cache.get(url)
        if not force and c and c.get("id") and c.get("name"):
            return c["id"], c["name"]
        cid, name = resolve_channel(url)
        self.cache[url] = {"id": cid, "name": name}
        save_json(CACHE_FILE, self.cache)
        self.q.put(("refresh", None, None))
        return cid, name

    def feed_fallback(self, url, old_cid, name, known, last_ts):
        """RSS 404: thử resolve lại channel id (cache có thể sai), không được thì dùng yt-dlp.
        Trả về (items, đã_lọc_bằng_yt-dlp)."""
        try:
            cid, _ = self.resolve(url, force=True)
            if cid != old_cid:
                self.log(f"{name}: channel id đổi {old_cid} → {cid}, thử lại RSS.")
                return fetch_feed(cid), False
        except FeedNotFound:
            pass
        except Exception as e:
            self.log(f"{name}: resolve lại lỗi: {e}", "err")
        self.log(f"{name}: RSS 404, chuyển sang lấy bằng yt-dlp.")
        return fetch_feed_ytdlp(url, known, last_ts), True

    def scan_channel(self, url):
        """Quét 1 kênh, trả về danh sách video mới (dict)."""
        cid, name = self.resolve(url)
        os.makedirs(DATA_DIR, exist_ok=True)
        path = os.path.join(DATA_DIR, safe_name(name) + ".txt")

        known, last_ts = read_record(path)
        via_ytdlp = False  # True: items đã được lọc "chỉ video mới" bên trong fetch_feed_ytdlp
        try:
            items = fetch_feed(cid)  # [(id, title, published)], mới nhất đứng đầu
        except FeedNotFound:
            items, via_ytdlp = self.feed_fallback(url, cid, name, known, last_ts)
        except FeedUnavailable as e:
            self.log(f"{name}: {e} -> chuyển sang lấy bằng yt-dlp.")
            items, via_ytdlp = fetch_feed_ytdlp(url, known, last_ts), True
        if not items:
            self.log(f"{name}: " + ("không có video mới." if via_ytdlp else "kênh chưa có video."))
            return []

        # Lần đầu: tạo file, chỉ ghi video mới nhất, không báo
        if not known:
            vid, title, pub = items[0]
            with open(path, "a", encoding="utf-8") as f:
                f.write(f"#{vid}|{title}|{pub}\n")
            self.log(f"{name}: tạo file lần đầu, ghi video mới nhất → {title}")
            return []

        # File cũ chưa có thời gian: lấy mốc từ các video đã biết còn trên feed
        for vid, title, pub in items:
            ts = parse_ts(pub)
            if vid in known and ts and (last_ts is None or ts > last_ts):
                last_ts = ts

        new = []
        for vid, title, pub in items:
            if vid in known:
                continue
            ts = parse_ts(pub)
            if not via_ytdlp and last_ts and ts and ts <= last_ts:
                continue  # cũ hơn video mới nhất đã ghi (vd: video mới nhất bị xóa)
            new.append((vid, title, pub, ts))
        if not new:
            self.log(f"{name}: không có video mới.")
            return []

        new.reverse()  # cũ -> mới (giữ đúng thứ tự khi không có thời gian đăng)
        new.sort(key=lambda x: x[3] or datetime.min.replace(tzinfo=timezone.utc))
        new = new[-MAX_NEW_PER_CHANNEL:]  # ghi từ cũ đến mới
        results = []
        for vid, title, pub, _ in new:
            with open(path, "a", encoding="utf-8") as f:
                f.write(f"#{vid}|{title}|{pub}\n")
            dur = get_duration(vid)
            results.append(
                {
                    "channel": name,
                    "id": vid,
                    "title": title,
                    "duration": dur,
                    "short": is_short(vid, dur),
                }
            )
            self.log(
                f"🆕 {name}: {title} [{'Short' if results[-1]['short'] else 'Video'}, "
                f"{fmt_duration(dur)}]",
                "new",
            )
        return results

    def run_cycle(self):
        self.phase = "scan"
        channels = read_lines(LIST_FILE)
        self.log(f"=== Bắt đầu lượt quét {len(channels)} kênh ===")
        all_new = []
        for url in channels:
            if self.stop_evt.is_set():
                break
            try:
                res = self.scan_channel(url)
                if res:
                    all_new.extend(res)
                    self.new_counts[url] = self.new_counts.get(url, 0) + len(res)
                    self.q.put(("refresh", None, None))
            except Exception as e:
                self.log(f"[Lỗi] {url}: {e}", "err")
            time.sleep(1)

        self.log(f"=== Xong lượt quét. Video mới: {len(all_new)} ===")
        if all_new:
            lines = [
                f"{r['channel']} — {r['title']} ({'Short' if r['short'] else 'Video'}, {fmt_duration(r['duration'])})"
                for r in all_new[:10]
            ]
            if len(all_new) > 10:
                lines.append(f"... và {len(all_new) - 10} video khác")
            self.q.put(("popup", f"Có {len(all_new)} video mới:\n\n" + "\n".join(lines), None))

            token, chat = self.cfg.get("token", ""), self.cfg.get("chat_id", "")
            if not token or not chat:
                self.log("Chưa cấu hình Telegram (tab Setting), bỏ qua gửi tin.", "err")
            else:
                for chunk in build_messages(all_new):
                    ok, err = send_telegram(token, chat, chunk)
                    self.log(
                        "Đã gửi Telegram." if ok else f"Gửi Telegram lỗi: {err}",
                        None if ok else "err",
                    )
                    time.sleep(0.5)
        self.phase = "idle"


def main():
    lock = acquire_lock()
    root = tk.Tk()
    if lock is None:
        root.withdraw()
        messagebox.showinfo("YouTube Channel Monitor", "Chương trình đang chạy rồi.")
        return
    App(root, minimized="--minimized" in sys.argv)
    root.mainloop()
    lock.close()


if __name__ == "__main__":
    main()

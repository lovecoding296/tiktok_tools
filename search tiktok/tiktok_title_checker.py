import os
import re
import json
import time
import socket
import itertools
import threading
import subprocess
import unicodedata
import urllib.request
import urllib.error
import tkinter as tk
from datetime import datetime, timezone
from tkinter import filedialog, messagebox
from urllib.parse import quote
from pathlib import Path

import customtkinter as ctk
from playwright.sync_api import sync_playwright
import yt_dlp

# Kéo-thả file (tùy chọn): pip install tkinterdnd2
try:
    from tkinterdnd2 import TkinterDnD, DND_FILES
    HAS_DND = True
except Exception:  # chưa cài -> vẫn dùng được nút chọn file
    HAS_DND = False

if HAS_DND:
    class _Base(ctk.CTk, TkinterDnD.DnDWrapper):
        pass
else:
    _Base = ctk.CTk

ctk.set_appearance_mode("light")
ctk.set_default_color_theme("blue")

CDP_PORT = 9222
CDP_URL = f"http://127.0.0.1:{CDP_PORT}"
BASE_DIR = Path(__file__).resolve().parent
CHROME_PROFILE = str(BASE_DIR / "chrome_tiktok_profile")
CHROME_PATHS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    "/usr/bin/google-chrome",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
]

LINE_RE = re.compile(r"^#([\w-]{11})\|(.*)$")

STATE_FILE = BASE_DIR / "tiktok_checker_state.json"   # nhớ các thư mục đang hiển thị ở bảng
STATE_NAMES = {"video.txt", "short.txt", "error.txt"}
COLOR_ROW = {"": "#ffffff", "ok": "#c6efce", "bad": "#f4c7c3"}

CH_TABS = {"videos": "Videos", "shorts": "Shorts", "streams": "Live"}
CH_SORTS = ["Lượt xem giảm dần", "Lượt xem tăng dần", "Mới nhất trước"]

# Màu nút theo chức năng: (màu nền, màu khi rê chuột)
PALETTE = {
    "green":  ("#2e9e4f", "#278a43"),   # bắt đầu / lưu / đúng
    "red":    ("#d64545", "#b93a3a"),   # dừng / sai / xóa
    "blue":   ("#2f7bd9", "#2868b8"),   # tìm kiếm / copy
    "gray":   ("#6c7a89", "#5a6673"),   # mở file / sắp xếp
    "purple": ("#7b57d1", "#6847b3"),   # metadata
    "orange": ("#e08a1e", "#c47616"),   # mở Chrome
    "pink":   ("#d6336c", "#b82a5b"),   # short
}


def make_btn(parent, text, command, color="blue", width=100, height=30, **kw):
    fg, hv = PALETTE[color]
    return ctk.CTkButton(parent, text=text, command=command, width=width, height=height,
                         corner_radius=15, fg_color=fg, hover_color=hv,
                         font=("Arial", 12, "bold"), **kw)


EN_WORDS = set("""the and of to in is you your how what why this that with for on are was not
but have has from they will would can could should about into more most best top new just
only when where which who why all any one our out up make made get got use using top
""".split())
OTHER_WORDS = set("""el la los las y de del que en un una por para con es se lo como mas pero
sus al esta este eso muy sin sobre entre cuando todo todos
o os um uma não nao que dos das do da em para com por mais mas como seu sua
le les des du et est un une pour dans qui sur pas avec plus ce cette
der die das und ist nicht ein eine mit von zu den dem auf für fur sich auch wie
yang dan di ke dari untuk dengan ini itu pada adalah tidak akan
il lo gli di che per con una sono non anche come
""".split())


# ---------- Tiện ích dòng "#id|tên" ----------
def parse_line(line: str):
    """Trả về (video_id | None, title)."""
    line = line.strip()
    m = LINE_RE.match(line)
    if m:
        return m.group(1), m.group(2).strip()
    return None, line


def make_line(vid, title: str) -> str:
    return f"#{vid}|{title}" if vid else title


def item_key(vid, title: str) -> str:
    return vid if vid else "t:" + title.lower()


def _enc(s: str) -> str:
    return s.replace("%", "%25").replace(";", "%3B").replace("=", "%3D").replace("\t", " ")


def _dec(s: str) -> str:
    return s.replace("%3D", "=").replace("%3B", ";").replace("%25", "%")


def make_file_line(vid, title: str, meta: dict) -> str:
    """#id|tên<TAB>t=short;v=123;d=20240101;s=ok;c=tên kênh  (phần sau TAB là thông tin kèm theo)."""
    parts = []
    if meta.get("type") in ("video", "short"):
        parts.append(f"t={meta['type']}")
    if isinstance(meta.get("views"), int):
        parts.append(f"v={meta['views']}")
    if meta.get("date"):
        parts.append(f"d={meta['date']}")
    if meta.get("status") in ("ok", "bad"):
        parts.append(f"s={meta['status']}")
    if meta.get("src"):
        parts.append("c=" + _enc(meta["src"]))
    base = make_line(vid, " ".join(title.split()))
    return base + ("\t" + ";".join(parts) if parts else "")


def parse_file_line(line: str):
    """Trả về (vid, title, meta) hoặc None. Đọc được cả định dạng cũ '#id|tên'."""
    line = line.rstrip("\r\n")
    if not line.strip():
        return None
    left, _, right = line.partition("\t")
    vid, title = parse_line(left)
    meta = {}
    for part in right.split(";"):
        k, sep, v = part.partition("=")
        if not sep:
            continue
        k, v = k.strip(), v.strip()
        if k == "t" and v in ("video", "short"):
            meta["type"] = v
        elif k == "v" and v.isdigit():
            meta["views"] = int(v)
        elif k == "d" and re.fullmatch(r"\d{8}", v):
            meta["date"] = v
        elif k == "s" and v in ("ok", "bad"):
            meta["status"] = v
        elif k == "c" and v:
            meta["src"] = _dec(v)
    return vid, title, meta


def read_entries(path: Path) -> list:
    try:
        text = path.read_text(encoding="utf-8-sig", errors="ignore")
    except OSError:
        return []
    out = []
    for ln in text.split("\n"):
        r = parse_file_line(ln)
        if r and r[1]:
            out.append(r)
    return out


def write_lines(path: Path, lines: list):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    os.replace(tmp, path)


def has_state_files(d: Path) -> bool:
    return any((d / n).exists() for n in STATE_NAMES)


def find_state_dirs(p: Path) -> list:
    """p là thư mục kênh (có video.txt/short.txt/error.txt) hoặc thư mục cha chứa nhiều thư mục kênh."""
    if has_state_files(p):
        return [p]
    try:
        return [s for s in sorted(p.iterdir()) if s.is_dir() and has_state_files(s)]
    except OSError:
        return []


def fmt_views(v) -> str:
    return f"{v:,}" if isinstance(v, int) else ""


def fmt_date(d) -> str:
    return f"{d[:4]}-{d[4:6]}-{d[6:8]}" if d and len(d) == 8 and d.isdigit() else ""


def entry_date(e: dict):
    """Ngày đăng YYYYMMDD (khi quét danh sách phẳng chỉ là ngày xấp xỉ)."""
    d = e.get("upload_date")
    if d and re.fullmatch(r"\d{8}", str(d)):
        return str(d)
    ts = e.get("timestamp") or e.get("release_timestamp")
    if ts:
        try:
            return datetime.fromtimestamp(int(ts), timezone.utc).strftime("%Y%m%d")
        except (OverflowError, OSError, ValueError):
            return None
    return None


def guess_type(e: dict, tab=None) -> str:
    if tab == "shorts":
        return "short"
    if tab:
        return "video"
    url = e.get("url") or e.get("webpage_url") or ""
    if "/shorts/" in url:
        return "short"
    dur = e.get("duration")
    if dur and dur <= 60:
        return "short"
    return "video"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def check_is_short(vid: str):
    """True = Short, False = video thường, None = không xác định được.
    /shorts/<id> trả 200 nếu là Short, còn video thường bị chuyển hướng sang /watch."""
    req = urllib.request.Request(
        f"https://www.youtube.com/shorts/{vid}", method="HEAD",
        headers={"User-Agent": "Mozilla/5.0", "Accept-Language": "en-US,en;q=0.9"})
    try:
        with _OPENER.open(req, timeout=10) as r:
            return r.status == 200
    except urllib.error.HTTPError as e:
        if e.code in (301, 302, 303, 307, 308):
            loc = e.headers.get("Location", "") or ""
            return False if "/watch" in loc else None
        return None
    except Exception:
        return None


def chuan_hoa_base(url: str):
    """Chuyển @handle / link kênh về link gốc của kênh (bỏ đuôi tab)."""
    url = url.strip()
    if not url:
        return None
    if url.startswith("@"):
        url = "https://www.youtube.com/" + url
    elif not url.startswith("http"):
        url = "https://www.youtube.com/" + url.lstrip("/")
    url = url.rstrip("/")
    return re.sub(r"/(videos|shorts|streams|featured|playlists|community|about)$", "", url)


def ten_file_an_toan(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", name).strip(" .")[:100]
    return name or "kenh"


def is_english(title: str) -> bool:
    letters = [c for c in title if c.isalpha()]
    if len(letters) < 3:
        return False
    if any(not c.isascii() for c in letters):  # có dấu / chữ khác Latin => loại
        return False
    words = re.findall(r"[a-z]+", title.lower())
    en = sum(1 for w in words if w in EN_WORDS)
    other = sum(1 for w in words if w in OTHER_WORDS and w not in EN_WORDS)
    return other <= en


# ---------- So khớp ----------
def tokenize(text: str, strip_hashtags: bool) -> list[str]:
    text = unicodedata.normalize("NFC", text).lower()
    if strip_hashtags:
        text = re.sub(r"[#@]\S+", " ", text)
    return re.findall(r"\w+", text, re.UNICODE)


def is_match(query: str, caption: str, max_extra: int, strip_hashtags: bool) -> bool:
    """Caption chứa nguyên tiêu đề (liền mạch) và thừa tối đa max_extra từ => khớp."""
    q = tokenize(query, strip_hashtags)
    c = tokenize(caption, strip_hashtags)
    if not q or len(c) < len(q) or len(c) - len(q) > max_extra:
        return False
    n = len(q)
    return any(c[i:i + n] == q for i in range(len(c) - n + 1))


def collect_desc(obj, out: list):
    if isinstance(obj, dict):
        d = obj.get("desc")
        if isinstance(d, str) and d.strip() and ("video" in obj or "author" in obj or "id" in obj):
            out.append(d)
        for v in obj.values():
            collect_desc(v, out)
    elif isinstance(obj, list):
        for v in obj:
            collect_desc(v, out)


def port_open(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def launch_chrome() -> bool:
    if port_open(CDP_PORT):
        return True
    exe = next((p for p in CHROME_PATHS if os.path.exists(p)), None)
    if not exe:
        return False
    subprocess.Popen([exe, f"--remote-debugging-port={CDP_PORT}",
                      f"--user-data-dir={CHROME_PROFILE}", "https://www.tiktok.com/"])
    for _ in range(30):
        if port_open(CDP_PORT):
            return True
        time.sleep(0.5)
    return False


# ---------- Dòng của bảng "không tìm thấy" ----------
def config_cols(frame):
    frame.columnconfigure(0, minsize=62)
    frame.columnconfigure(1, weight=1)
    frame.columnconfigure(2, minsize=90)
    frame.columnconfigure(3, minsize=90)
    frame.columnconfigure(4, minsize=150)


class RowView:
    def __init__(self, app, parent, item):
        self.app = app
        self.item = item
        self.frame = tk.Frame(parent, bd=0, highlightthickness=1, highlightbackground="#cccccc")
        config_cols(self.frame)
        self.lbl_type = tk.Label(self.frame, anchor="w", padx=6)
        self.lbl_title = tk.Label(self.frame, anchor="w", justify="left", padx=6, pady=3,
                                  wraplength=app.title_wrap)
        self.lbl_views = tk.Label(self.frame, anchor="e", padx=6)
        self.lbl_date = tk.Label(self.frame, anchor="center")
        self.box = tk.Frame(self.frame)
        kw = dict(height=24, corner_radius=12, font=("Arial", 11, "bold"), border_width=1)
        self.btn_copy = ctk.CTkButton(self.box, text="Copy", width=52,
                                      fg_color=PALETTE["blue"][0], hover_color=PALETTE["blue"][1],
                                      border_color=PALETTE["blue"][0],
                                      command=lambda: app.copy_link(self.item), **kw)
        self.btn_ok = ctk.CTkButton(self.box, text="✔", width=34, border_color=PALETTE["green"][0],
                                    command=lambda: app.set_item_status(self.item, "ok"), **kw)
        self.btn_bad = ctk.CTkButton(self.box, text="✖", width=34, border_color=PALETTE["red"][0],
                                     command=lambda: app.set_item_status(self.item, "bad"), **kw)
        for b in (self.btn_copy, self.btn_ok, self.btn_bad):
            b.pack(side="left", padx=2, pady=2)
        self.lbl_type.grid(row=0, column=0, sticky="nsew")
        self.lbl_title.grid(row=0, column=1, sticky="nsew")
        self.lbl_views.grid(row=0, column=2, sticky="nsew")
        self.lbl_date.grid(row=0, column=3, sticky="nsew")
        self.box.grid(row=0, column=4, sticky="nsew")
        self.refresh()

    def refresh(self):
        it = self.item
        self.lbl_type.config(text=it["type"])
        self.lbl_title.config(text=it["title"])
        self.lbl_views.config(text=fmt_views(it["views"]))
        self.lbl_date.config(text=fmt_date(it["date"]))
        self.apply_status()

    def apply_status(self):
        st = self.item["status"]
        bg = COLOR_ROW.get(st, "#ffffff")
        for w in (self.frame, self.lbl_type, self.lbl_title, self.lbl_views, self.lbl_date, self.box):
            w.config(bg=bg)
        g, r = PALETTE["green"], PALETTE["red"]
        self.btn_copy.configure(bg_color=bg)
        # nút đang được chọn: nền đặc, chữ trắng; chưa chọn: nền trắng, viền + chữ màu
        self.btn_ok.configure(bg_color=bg,
                              fg_color=g[0] if st == "ok" else "#ffffff",
                              text_color="#ffffff" if st == "ok" else g[0],
                              hover_color=g[1] if st == "ok" else "#dff3e4")
        self.btn_bad.configure(bg_color=bg,
                               fg_color=r[0] if st == "bad" else "#ffffff",
                               text_color="#ffffff" if st == "bad" else r[0],
                               hover_color=r[1] if st == "bad" else "#f9dcdc")


# ---------- GUI ----------
class App(_Base):
    def __init__(self):
        super().__init__()
        self.title("TikTok Title Checker")
        self.geometry("1280x860")
        self.dnd_ok = False
        if HAS_DND:
            try:
                self.TkdndVersion = TkinterDnD._require(self)
                self.dnd_ok = True
            except Exception:
                self.dnd_ok = False

        self.stop_event = threading.Event()
        self.yt_stop_event = threading.Event()
        self.delay = 2.0
        self.max_extra = 2
        self.top_n = 5
        self.busy = False
        self.meta_running = False
        self._meta_applied = 0
        self._meta_dirty: set[str] = set()

        # dữ liệu
        self.meta: dict[str, dict] = {}      # key -> {type, views, date, status, src}
        self.nf: dict[str, dict] = {}        # bảng "không tìm thấy" (key -> item)
        self.err: dict[str, dict] = {}       # lỗi / captcha (ghi ra error.txt)
        self.src_dirs: dict[str, Path] = {}  # tên kênh/từ khóa -> thư mục lưu
        self.loaded_dirs: set[str] = set()
        self.nf_dirs: list[str] = []
        self._seq = itertools.count()

        # trạng thái bảng
        self.rows: dict[str, RowView] = {}
        self.sort_mode = ""        # "", "views", "date"
        self.sort_desc = True
        self.filter_type = ""      # "", "video", "short"
        self.title_wrap = 420
        self._refresh_job = None

        self.delay_var = tk.StringVar(value="2")
        self.extra_var = tk.StringVar(value="2")
        self.topn_var = tk.StringVar(value="5")
        self.strip_tag_var = tk.BooleanVar(value=True)
        for v in (self.delay_var, self.extra_var, self.topn_var):
            v.trace_add("write", lambda *_: self._sync_opts())

        # YouTube search vars
        self.yt_kw_var = tk.StringVar()
        self.yt_views_var = tk.StringVar(value="100000")
        self.yt_max_var = tk.StringVar(value="200")
        self.yt_scan_var = tk.StringVar(value="1000")
        self.yt_append_var = tk.BooleanVar(value=False)
        self.yt_english_var = tk.BooleanVar(value=True)

        # YouTube channel vars
        self.source_name = ""  # tên kênh / từ khóa mặc định cho dòng không rõ nguồn
        self.ch_limit_var = tk.StringVar(value="200")
        self.ch_views_var = tk.StringVar(value="500000")
        self.ch_sort_var = tk.StringVar(value=CH_SORTS[0])
        self.ch_detail_var = tk.BooleanVar(value=True)
        self.ch_english_var = tk.BooleanVar(value=False)
        self.ch_append_var = tk.BooleanVar(value=False)
        self.ch_dir_var = tk.StringVar(value=str(BASE_DIR))
        self.ch_tab_vars = {k: tk.BooleanVar(value=True) for k in CH_TABS}

        self._build_ui()
        self._setup_dnd()
        self.load_state()

    def _sync_opts(self):
        try:
            self.delay = max(0.0, float(self.delay_var.get().replace(",", ".")))
        except ValueError:
            pass
        try:
            self.max_extra = max(0, int(self.extra_var.get()))
        except ValueError:
            pass
        try:
            self.top_n = max(1, int(self.topn_var.get()))
        except ValueError:
            pass

    def _build_ui(self):
        top = ctk.CTkFrame(self, corner_radius=12)
        top.pack(fill="x", padx=10, pady=(10, 4))
        pad = dict(side="left")
        ctk.CTkLabel(top, text="Delay (giây):").pack(padx=(12, 4), pady=8, **pad)
        ctk.CTkEntry(top, textvariable=self.delay_var, width=50, corner_radius=10).pack(**pad)
        ctk.CTkLabel(top, text="Kiểm tra top:").pack(padx=(14, 4), **pad)
        ctk.CTkEntry(top, textvariable=self.topn_var, width=50, corner_radius=10).pack(**pad)
        ctk.CTkLabel(top, text="Thừa tối đa (từ):").pack(padx=(14, 4), **pad)
        ctk.CTkEntry(top, textvariable=self.extra_var, width=50, corner_radius=10).pack(**pad)
        ctk.CTkCheckBox(top, text="Bỏ qua #hashtag/@mention", variable=self.strip_tag_var,
                        checkbox_width=20, checkbox_height=20, corner_radius=6).pack(padx=14, **pad)
        make_btn(top, "Mở Chrome", self.open_chrome, "orange", 110).pack(padx=4, **pad)
        self.btn_start = make_btn(top, "▶ Bắt đầu", self.start, "green", 110)
        self.btn_start.pack(padx=4, **pad)
        self.btn_stop = make_btn(top, "■ Dừng", self.stop, "red", 90, state="disabled")
        self.btn_stop.pack(padx=4, **pad)

        self.status = tk.StringVar(value="Sẵn sàng")
        ctk.CTkLabel(self, textvariable=self.status, anchor="w").pack(fill="x", padx=14)

        main = ctk.CTkFrame(self, fg_color="transparent")
        main.pack(fill="both", expand=True, padx=6, pady=6)
        main.columnconfigure(0, weight=2, minsize=430)
        main.columnconfigure(1, weight=3)
        main.rowconfigure(0, weight=1)

        left = ctk.CTkFrame(main, fg_color="transparent")
        left.grid(row=0, column=0, sticky="nsew", padx=4)

        tabs = ctk.CTkTabview(left, height=330, corner_radius=12)
        tabs.pack(fill="x", pady=(0, 6))

        # ---- Tab tìm tiêu đề YouTube ----
        yt = tabs.add("Tìm theo từ khóa")
        yt.columnconfigure(1, weight=1)

        ctk.CTkLabel(yt, text="Từ khóa:").grid(row=0, column=0, sticky="w", padx=4, pady=3)
        e = ctk.CTkEntry(yt, textvariable=self.yt_kw_var, corner_radius=10)
        e.grid(row=0, column=1, columnspan=3, sticky="we", padx=4, pady=3)
        e.bind("<Return>", lambda _: self.yt_start())

        ctk.CTkLabel(yt, text="View tối thiểu:").grid(row=1, column=0, sticky="w", padx=4)
        ctk.CTkEntry(yt, textvariable=self.yt_views_var, width=90, corner_radius=10).grid(
            row=1, column=1, sticky="w", padx=4, pady=3)
        ctk.CTkLabel(yt, text="Số tiêu đề tối đa:").grid(row=1, column=2, sticky="e")
        ctk.CTkEntry(yt, textvariable=self.yt_max_var, width=60, corner_radius=10).grid(
            row=1, column=3, sticky="w", padx=4)

        ctk.CTkLabel(yt, text="Quét tối đa:").grid(row=2, column=0, sticky="w", padx=4, pady=3)
        ctk.CTkEntry(yt, textvariable=self.yt_scan_var, width=90, corner_radius=10).grid(
            row=2, column=1, sticky="w", padx=4)
        ctk.CTkCheckBox(yt, text="Thêm vào danh sách (không xóa cũ)", variable=self.yt_append_var,
                        checkbox_width=20, checkbox_height=20).grid(
            row=2, column=2, columnspan=2, sticky="w", padx=4)

        ctk.CTkCheckBox(yt, text="Chỉ lấy tiêu đề tiếng Anh", variable=self.yt_english_var,
                        checkbox_width=20, checkbox_height=20).grid(
            row=3, column=0, columnspan=2, sticky="w", padx=4, pady=6)

        byt = ctk.CTkFrame(yt, fg_color="transparent")
        byt.grid(row=4, column=0, columnspan=4, sticky="w", padx=4, pady=(2, 6))
        self.btn_yt = make_btn(byt, "🔍 Tìm YouTube", self.yt_start, "blue", 130)
        self.btn_yt.pack(side="left")
        self.btn_yt_stop = make_btn(byt, "Dừng tìm", self.yt_stop, "red", 90, state="disabled")
        self.btn_yt_stop.pack(side="left", padx=6)

        # ---- Tab quét kênh YouTube ----
        ch = tabs.add("Quét kênh YouTube")
        ch.columnconfigure(1, weight=1)

        ctk.CTkLabel(ch, text="Link kênh / @handle (mỗi dòng 1 kênh):").grid(
            row=0, column=0, columnspan=4, sticky="w")
        self.txt_channels = ctk.CTkTextbox(ch, height=70, wrap="none", corner_radius=10,
                                           border_width=1, border_color="#c8c8c8")
        self.txt_channels.grid(row=1, column=0, columnspan=4, sticky="we", pady=(0, 4))

        ctk.CTkLabel(ch, text="Giới hạn số video:").grid(row=2, column=0, sticky="w")
        ctk.CTkEntry(ch, textvariable=self.ch_limit_var, width=70, corner_radius=10).grid(
            row=2, column=1, sticky="w", padx=4)
        ctk.CTkLabel(ch, text="View tối thiểu:").grid(row=2, column=2, sticky="e")
        ctk.CTkEntry(ch, textvariable=self.ch_views_var, width=90, corner_radius=10).grid(
            row=2, column=3, sticky="w", padx=4)

        ctk.CTkLabel(ch, text="Sắp xếp:").grid(row=3, column=0, sticky="w", pady=3)
        ctk.CTkOptionMenu(ch, variable=self.ch_sort_var, values=CH_SORTS, width=170,
                          corner_radius=10).grid(row=3, column=1, sticky="w", padx=4)

        tabs_frm = ctk.CTkFrame(ch, fg_color="transparent")
        tabs_frm.grid(row=4, column=0, columnspan=4, sticky="w")
        ctk.CTkLabel(tabs_frm, text="Quét tab:").pack(side="left")
        for key, label in CH_TABS.items():
            ctk.CTkCheckBox(tabs_frm, text=label, variable=self.ch_tab_vars[key], width=70,
                            checkbox_width=20, checkbox_height=20).pack(side="left", padx=(8, 0))

        opts_frm = ctk.CTkFrame(ch, fg_color="transparent")
        opts_frm.grid(row=5, column=0, columnspan=4, sticky="w", pady=3)
        ctk.CTkCheckBox(opts_frm, text="Lấy view chi tiết nếu thiếu (chậm)",
                        variable=self.ch_detail_var, checkbox_width=20,
                        checkbox_height=20).pack(side="left")
        ctk.CTkCheckBox(opts_frm, text="Chỉ tiếng Anh", variable=self.ch_english_var,
                        checkbox_width=20, checkbox_height=20).pack(side="left", padx=8)
        ctk.CTkCheckBox(opts_frm, text="Thêm vào danh sách (không xóa cũ)",
                        variable=self.ch_append_var, checkbox_width=20,
                        checkbox_height=20).pack(side="left")

        dir_frm = ctk.CTkFrame(ch, fg_color="transparent")
        dir_frm.grid(row=6, column=0, columnspan=4, sticky="we", pady=2)
        ctk.CTkLabel(dir_frm, text="Thư mục gốc:").pack(side="left")
        ctk.CTkEntry(dir_frm, textvariable=self.ch_dir_var, corner_radius=10).pack(
            side="left", fill="x", expand=True, padx=4)
        make_btn(dir_frm, "Chọn...", self.choose_ch_dir, "gray", 80, height=28).pack(side="left")

        bch = ctk.CTkFrame(ch, fg_color="transparent")
        bch.grid(row=7, column=0, columnspan=4, sticky="w", pady=(4, 2))
        self.btn_ch = make_btn(bch, "🔍 Quét kênh", self.ch_start, "blue", 120)
        self.btn_ch.pack(side="left")
        self.btn_ch_stop = make_btn(bch, "Dừng quét", self.yt_stop, "red", 100, state="disabled")
        self.btn_ch_stop.pack(side="left", padx=6)

        # ---- Danh sách tiêu đề ----
        lf_titles = ctk.CTkFrame(left, corner_radius=12)
        lf_titles.pack(fill="both", expand=True)
        self.list_frame = lf_titles
        ctk.CTkLabel(lf_titles, text="Danh sách để tìm trên TikTok (chọn file hoặc kéo file vào đây)",
                     anchor="w", font=("Arial", 12, "bold")).pack(fill="x", padx=10, pady=(8, 0))
        self.txt_titles = ctk.CTkTextbox(lf_titles, wrap="word", undo=True, corner_radius=10,
                                         border_width=1, border_color="#c8c8c8")
        self.txt_titles.pack(fill="both", expand=True, padx=8, pady=6)
        bl = ctk.CTkFrame(lf_titles, fg_color="transparent")
        bl.pack(fill="x", padx=8, pady=(0, 8))
        make_btn(bl, "Mở file .txt", self.load_file, "gray", 100, height=28).pack(side="left")
        make_btn(bl, "Mở thư mục", self.load_list_dir, "gray", 100, height=28).pack(side="left", padx=4)
        make_btn(bl, "Lưu danh sách", self.save_titles, "green", 110, height=28).pack(side="left")
        make_btn(bl, "Xóa", lambda: self.txt_titles.delete("1.0", "end"),
                 "red", 60, height=28).pack(side="left", padx=4)

        # ---- Bên phải ----
        right = ctk.CTkFrame(main, fg_color="transparent")
        right.grid(row=0, column=1, sticky="nsew", padx=4)
        right.rowconfigure(0, weight=3)
        right.rowconfigure(1, weight=1)
        right.columnconfigure(0, weight=1)

        # ---- Bảng KHÔNG tìm thấy ----
        nf = ctk.CTkFrame(right, corner_radius=12)
        nf.grid(row=0, column=0, sticky="nsew", pady=(0, 6))
        self.nf_frame = nf
        ctk.CTkLabel(nf, text="❌ KHÔNG tìm thấy trên TikTok (chọn file hoặc kéo file vào bảng)",
                     anchor="w", font=("Arial", 12, "bold")).pack(fill="x", padx=10, pady=(8, 0))

        tb = ctk.CTkFrame(nf, fg_color="transparent")
        tb.pack(fill="x", padx=8, pady=(6, 2))
        self.btn_sort_views = make_btn(tb, "Lượt xem", lambda: self.toggle_sort("views"),
                                       "gray", 95, height=28)
        self.btn_sort_views.pack(side="left")
        self.btn_sort_date = make_btn(tb, "Ngày đăng", lambda: self.toggle_sort("date"),
                                      "gray", 95, height=28)
        self.btn_sort_date.pack(side="left", padx=4)
        self.btn_f_video = make_btn(tb, "Video", lambda: self.toggle_filter("video"),
                                    "blue", 70, height=28, border_width=2)
        self.btn_f_video.pack(side="left", padx=(10, 0))
        self.btn_f_short = make_btn(tb, "Short", lambda: self.toggle_filter("short"),
                                    "pink", 70, height=28, border_width=2)
        self.btn_f_short.pack(side="left", padx=4)
        self.btn_meta = make_btn(tb, "⟳ Load Metadata", self.meta_toggle, "purple", 140, height=28)
        self.btn_meta.pack(side="left", padx=(10, 0))
        self._paint_filter()

        tb2 = ctk.CTkFrame(nf, fg_color="transparent")
        tb2.pack(fill="x", padx=8)
        self.lbl_counts = ctk.CTkLabel(tb2, text="", anchor="w")
        self.lbl_counts.pack(side="left", padx=2)
        make_btn(tb2, "Mở thư mục...", self.choose_nf_dir, "gray", 105, height=26).pack(side="right")
        make_btn(tb2, "Mở file...", self.choose_nf_files, "gray", 85, height=26).pack(side="right", padx=4)

        tbl = ctk.CTkFrame(nf, fg_color="transparent")
        tbl.pack(fill="both", expand=True, padx=8, pady=(2, 8))
        self.nf_table = tbl
        hdr = tk.Frame(tbl, bg="#e8e8e8")
        hdr.pack(fill="x")
        config_cols(hdr)
        hdr.columnconfigure(5, minsize=17)
        for col, text, anchor in ((0, "Loại", "w"), (1, "Tiêu đề", "w"), (2, "Lượt xem", "e"),
                                  (3, "Ngày đăng", "center"), (4, "Thao tác", "center")):
            tk.Label(hdr, text=text, anchor=anchor, bg="#e8e8e8", padx=6,
                     font=("Arial", 9, "bold")).grid(row=0, column=col, sticky="ew")
        body = tk.Frame(tbl, bg="#ffffff")
        body.pack(fill="both", expand=True)
        self.nf_canvas = tk.Canvas(body, highlightthickness=0, yscrollincrement=20, bg="#ffffff")
        vs = ctk.CTkScrollbar(body, orientation="vertical", command=self.nf_canvas.yview)
        self.nf_canvas.configure(yscrollcommand=vs.set)
        vs.pack(side="right", fill="y")
        self.nf_canvas.pack(side="left", fill="both", expand=True)
        self.nf_inner = tk.Frame(self.nf_canvas, bg="#ffffff")
        self._nf_win = self.nf_canvas.create_window((0, 0), window=self.nf_inner, anchor="nw")
        self.nf_inner.bind("<Configure>",
                           lambda e: self.nf_canvas.configure(scrollregion=self.nf_canvas.bbox("all")))
        self.nf_canvas.bind("<Configure>", self._on_canvas_resize)
        self.nf_hint = tk.Label(self.nf_inner, bg="#ffffff", fg="#888888", pady=24,
                                text="Chưa có dữ liệu.\nChọn file / thư mục hoặc kéo file vào bảng này.")

        # ---- Log ----
        lf = ctk.CTkFrame(right, corner_radius=12)
        lf.grid(row=1, column=0, sticky="nsew")
        ctk.CTkLabel(lf, text="Log", anchor="w", font=("Arial", 12, "bold")).pack(
            fill="x", padx=10, pady=(8, 0))
        self.txt_log = ctk.CTkTextbox(lf, wrap="word", height=120, corner_radius=10,
                                      border_width=1, border_color="#c8c8c8")
        self.txt_log.pack(fill="both", expand=True, padx=8, pady=8)
        self.txt_log.configure(state="disabled")

        self.bind_all("<MouseWheel>", self._on_wheel)
        self.bind_all("<Button-4>", self._on_wheel)
        self.bind_all("<Button-5>", self._on_wheel)

    # ----- kéo thả -----
    def _setup_dnd(self):
        if not self.dnd_ok:
            self.log("ℹ Muốn kéo-thả file: pip install tkinterdnd2 (hiện vẫn dùng được nút chọn file).")
            return
        self.drop_target_register(DND_FILES)
        self.dnd_bind("<<Drop>>", self._on_drop)

    @staticmethod
    def _inside(w, container) -> bool:
        while w is not None:
            if w is container:
                return True
            w = getattr(w, "master", None)
        return False

    def _on_drop(self, event):
        paths = list(self.tk.splitlist(event.data))
        try:
            w = self.winfo_containing(event.x_root, event.y_root)
        except Exception:
            w = None
        if self._inside(w, self.nf_frame):
            self.load_not_found_paths(paths)
        elif self._inside(w, self.list_frame):
            self.load_list_paths(paths)
        else:
            self.set_status("Hãy thả file vào bảng 'KHÔNG tìm thấy' hoặc khung 'Danh sách'.")
        return event.action

    # ----- cuộn bảng -----
    def _on_wheel(self, e):
        try:
            w = self.winfo_containing(e.x_root, e.y_root)
        except Exception:
            return
        if not self._inside(w, self.nf_table):
            return
        num = getattr(e, "num", 0)
        step = -3 if num == 4 else 3 if num == 5 else (-3 if e.delta > 0 else 3)
        self.nf_canvas.yview_scroll(step, "units")

    def _on_canvas_resize(self, e):
        self.nf_canvas.itemconfigure(self._nf_win, width=e.width)
        wrap = max(120, e.width - (62 + 90 + 90 + 150) - 24)
        if abs(wrap - self.title_wrap) > 8:
            self.title_wrap = wrap
            for rv in self.rows.values():
                rv.lbl_title.config(wraplength=wrap)

    # ----- tiện ích -----
    def log(self, msg):
        """Ghi 1 dòng vào cuối ô Log (dòng mới nằm dưới cùng)."""
        def _do():
            self.txt_log.configure(state="normal")
            self.txt_log.insert("end", msg + "\n")
            self.txt_log._textbox.see("end")
            self.txt_log.configure(state="disabled")
        self.after(0, _do)

    def set_status(self, s):
        self.after(0, lambda: self.status.set(s))

    def default_src(self) -> str:
        return self.source_name or self.yt_kw_var.get().strip() or "khong_tim_thay"

    def set_meta(self, key, **kw):
        m = self.meta.setdefault(key, {})
        for k, v in kw.items():
            if v not in (None, ""):
                m[k] = v

    def open_chrome(self):
        if launch_chrome():
            self.log("Chrome đã sẵn sàng (cổng 9222). Hãy đăng nhập TikTok nếu cần.")
        else:
            messagebox.showerror("Lỗi", "Không tìm thấy Chrome. Hãy sửa CHROME_PATHS trong code.")

    # ----- nhập file -----
    def import_file(self, p: Path) -> list:
        """Đọc 1 file -> [(vid, title, meta)] đã chuẩn hóa (type/src) và nhớ thư mục lưu của từng nguồn."""
        name = p.name.lower()
        is_state = name in STATE_NAMES
        forced = {"video.txt": "video", "short.txt": "short"}.get(name)
        out = []
        for vid, title, m in read_entries(p):
            src = m.get("src") or (p.parent.name if is_state else p.stem)
            if is_state:
                d = p.parent
            else:
                safe = ten_file_an_toan(src)
                d = p.parent if p.parent.name == safe else p.parent / safe
            if is_state or src not in self.src_dirs:
                self.src_dirs[src] = d
            m["src"] = src
            m["type"] = forced or m.get("type") or "video"
            out.append((vid, title, m))
        return out

    def build_item(self, vid, title, m) -> dict:
        return {"key": item_key(vid, title), "id": vid, "title": title,
                "type": m.get("type") or "video", "views": m.get("views"), "date": m.get("date"),
                "status": m.get("status") or "", "src": m.get("src") or self.default_src(),
                "seq": next(self._seq)}

    def add_loaded(self, vid, title, m, to_err=False):
        key = item_key(vid, title)
        meta = self.meta.setdefault(key, {})
        meta.update({k: v for k, v in m.items() if v not in (None, "")})
        if key in self.nf or (to_err and key in self.err):
            return
        item = self.build_item(vid, title, meta)
        if to_err:
            self.err[key] = item
        else:
            self.err.pop(key, None)
            self.nf[key] = item

    def load_state_dir(self, d: Path):
        """Nạp video.txt + short.txt (hiện ở bảng) và error.txt (giữ nền) của 1 thư mục kênh."""
        d = Path(d)
        self.loaded_dirs.add(str(d))
        for fname in ("video.txt", "short.txt", "error.txt"):
            p = d / fname
            if p.exists():
                for vid, title, m in self.import_file(p):
                    self.add_loaded(vid, title, m, to_err=(fname == "error.txt"))

    def load_not_found_paths(self, paths):
        dirs, files = [], []
        for raw in paths:
            p = Path(raw)
            if p.is_dir():
                dirs.extend(find_state_dirs(p))
            elif p.is_file():
                if p.name.lower() in STATE_NAMES:
                    dirs.append(p.parent)
                else:
                    files.append(p)
        dirs = list(dict.fromkeys(dirs))
        if not dirs and not files:
            messagebox.showinfo("Không có dữ liệu",
                                "Không thấy video.txt / short.txt / error.txt hoặc file .txt nào để nạp.")
            return
        self.nf.clear()
        self.err.clear()
        self.loaded_dirs.clear()
        self.nf_dirs = []
        for d in dirs:
            self.load_state_dir(d)
            self.register_dir(d)
        for f in files:
            for vid, title, m in self.import_file(f):
                self.add_loaded(vid, title, m)
        self.refresh_table()
        self.log(f"Đã nạp bảng không tìm thấy: {len(self.nf)} dòng "
                 f"({len(dirs)} thư mục, {len(files)} file lẻ).")

    def choose_nf_files(self):
        ps = filedialog.askopenfilenames(title="Chọn file không tìm thấy (video.txt / short.txt ...)",
                                         filetypes=[("Text", "*.txt"), ("All", "*.*")])
        if ps:
            self.load_not_found_paths(list(ps))

    def choose_nf_dir(self):
        d = filedialog.askdirectory(title="Chọn thư mục kênh (hoặc thư mục chứa nhiều kênh)")
        if d:
            self.load_not_found_paths([d])

    def load_list_paths(self, paths):
        files = []
        for raw in paths:
            p = Path(raw)
            if p.is_dir():
                files.extend(sorted(p.rglob("*.txt")))
            elif p.is_file():
                files.append(p)
        if not files:
            messagebox.showinfo("Không có dữ liệu", "Không tìm thấy file .txt nào.")
            return
        lines, seen = [], set()
        for f in files:
            for vid, title, m in self.import_file(f):
                key = item_key(vid, title)
                self.meta.setdefault(key, {}).update({k: v for k, v in m.items() if v not in (None, "")})
                if key not in seen:
                    seen.add(key)
                    lines.append(make_line(vid, title))
        self.txt_titles.delete("1.0", "end")
        if lines:
            self.txt_titles.insert("1.0", "\n".join(lines) + "\n")
        self.log(f"Đã nạp danh sách: {len(lines)} dòng từ {len(files)} file.")

    def load_file(self):
        ps = filedialog.askopenfilenames(filetypes=[("Text", "*.txt"), ("All", "*.*")])
        if ps:
            self.load_list_paths(list(ps))

    def load_list_dir(self):
        d = filedialog.askdirectory(title="Chọn thư mục (nạp mọi file .txt bên trong)")
        if d:
            self.load_list_paths([d])

    def save_titles(self):
        p = filedialog.asksaveasfilename(defaultextension=".txt", filetypes=[("Text", "*.txt")])
        if not p:
            return
        out = []
        for ln in self.txt_titles.get("1.0", "end").splitlines():
            if ln.strip():
                vid, title = parse_line(ln)
                out.append(make_file_line(vid, title, self.meta.get(item_key(vid, title), {})))
        Path(p).write_text("\n".join(out), encoding="utf-8")

    # ----- lưu / nhớ dữ liệu -----
    def dir_for(self, src: str) -> Path:
        d = self.src_dirs.get(src)
        if d:
            return Path(d)
        root = Path(self.ch_dir_var.get().strip() or BASE_DIR)
        d = root / ten_file_an_toan(src)
        self.src_dirs[src] = d
        return d

    def save_src(self, src: str):
        """Ghi video.txt / short.txt / error.txt trong thư mục của kênh `src`."""
        d = self.dir_for(src)
        groups = {"video": [], "short": [], "error": []}
        for it in self.nf.values():
            if it["src"] == src:
                groups["short" if it["type"] == "short" else "video"].append(
                    make_file_line(it["id"], it["title"], it))
        for it in self.err.values():
            if it["src"] == src:
                groups["error"].append(make_file_line(it["id"], it["title"], it))
        try:
            for kind, lines in groups.items():
                p = d / f"{kind}.txt"
                if lines or p.exists():
                    write_lines(p, lines)
            if any(groups.values()):
                self.register_dir(d)
        except OSError as e:
            self.log(f"⚠ Không ghi được thư mục {d}: {e}")

    def register_dir(self, d: Path):
        s = str(d)
        if s not in self.nf_dirs:
            self.nf_dirs.append(s)
        self.save_state()

    def save_state(self):
        try:
            STATE_FILE.write_text(json.dumps({"dirs": self.nf_dirs}, ensure_ascii=False, indent=1),
                                  encoding="utf-8")
        except OSError:
            pass

    def load_state(self):
        """Mở lại chương trình: nạp lại bảng từ các thư mục đã dùng lần trước."""
        try:
            dirs = json.loads(STATE_FILE.read_text(encoding="utf-8")).get("dirs", [])
        except (OSError, ValueError):
            return
        for s in dirs:
            d = Path(s)
            if d.is_dir():
                self.load_state_dir(d)
                self.nf_dirs.append(str(d))
        if self.nf:
            self.log(f"Đã nạp lại bảng không tìm thấy từ lần trước: {len(self.nf)} dòng.")
        self.refresh_table()

    # ----- bảng không tìm thấy: hiển thị -----
    def visible_items(self) -> list:
        items = list(self.nf.values())
        if self.filter_type:
            items = [i for i in items if i["type"] == self.filter_type]
        desc = self.sort_desc
        if self.sort_mode == "views":
            items.sort(key=lambda i: (i["views"] is None,
                                      -(i["views"] or 0) if desc else (i["views"] or 0)))
        elif self.sort_mode == "date":
            def dk(i):
                d = i["date"]
                if d:   # dòng không có ngày: xuống cuối, theo thứ tự quét
                    return (0, -int(d) if desc else int(d))
                return (1, i["seq"] if desc else -i["seq"])
            items.sort(key=dk)
        else:           # mặc định: video ở trên, short ở dưới
            items.sort(key=lambda i: 0 if i["type"] == "video" else 1)
        return items

    def schedule_refresh(self):
        if self._refresh_job is None:
            self._refresh_job = self.after(150, self.refresh_table)

    def refresh_table(self):
        self._refresh_job = None
        for key in list(self.rows):
            if key not in self.nf:
                self.rows.pop(key).frame.destroy()
        items = self.visible_items()
        for rv in self.rows.values():
            rv.frame.pack_forget()
        self.nf_hint.pack_forget()
        for it in items:
            rv = self.rows.get(it["key"])
            if rv is None:
                rv = RowView(self, self.nf_inner, it)
                self.rows[it["key"]] = rv
            else:
                rv.item = it
                rv.refresh()
            rv.frame.pack(fill="x")
        if not items:
            self.nf_hint.pack()
        self.update_counts(len(items))

    def update_counts(self, shown=None):
        n = len(self.nf)
        v = sum(1 for i in self.nf.values() if i["type"] == "video")
        ok = sum(1 for i in self.nf.values() if i["status"] == "ok")
        bad = sum(1 for i in self.nf.values() if i["status"] == "bad")
        txt = f"Tổng {n} | video {v} | short {n - v} | ✔ {ok} | ✖ {bad} | lỗi/captcha: {len(self.err)}"
        if shown is not None and shown != n:
            txt += f" | đang hiện {shown}"
        self.lbl_counts.configure(text=txt)

    def toggle_sort(self, mode):
        if self.sort_mode != mode:
            self.sort_mode, self.sort_desc = mode, True
        elif self.sort_desc:
            self.sort_desc = False
        else:
            self.sort_mode, self.sort_desc = "", True
        arrow = "▼" if self.sort_desc else "▲"
        self.btn_sort_views.configure(text="Lượt xem" + (f" {arrow}" if self.sort_mode == "views" else ""))
        self.btn_sort_date.configure(text="Ngày đăng" + (f" {arrow}" if self.sort_mode == "date" else ""))
        self.refresh_table()

    def _paint_filter(self):
        """Nút Video / Short: đang bật = nền đặc, tắt = nền trắng viền màu."""
        for kind, btn, col in (("video", self.btn_f_video, "blue"),
                               ("short", self.btn_f_short, "pink")):
            fg, hv = PALETTE[col]
            on = self.filter_type == kind
            btn.configure(fg_color=fg if on else "#ffffff",
                          text_color="#ffffff" if on else fg,
                          border_color=fg,
                          hover_color=hv if on else "#eef3fb")

    def toggle_filter(self, kind):
        self.filter_type = "" if self.filter_type == kind else kind
        self._paint_filter()
        self.refresh_table()

    # ----- thao tác trên dòng -----
    def copy_link(self, item):
        vid = item["id"]
        if not vid:
            self.log("⚠ Dòng này không có ID video nên không có link YouTube.")
            return
        link = f"https://www.youtube.com/watch?v={vid}"
        self.clipboard_clear()
        self.clipboard_append(link)
        self.update_idletasks()
        self.log(f"Đã copy: {link}")   # in ở cuối ô Log

    def set_item_status(self, item, st):
        new = "" if item["status"] == st else st   # bấm lại lần nữa = bỏ đánh dấu
        item["status"] = new
        self.meta.setdefault(item["key"], {})["status"] = new
        rv = self.rows.get(item["key"])
        if rv:
            rv.apply_status()
        self.save_src(item["src"])
        self.refresh_counts_only()

    def refresh_counts_only(self):
        shown = len(self.visible_items()) if self.filter_type else None
        self.update_counts(shown)

    # ----- Load Metadata -----
    def apply_meta(self, key, upd: dict):
        """Chạy ở luồng giao diện: ghi type/views/date mới vào item + meta."""
        m = self.meta.setdefault(key, {})
        m.update(upd)
        cur = self.nf.get(key) or self.err.get(key)
        if cur is not None:
            cur.update(upd)
            self._meta_dirty.add(cur["src"])
        self._meta_applied += 1
        if self._meta_applied % 25 == 0:   # lưu định kỳ để không mất dữ liệu nếu tắt giữa chừng
            self.flush_meta()

    def flush_meta(self):
        for s in list(self._meta_dirty):
            self.save_src(s)
        self._meta_dirty.clear()
        self.refresh_table()

    def meta_toggle(self):
        if self.meta_running:
            self.yt_stop()
            return
        if self.busy:
            self.set_status("Đang quét YouTube, hãy đợi xong rồi bấm Load Metadata.")
            return
        items = [it for it in self.nf.values() if it["id"]]
        skipped = len(self.nf) - len(items)
        if not items:
            messagebox.showinfo("Không có dữ liệu",
                                "Bảng chưa có dòng nào có ID video để tải metadata.")
            return
        if skipped:
            self.log(f"Load Metadata: bỏ qua {skipped} dòng không có ID video.")
        self.yt_stop_event.clear()
        self._set_busy(True)
        self.meta_running = True
        self._meta_applied = 0
        fg, hv = PALETTE["red"]
        self.btn_meta.configure(text="■ Dừng", fg_color=fg, hover_color=hv)
        threading.Thread(target=self.meta_worker, args=(list(items),), daemon=True).start()

    def meta_worker(self, items):
        opts = {"quiet": True, "no_warnings": True, "skip_download": True}
        total = len(items)
        cnt = {"type": 0, "views": 0, "date": 0, "fail": 0}
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                for i, it in enumerate(items, 1):
                    if self.yt_stop_event.is_set():
                        break
                    vid = it["id"]
                    self.set_status(f"Load Metadata [{i}/{total}]: {it['title'][:70]}")

                    is_short = check_is_short(vid)            # luôn kiểm tra lại loại
                    need_views = it["views"] is None
                    need_date = not it["date"]
                    info = None
                    if need_views or need_date or is_short is None:
                        try:
                            info = ydl.extract_info(f"https://www.youtube.com/watch?v={vid}",
                                                    download=False)
                        except Exception as e:
                            self.log(f"⚠ Không lấy được metadata {vid}: {str(e)[:90]}")
                    if is_short is None and info and info.get("duration"):
                        is_short = info["duration"] <= 60      # dự phòng khi không kiểm tra được

                    upd = {}
                    if is_short is not None:
                        new_t = "short" if is_short else "video"
                        if new_t != it["type"]:
                            upd["type"] = new_t
                            cnt["type"] += 1
                    if info:
                        v = info.get("view_count")
                        if need_views and isinstance(v, int):
                            upd["views"] = v
                            cnt["views"] += 1
                        if need_date:
                            d = entry_date(info)
                            if d:
                                upd["date"] = d
                                cnt["date"] += 1
                    if is_short is None and info is None:
                        cnt["fail"] += 1
                    if upd:
                        self.after(0, lambda k=it["key"], u=upd: self.apply_meta(k, u))
                    time.sleep(0.1)
            stopped = " (đã dừng)" if self.yt_stop_event.is_set() else ""
            msg = (f"Load Metadata xong{stopped}: đổi loại {cnt['type']}, bổ sung view {cnt['views']}, "
                   f"bổ sung ngày đăng {cnt['date']}, lỗi {cnt['fail']}.")
            self.log(msg)
            self.set_status(msg)
        except Exception as e:
            self.log(f"⚠ Lỗi Load Metadata: {e}")
            self.set_status("Lỗi Load Metadata")
        finally:
            def done():
                self.flush_meta()
                self.meta_running = False
                fg, hv = PALETTE["purple"]
                self.btn_meta.configure(text="⟳ Load Metadata", fg_color=fg, hover_color=hv)
                self._set_busy(False)
            self.after(0, done)

    # ----- tìm YouTube -----
    def yt_start(self):
        if self.busy:
            return
        kw = self.yt_kw_var.get().strip()
        if not kw:
            messagebox.showwarning("Thiếu từ khóa", "Nhập từ khóa YouTube trước.")
            return
        try:
            min_v = int(re.sub(r"[,\.\s]", "", self.yt_views_var.get()))
            max_t = int(self.yt_max_var.get())
            scan = int(self.yt_scan_var.get())
        except ValueError:
            messagebox.showerror("Lỗi", "View / số tiêu đề / quét tối đa phải là số.")
            return
        if max_t < 1 or scan < 1:
            messagebox.showerror("Lỗi", "Số tiêu đề và quét tối đa phải ≥ 1.")
            return

        self.yt_stop_event.clear()
        self._set_busy(True)
        self.source_name = kw

        if not self.yt_append_var.get():
            self.txt_titles.delete("1.0", "end")

        # id + tiêu đề đã có trong danh sách (để loại trùng)
        seen_ids, seen_titles = set(), set()
        for ln in self.txt_titles.get("1.0", "end").splitlines():
            if ln.strip():
                vid, t = parse_line(ln)
                if vid:
                    seen_ids.add(vid)
                seen_titles.add(t.lower())

        threading.Thread(target=self.yt_worker,
                         args=(kw, min_v, max_t, scan, seen_ids, seen_titles,
                               self.yt_english_var.get()),
                         daemon=True).start()

    def yt_stop(self):
        self.yt_stop_event.set()
        self.set_status("Đang dừng...")

    def _append_line(self, line):
        content = self.txt_titles.get("1.0", "end-1c")
        prefix = "" if (not content or content.endswith("\n")) else "\n"
        self.txt_titles.insert("end", prefix + line + "\n")
        self.txt_titles._textbox.see("end")

    def sort_lines_by_views(self, lines: list[str]) -> list[str]:
        """View cao lên trên; dòng không có dữ liệu view xuống cuối (giữ thứ tự cũ)."""
        def key(line):
            vid, title = parse_line(line)
            v = self.meta.get(item_key(vid, title), {}).get("views")
            return (0, -v) if isinstance(v, int) else (1, 0)
        return sorted(lines, key=key)  # sorted ổn định

    def sort_list_by_views(self):
        lines = [t.strip() for t in self.txt_titles.get("1.0", "end").splitlines() if t.strip()]
        lines = self.sort_lines_by_views(lines)
        self.txt_titles.delete("1.0", "end")
        if lines:
            self.txt_titles.insert("1.0", "\n".join(lines) + "\n")

    def yt_worker(self, kw, min_v, max_t, scan, seen_ids, seen_titles, only_en):
        opts = {
            "quiet": True,
            "no_warnings": True,
            "extract_flat": True,
            "lazy_playlist": True,
            "skip_download": True,
            "extractor_args": {"youtubetab": {"approximate_date": [""]}},
        }
        found = scanned = skipped_lang = 0
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(f"ytsearch{scan}:{kw}", download=False)
                for entry in info.get("entries") or []:
                    if self.yt_stop_event.is_set() or found >= max_t:
                        break
                    scanned += 1
                    if entry:
                        vid = entry.get("id") or ""
                        views = entry.get("view_count") or 0
                        title = " ".join((entry.get("title") or "").split())
                        if title and vid and views >= min_v \
                                and vid not in seen_ids and title.lower() not in seen_titles:
                            if only_en and not is_english(title):
                                skipped_lang += 1
                            else:
                                seen_ids.add(vid)
                                seen_titles.add(title.lower())
                                self.set_meta(vid, type=guess_type(entry), views=views,
                                              date=entry_date(entry), src=kw)
                                found += 1
                                line = make_line(vid, title)
                                self.after(0, lambda l=line: self._append_line(l))
                    self.set_status(f"YouTube: quét {scanned} | đạt {found}/{max_t} "
                                    f"| loại ngôn ngữ {skipped_lang}")
        except Exception as e:
            self.log(f"⚠ Lỗi tìm YouTube: {e}")
            self.set_status("Lỗi tìm YouTube")
        else:
            msg = (f"YouTube xong: quét {scanned}, thêm {found} (≥ {min_v:,} view), "
                   f"loại {skipped_lang} không phải tiếng Anh. Đã sắp xếp theo view.")
            self.log(msg)
            self.set_status(msg)
        finally:
            def done():
                self.sort_list_by_views()  # view cao ở trên
                self._set_busy(False)
            self.after(0, done)

    # ----- quét kênh YouTube -----
    def _set_busy(self, busy: bool):
        self.busy = busy
        st, st_inv = ("disabled", "normal") if busy else ("normal", "disabled")
        for b in (self.btn_yt, self.btn_ch):
            b.configure(state=st)
        for b in (self.btn_yt_stop, self.btn_ch_stop):
            b.configure(state=st_inv)

    def choose_ch_dir(self):
        d = filedialog.askdirectory()
        if d:
            self.ch_dir_var.set(d)

    def ch_start(self):
        if self.busy:
            return
        urls = [u for u in (chuan_hoa_base(l) for l in
                            self.txt_channels.get("1.0", "end").splitlines()) if u]
        if not urls:
            messagebox.showwarning("Thiếu kênh", "Nhập ít nhất 1 link kênh / @handle.")
            return
        try:
            limit = int(re.sub(r"[,\.\s]", "", self.ch_limit_var.get()))
            min_v = int(re.sub(r"[,\.\s]", "", self.ch_views_var.get()))
        except ValueError:
            messagebox.showerror("Lỗi", "Giới hạn và view tối thiểu phải là số nguyên.")
            return
        if limit < 1:
            messagebox.showerror("Lỗi", "Giới hạn phải ≥ 1.")
            return
        tabs = [k for k, v in self.ch_tab_vars.items() if v.get()]
        if not tabs:
            messagebox.showerror("Lỗi", "Chọn ít nhất 1 tab để quét.")
            return
        out_dir = Path(self.ch_dir_var.get().strip() or BASE_DIR)
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            messagebox.showerror("Lỗi", f"Không tạo được thư mục lưu: {e}")
            return

        self.yt_stop_event.clear()
        self._set_busy(True)

        if not self.ch_append_var.get():
            self.txt_titles.delete("1.0", "end")

        seen_ids, seen_titles = set(), set()
        for ln in self.txt_titles.get("1.0", "end").splitlines():
            if ln.strip():
                vid, t = parse_line(ln)
                if vid:
                    seen_ids.add(vid)
                seen_titles.add(t.lower())

        threading.Thread(
            target=self.ch_worker,
            args=(urls, tabs, limit, min_v, self.ch_sort_var.get(), self.ch_detail_var.get(),
                  self.ch_english_var.get(), out_dir, seen_ids, seen_titles),
            daemon=True,
        ).start()

    def ch_worker(self, urls, tabs, limit, min_v, sort_mode, detail, only_en,
                  out_dir, seen_ids, seen_titles):
        names, total_saved, total_added = [], 0, 0
        try:
            for i, url in enumerate(urls, 1):
                if self.yt_stop_event.is_set():
                    break
                self.log(f"=== [{i}/{len(urls)}] {url}")
                try:
                    name, found, scanned = self.scan_channel(
                        url, tabs, limit, min_v, sort_mode, detail, only_en)
                except Exception as e:
                    self.log(f"⚠ Lỗi kênh này: {e}")
                    continue

                names.append(name)
                folder = out_dir / ten_file_an_toan(name)   # thư mục tên kênh
                self.src_dirs[name] = folder
                lines = [make_file_line(f["id"], f["title"], {**f, "src": name}) for f in found]

                # lưu <thư mục kênh>/<tên kênh>.txt
                path = folder / (ten_file_an_toan(name) + ".txt")
                try:
                    write_lines(path, lines)
                    self.log(f"Kênh '{name}': quét {scanned}, lưu {len(lines)} -> {path}")
                except OSError as e:
                    self.log(f"⚠ Không ghi được {path}: {e}")
                total_saved += len(lines)

                # đưa vào danh sách chính để kiểm tra TikTok (bỏ trùng)
                for f in found:
                    vid, title = f["id"], f["title"]
                    self.set_meta(vid, type=f["type"], views=f["views"], date=f["date"], src=name)
                    if vid in seen_ids or title.lower() in seen_titles:
                        continue
                    seen_ids.add(vid)
                    seen_titles.add(title.lower())
                    total_added += 1
                    self.after(0, lambda l=make_line(vid, title): self._append_line(l))

            if len(names) == 1:
                self.source_name = names[0]
            elif names:
                self.source_name = "nhieu_kenh"

            msg = (f"Quét kênh xong: {len(names)} kênh, lưu {total_saved} dòng, "
                   f"thêm {total_added} vào danh sách."
                   + (" (đã dừng)" if self.yt_stop_event.is_set() else ""))
            self.log(msg)
            self.set_status(msg)
        except Exception as e:
            self.log(f"⚠ Lỗi quét kênh: {e}")
            self.set_status("Lỗi quét kênh")
        finally:
            def done():
                if sort_mode == CH_SORTS[0]:
                    self.sort_list_by_views()
                self._set_busy(False)
            self.after(0, done)

    def scan_channel(self, base_url, tabs, limit, min_v, sort_mode, detail, only_en):
        """Quét 1 kênh -> (tên kênh, [dict id/title/views/type/date/order] đã sort + cắt limit, số video quét)."""
        flat_opts = {"extract_flat": True, "lazy_playlist": True, "quiet": True,
                     "no_warnings": True, "skip_download": True,
                     "extractor_args": {"youtubetab": {"approximate_date": [""]}}}
        detail_opts = {"quiet": True, "no_warnings": True, "skip_download": True}

        found, seen = [], set()
        scanned = skipped_lang = order = 0
        name = None
        newest = sort_mode == CH_SORTS[2]
        enough = False

        with yt_dlp.YoutubeDL(flat_opts) as ydl, yt_dlp.YoutubeDL(detail_opts) as ydl2:
            for tab in tabs:
                if self.yt_stop_event.is_set() or enough:
                    break
                self.log(f"-- Tab {CH_TABS[tab]}")
                try:
                    info = ydl.extract_info(f"{base_url}/{tab}", download=False)
                except Exception as e:
                    self.log(f"   Bỏ qua tab (không có hoặc lỗi): {str(e)[:100]}")
                    continue

                if not name:
                    name = info.get("channel") or info.get("uploader") or info.get("title") or "kenh"
                    name = re.sub(r"\s*-\s*(Videos|Shorts|Live)$", "", name)
                    self.log(f"Kênh: {name}")

                tab_scanned = 0
                for e in info.get("entries") or []:
                    if self.yt_stop_event.is_set():
                        break
                    if not e:
                        continue
                    vid, title = e.get("id"), e.get("title")
                    if not vid or not title or vid in seen:
                        continue
                    seen.add(vid)
                    scanned += 1
                    tab_scanned += 1
                    title = " ".join(title.split())

                    views = e.get("view_count")
                    date = entry_date(e)
                    if views is None and detail:
                        try:
                            d = ydl2.extract_info(f"https://www.youtube.com/watch?v={vid}",
                                                  download=False)
                            views = d.get("view_count")
                            date = entry_date(d) or date
                        except Exception:
                            views = None
                    if views is None or views < min_v:
                        pass
                    elif only_en and not is_english(title):
                        skipped_lang += 1
                    else:
                        order += 1
                        found.append({"views": views, "order": order, "id": vid, "title": title,
                                      "type": guess_type(e, tab), "date": date})
                        if newest and len(found) >= limit:
                            enough = True
                            break

                    if tab_scanned % 25 == 0:
                        self.set_status(f"Kênh {name}: tab {CH_TABS[tab]} quét {tab_scanned} | "
                                        f"đạt {len(found)} | loại ngôn ngữ {skipped_lang}")

        if sort_mode == CH_SORTS[0]:
            found.sort(key=lambda x: x["views"], reverse=True)
        elif sort_mode == CH_SORTS[1]:
            found.sort(key=lambda x: x["views"])
        else:
            found.sort(key=lambda x: x["order"])
        found = found[:limit]

        for f in found[:10]:
            self.log(f"   {f['views']:,} | {f['type']} | {f['title']}")
        if len(found) > 10:
            self.log(f"   ... (còn {len(found) - 10} dòng nữa)")
        return name or "kenh", found, scanned

    # ----- điều khiển TikTok -----
    def start(self):
        lines = [t.strip() for t in self.txt_titles.get("1.0", "end").splitlines() if t.strip()]
        lines = list(dict.fromkeys(lines))
        lines = self.sort_lines_by_views(lines)  # kiểm tra video view cao trước
        if not lines:
            messagebox.showwarning("Thiếu dữ liệu", "Hãy nhập ít nhất 1 tiêu đề.")
            return
        if not launch_chrome():
            messagebox.showerror("Lỗi", "Không mở được Chrome ở chế độ debug.")
            return

        # mỗi dòng thuộc 1 kênh/từ khóa -> nạp thư mục kênh đó để ghi đúng chỗ, không mất dữ liệu cũ
        default_src = self.default_src()
        srcs = []
        for ln in lines:
            vid, title = parse_line(ln)
            m = self.meta.setdefault(item_key(vid, title), {})
            if not m.get("src"):
                m["src"] = default_src
            if m["src"] not in srcs:
                srcs.append(m["src"])
        for s in srcs:
            d = self.dir_for(s)
            if d.is_dir() and str(d) not in self.loaded_dirs:
                self.load_state_dir(d)
                self.register_dir(d)
        self.refresh_table()
        for s in srcs:
            self.log(f"Kết quả của '{s}' sẽ lưu trong thư mục: {self.dir_for(s)}")

        self.stop_event.clear()
        self.btn_start.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        threading.Thread(target=self.run_worker,
                         args=(lines, self.strip_tag_var.get(), default_src),
                         daemon=True).start()

    def stop(self):
        self.stop_event.set()
        self.set_status("Đang dừng...")

    def finish(self):
        self.after(0, lambda: (self.btn_start.configure(state="normal"),
                               self.btn_stop.configure(state="disabled")))

    def record_result(self, vid, title, src, result):
        """Chạy ở luồng giao diện: cập nhật bảng + ghi file ngay."""
        key = item_key(vid, title)
        m = self.meta.setdefault(key, {})
        m.setdefault("src", src)
        src = m["src"]
        changed = False
        if result == "found":                 # đã có trên TikTok -> bỏ khỏi bảng / error
            changed = self.nf.pop(key, None) is not None
            changed = (self.err.pop(key, None) is not None) or changed
        elif result == "notfound":
            was_err = self.err.pop(key, None) is not None
            is_new = key not in self.nf
            if is_new:
                self.nf[key] = self.build_item(vid, title, m)   # giữ ✔/✖ cũ nếu có
            changed = is_new or was_err
        else:                                 # error / captcha
            if key not in self.nf and key not in self.err:
                self.err[key] = self.build_item(vid, title, m)
                changed = True
        if changed:
            self.save_src(src)
            self.schedule_refresh()

    # ----- worker -----
    def run_worker(self, lines, strip_tag, default_src):
        page = None
        nf_count = err_count = 0
        try:
            with sync_playwright() as p:
                browser = p.chromium.connect_over_cdp(CDP_URL)
                ctx = browser.contexts[0]
                page = ctx.new_page()
                total = len(lines)

                for i, line in enumerate(lines, 1):
                    if self.stop_event.is_set():
                        break
                    vid, title = parse_line(line)
                    if not title:
                        continue
                    m = self.meta.get(item_key(vid, title), {})
                    src = m.get("src") or default_src
                    v = m.get("views")
                    vtxt = f" ({v:,} view)" if isinstance(v, int) else ""
                    self.set_status(f"[{i}/{total}] Đang tìm: {title}{vtxt}")

                    failed = captcha = False
                    try:
                        captions, captcha = self.search(page, title)
                    except Exception as e:
                        self.log(f"⚠ Lỗi khi tìm '{title}': {e}")
                        captions, failed = [], True
                    if self.stop_event.is_set():
                        break

                    captions = captions[:self.top_n]  # chỉ xét N kết quả đầu
                    if failed or not captions:
                        why = "lỗi" if failed else ("captcha" if captcha else "không có kết quả")
                        self.log(f"[{i}/{total}] ⚠ {why} -> error.txt: {title}")
                        result = "error"
                        err_count += 1
                    elif any(is_match(title, c, self.max_extra, strip_tag) for c in captions):
                        self.log(f"[{i}/{total}] ✓ Có: {title}")
                        result = "found"
                    else:
                        self.log(f"[{i}/{total}] ✗ Không khớp top {len(captions)}: {title}")
                        result = "notfound"
                        nf_count += 1
                    self.after(0, lambda v=vid, t=title, s=src, r=result:
                               self.record_result(v, t, s, r))

                    if i < total:
                        end = time.time() + self.delay
                        while time.time() < end and not self.stop_event.is_set():
                            time.sleep(0.1)

                try:
                    page.close()  # chỉ đóng tab của tool, không đóng Chrome
                except Exception:
                    pass
            self.set_status("Đã dừng." if self.stop_event.is_set()
                            else f"Xong. Không tìm thấy: {nf_count} | lỗi/captcha: {err_count} "
                                 f"(tổng {len(lines)})")
        except Exception as e:
            self.log(f"Lỗi nghiêm trọng: {e}")
            self.set_status("Lỗi")
        finally:
            self.finish()

    def search(self, page, title):
        """Trả về (danh sách caption, có gặp captcha không)."""
        captions: list[str] = []
        captcha_seen = False

        def on_response(resp):
            try:
                if "/api/search/" in resp.url and "json" in (resp.headers.get("content-type") or ""):
                    collect_desc(json.loads(resp.text()), captions)
            except Exception:
                pass

        page.on("response", on_response)
        try:
            page.goto(f"https://www.tiktok.com/search/video?q={quote(title)}",
                      wait_until="domcontentloaded", timeout=30000)

            warned = False
            deadline = time.time() + 12
            while time.time() < deadline and not self.stop_event.is_set():
                if not captions:
                    try:
                        captions.extend(page.eval_on_selector_all(
                            '[data-e2e="search-card-desc"]', "els => els.map(e => e.innerText)"))
                    except Exception:
                        pass
                if captions:
                    page.wait_for_timeout(1000)
                    break
                if page.query_selector("#captcha_container, .captcha_verify_container, [id*='captcha']"):
                    captcha_seen = True
                    if not warned:
                        self.log("⚠ TikTok yêu cầu captcha — hãy giải trong cửa sổ Chrome...")
                        page.bring_to_front()
                        warned = True
                    deadline = time.time() + 8  # gia hạn trong lúc giải captcha
                page.wait_for_timeout(500)
        finally:
            page.remove_listener("response", on_response)

        return list(dict.fromkeys(captions)), captcha_seen


if __name__ == "__main__":
    App().mainloop()
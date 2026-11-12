
import os, re, json, time, socket, itertools, threading, subprocess, unicodedata
import urllib.request, urllib.error
from datetime import datetime, timezone
from urllib.parse import quote
from pathlib import Path

from PySide6.QtCore import Qt, QObject, Signal, QTimer
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QFrame, QLabel, QPushButton, QLineEdit,
    QTextEdit, QCheckBox, QComboBox, QTabWidget, QVBoxLayout, QHBoxLayout,
    QGridLayout, QSplitter, QTableWidget, QTableWidgetItem, QHeaderView,
    QFileDialog, QMessageBox, QAbstractItemView
)
from playwright.sync_api import sync_playwright
import yt_dlp

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

STATE_FILE = BASE_DIR / "tiktok_checker_state.json"

STATE_NAMES = {"video.txt", "short.txt", "error.txt"}

COLOR_ROW = {"": "#ffffff", "ok": "#c6efce", "bad": "#f4c7c3"}

CH_TABS = {"videos": "Videos", "shorts": "Shorts", "streams": "Live"}

CH_SORTS = ["Views (high to low)", "Views (low to high)", "Newest first"]

PALETTE = {
    "green":  ("#2e9e4f", "#278a43"),   # bắt đầu / lưu / đúng
    "red":    ("#d64545", "#b93a3a"),   # dừng / sai / xóa
    "blue":   ("#2f7bd9", "#2868b8"),   # tìm kiếm / copy
    "gray":   ("#6c7a89", "#5a6673"),   # mở file / sắp xếp
    "purple": ("#7b57d1", "#6847b3"),   # metadata
    "orange": ("#e08a1e", "#c47616"),   # mở Chrome
    "pink":   ("#d6336c", "#b82a5b"),   # short
}

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


class ValueVar:
    def __init__(self, value=None): self._value = value
    def get(self): return self._value
    def set(self, value): self._value = value

class UiDispatcher(QObject):
    call = Signal(int, object)
    def __init__(self, parent=None):
        super().__init__(parent)
        self.call.connect(self._schedule)
    def _schedule(self, ms, callback):
        QTimer.singleShot(max(0, ms), callback)

class StatusLabel(QLabel):
    def set(self, value): self.setText(str(value))

class TextAdapter:
    def __init__(self, edit):
        self.edit = edit
        self._textbox = self
    def get(self, start="1.0", end="end"):
        text = self.edit.toPlainText()
        return text + ("\n" if end == "end" else "")
    def delete(self, start, end):
        self.edit.clear()
    def insert(self, where, text):
        if where in ("1.0", "0"):
            self.edit.setPlainText(text)
        else:
            cur = self.edit.textCursor()
            cur.movePosition(cur.MoveOperation.End)
            cur.insertText(text)
    def see(self, where):
        self.edit.moveCursor(self.edit.textCursor().MoveOperation.End)
    def configure(self, **kwargs):
        if "state" in kwargs:
            self.edit.setReadOnly(kwargs["state"] == "disabled")

class CompatButton(QPushButton):
    def __init__(self, text="", parent=None, role="neutral"):
        super().__init__(text, parent)
        self.role = role
        self.setCursor(Qt.CursorShape.PointingHandCursor)
    def configure(self, **kwargs):
        if "text" in kwargs: self.setText(kwargs["text"])
        if "state" in kwargs:
            self.setEnabled(kwargs["state"] != "disabled")
        fg = kwargs.get("fg_color")
        if fg:
            self.setStyleSheet(f"QPushButton {{ background:{fg}; color:white; border:0; border-radius:7px; padding:6px 12px; }}")
    def pack(self, *args, **kwargs): pass

class Dialogs:
    @staticmethod
    def showinfo(title, text, **kwargs): QMessageBox.information(None, title, text)
    @staticmethod
    def showwarning(title, text, **kwargs): QMessageBox.warning(None, title, text)
    @staticmethod
    def showerror(title, text, **kwargs): QMessageBox.critical(None, title, text)

class FileDialogs:
    @staticmethod
    def askopenfilenames(**kwargs):
        files, _ = QFileDialog.getOpenFileNames(None, kwargs.get("title","Select file"), "", "Text files (*.txt);;All files (*.*)")
        return files
    @staticmethod
    def askdirectory(**kwargs):
        return QFileDialog.getExistingDirectory(None, kwargs.get("title","Select folder"))
    @staticmethod
    def asksaveasfilename(**kwargs):
        path, _ = QFileDialog.getSaveFileName(None, "Save file", "", "Text files (*.txt)")
        return path

messagebox = Dialogs
filedialog = FileDialogs



class App(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("TikTok Title Checker")
        self.resize(1440, 900)
        self.setMinimumSize(1120, 720)
        self.setAcceptDrops(True)
        self.dispatcher = UiDispatcher(self)

        self.stop_event = threading.Event()
        self.yt_stop_event = threading.Event()
        self.delay = 2.0
        self.max_extra = 2
        self.top_n = 5
        self.busy = False
        self.meta_running = False
        self._meta_applied = 0
        self._meta_dirty = set()

        self.meta = {}
        self.nf = {}
        self.err = {}
        self.src_dirs = {}
        self.loaded_dirs = set()
        self.nf_dirs = []
        self._seq = itertools.count()

        self.rows = {}
        self.sort_mode = ""
        self.sort_desc = True
        self.filter_type = ""
        self.search_text = ""
        self.title_wrap = 420
        self._refresh_job = None

        self.delay_var = ValueVar("2")
        self.extra_var = ValueVar("2")
        self.topn_var = ValueVar("5")
        self.strip_tag_var = ValueVar(True)
        self.yt_kw_var = ValueVar("")
        self.yt_views_var = ValueVar("100000")
        self.yt_max_var = ValueVar("200")
        self.yt_scan_var = ValueVar("1000")
        self.yt_append_var = ValueVar(False)
        self.yt_english_var = ValueVar(True)

        self.source_name = ""
        self.ch_limit_var = ValueVar("200")
        self.ch_views_var = ValueVar("500000")
        self.ch_sort_var = ValueVar(CH_SORTS[0])
        self.ch_detail_var = ValueVar(True)
        self.ch_english_var = ValueVar(False)
        self.ch_append_var = ValueVar(False)
        self.ch_dir_var = ValueVar(str(BASE_DIR))
        self.ch_tab_vars = {k: ValueVar(True) for k in CH_TABS}

        self._build_ui()
        self.load_state()

    def _dispatch_call(self, ms, callback):
        # The dispatcher signal is queued to the GUI thread.
        QTimer.singleShot(max(0, ms), callback)

    def after(self, ms, callback):
        self.dispatcher.call.emit(ms, callback)

    def clipboard_clear(self):
        QApplication.clipboard().clear()

    def clipboard_append(self, text):
        QApplication.clipboard().setText(text)

    def update_idletasks(self):
        QApplication.processEvents()

    def _build_ui(self):
        self.setStyleSheet("""
            QMainWindow, QWidget { background:#F4F6FA; color:#172033; font-family:'Segoe UI'; font-size:10pt; }
            QFrame#panel { background:#FFFFFF; border:1px solid #E3E8F0; border-radius:10px; }
            QFrame#topbar { background:#E9EDF3; border:1px solid #D7DEE9; border-radius:10px; }
            QLabel, QCheckBox { background:transparent; }
            QWidget#cell { background:transparent; }
            QLabel#sectionTitle { font-size:11pt; font-weight:650; color:#172033; }
            QLineEdit, QTextEdit, QComboBox {
                background:#FFFFFF; border:1px solid #D7DEE9; border-radius:7px;
                padding:6px 8px; selection-background-color:#2563EB;
            }
            QLineEdit:focus, QTextEdit:focus, QComboBox:focus { border:1px solid #4385F5; }
            QPushButton {
                background:#E9EEF5; color:#263449; border:1px solid #DCE3ED;
                border-radius:7px; padding:6px 11px; min-height:22px;
            }
            QPushButton:hover { background:#DDE7F4; }
            QPushButton:disabled { color:#9AA4B2; background:#EEF1F5; }
            QCheckBox { spacing:7px; }
            QTableWidget { background:#FFFFFF; border:1px solid #E2E8F0; border-radius:7px; gridline-color:#EDF0F4; }
            QHeaderView::section { background:#F1F4F8; color:#455269; padding:9px 6px; border:0; border-bottom:1px solid #DDE4ED; font-weight:600; }
            QTableWidget::item { padding:5px; }
            QHeaderView::section:hover { background:#E3EAF4; }
            QTabWidget::pane { border:1px solid #E2E8F0; border-radius:8px; background:white; }
            QTabBar::tab { background:#E9EEF5; padding:8px 14px; margin-right:3px; border-top-left-radius:6px; border-top-right-radius:6px; }
            QTabBar::tab:selected { background:#FFFFFF; color:#2563EB; font-weight:600; }
        """)

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)

        top = QFrame(); top.setObjectName("topbar")
        top_l = QHBoxLayout(top); top_l.setContentsMargins(12,8,12,8); top_l.setSpacing(8)
        top_l.addWidget(QLabel("Delay (sec):"))
        self.ed_delay = QLineEdit("2"); self.ed_delay.setFixedWidth(62); top_l.addWidget(self.ed_delay)
        self.ed_delay.textChanged.connect(lambda v: (self.delay_var.set(v), self._sync_opts()))
        top_l.addWidget(QLabel("Check top:"))
        self.ed_topn = QLineEdit("5"); self.ed_topn.setFixedWidth(58); top_l.addWidget(self.ed_topn)
        self.ed_topn.textChanged.connect(lambda v: (self.topn_var.set(v), self._sync_opts()))
        top_l.addWidget(QLabel("Max extra words:"))
        self.ed_extra = QLineEdit("2"); self.ed_extra.setFixedWidth(58); top_l.addWidget(self.ed_extra)
        self.ed_extra.textChanged.connect(lambda v: (self.extra_var.set(v), self._sync_opts()))
        self.chk_strip = QCheckBox("Ignore #hashtag/@mention"); self.chk_strip.setChecked(True)
        self.chk_strip.toggled.connect(self.strip_tag_var.set); top_l.addWidget(self.chk_strip)
        top_l.addStretch(1)
        self.btn_open_chrome = self._button("Open Chrome", self.open_chrome, "#D97706")
        top_l.addWidget(self.btn_open_chrome)
        self.btn_start = self._button("▶ Start", self.start, "#16A34A"); top_l.addWidget(self.btn_start)
        self.btn_stop = self._button("■ Stop", self.stop, "#DC4545"); self.btn_stop.setEnabled(False); top_l.addWidget(self.btn_stop)
        root.addWidget(top)

        self.status = StatusLabel("Ready")
        self.status.setStyleSheet("color:#526078; padding:1px 4px;")
        root.addWidget(self.status)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        root.addWidget(splitter, 1)

        left = QWidget(); ll = QVBoxLayout(left); ll.setContentsMargins(0,0,5,0); ll.setSpacing(9)
        self.tabs = QTabWidget()
        self._build_youtube_search_tab()
        self._build_channel_tab()
        ll.addWidget(self.tabs)

        list_panel = QFrame(); list_panel.setObjectName("panel")
        lp = QVBoxLayout(list_panel); lp.setContentsMargins(10,9,10,9); lp.setSpacing(7)
        title = QLabel("Titles to search on TikTok"); title.setObjectName("sectionTitle"); lp.addWidget(title)
        self.title_edit = QTextEdit(); self.title_edit.setPlaceholderText("One title per line, or #video_id|title")
        self.title_edit.setAcceptRichText(False); lp.addWidget(self.title_edit, 1)
        self.txt_titles = TextAdapter(self.title_edit)
        bl = QHBoxLayout()
        for text, fn, col in [
            ("Open .txt file", self.load_file, "#64748B"),
            ("Open folder", self.load_list_dir, "#64748B"),
            ("Save list", self.save_titles, "#16A34A"),
            ("Clear", self._clear_titles, "#DC4545")]:
            bl.addWidget(self._button(text, fn, col))
        lp.addLayout(bl); ll.addWidget(list_panel, 1)
        splitter.addWidget(left)

        right = QWidget(); rl = QVBoxLayout(right); rl.setContentsMargins(5,0,0,0); rl.setSpacing(9)
        table_panel = QFrame(); table_panel.setObjectName("panel")
        tp = QVBoxLayout(table_panel); tp.setContentsMargins(10,9,10,9); tp.setSpacing(7)
        section = QLabel("NOT found on TikTok"); section.setObjectName("sectionTitle"); tp.addWidget(section)
        tools = QHBoxLayout()
        self.ed_search = QLineEdit()
        self.ed_search.setPlaceholderText("🔍 Search title...")
        self.ed_search.setClearButtonEnabled(True)
        self.ed_search.setFixedWidth(240)
        self.ed_search.textChanged.connect(self.on_search_changed)
        tools.addWidget(self.ed_search)
        self.btn_meta = self._button("⟳ Load Metadata", self.meta_toggle, "#7C55C7")
        self.btn_meta.setToolTip("Load Metadata")
        tools.addWidget(self.btn_meta)
        tools.addStretch(1)
        tools.addWidget(self._button("Open file...", self.choose_nf_files, "#64748B"))
        tools.addWidget(self._button("Open folder...", self.choose_nf_dir, "#64748B"))
        tools.addWidget(self._button("Clear", self.clear_table, "#DC4545"))
        tp.addLayout(tools)
        self.lbl_counts = QLabel("Total 0"); self.lbl_counts.setStyleSheet("color:#64748B;"); tp.addWidget(self.lbl_counts)
        self.nf_table = QTableWidget(0,5)
        self.nf_table.setHorizontalHeaderLabels(["Type","Title","Views","Date","Actions"])
        self.nf_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.nf_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.nf_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.nf_table.verticalHeader().setVisible(False)
        h = self.nf_table.horizontalHeader()
        h.setSectionsClickable(True)
        h.setCursor(Qt.CursorShape.PointingHandCursor)
        h.sectionClicked.connect(self.on_header_clicked)
        h.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        h.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        h.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        h.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        h.setSectionResizeMode(4, QHeaderView.ResizeMode.Fixed)
        self.nf_table.setColumnWidth(4, 170)
        self.nf_table.setAlternatingRowColors(True)
        self.nf_table.cellClicked.connect(self.on_cell_clicked)
        tp.addWidget(self.nf_table, 1)
        rl.addWidget(table_panel, 3)

        log_panel = QFrame(); log_panel.setObjectName("panel")
        log_l = QVBoxLayout(log_panel); log_l.setContentsMargins(10,8,10,8)
        log_title = QLabel("Log"); log_title.setObjectName("sectionTitle"); log_l.addWidget(log_title)
        self.log_edit = QTextEdit(); self.log_edit.setReadOnly(True); self.log_edit.setMaximumHeight(150)
        log_l.addWidget(self.log_edit)
        self.txt_log = TextAdapter(self.log_edit)
        rl.addWidget(log_panel, 1)
        splitter.addWidget(right)
        splitter.setSizes([520, 900])
        self._update_headers()

    def _button(self, text, callback, color="#64748B"):
        b = CompatButton(text)
        b.clicked.connect(callback)
        b.setStyleSheet(f"QPushButton {{ background:{color}; color:white; border:0; border-radius:7px; padding:6px 11px; font-weight:600; }} QPushButton:hover {{ background:{color}; }}")
        return b

    def _build_youtube_search_tab(self):
        page = QWidget(); grid = QGridLayout(page); grid.setContentsMargins(10,10,10,10); grid.setHorizontalSpacing(8); grid.setVerticalSpacing(8)
        self.yt_kw = QLineEdit(); self.yt_kw.setPlaceholderText("Enter YouTube keyword")
        self.yt_views = QLineEdit("100000"); self.yt_views.setFixedWidth(105)
        self.yt_max = QLineEdit("200"); self.yt_max.setFixedWidth(75)
        self.yt_scan = QLineEdit("1000"); self.yt_scan.setFixedWidth(105)
        self.yt_append = QCheckBox("Append to list (keep existing)")
        self.yt_english = QCheckBox("English titles only"); self.yt_english.setChecked(True)
        self.yt_kw.textChanged.connect(self.yt_kw_var.set)
        self.yt_views.textChanged.connect(self.yt_views_var.set)
        self.yt_max.textChanged.connect(self.yt_max_var.set)
        self.yt_scan.textChanged.connect(self.yt_scan_var.set)
        self.yt_append.toggled.connect(self.yt_append_var.set)
        self.yt_english.toggled.connect(self.yt_english_var.set)
        self.yt_kw.returnPressed.connect(self.yt_start)
        grid.addWidget(QLabel("Keyword:"),0,0); grid.addWidget(self.yt_kw,0,1,1,3)
        grid.addWidget(QLabel("Min views:"),1,0); grid.addWidget(self.yt_views,1,1)
        grid.addWidget(QLabel("Max titles:"),1,2); grid.addWidget(self.yt_max,1,3)
        grid.addWidget(QLabel("Max scan:"),2,0); grid.addWidget(self.yt_scan,2,1)
        grid.addWidget(self.yt_append,2,2,1,2)
        grid.addWidget(self.yt_english,3,0,1,2)
        self.btn_yt = self._button("🔍 Search YouTube", self.yt_start, "#2563EB")
        self.btn_yt_stop = self._button("Stop search", self.yt_stop, "#DC4545"); self.btn_yt_stop.setEnabled(False)
        row=QHBoxLayout(); row.addWidget(self.btn_yt); row.addWidget(self.btn_yt_stop); row.addStretch(1); grid.addLayout(row,4,0,1,4)
        self.tabs.addTab(page,"Keyword search")

    def _build_channel_tab(self):
        page = QWidget(); grid = QGridLayout(page); grid.setContentsMargins(10,10,10,10); grid.setHorizontalSpacing(8); grid.setVerticalSpacing(6)
        grid.addWidget(QLabel("Channel link / @handle (one per line):"),0,0,1,4)
        self.channels_edit = QTextEdit(); self.channels_edit.setMaximumHeight(65); self.channels_edit.setPlaceholderText("https://www.youtube.com/@channel")
        grid.addWidget(self.channels_edit,1,0,1,4); self.txt_channels = TextAdapter(self.channels_edit)
        self.ch_limit = QLineEdit("200"); self.ch_views = QLineEdit("500000")
        self.ch_limit.textChanged.connect(self.ch_limit_var.set)
        self.ch_views.textChanged.connect(self.ch_views_var.set)
        grid.addWidget(QLabel("Video limit:"),2,0); grid.addWidget(self.ch_limit,2,1)
        grid.addWidget(QLabel("Min views:"),2,2); grid.addWidget(self.ch_views,2,3)
        self.ch_sort = QComboBox(); self.ch_sort.addItems(CH_SORTS); self.ch_sort.currentTextChanged.connect(self.ch_sort_var.set); grid.addWidget(QLabel("Sort by:"),3,0); grid.addWidget(self.ch_sort,3,1,1,3)
        grid.addWidget(QLabel("Scan tabs:"),4,0)
        self.ch_tabs_checks={}
        tabs_box=QHBoxLayout()
        for key,label in CH_TABS.items():
            cb=QCheckBox(label); cb.setChecked(True); cb.toggled.connect(self.ch_tab_vars[key].set); self.ch_tabs_checks[key]=cb; tabs_box.addWidget(cb)
        tabs_box.addStretch(1); grid.addLayout(tabs_box,4,1,1,3)
        self.ch_detail=QCheckBox("Fetch detailed views if missing (slow)"); self.ch_detail.setChecked(True); self.ch_detail.toggled.connect(self.ch_detail_var.set)
        self.ch_english=QCheckBox("English only"); self.ch_english.toggled.connect(self.ch_english_var.set); self.ch_append=QCheckBox("Append to list (keep existing)"); self.ch_append.toggled.connect(self.ch_append_var.set)
        opts=QHBoxLayout(); opts.addWidget(self.ch_detail); opts.addWidget(self.ch_english); opts.addWidget(self.ch_append); grid.addLayout(opts,5,0,1,4)
        self.ch_dir=QLineEdit(str(BASE_DIR)); self.ch_dir.textChanged.connect(self.ch_dir_var.set); grid.addWidget(QLabel("Root folder:"),6,0); grid.addWidget(self.ch_dir,6,1,1,2)
        grid.addWidget(self._button("Browse...",self.choose_ch_dir,"#64748B"),6,3)
        self.btn_ch=self._button("🔍 Scan channel",self.ch_start,"#2563EB")
        self.btn_ch_stop=self._button("Stop scan",self.yt_stop,"#DC4545"); self.btn_ch_stop.setEnabled(False)
        row=QHBoxLayout(); row.addWidget(self.btn_ch); row.addWidget(self.btn_ch_stop); row.addStretch(1); grid.addLayout(row,7,0,1,4)
        self.tabs.addTab(page,"YouTube channel scan")

    def _clear_titles(self): self.title_edit.clear()

    def _setup_dnd(self): pass

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls(): event.acceptProposedAction()
        else: event.ignore()

    def dropEvent(self, event):
        paths=[u.toLocalFile() for u in event.mimeData().urls()]
        # Dropped files default to the not-found table when dropped over its region,
        # otherwise load them into the title list.
        pos=event.position().toPoint()
        target=self.childAt(pos)
        w=target
        in_table=False
        while w is not None:
            if w is self.nf_table or w is self.nf_table.viewport(): in_table=True; break
            w=w.parent()
        if in_table: self.load_not_found_paths(paths)
        else: self.load_list_paths(paths)
        event.acceptProposedAction()

    def log(self, msg):
        def _do():
            self.log_edit.append(str(msg))
            self.log_edit.moveCursor(self.log_edit.textCursor().MoveOperation.End)
        self.after(0,_do)

    def set_status(self, value):
        self.after(0,lambda:self.status.set(value))

    def schedule_refresh(self):
        if self._refresh_job is None:
            self._refresh_job=True
            self.after(150,self.refresh_table)

    def refresh_table(self):
        self._refresh_job=None
        items=self.visible_items()
        self.nf_table.setUpdatesEnabled(False)
        self.nf_table.setRowCount(len(items))
        self.rows={}
        for row,it in enumerate(items):
            self.rows[it["key"]]=row
            vals=[it["type"],it["title"],fmt_views(it["views"]),fmt_date(it["date"])]
            for col,val in enumerate(vals):
                cell=QTableWidgetItem(str(val))
                if col==2: cell.setTextAlignment(Qt.AlignmentFlag.AlignRight|Qt.AlignmentFlag.AlignVCenter)
                if col==0: cell.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                bg={"ok":"#E2F5E8","bad":"#FCE8E8","":"#FFFFFF"}.get(it["status"],"#FFFFFF")
                cell.setBackground(QColor(bg))
                self.nf_table.setItem(row,col,cell)
            actions=QWidget(); actions.setObjectName("cell"); al=QHBoxLayout(actions); al.setContentsMargins(3,2,3,2); al.setSpacing(4)
            copy=self._button("Copy",lambda _,item=it:self.copy_link(item),"#2563EB")
            ok=self._button("✓",lambda _,item=it:self.set_item_status(item,"ok"),"#16A34A")
            bad=self._button("✕",lambda _,item=it:self.set_item_status(item,"bad"),"#DC4545")
            copy.setFixedWidth(58); ok.setFixedWidth(42); bad.setFixedWidth(42)
            for _b in (copy, ok, bad): _b.setFixedHeight(32)
            al.addWidget(copy); al.addWidget(ok); al.addWidget(bad)
            self.nf_table.setCellWidget(row,4,actions)
            self.nf_table.setRowHeight(row,54 if len(it["title"])<95 else 70)
        self.nf_table.setUpdatesEnabled(True)
        self.update_counts(len(items))

    def update_counts(self, shown=None):
        n=len(self.nf); v=sum(1 for i in self.nf.values() if i["type"]=="video")
        ok=sum(1 for i in self.nf.values() if i["status"]=="ok")
        bad=sum(1 for i in self.nf.values() if i["status"]=="bad")
        txt=f"Total {n} | video {v} | short {n-v} | ✓ {ok} | ✕ {bad} | errors/captcha: {len(self.err)}"
        if shown is not None and shown != n: txt += f" | showing {shown}"
        self.lbl_counts.setText(txt)

    def toggle_sort(self, mode):
        if self.sort_mode != mode: self.sort_mode,self.sort_desc=mode,True
        elif self.sort_desc: self.sort_desc=False
        else: self.sort_mode,self.sort_desc="",True
        self._update_headers()
        self.refresh_table()

    def _paint_filter(self):
        self._update_headers()

    def set_item_status(self, item, st):
        new="" if item["status"]==st else st
        item["status"]=new
        self.meta.setdefault(item["key"],{})["status"]=new
        self.save_src(item["src"])
        self.refresh_table()


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

    def default_src(self) -> str:
        return self.source_name or self.yt_kw_var.get().strip() or "khong_tim_thay"

    def set_meta(self, key, **kw):
        m = self.meta.setdefault(key, {})
        for k, v in kw.items():
            if v not in (None, ""):
                m[k] = v

    def open_chrome(self):
        if launch_chrome():
            self.log("Chrome is ready (port 9222). Log in to TikTok if needed.")
        else:
            messagebox.showerror("Error", "Chrome not found. Edit CHROME_PATHS in the code.")

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
            messagebox.showinfo("No data",
                                "No video.txt / short.txt / error.txt or other .txt files found to load.")
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
        self.log(f"Loaded not-found table: {len(self.nf)} rows "
                 f"({len(dirs)} folders, {len(files)} loose files).")

    def choose_nf_files(self):
        ps = filedialog.askopenfilenames(title="Select not-found files (video.txt / short.txt ...)",
                                         filetypes=[("Text", "*.txt"), ("All", "*.*")])
        if ps:
            self.load_not_found_paths(list(ps))

    def choose_nf_dir(self):
        d = filedialog.askdirectory(title="Select a channel folder (or a folder containing several channels)")
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
            messagebox.showinfo("No data", "No .txt files found.")
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
        self.log(f"Loaded list: {len(lines)} lines from {len(files)} files.")

    def load_file(self):
        ps = filedialog.askopenfilenames(filetypes=[("Text", "*.txt"), ("All", "*.*")])
        if ps:
            self.load_list_paths(list(ps))

    def load_list_dir(self):
        d = filedialog.askdirectory(title="Select a folder (loads every .txt file inside)")
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
            self.log(f"⚠ Could not write folder {d}: {e}")

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
            self.log(f"Reloaded not-found table from last session: {len(self.nf)} rows.")
        self.refresh_table()

    def visible_items(self) -> list:
        items = list(self.nf.values())
        if self.filter_type:
            items = [i for i in items if i["type"] == self.filter_type]
        q = self.search_text
        if q:
            items = [i for i in items
                     if q in unicodedata.normalize("NFC", i["title"]).lower()]
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

    def update_counts(self, shown=None):
        n = len(self.nf)
        v = sum(1 for i in self.nf.values() if i["type"] == "video")
        ok = sum(1 for i in self.nf.values() if i["status"] == "ok")
        bad = sum(1 for i in self.nf.values() if i["status"] == "bad")
        txt = f"Total {n} | video {v} | short {n - v} | ✔ {ok} | ✖ {bad} | errors/captcha: {len(self.err)}"
        if shown is not None and shown != n:
            txt += f" | showing {shown}"
        self.lbl_counts.setText(txt)

    def toggle_filter(self, kind=None):
        """Cycle the Type filter: All -> Video -> Short -> All."""
        order = ["", "video", "short"]
        self.filter_type = order[(order.index(self.filter_type) + 1) % 3]
        self._update_headers()
        self.refresh_table()

    def on_cell_clicked(self, row, col):
        if col != 1:
            return
        cell = self.nf_table.item(row, 1)
        if cell is None:
            return
        self.clipboard_clear()
        self.clipboard_append(cell.text())
        self.log(f"Copied title: {cell.text()}")

    def on_search_changed(self, text):
        self.search_text = unicodedata.normalize("NFC", text).strip().lower()
        self.refresh_table()

    def clear_table(self):
        """Clear the table (in memory). Files on disk are left untouched."""
        self.nf.clear()
        self.err.clear()
        self.loaded_dirs.clear()
        self.nf_dirs = []
        self.save_state()
        self.refresh_table()
        self.log("Table cleared (files on disk were not deleted).")

    def on_header_clicked(self, col):
        if col == 0:
            self.toggle_filter()
        elif col == 2:
            self.toggle_sort("views")
        elif col == 3:
            self.toggle_sort("date")

    def _update_headers(self):
        arrow = " ▼" if self.sort_desc else " ▲"
        type_txt = {"video": "Type: Video", "short": "Type: Short"}.get(self.filter_type, "Type")
        labels = [
            (type_txt, "Click to filter: All → Video → Short"),
            ("Title", ""),
            ("Views" + (arrow if self.sort_mode == "views" else ""), "Click to sort by views"),
            ("Date" + (arrow if self.sort_mode == "date" else ""), "Click to sort by upload date"),
            ("Actions", ""),
        ]
        for i, (text, tip) in enumerate(labels):
            item = QTableWidgetItem(text)
            item.setToolTip(tip)
            self.nf_table.setHorizontalHeaderItem(i, item)

    def copy_link(self, item):
        vid = item["id"]
        if not vid:
            self.log("⚠ This row has no video ID, so there is no YouTube link.")
            return
        link = f"https://www.youtube.com/watch?v={vid}"
        self.clipboard_clear()
        self.clipboard_append(link)
        self.update_idletasks()
        self.log(f"Copied: {link}")

    def refresh_counts_only(self):
        shown = len(self.visible_items()) if (self.filter_type or self.search_text) else None
        self.update_counts(shown)

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
            self.set_status("A YouTube scan is running; wait for it to finish, then click Load Metadata.")
            return
        items = [it for it in self.nf.values() if it["id"]]
        skipped = len(self.nf) - len(items)
        if not items:
            messagebox.showinfo("No data",
                                "The table has no rows with a video ID to load metadata for.")
            return
        if skipped:
            self.log(f"Load Metadata: skipped {skipped} rows without a video ID.")
        self.yt_stop_event.clear()
        self._set_busy(True)
        self.meta_running = True
        self._meta_applied = 0
        fg, hv = PALETTE["red"]
        self.btn_meta.configure(text="■ Stop", fg_color=fg, hover_color=hv)
        self.btn_meta.setToolTip("Stop")
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
                            self.log(f"⚠ Could not get metadata for {vid}: {str(e)[:90]}")
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
            stopped = " (stopped)" if self.yt_stop_event.is_set() else ""
            msg = (f"Load Metadata done{stopped}: type changed {cnt['type']}, views added {cnt['views']}, "
                   f"dates added {cnt['date']}, errors {cnt['fail']}.")
            self.log(msg)
            self.set_status(msg)
        except Exception as e:
            self.log(f"⚠ Load Metadata error: {e}")
            self.set_status("Load Metadata error")
        finally:
            def done():
                self.flush_meta()
                self.meta_running = False
                fg, hv = PALETTE["purple"]
                self.btn_meta.configure(text="⟳ Load Metadata", fg_color=fg, hover_color=hv)
                self.btn_meta.setToolTip("Load Metadata")
                self._set_busy(False)
            self.after(0, done)

    def yt_start(self):
        if self.busy:
            return
        kw = self.yt_kw_var.get().strip()
        if not kw:
            messagebox.showwarning("Missing keyword", "Enter a YouTube keyword first.")
            return
        try:
            min_v = int(re.sub(r"[,\.\s]", "", self.yt_views_var.get()))
            max_t = int(self.yt_max_var.get())
            scan = int(self.yt_scan_var.get())
        except ValueError:
            messagebox.showerror("Error", "Min views / max titles / max scan must be numbers.")
            return
        if max_t < 1 or scan < 1:
            messagebox.showerror("Error", "Max titles and max scan must be ≥ 1.")
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
        self.set_status("Stopping...")

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
        return sorted(lines, key=key)

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
                    self.set_status(f"YouTube: scanned {scanned} | matched {found}/{max_t} "
                                    f"| language-filtered {skipped_lang}")
        except Exception as e:
            self.log(f"⚠ YouTube search error: {e}")
            self.set_status("YouTube search error")
        else:
            msg = (f"YouTube done: scanned {scanned}, added {found} (≥ {min_v:,} views), "
                   f"excluded {skipped_lang} non-English. Sorted by views.")
            self.log(msg)
            self.set_status(msg)
        finally:
            def done():
                self.sort_list_by_views()  # view cao ở trên
                self._set_busy(False)
            self.after(0, done)

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
            self.ch_dir.setText(d)
            self.ch_dir_var.set(d)

    def ch_start(self):
        if self.busy:
            return
        urls = [u for u in (chuan_hoa_base(l) for l in
                            self.txt_channels.get("1.0", "end").splitlines()) if u]
        if not urls:
            messagebox.showwarning("Missing channel", "Enter at least one channel link / @handle.")
            return
        try:
            limit = int(re.sub(r"[,\.\s]", "", self.ch_limit_var.get()))
            min_v = int(re.sub(r"[,\.\s]", "", self.ch_views_var.get()))
        except ValueError:
            messagebox.showerror("Error", "Limit and min views must be integers.")
            return
        if limit < 1:
            messagebox.showerror("Error", "Limit must be ≥ 1.")
            return
        tabs = [k for k, v in self.ch_tab_vars.items() if v.get()]
        if not tabs:
            messagebox.showerror("Error", "Select at least one tab to scan.")
            return
        out_dir = Path(self.ch_dir_var.get().strip() or BASE_DIR)
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            messagebox.showerror("Error", f"Could not create output folder: {e}")
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
                    self.log(f"⚠ Error on this channel: {e}")
                    continue

                names.append(name)
                folder = out_dir / ten_file_an_toan(name)   # thư mục tên kênh
                self.src_dirs[name] = folder
                lines = [make_file_line(f["id"], f["title"], {**f, "src": name}) for f in found]

                # lưu <thư mục kênh>/<tên kênh>.txt
                path = folder / (ten_file_an_toan(name) + ".txt")
                try:
                    write_lines(path, lines)
                    self.log(f"Channel '{name}': scanned {scanned}, saved {len(lines)} -> {path}")
                except OSError as e:
                    self.log(f"⚠ Could not write {path}: {e}")
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

            msg = (f"Channel scan done: {len(names)} channels, saved {total_saved} rows, "
                   f"added {total_added} to the list."
                   + (" (stopped)" if self.yt_stop_event.is_set() else ""))
            self.log(msg)
            self.set_status(msg)
        except Exception as e:
            self.log(f"⚠ Channel scan error: {e}")
            self.set_status("Channel scan error")
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
                    self.log(f"   Skipping tab (missing or error): {str(e)[:100]}")
                    continue

                if not name:
                    name = info.get("channel") or info.get("uploader") or info.get("title") or "kenh"
                    name = re.sub(r"\s*-\s*(Videos|Shorts|Live)$", "", name)
                    self.log(f"Channel: {name}")

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
                        self.set_status(f"Channel {name}: tab {CH_TABS[tab]} scanned {tab_scanned} | "
                                        f"matched {len(found)} | language-filtered {skipped_lang}")

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
            self.log(f"   ... ({len(found) - 10} more rows)")
        return name or "kenh", found, scanned

    def start(self):
        lines = [t.strip() for t in self.txt_titles.get("1.0", "end").splitlines() if t.strip()]
        lines = list(dict.fromkeys(lines))
        lines = self.sort_lines_by_views(lines)  # kiểm tra video view cao trước
        if not lines:
            messagebox.showwarning("Missing data", "Enter at least one title.")
            return
        if not launch_chrome():
            messagebox.showerror("Error", "Could not open Chrome in debug mode.")
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
            self.log(f"Results for '{s}' will be saved in folder: {self.dir_for(s)}")

        self.stop_event.clear()
        self.btn_start.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        threading.Thread(target=self.run_worker,
                         args=(lines, self.strip_tag_var.get(), default_src),
                         daemon=True).start()

    def stop(self):
        self.stop_event.set()
        self.set_status("Stopping...")

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
                    vtxt = f" ({v:,} views)" if isinstance(v, int) else ""
                    self.set_status(f"[{i}/{total}] Searching: {title}{vtxt}")

                    failed = captcha = False
                    try:
                        captions, captcha = self.search(page, title)
                    except Exception as e:
                        self.log(f"⚠ Error while searching '{title}': {e}")
                        captions, failed = [], True
                    if self.stop_event.is_set():
                        break

                    captions = captions[:self.top_n]  # chỉ xét N kết quả đầu
                    if failed or not captions:
                        why = "error" if failed else ("captcha" if captcha else "no results")
                        self.log(f"[{i}/{total}] ⚠ {why} -> error.txt: {title}")
                        result = "error"
                        err_count += 1
                    elif any(is_match(title, c, self.max_extra, strip_tag) for c in captions):
                        self.log(f"[{i}/{total}] ✓ Found: {title}")
                        result = "found"
                    else:
                        self.log(f"[{i}/{total}] ✗ No match in top {len(captions)}: {title}")
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
            self.set_status("Stopped." if self.stop_event.is_set()
                            else f"Done. Not found: {nf_count} | errors/captcha: {err_count} "
                                 f"(total {len(lines)})")
        except Exception as e:
            self.log(f"Fatal error: {e}")
            self.set_status("Error")
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
                        self.log("⚠ TikTok requires a captcha — solve it in the Chrome window...")
                        page.bring_to_front()
                        warned = True
                    deadline = time.time() + 8  # gia hạn trong lúc giải captcha
                page.wait_for_timeout(500)
        finally:
            page.remove_listener("response", on_response)

        return list(dict.fromkeys(captions)), captcha_seen


if __name__ == "__main__":
    app = QApplication([])
    window = App()
    window.show()
    app.exec()

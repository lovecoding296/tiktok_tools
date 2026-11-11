import os
import re
import json
import time
import socket
import threading
import subprocess
import unicodedata
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from urllib.parse import quote
from pathlib import Path

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


def keyword_file(kw: str) -> Path:
    """File lưu kết quả không tìm thấy, tên = từ khóa."""
    name = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", kw).strip(" .")[:100] or "khong_tim_thay"
    return BASE_DIR / f"{name}.txt"


def read_file_lines(path: Path) -> set:
    if not path.exists():
        return set()
    return {ln.strip() for ln in path.read_text(encoding="utf-8", errors="ignore").splitlines()
            if ln.strip()}


def append_to_file(path: Path, line: str):
    need_nl = False
    if path.exists() and path.stat().st_size > 0:
        with open(path, "rb") as f:
            f.seek(-1, os.SEEK_END)
            need_nl = f.read(1) not in (b"\n", b"\r")
    with open(path, "a", encoding="utf-8") as f:
        f.write(("\n" if need_nl else "") + line + "\n")


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


# ---------- GUI ----------
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("TikTok Title Checker")
        self.geometry("1000x780")

        self.stop_event = threading.Event()
        self.yt_stop_event = threading.Event()
        self.delay = 2.0
        self.max_extra = 2
        self.top_n = 5
        self.view_map: dict[str, int] = {}  # video_id -> lượt view (từ YouTube)

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

        self._build_ui()

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
        top = ttk.Frame(self, padding=8)
        top.pack(fill="x")
        ttk.Label(top, text="Delay (giây):").pack(side="left")
        ttk.Spinbox(top, from_=0, to=60, increment=0.5, width=5,
                    textvariable=self.delay_var).pack(side="left", padx=(4, 12))
        ttk.Label(top, text="Kiểm tra top:").pack(side="left")
        ttk.Spinbox(top, from_=1, to=30, width=4,
                    textvariable=self.topn_var).pack(side="left", padx=(4, 12))
        ttk.Label(top, text="Thừa tối đa (từ):").pack(side="left")
        ttk.Spinbox(top, from_=0, to=10, width=4,
                    textvariable=self.extra_var).pack(side="left", padx=(4, 12))
        ttk.Checkbutton(top, text="Bỏ qua #hashtag/@mention",
                        variable=self.strip_tag_var).pack(side="left", padx=(0, 12))
        ttk.Button(top, text="Mở Chrome", command=self.open_chrome).pack(side="left", padx=4)
        self.btn_start = ttk.Button(top, text="▶ Bắt đầu", command=self.start)
        self.btn_start.pack(side="left", padx=4)
        self.btn_stop = ttk.Button(top, text="■ Dừng", command=self.stop, state="disabled")
        self.btn_stop.pack(side="left", padx=4)

        self.status = tk.StringVar(value="Sẵn sàng")
        ttk.Label(self, textvariable=self.status, padding=(8, 0)).pack(anchor="w")

        main = ttk.PanedWindow(self, orient="horizontal")
        main.pack(fill="both", expand=True, padx=8, pady=8)

        left = ttk.Frame(main)

        # ---- Khung tìm tiêu đề YouTube ----
        yt = ttk.LabelFrame(left, text="Tìm tiêu đề YouTube")
        yt.pack(fill="x", padx=0, pady=(0, 6))
        yt.columnconfigure(1, weight=1)

        ttk.Label(yt, text="Từ khóa:").grid(row=0, column=0, sticky="w", padx=4, pady=3)
        e = ttk.Entry(yt, textvariable=self.yt_kw_var)
        e.grid(row=0, column=1, columnspan=3, sticky="we", padx=4, pady=3)
        e.bind("<Return>", lambda _: self.yt_start())

        ttk.Label(yt, text="View tối thiểu:").grid(row=1, column=0, sticky="w", padx=4)
        ttk.Entry(yt, textvariable=self.yt_views_var, width=10).grid(row=1, column=1, sticky="w", padx=4)
        ttk.Label(yt, text="Số tiêu đề tối đa:").grid(row=1, column=2, sticky="e")
        ttk.Entry(yt, textvariable=self.yt_max_var, width=6).grid(row=1, column=3, sticky="w", padx=4)

        ttk.Label(yt, text="Quét tối đa:").grid(row=2, column=0, sticky="w", padx=4, pady=3)
        ttk.Entry(yt, textvariable=self.yt_scan_var, width=10).grid(row=2, column=1, sticky="w", padx=4)
        ttk.Checkbutton(yt, text="Thêm vào danh sách (không xóa cũ)",
                        variable=self.yt_append_var).grid(row=2, column=2, columnspan=2, sticky="w")

        ttk.Checkbutton(yt, text="Chỉ lấy tiêu đề tiếng Anh",
                        variable=self.yt_english_var).grid(row=3, column=0, columnspan=2, sticky="w", padx=4)

        byt = ttk.Frame(yt)
        byt.grid(row=4, column=0, columnspan=4, sticky="w", padx=4, pady=(2, 6))
        self.btn_yt = ttk.Button(byt, text="🔍 Tìm YouTube", command=self.yt_start)
        self.btn_yt.pack(side="left")
        self.btn_yt_stop = ttk.Button(byt, text="Dừng tìm", command=self.yt_stop, state="disabled")
        self.btn_yt_stop.pack(side="left", padx=4)

        # ---- Danh sách tiêu đề ----
        lf_titles = ttk.LabelFrame(left, text="Danh sách (mỗi dòng: #id|tên video, hoặc chỉ tên video)")
        lf_titles.pack(fill="both", expand=True)
        self.txt_titles = tk.Text(lf_titles, wrap="word", undo=True)
        self.txt_titles.pack(fill="both", expand=True, padx=4, pady=4)
        bl = ttk.Frame(lf_titles)
        bl.pack(fill="x", padx=4, pady=(0, 4))
        ttk.Button(bl, text="Mở file .txt", command=self.load_file).pack(side="left")
        ttk.Button(bl, text="Lưu danh sách", command=self.save_titles).pack(side="left", padx=4)
        ttk.Button(bl, text="Xóa", command=lambda: self.txt_titles.delete("1.0", "end")).pack(side="left")
        main.add(left, weight=1)

        right = ttk.PanedWindow(main, orient="vertical")
        nf = ttk.LabelFrame(right, text="❌ KHÔNG tìm thấy trên TikTok (#id|tên video)")
        self.txt_notfound = tk.Text(nf, wrap="word", fg="#b00020")
        self.txt_notfound.pack(fill="both", expand=True, padx=4, pady=4)
        bn = ttk.Frame(nf)
        bn.pack(fill="x", padx=4, pady=(0, 4))
        ttk.Button(bn, text="Copy", command=self.copy_nf).pack(side="left")
        right.add(nf, weight=2)

        lf = ttk.LabelFrame(right, text="Log")
        self.txt_log = tk.Text(lf, wrap="word", height=10, state="disabled")
        self.txt_log.pack(fill="both", expand=True, padx=4, pady=4)
        right.add(lf, weight=1)
        main.add(right, weight=1)

    # ----- tiện ích -----
    def log(self, msg):
        def _do():
            self.txt_log.config(state="normal")
            self.txt_log.insert("end", msg + "\n")
            self.txt_log.see("end")
            self.txt_log.config(state="disabled")
        self.after(0, _do)

    def add_notfound(self, line):
        self.after(0, lambda: (self.txt_notfound.insert("end", line + "\n"),
                               self.txt_notfound.see("end")))

    def set_status(self, s):
        self.after(0, lambda: self.status.set(s))

    def load_file(self):
        p = filedialog.askopenfilename(filetypes=[("Text", "*.txt"), ("All", "*.*")])
        if p:
            self.txt_titles.delete("1.0", "end")
            self.txt_titles.insert("1.0", Path(p).read_text(encoding="utf-8", errors="ignore"))

    def save_titles(self):
        p = filedialog.asksaveasfilename(defaultextension=".txt", filetypes=[("Text", "*.txt")])
        if p:
            Path(p).write_text(self.txt_titles.get("1.0", "end").strip(), encoding="utf-8")

    def copy_nf(self):
        self.clipboard_clear()
        self.clipboard_append(self.txt_notfound.get("1.0", "end").strip())

    def open_chrome(self):
        if launch_chrome():
            self.log("Chrome đã sẵn sàng (cổng 9222). Hãy đăng nhập TikTok nếu cần.")
        else:
            messagebox.showerror("Lỗi", "Không tìm thấy Chrome. Hãy sửa CHROME_PATHS trong code.")

    # ----- sắp xếp theo view -----
    def sort_lines_by_views(self, lines: list[str]) -> list[str]:
        """View cao lên trên; dòng không có dữ liệu view xuống cuối (giữ thứ tự cũ)."""
        def key(line):
            vid, _ = parse_line(line)
            return -self.view_map.get(vid, -1) if vid else 1
        return sorted(lines, key=key)  # sorted ổn định

    def sort_list_by_views(self):
        lines = [t.strip() for t in self.txt_titles.get("1.0", "end").splitlines() if t.strip()]
        lines = self.sort_lines_by_views(lines)
        self.txt_titles.delete("1.0", "end")
        if lines:
            self.txt_titles.insert("1.0", "\n".join(lines) + "\n")

    # ----- tìm YouTube -----
    def yt_start(self):
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
        self.btn_yt.config(state="disabled")
        self.btn_yt_stop.config(state="normal")

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
        self.set_status("Đang dừng tìm YouTube...")

    def _append_line(self, line):
        content = self.txt_titles.get("1.0", "end-1c")
        prefix = "" if (not content or content.endswith("\n")) else "\n"
        self.txt_titles.insert("end", prefix + line + "\n")
        self.txt_titles.see("end")

    def yt_worker(self, kw, min_v, max_t, scan, seen_ids, seen_titles, only_en):
        opts = {
            "quiet": True,
            "no_warnings": True,
            "extract_flat": True,
            "lazy_playlist": True,
            "skip_download": True,
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
                                self.view_map[vid] = views
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
                self.btn_yt.config(state="normal")
                self.btn_yt_stop.config(state="disabled")
            self.after(0, done)

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
        out_path = keyword_file(self.yt_kw_var.get().strip())
        self.log(f"Tiêu đề không tìm thấy sẽ được ghi vào: {out_path}")
        self.txt_notfound.delete("1.0", "end")
        self.stop_event.clear()
        self.btn_start.config(state="disabled")
        self.btn_stop.config(state="normal")
        threading.Thread(target=self.run_worker,
                         args=(lines, self.strip_tag_var.get(), out_path),
                         daemon=True).start()

    def stop(self):
        self.stop_event.set()
        self.set_status("Đang dừng...")

    def finish(self):
        self.after(0, lambda: (self.btn_start.config(state="normal"),
                               self.btn_stop.config(state="disabled")))

    def save_notfound_line(self, out_path: Path, existing: set, line: str):
        """Hiện ở khung đỏ + ghi nối vào file ngay (bỏ qua dòng đã có trong file)."""
        self.add_notfound(line)
        if line in existing:
            return
        try:
            append_to_file(out_path, line)
            existing.add(line)
        except OSError as e:
            self.log(f"⚠ Không ghi được file {out_path.name}: {e}")

    # ----- worker -----
    def run_worker(self, lines, strip_tag, out_path):
        page = None
        existing = read_file_lines(out_path)
        try:
            with sync_playwright() as p:
                browser = p.chromium.connect_over_cdp(CDP_URL)
                ctx = browser.contexts[0]
                page = ctx.new_page()
                total, nf_count = len(lines), 0

                for i, line in enumerate(lines, 1):
                    if self.stop_event.is_set():
                        break
                    vid, title = parse_line(line)
                    if not title:
                        continue
                    v = self.view_map.get(vid)
                    vtxt = f" ({v:,} view)" if v else ""
                    self.set_status(f"[{i}/{total}] Đang tìm: {title}{vtxt}")
                    try:
                        captions = self.search(page, title)
                    except Exception as e:
                        self.log(f"⚠ Lỗi khi tìm '{title}': {e}")
                        captions = []

                    captions = captions[:self.top_n]  # chỉ xét N kết quả đầu
                    out_line = make_line(vid, title)  # giữ nguyên #id|tên
                    if not captions:
                        self.log(f"[{i}/{total}] ✗ Không có kết quả (có thể captcha): {title}")
                        self.save_notfound_line(out_path, existing, out_line)
                        nf_count += 1
                    elif any(is_match(title, c, self.max_extra, strip_tag) for c in captions):
                        self.log(f"[{i}/{total}] ✓ Có: {title}")
                    else:
                        self.log(f"[{i}/{total}] ✗ Không khớp top {len(captions)}: {title}")
                        self.save_notfound_line(out_path, existing, out_line)
                        nf_count += 1

                    if i < total:
                        end = time.time() + self.delay
                        while time.time() < end and not self.stop_event.is_set():
                            time.sleep(0.1)

                try:
                    page.close()  # chỉ đóng tab của tool, không đóng Chrome
                except Exception:
                    pass
            self.set_status("Đã dừng." if self.stop_event.is_set()
                            else f"Xong. Không tìm thấy: {nf_count}/{len(lines)} → {out_path.name}")
        except Exception as e:
            self.log(f"Lỗi nghiêm trọng: {e}")
            self.set_status("Lỗi")
        finally:
            self.finish()

    def search(self, page, title):
        captions: list[str] = []

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
                    if not warned:
                        self.log("⚠ TikTok yêu cầu captcha — hãy giải trong cửa sổ Chrome...")
                        page.bring_to_front()
                        warned = True
                    deadline = time.time() + 8  # gia hạn trong lúc giải captcha
                page.wait_for_timeout(500)
        finally:
            page.remove_listener("response", on_response)

        return list(dict.fromkeys(captions))


if __name__ == "__main__":
    App().mainloop()
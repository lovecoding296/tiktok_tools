# pip install requests send2trash PyQt6
import os, re, csv, io, json, time, shutil, threading, queue, mimetypes, requests
import sys
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (QApplication, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QLineEdit,
                             QPushButton, QSpinBox, QTabWidget, QScrollArea, QPlainTextEdit, QMessageBox, QFileDialog)
from concurrent.futures import ThreadPoolExecutor
from urllib3.fields import RequestField
from urllib3.filepost import choose_boundary
from send2trash import send2trash

UPLOAD_URL = "http://{ip}/upload.json"
UPLOAD_FIELD = "files[]"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE_DIR, "state.json")     # trạng thái chạy: sent / failed / pending
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")   # cấu hình: số máy, số video/folder, IP, số folder/máy, đường dẫn folder xây kênh + folder đã cut xong
VIDEO_EXT = (".mp4", ".mov", ".m4v", ".mkv", ".avi")
START_NUM = 1                      # DỌN: m = số video đứng trước video có số này (không có thì đếm cả folder); chuyển m % per_folder video cuối vào DELETE_LATER_DIR
SHORT_DIR = r"D:\short videos"         # DỌN: video không có số -> chuyển vào SHORT_DIR/<folder cha của video>/
DELETE_LATER_DIR = r"D:\delete later"  # DỌN: video dư -> chuyển vào DELETE_LATER_DIR/<folder ông (máy)>/<folder cha (kênh)>/<video>
MAX_MACHINES = 200
MAX_PER_FOLDER = 50
MAX_PARALLEL = 0                  # số máy gửi cùng lúc tối đa (0 = tất cả máy cùng lúc). Nếu mạng/router nghẽn thì thử 10-20
MAX_FOLDERS = 100                  # số folder kênh tối đa mỗi máy (đọc từ Google Sheet)
SHEET_COL_MACHINE = "I"            # mặc định: cột số thứ tự máy (sửa được trên UI, lưu trong config.json)
SHEET_COL_VIEW = "K"               # mặc định: cột view, ô này RỖNG thì tính thêm 1 folder cho máy (sửa được trên UI)

lock = threading.Lock()        # chặn chạy CHUYỂN / DỌN / RETRY cùng lúc
save_lock = threading.Lock()   # chỉ 1 luồng ghi file tại một thời điểm
logq = queue.Queue()

def log(msg):
    logq.put(msg)

def load_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}

def to_int(v, default, lo, hi):
    try:
        v = int(v)
    except (TypeError, ValueError):
        v = default
    return max(lo, min(hi, v))

# ===== CONFIG (lưu trong config.json) =====
# cfg = {"root": str, "source": str, "num_machines": int, "per_folder": int, "ips": [str, ...], "sheet_url": str, "sheet_col_machine": str, "sheet_col_view": str, "sheet_counts": {"số máy": int}}
# root:   folder xây kênh chứa folder máy (1, 2, 3...), mỗi folder máy chứa các folder kênh (có lưu, mở lại tool tự điền)
# source: folder đã cut xong chứa các folder kênh dự trữ, dùng để bù khi folder rỗng bị xoá (có lưu, mở lại tool tự điền)
# ips giữ nguyên cả phần dư khi giảm số máy, tăng lại sẽ thấy IP cũ.
# sheet_url:    link Google Sheet (phải chia sẻ "Bất kỳ ai có đường liên kết" xem được).
# sheet_col_machine / sheet_col_view: tên cột (chữ cái, vd "I", "K"). Cột số máy = sheet_col_machine; cột view RỖNG -> +1 folder cho máy đó.
# sheet_counts: {"số máy": số folder} lần đọc sheet gần nhất (DỌN luôn đọc lại sheet trước khi cân folder).
#   DỌN: máy thiếu -> bù từ folder đã cut xong, máy thừa -> chuyển folder cuối về folder đã cut xong. Máy không có dòng nào trong sheet = 0 folder.
# sent:    {"máy/folder": [tên file]} gửi xong nhưng không bỏ được vào Thùng rác (tránh gửi lại)
# failed:  [số máy] máy chưa gửi xong (mất kết nối / lỗi HTTP), chờ RETRY
# pending: {"số máy": {"máy/folder": [tên file]}} kế hoạch của lượt gửi đang dang dở.
raw_state = load_json(STATE_FILE)
state = {"sent": {}, "failed": [], "pending": {}}
for k in state:
    if k in raw_state:
        state[k] = raw_state[k]
if not isinstance(state["failed"], list):
    state["failed"] = []
if not isinstance(state["pending"], dict):
    state["pending"] = {}
if not isinstance(state["sent"], dict):
    state["sent"] = {}

cfg = {"root": "", "source": "", "num_machines": 20, "per_folder": 2, "ips": [], "sheet_url": "", "sheet_col_machine": SHEET_COL_MACHINE, "sheet_col_view": SHEET_COL_VIEW, "sheet_counts": {}}
if os.path.exists(CONFIG_FILE):
    cfg.update(load_json(CONFIG_FILE))
else:                                   # lần đầu: lấy ips từ state.json cũ nếu có
    if isinstance(raw_state.get("ips"), list):
        cfg["ips"] = raw_state["ips"]
cfg["root"] = cfg["root"] if isinstance(cfg.get("root"), str) else ""        # nhớ folder xây kênh từ lần mở trước
cfg["source"] = cfg["source"] if isinstance(cfg.get("source"), str) else ""  # nhớ folder đã cut xong từ lần mở trước
cfg["num_machines"] = to_int(cfg.get("num_machines"), 20, 1, MAX_MACHINES)
cfg["per_folder"] = to_int(cfg.get("per_folder"), 2, 1, MAX_PER_FOLDER)
cfg["ips"] = [str(x) for x in cfg["ips"]] if isinstance(cfg.get("ips"), list) else []
cfg["ips"] += [""] * (cfg["num_machines"] - len(cfg["ips"]))
for _k in ("default_folders", "custom_folders", "folder_counts"):      # bản cũ setup bằng tay -> bỏ, giờ lấy từ Google Sheet
    cfg.pop(_k, None)
cfg["sheet_url"] = cfg["sheet_url"].strip() if isinstance(cfg.get("sheet_url"), str) else ""
for _k, _d in (("sheet_col_machine", SHEET_COL_MACHINE), ("sheet_col_view", SHEET_COL_VIEW)):
    _v = cfg.get(_k)
    cfg[_k] = _v.strip().upper() if isinstance(_v, str) and re.fullmatch(r"[A-Za-z]{1,3}", _v.strip()) else _d
_sc = cfg.get("sheet_counts")
cfg["sheet_counts"] = {str(int(k)): to_int(v, 0, 0, MAX_FOLDERS) for k, v in (_sc.items() if isinstance(_sc, dict) else [])
                       if str(k).isdigit() and 1 <= int(k) <= MAX_MACHINES}

def nat(s):
    """Khoá sắp xếp tự nhiên: 'a-2' < 'a-10'."""
    return [int(p) if i % 2 else p for i, p in enumerate(re.split(r"(\d+)", s.lower()))]

def write_json(path, obj):
    """Ghi JSON an toàn: file tạm + os.replace, thử lại khi bị khoá. Không bao giờ văng lỗi."""
    with save_lock:
        tmp = path + ".tmp"
        err = None
        for _ in range(6):
            try:
                data = json.dumps(obj, ensure_ascii=False, indent=2)
                with open(tmp, "w", encoding="utf-8") as f:
                    f.write(data)
                os.replace(tmp, path)
                return True
            except (OSError, RuntimeError) as e:
                err = e
                time.sleep(0.3)
        log(f"CẢNH BÁO: không ghi được {path}: {err}. "
            f"Đóng chương trình đang mở file đó, hoặc chuyển tool ra thư mục không đồng bộ OneDrive.")
        return False

def save_state():
    return write_json(STATE_FILE, state)

def save_config():
    return write_json(CONFIG_FILE, dict(cfg))

def clean_ip(s):
    return re.sub(r"^https?://", "", s.strip()).strip("/")

def ip_of(m):
    """IP của máy m (1-based). Rỗng nếu chưa điền hoặc máy nằm ngoài số máy hiện tại."""
    if 1 <= m <= cfg["num_machines"] and m <= len(cfg["ips"]):
        return clean_ip(cfg["ips"][m - 1])
    return ""

def dup_ips():
    """{ip: [các máy dùng chung]} cho những IP bị trùng (chỉ xét máy trong số máy hiện tại)."""
    seen = {}
    for m in range(1, cfg["num_machines"] + 1):
        ip = ip_of(m)
        if ip:
            seen.setdefault(ip.lower(), []).append(m)
    return {ip: ms for ip, ms in seen.items() if len(ms) > 1}

def abort_if_dup():
    """True nếu có IP trùng (đã log) -> caller phải dừng, không gửi gì cả."""
    d = dup_ips()
    if not d:
        return False
    for ip, ms in d.items():
        log(f"!!! IP TRÙNG {ip}: máy {', '.join(map(str, ms))}")
    log("Dừng: mỗi máy phải có IP riêng. Sửa IP trùng rồi chạy lại.")
    return True

def last_num(name):
    """Số cuối cùng (nguyên cả số) trong tên file, bỏ đuôi. Không có số -> None."""
    nums = re.findall(r"\d+", os.path.splitext(name)[0])
    return int(nums[-1]) if nums else None

MAX_NUM = 50   # số đọc được phải < MAX_NUM, từ 50 trở lên coi như KHÔNG có số

# Chỉ 4 dạng số được công nhận (tên video: "... ALL PARTS Pt. 1 #tag", "... (1) #tag", "...-1 #tag", "... 1 편 #tag"):
NUM_PATTERNS = (
    re.compile(r"\bPt\.\s*(\d+)(?!\d)", re.I),        # Pt. n
    re.compile(r"\((\d+)\)"),                         # (n)
    re.compile(r"(?<=[^\W\d_])-(\d+)(?!\d)"),          # -n  (sau một chữ cái, vd PARTS-1)
    re.compile(r"(\d+)\s*편"),                          # n 편
)

def video_num(name):
    """Số thứ tự của VIDEO, chỉ đọc theo 4 dạng: 'Pt. n', '(n)', '-n', 'n 편' (bỏ đuôi file và các #hashtag).
    Nhiều chỗ khớp -> lấy chỗ nằm cuối cùng. Không khớp hoặc giá trị >= MAX_NUM -> None."""
    stem = re.sub(r"#\S+", " ", os.path.splitext(name)[0])      # bỏ hashtag để số trong hashtag không bị đọc nhầm
    best = None
    for pat in NUM_PATTERNS:
        for m in pat.finditer(stem):
            if best is None or m.start() > best[0]:
                best = (m.start(), int(m.group(1)))
    if best is None or best[1] >= MAX_NUM:
        return None
    return best[1]

def same_path(a, b):
    """True nếu 2 đường dẫn (đều không rỗng) trỏ cùng một folder."""
    a, b = a.strip(), b.strip()
    return bool(a) and bool(b) and os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))

def same_parent(a, b):
    """True nếu 2 đường dẫn nằm trong cùng một folder cha (hoặc 1 trong 2 rỗng -> chưa so sánh)."""
    a, b = a.strip(), b.strip()
    if not a or not b:
        return True
    return os.path.normcase(os.path.dirname(os.path.abspath(a))) == os.path.normcase(os.path.dirname(os.path.abspath(b)))

def check_paths(need_source):
    """True nếu đường dẫn hợp lệ: đã chọn folder xây kênh (và folder đã cut xong nếu cần), 2 folder phải khác nhau và chung folder cha."""
    root, src = cfg["root"].strip(), cfg["source"].strip()
    if not root or not os.path.isdir(root):
        log("Chưa chọn folder xây kênh (hoặc folder không tồn tại)")
        return False
    if need_source and (not src or not os.path.isdir(src)):
        log("Chưa chọn folder đã cut xong (hoặc folder không tồn tại)")
        return False
    if same_path(root, src):
        log("!!! Folder đã cut xong và folder xây kênh phải khác nhau -> dừng")
        return False
    if not same_parent(root, src):
        log("!!! Folder đã cut xong và folder xây kênh phải nằm chung 1 folder cha -> dừng")
        return False
    return True

def folders_of(m):
    """Số folder kênh mong muốn của máy m (1-based) theo Google Sheet; máy không có dòng nào -> 0."""
    return cfg["sheet_counts"].get(str(m), 0)

def col_index(letters):
    """Tên cột chữ cái -> chỉ số 0-based (A=0, I=8, K=10, AA=26). Không hợp lệ -> None."""
    letters = (letters or "").strip().upper()
    if not re.fullmatch(r"[A-Z]{1,3}", letters):
        return None
    n = 0
    for ch in letters:
        n = n * 26 + ord(ch) - 64
    return n - 1

def fetch_sheet_counts(url, col_machine, col_view):
    """Đọc Google Sheet (CSV export, sheet phải chia sẻ 'Bất kỳ ai có đường liên kết').
    Cột col_machine = số máy, ô cột col_view RỖNG -> tính thêm 1 folder cho máy đó. Trả về {"số máy": số folder}."""
    im, iv = col_index(col_machine), col_index(col_view)
    if im is None or iv is None:
        raise ValueError("Tên cột không hợp lệ (nhập chữ cái, vd I, K)")
    mid = re.search(r"/d/([\w-]+)", url)
    if not mid:
        raise ValueError("Link Google Sheet không hợp lệ (không thấy /d/<id>)")
    gid = re.search(r"[#&?]gid=(\d+)", url)
    export = f"https://docs.google.com/spreadsheets/d/{mid.group(1)}/export?format=csv" + (f"&gid={gid.group(1)}" if gid else "")
    r = requests.get(export, timeout=30)
    r.raise_for_status()
    if "text/html" in r.headers.get("Content-Type", "").lower():
        raise ValueError("Không đọc được sheet: hãy chia sẻ sheet ở chế độ 'Bất kỳ ai có đường liên kết' (người xem)")
    counts = {}
    for row in csv.reader(io.StringIO(r.content.decode("utf-8-sig"))):
        if len(row) <= im:
            continue
        v = row[im].strip()
        if not v.isdigit() or not 1 <= int(v) <= MAX_MACHINES:      # bỏ dòng tiêu đề / dòng không phải số máy
            continue
        k = str(int(v))
        counts.setdefault(k, 0)
        view = row[iv].strip() if len(row) > iv else ""
        if not view:
            counts[k] = min(counts[k] + 1, MAX_FOLDERS)
    return counts

sheet_dirty = [False]            # luồng khác đọc sheet xong -> báo GUI vẽ lại bảng

def refresh_sheet():
    """Đọc sheet theo link đang lưu, cập nhật cfg['sheet_counts']. True nếu thành công (chạy được trong luồng nền)."""
    url = cfg["sheet_url"].strip()
    if not url:
        log("!!! Chưa nhập link Google Sheet (tab Setup kênh)")
        return False
    try:
        counts = fetch_sheet_counts(url, cfg["sheet_col_machine"], cfg["sheet_col_view"])
    except Exception as e:
        log(f"!!! Lỗi đọc Google Sheet: {e}")
        return False
    cfg["sheet_counts"] = counts
    save_config()
    sheet_dirty[0] = True
    log(f"Đã đọc Google Sheet (cột số máy {cfg['sheet_col_machine']}, cột view {cfg['sheet_col_view']}): {len(counts)} máy, tổng {sum(counts.values())} folder")
    return True

def machine_folders():
    """{số máy: tên folder máy} lấy từ các folder con của folder xây kênh (tên có số 1..num_machines)."""
    res = {}
    n = cfg["num_machines"]
    with os.scandir(cfg["root"]) as it:
        for e in sorted((e for e in it if e.is_dir()), key=lambda e: nat(e.name)):
            m = last_num(e.name)
            if m is not None and 1 <= m <= n and m not in res:
                res[m] = e.name
    return res

def sub_folders(mname):
    """Các folder con trong folder máy -> đường dẫn tương đối 'máy/folder'."""
    with os.scandir(os.path.join(cfg["root"], mname)) as it:
        return [os.path.join(mname, e.name) for e in sorted((e for e in it if e.is_dir()),
                                                            key=lambda e: nat(e.name))]

def list_videos(rel):
    sent = set(state["sent"].get(rel, []))
    try:
        with os.scandir(os.path.join(cfg["root"], rel)) as it:
            return sorted((e.name for e in it
                           if e.is_file() and e.name.lower().endswith(VIDEO_EXT)
                           and e.name not in sent), key=nat)
    except OSError:
        return []

def dir_has_videos(path, default=True):
    """True nếu folder có ít nhất 1 file video. Không đọc được -> trả về `default`."""
    try:
        with os.scandir(path) as it:
            return any(e.is_file() and e.name.lower().endswith(VIDEO_EXT) for e in it)
    except OSError:
        return default

def has_videos(rel):
    """True nếu folder còn ít nhất 1 file video trên đĩa (kể cả file kẹt trong 'sent')."""
    return dir_has_videos(os.path.join(cfg["root"], rel), True)   # không đọc được -> không coi là rỗng

def move_delete_later(rel, name, why):
    """Chuyển 1 video vào DELETE_LATER_DIR/<rel>/<video>, rel = 'máy/folder' (giữ cả folder cha và folder ông).
    Trùng tên file thì thêm hậu tố (2), (3)... True nếu thành công."""
    src = os.path.join(cfg["root"], rel, name)
    dest_dir = os.path.join(DELETE_LATER_DIR, rel)
    try:
        os.makedirs(dest_dir, exist_ok=True)
        dest = os.path.join(dest_dir, name)
        if os.path.exists(dest):
            stem, ext = os.path.splitext(name)
            k = 2
            while os.path.exists(dest):
                dest = os.path.join(dest_dir, f"{stem} ({k}){ext}")
                k += 1
        shutil.move(src, dest)
        log(f"Dọn ({why}) -> {dest}")
        return True
    except Exception as e:
        state["sent"].setdefault(rel, []).append(name)
        log(f"Không chuyển được {rel}/{name} sang {DELETE_LATER_DIR}: {e}")
        return False

def move_short(rel, name):
    """Video không có số -> SHORT_DIR/<tên folder cha của video>/<video>. Trùng tên file thì ghi đè. True nếu thành công."""
    src = os.path.join(cfg["root"], rel, name)
    dest_dir = os.path.join(SHORT_DIR, os.path.basename(rel))
    try:
        os.makedirs(dest_dir, exist_ok=True)
        dest = os.path.join(dest_dir, name)
        if os.path.exists(dest):                          # trùng tên file -> ghi đè
            os.remove(dest)
        shutil.move(src, dest)
        log(f"Dọn (không có số) -> {dest}")
        return True
    except Exception as e:
        state["sent"].setdefault(rel, []).append(name)
        log(f"Không chuyển được {rel}/{name} sang {SHORT_DIR}: {e}")
        return False

def clean_head(rel):
    """1) Video KHÔNG có số -> chuyển sang SHORT_DIR/<folder cha của video>/.
    2) m = số video (có số) đứng trước video số 1 đầu tiên (folder không có video số 1 thì m = toàn bộ video có số),
       n = số video/folder (per_folder trong config.json). Dư r = m % n -> chuyển r video cuối của nhóm đó vào DELETE_LATER_DIR
       (vd m=9, n=2 -> chuyển 1 video cuối; m=9, n=3 -> không chuyển).
    Trả về số video đã dọn."""
    n = 0
    numbered = []
    for name in list_videos(rel):
        if video_num(name) is None:
            n += move_short(rel, name)
        else:
            numbered.append(name)
    group = []
    for name in numbered:
        if video_num(name) == START_NUM:
            break
        group.append(name)
    m = len(group)
    per = max(cfg["per_folder"], 1)
    r = m % per
    if r:
        log(f"Folder {rel}: m={m}, n={per} -> dư {r}, chuyển {r} video cuối vào delete later")
        for name in group[-r:]:
            n += move_delete_later(rel, name, f"dư cuối, m={m} n={per}")
    return n

def available(rel, per_folder):
    """N video đầu của folder. Video đầu KHÔNG có số (không theo dạng Pt. n / (n) / -n / n 편) -> chỉ lấy đúng 1 video đó.
    Đang lấy mà gặp video không có số -> dừng trước nó (nó sẽ được gửi riêng ở lượt sau)."""
    files = list_videos(rel)
    if not files:
        return []
    if video_num(files[0]) is None:
        return files[:1]
    out = []
    for name in files[:per_folder]:
        if video_num(name) is None:
            break
        out.append(name)
    return out

def build_items(mname):
    """[(rel, [file,...]), ...] cho 1 máy: N video đầu của mỗi folder trong folder máy."""
    per_folder = cfg["per_folder"]
    items = []
    for rel in sub_folders(mname):
        files = available(rel, per_folder)
        if files:
            items.append((rel, files))
    return items

def pending_items(m):
    """Kế hoạch dang dở của máy m, chỉ giữ file còn trên PC và chưa nằm trong 'sent'."""
    items = []
    for rel, names in state["pending"].get(str(m), {}).items():
        sent = set(state["sent"].get(rel, []))
        files = [n for n in names
                 if n not in sent and os.path.isfile(os.path.join(cfg["root"], rel, n))]
        if files:
            items.append((rel, files))
    return items

def set_pending(m, items):
    state["pending"][str(m)] = {rel: list(files) for rel, files in items}

def settle(jobs, results):
    """Xử lý kết quả: máy gửi xong hết -> xoá pending. Trả về list máy CHƯA xong
    (mất kết nối hoặc có file lỗi HTTP) -> giữ pending để RETRY gửi đúng phần còn lại."""
    bad = []
    for (m, ip, items), (res, dead) in zip(jobs, results):
        done = dict(res)
        if dead or any(done.get(rel, 0) < len(files) for rel, files in items):
            bad.append(m)
        else:
            state["pending"].pop(str(m), None)
    return bad

class StreamUpload:
    """Body multipart/form-data đọc từng khúc 1MB từ đĩa.
    requests.post(files=...) nạp CẢ FILE vào RAM rồi mới gửi (video nặng x nhiều máy = đầy RAM, bắt đầu gửi chậm).
    Dạng này gửi ngay, RAM gần như không đổi. Header/boundary dựng bằng đúng hàm của urllib3 nên byte gửi đi giống hệt cũ."""
    CHUNK = 1024 * 1024

    def __init__(self, path, name, ctype):
        rf = RequestField(name=UPLOAD_FIELD, data=b"", filename=name)
        rf.make_multipart(content_type=ctype)
        boundary = choose_boundary()
        self.head = ("--%s\r\n" % boundary).encode() + rf.render_headers().encode("utf-8")
        self.tail = ("\r\n--%s--\r\n" % boundary).encode()
        self.path = path
        self.size = os.path.getsize(path)
        self.content_type = "multipart/form-data; boundary=" + boundary

    def __len__(self):
        return len(self.head) + self.size + len(self.tail)

    def __iter__(self):
        yield self.head
        with open(self.path, "rb") as f:
            while True:
                b = f.read(self.CHUNK)
                if not b:
                    break
                yield b
        yield self.tail

def trash_sent(tag, rel, name, path, info):
    """Chạy ở luồng riêng (1 luồng duy nhất): máy gửi tiếp video sau, không phải đợi Thùng rác."""
    try:
        send2trash(os.path.abspath(path))
        log(f"{tag} OK {info} + vào Thùng rác: {rel}/{name}")
    except Exception as e:
        state["sent"].setdefault(rel, []).append(name)
        log(f"{tag} OK {info} nhưng không bỏ được vào Thùng rác {rel}/{name}: {e}")

def upload_one(m, ip, items, trasher):
    """-> ([(rel, số video OK), ...], dead). dead=True nếu máy không kết nối được."""
    tag = f"[Máy {m} | {ip}]"
    res = []
    dead = False
    for rel, files in items:
        ok = 0
        for name in files:
            if dead:
                break
            path = os.path.join(cfg["root"], rel, name)
            try:
                ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
                body = StreamUpload(path, name, ctype)
                t0 = time.time()
                r = requests.post(UPLOAD_URL.format(ip=ip), data=body,
                                  headers={"Referer": f"http://{ip}/", "Content-Type": body.content_type},
                                  timeout=(10, 1800))
                if r.status_code != 200:
                    log(f"{tag} LỖI {r.status_code}  {rel}/{name} (giữ lại trên PC)")
                    continue
                ok += 1
                dt = max(time.time() - t0, 0.001)
                mb = body.size / 1048576
                trasher.submit(trash_sent, tag, rel, name, path, f"{mb:.0f}MB/{dt:.0f}s ({mb / dt:.1f}MB/s)")
            except requests.exceptions.ConnectionError:
                dead = True
                log(f"{tag} KHÔNG KẾT NỐI ĐƯỢC -> bỏ qua máy này (kiểm tra VLC đang mở, "
                    f"màn hình không khoá, đúng IP). Video giữ lại trên PC, bấm RETRY để gửi lại")
            except Exception as e:
                log(f"{tag} LỖI {rel}/{name}: {e} (giữ lại trên PC)")
        res.append((rel, ok))
    return res, dead

def run_jobs(jobs):
    """jobs = [(m, ip, items), ...] chạy song song mỗi máy 1 luồng; Thùng rác xử lý nền bằng 1 luồng chung."""
    workers = min(len(jobs), MAX_PARALLEL) if MAX_PARALLEL > 0 else len(jobs)
    trasher = ThreadPoolExecutor(max_workers=1)
    try:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            return list(ex.map(lambda j: upload_one(*j, trasher), jobs))
    finally:
        trasher.shutdown(wait=True)             # đợi dọn Thùng rác xong rồi mới settle / lưu state

def summarize(jobs, results, title):
    total_ok = sum(ok for res, _ in results for _, ok in res)
    total_all = sum(len(files) for _, _, items in jobs for _, files in items)
    log(f"=== {title}: {total_ok}/{total_all} video thành công ({len(jobs)} máy) ===")

def refill_machine(mname, count):
    """Chuyển `count` folder kênh (có video) từ folder đã cut xong sang folder máy `mname`.
    Lấy theo thứ tự tên tự nhiên. Trả về số folder đã chuyển."""
    src = cfg["source"].strip()
    if not src or not os.path.isdir(src):
        log(f"Folder đã cut xong không tồn tại: '{src}' -> không bù được {count} folder cho '{mname}'")
        return 0
    if same_path(src, cfg["root"]):
        log("Folder đã cut xong trùng folder xây kênh -> bỏ qua, không bù folder")
        return 0
    try:
        with os.scandir(src) as it:
            cands = sorted((e.path for e in it if e.is_dir() and dir_has_videos(e.path, False)),
                           key=lambda p: nat(os.path.basename(p)))
    except OSError as e:
        log(f"Không đọc được folder đã cut xong {src}: {e}")
        return 0

    moved = 0
    for p in cands:
        if moved >= count:
            break
        base = os.path.basename(p)
        dest = os.path.join(cfg["root"], mname, base)
        k = 2
        while os.path.exists(dest):                       # trùng tên -> thêm hậu tố
            dest = os.path.join(cfg["root"], mname, f"{base} ({k})")
            k += 1
        try:
            shutil.move(p, dest)
            moved += 1
            state["sent"].pop(os.path.join(mname, os.path.basename(dest)), None)
            log(f"Bù folder: {p} -> {mname}/{os.path.basename(dest)}")
        except Exception as e:
            log(f"Không chuyển được {p}: {e}")
    if moved < count:
        log(f"!!! '{mname}': chỉ bù được {moved}/{count} folder (folder đã cut xong hết folder có video)")
    return moved

def forget_folder(rel):
    """Xoá dấu vết state (sent / pending) của folder `rel` khi folder bị xoá hoặc chuyển đi."""
    state["sent"].pop(rel, None)
    for pend in state["pending"].values():
        if isinstance(pend, dict):
            pend.pop(rel, None)

def return_to_source(rels):
    """Chuyển các folder kênh (đường dẫn tương đối 'máy/folder') từ folder xây kênh về folder đã cut xong.
    Trả về số folder đã chuyển."""
    src = cfg["source"].strip()
    moved = 0
    for rel in rels:
        base = os.path.basename(rel)
        dest = os.path.join(src, base)
        k = 2
        while os.path.exists(dest):                       # trùng tên -> thêm hậu tố
            dest = os.path.join(src, f"{base} ({k})")
            k += 1
        try:
            shutil.move(os.path.join(cfg["root"], rel), dest)
            moved += 1
            forget_folder(rel)
            log(f"Thừa folder: {rel} -> {dest}")
        except Exception as e:
            log(f"Không chuyển được {rel} về folder đã cut xong: {e}")
    return moved

def do_clean():
    if not lock.acquire(blocking=False):
        return log("Đang chạy, đợi xong đã")
    try:
        save_config()
        if not check_paths(True):
            return
        if not refresh_sheet():                            # cần số folder mỗi máy từ sheet, lỗi thì dừng (không dọn bằng số cũ)
            return
        try:
            machines = machine_folders()
        except Exception as e:
            return log(f"Lỗi folder xây kênh: {e}")
        if not machines:
            return log(f"Không thấy folder máy (tên có số 1..{cfg['num_machines']}) trong folder xây kênh")
        total = 0
        empties = []                                       # (tên folder máy, rel, path)
        for m, mname in machines.items():
            try:
                for rel in sub_folders(mname):
                    total += clean_head(rel)
                    if not has_videos(rel):
                        empties.append((mname, rel, os.path.normpath(os.path.join(cfg["root"], rel))))
            except Exception as e:
                log(f"Lỗi folder máy {mname}: {e}")
        log(f"=== Dọn xong: {total} video đã chuyển (không số -> short videos, dư -> delete later) ({len(machines)} máy) ===")

        if empties:
            log(f"!!! {len(empties)} folder RỖNG (không còn video):")
            for _, _, path in empties:
                log(f"    RỖNG: {path}")
            for _, rel, path in empties:                   # xoá folder rỗng (vào Thùng rác, khôi phục được)
                try:
                    send2trash(os.path.abspath(path))
                    forget_folder(rel)
                except Exception as e:
                    log(f"Không xoá được folder rỗng {path}: {e}")
        else:
            log("Không có folder nào rỗng")

        # cân số folder mỗi máy theo Google Sheet: thiếu -> bù từ folder đã cut xong, thừa -> trả về folder đã cut xong
        log(f"=== Cân số folder theo Google Sheet (folder đã cut xong: {cfg['source']}) ===")
        missing = [m for m in range(1, cfg["num_machines"] + 1) if m not in machines]
        if missing:
            log(f"Chưa có folder máy: {', '.join(map(str, missing))} -> bỏ qua")
        tot_in = tot_out = 0
        for m, mname in machines.items():
            target = folders_of(m)
            try:
                subs = sub_folders(mname)
            except Exception as e:
                log(f"Lỗi folder máy {mname}: {e}")
                continue
            cur = len(subs)
            if cur == target:
                continue
            log(f"Máy {m}: có {cur} folder, cần {target}")
            if cur < target:
                tot_in += refill_machine(mname, target - cur)
            else:
                tot_out += return_to_source(subs[target:])   # trả về các folder cuối
        log(f"=== Cân folder xong: bù {tot_in}, trả về folder đã cut xong {tot_out} ===")
    finally:
        try:
            save_state()
        finally:
            lock.release()

def do_send():
    if not lock.acquire(blocking=False):
        return log("Đang chạy, đợi xong đã")
    try:
        save_config()
        if not check_paths(False):
            return
        if abort_if_dup():                                  # IP trùng -> không gửi gì cả
            return
        save_state()
        try:
            machines = machine_folders()
        except Exception as e:
            return log(f"Lỗi folder xây kênh: {e}")

        jobs = []
        for m in range(1, cfg["num_machines"] + 1):
            ip = ip_of(m)
            if not ip:
                continue
            if m not in machines:
                log(f"Máy {m} có IP nhưng không có folder '{m}' trong folder xây kênh -> bỏ qua")
                continue
            try:
                items = []
                if str(m) in state["pending"]:          # máy đang dở: gửi nốt kế hoạch cũ
                    items = pending_items(m)
                    if items:
                        log(f"Máy {m}: gửi nốt kế hoạch dang dở (không lấy thêm video mới)")
                    else:
                        state["pending"].pop(str(m), None)   # kế hoạch cũ đã xong hết
                if not items:
                    items = build_items(machines[m])
            except Exception as e:
                log(f"Máy {m}: lỗi đọc folder {machines[m]}: {e}")
                continue
            if items:
                set_pending(m, items)
                jobs.append((m, ip, items))
            else:
                state["pending"].pop(str(m), None)
                if m in state["failed"]:
                    state["failed"].remove(m)
                log(f"Máy {m}: hết video")
        if not jobs:
            save_state()
            return log("Không có máy nào để gửi (thiếu IP hoặc hết video)")

        save_state()                                    # lưu kế hoạch trước khi gửi (đóng tool giữa chừng vẫn retry đúng)
        log(f"Gửi cho {len(jobs)} máy ({cfg['per_folder']} video/folder): "
            f"{', '.join(str(m) for m, _, _ in jobs)}")
        results = run_jobs(jobs)

        bad = settle(jobs, results)
        for m, _, _ in jobs:
            if m in bad:
                if m not in state["failed"]:
                    state["failed"].append(m)
            elif m in state["failed"]:
                state["failed"].remove(m)

        summarize(jobs, results, "Xong")
        if bad:
            log(f"!!! Máy chưa gửi xong ({len(bad)}): {', '.join(map(str, bad))} -> sửa xong bấm RETRY")
    finally:
        try:
            save_state()
        finally:
            lock.release()

def do_retry():
    if not lock.acquire(blocking=False):
        return log("Đang chạy, đợi xong đã")
    try:
        save_config()
        if not check_paths(False):
            return
        if abort_if_dup():                                  # IP trùng -> không gửi gì cả
            return
        if not state["failed"]:
            return log("Không có máy lỗi nào để retry")
        try:
            machines = machine_folders()
        except Exception as e:
            return log(f"Lỗi folder xây kênh: {e}")

        jobs = []
        keep = []                                      # máy giữ lại trong danh sách lỗi
        for m in sorted(state["failed"]):
            ip = ip_of(m)
            if not ip or m not in machines:
                log(f"Máy {m}: thiếu IP hoặc folder -> giữ trong danh sách lỗi")
                keep.append(m)
                continue
            if str(m) in state["pending"]:             # đúng kế hoạch lượt trước, chỉ lọc file còn lại
                items = pending_items(m)
            else:                                      # state cũ chưa có pending -> tính 1 lần rồi lưu lại
                items = build_items(machines[m])
            if items:
                set_pending(m, items)
                jobs.append((m, ip, items))
            else:
                state["pending"].pop(str(m), None)     # không còn gì để gửi
        if not jobs:
            state["failed"] = keep
            save_state()
            return log("Không còn video để retry")

        save_state()
        log(f"RETRY {len(jobs)} máy: {', '.join(str(m) for m, _, _ in jobs)}")
        results = run_jobs(jobs)

        keep += settle(jobs, results)
        state["failed"] = sorted(set(keep))

        summarize(jobs, results, "Retry xong")
        if state["failed"]:
            log(f"!!! Vẫn còn máy lỗi: {', '.join(map(str, state['failed']))}")
    finally:
        try:
            save_state()
        finally:
            lock.release()

def run(fn):
    threading.Thread(target=fn, daemon=True).start()

def pump_log():
    """Chạy trên luồng GUI mỗi 200ms: đổ log từ queue ra ô log, vẽ lại bảng sheet nếu luồng nền đã đọc xong."""
    msgs = []
    while not logq.empty():
        msgs.append(logq.get())
    if msgs:
        txt.appendPlainText("\n".join(msgs))
        sb = txt.verticalScrollBar()
        sb.setValue(sb.maximum())
    if sheet_dirty[0]:
        sheet_dirty[0] = False
        build_sheet_table()

def confirm_run(fn, action):
    """Bấm DỌN / CHUYỂN: hỏi lại đường dẫn, chọn Có mới bắt đầu chạy."""
    r = root_edit.text().strip() or "(chưa chọn)"
    c = src_edit.text().strip() or "(chưa chọn)"
    box = QMessageBox(win)
    box.setIcon(QMessageBox.Icon.Question)
    box.setWindowTitle("Xác nhận đường dẫn")
    box.setText(f"Folder xây kênh:\n{r}\n\nFolder đã cut xong:\n{c}\n\nĐúng đường dẫn chưa? Bắt đầu {action}?")
    yes = box.addButton("Có", QMessageBox.ButtonRole.YesRole)
    no = box.addButton("Không", QMessageBox.ButtonRole.NoRole)
    box.setDefaultButton(no)
    box.exec()
    if box.clickedButton() is yes:
        run(fn)

# ===== GUI (PyQt6) =====
GRAY = "color:#777;"
BOLD = "font-weight:bold;"

class MainWindow(QWidget):
    def closeEvent(self, ev):
        apply_counts()
        save_config()
        save_state()
        ev.accept()

app = QApplication(sys.argv)
app.setStyleSheet("""
QLineEdit { background: white; color: #212529; border: 1px solid #ced4da; padding: 2px 6px; selection-background-color: #86b7fe; }
QLineEdit:focus { border: 2px solid #86b7fe; padding: 1px 5px; }
""")

win = MainWindow()
win.setWindowTitle("VLC iPhone Sender")
win.resize(900, 620)
lay = QVBoxLayout(win)
lay.setContentsMargins(8, 6, 8, 6)

# --- tự lưu config.json sau 0.5s kể từ lần sửa cuối; áp số máy / số video sau 0.6s ---
save_timer = QTimer(win)
save_timer.setSingleShot(True)
save_timer.setInterval(500)
save_timer.timeout.connect(save_config)

def schedule_save():
    save_timer.start()           # start() khi đang chạy = đếm lại từ đầu

apply_timer = QTimer(win)
apply_timer.setSingleShot(True)
apply_timer.setInterval(600)

def schedule_apply(*_):
    apply_timer.start()

# --- folder xây kênh / folder đã cut xong ---
def on_root_change(text):
    cfg["root"] = text
    schedule_save()

def on_src_change(text):
    cfg["source"] = text
    schedule_save()

def pick_root():
    p = QFileDialog.getExistingDirectory(win, "Chọn folder xây kênh", root_edit.text() or "")
    if not p:
        return
    if same_path(p, src_edit.text()):
        QMessageBox.warning(win, "Trùng folder", "Folder xây kênh không được trùng với folder đã cut xong.")
        return
    if not same_parent(p, src_edit.text()):
        QMessageBox.warning(win, "Khác folder cha", "Folder xây kênh và folder đã cut xong phải nằm chung 1 folder cha.")
        return
    root_edit.setText(p)

def pick_src():
    p = QFileDialog.getExistingDirectory(win, "Chọn folder đã cut xong", src_edit.text() or "")
    if not p:
        return
    if same_path(p, root_edit.text()):
        QMessageBox.warning(win, "Trùng folder", "Folder đã cut xong không được trùng với folder xây kênh.")
        return
    if not same_parent(p, root_edit.text()):
        QMessageBox.warning(win, "Khác folder cha", "Folder đã cut xong và folder xây kênh phải nằm chung 1 folder cha.")
        return
    src_edit.setText(p)

def path_row(label, btn_text, value, on_pick):
    row = QHBoxLayout()
    lb = QLabel(label); lb.setFixedWidth(130)
    ed = QLineEdit(value)
    btn = QPushButton(btn_text); btn.setMinimumWidth(180)
    btn.clicked.connect(lambda: on_pick())
    row.addWidget(lb); row.addWidget(ed, 1); row.addWidget(btn)
    return row, ed

row_root, root_edit = path_row("Folder xây kênh:", "Chọn folder xây kênh", cfg["root"], pick_root)
row_src, src_edit = path_row("Folder đã cut xong:", "Chọn folder đã cut xong", cfg["source"], pick_src)
root_edit.textChanged.connect(on_root_change)
src_edit.textChanged.connect(on_src_change)
lay.addLayout(row_root)
lay.addLayout(row_src)

# --- khung cuộn dùng chung cho bảng IP và bảng sheet ---
def make_scroll():
    sc = QScrollArea()
    sc.setWidgetResizable(True)
    return sc

def set_scroll_content(sc, inner, min_h=40, max_h=300):
    sc.setWidget(inner)                                  # widget cũ tự bị xoá
    sc.setFixedHeight(min(max(inner.sizeHint().height() + 4, min_h), max_h))
    sc.verticalScrollBar().setValue(0)

# --- tab ---
tabs = QTabWidget()
tab_main = QWidget()
tab_setup = QWidget()
tabs.addTab(tab_main, "Chính")
tabs.addTab(tab_setup, "Setup kênh")
lay.addWidget(tabs)

# --- tab Chính: số máy / số video mỗi folder / IP từng máy ---
ip_edits = []

def apply_counts():
    n = nm_spin.value()
    p = pf_spin.value()
    rebuild = False
    if n != cfg["num_machines"]:
        cfg["num_machines"] = n
        rebuild = True
    cfg["per_folder"] = p
    if rebuild:
        build_ip_grid()
        build_sheet_table()
    schedule_save()

apply_timer.timeout.connect(apply_counts)

main_lay = QVBoxLayout(tab_main)
main_lay.setContentsMargins(4, 4, 4, 4)
conf = QHBoxLayout()
nm_spin = QSpinBox(); nm_spin.setRange(1, MAX_MACHINES); nm_spin.setValue(cfg["num_machines"]); nm_spin.setFixedWidth(70)
pf_spin = QSpinBox(); pf_spin.setRange(1, MAX_PER_FOLDER); pf_spin.setValue(cfg["per_folder"]); pf_spin.setFixedWidth(70)
nm_spin.valueChanged.connect(schedule_apply)
pf_spin.valueChanged.connect(schedule_apply)
note_lb = QLabel("(tự lưu vào config.json)"); note_lb.setStyleSheet(GRAY)
conf.addWidget(QLabel("Số máy (IP):")); conf.addWidget(nm_spin); conf.addSpacing(16)
conf.addWidget(QLabel("Số video / folder:")); conf.addWidget(pf_spin); conf.addSpacing(12)
conf.addWidget(note_lb); conf.addStretch(1)
main_lay.addLayout(conf)
main_lay.addWidget(QLabel("IP từng máy (máy N <-> folder N trong folder xây kênh; để trống = bỏ qua):"))
ip_scroll = make_scroll()
main_lay.addWidget(ip_scroll)
main_lay.addStretch(1)

def on_ip_change(i, text):
    if i < len(cfg["ips"]):
        cfg["ips"][i] = text
    schedule_save()

def build_ip_grid():
    n = cfg["num_machines"]
    cfg["ips"] += [""] * (n - len(cfg["ips"]))
    ip_edits.clear()
    inner = QWidget()
    gl = QGridLayout(inner)
    gl.setContentsMargins(4, 2, 4, 2)
    gl.setHorizontalSpacing(6)
    gl.setVerticalSpacing(2)
    per = max((n + 3) // 4, 1)                           # chia 4 cột: mỗi cột `per` máy
    for i in range(n):
        ed = QLineEdit(cfg["ips"][i])                    # gán giá trị trước, nối signal sau (không kích hoạt lưu)
        ed.textChanged.connect(lambda t, i=i: on_ip_change(i, t))
        ip_edits.append(ed)
        g, row = divmod(i, per)
        lb = QLabel(f"Máy {i + 1}:")
        lb.setMinimumWidth(48)
        lb.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        gl.addWidget(lb, row, g * 2)
        gl.addWidget(ed, row, g * 2 + 1)
    for c in (1, 3, 5, 7):
        gl.setColumnStretch(c, 1)
    gl.setRowStretch(per, 1)
    set_scroll_content(ip_scroll, inner, min_h=30)

def clear_ips():
    """Xoá hết IP trong các ô nhập và trong config.json."""
    for ed in ip_edits:
        ed.setText("")
    cfg["ips"] = [""] * max(cfg["num_machines"], len(cfg["ips"]))
    save_config()
    log("Đã xoá hết IP")

build_ip_grid()

# --- tab Setup kênh: số folder kênh của từng máy, đọc từ Google Sheet ---
setup_lay = QVBoxLayout(tab_setup)
setup_lay.setContentsMargins(4, 6, 4, 4)

sheet_edit = QLineEdit(cfg["sheet_url"])
colm_edit = QLineEdit(cfg["sheet_col_machine"])
colv_edit = QLineEdit(cfg["sheet_col_view"])
sheet_info = QLabel("")

def on_sheet_change(text):
    cfg["sheet_url"] = text.strip()
    schedule_save()

def on_col_change(*_):
    cfg["sheet_col_machine"] = colm_edit.text().strip().upper()
    cfg["sheet_col_view"] = colv_edit.text().strip().upper()
    schedule_save()

def read_sheet_clicked():
    cfg["sheet_url"] = sheet_edit.text().strip()
    on_col_change()
    threading.Thread(target=refresh_sheet, daemon=True).start()

sheet_edit.textChanged.connect(on_sheet_change)
colm_edit.textChanged.connect(on_col_change)
colv_edit.textChanged.connect(on_col_change)

def colored_button(text, bg, cb, min_w=110):
    b = QPushButton(text)
    b.setMinimumWidth(min_w)
    b.setCursor(Qt.CursorShape.PointingHandCursor)
    b.setStyleSheet(f"QPushButton {{ background:{bg}; color:white; border:none; padding:6px 10px; }}"
                    f"QPushButton:hover {{ background:{QColor(bg).darker(112).name()}; }}")
    b.clicked.connect(lambda: cb())
    return b

shf = QHBoxLayout()
shf.addWidget(QLabel("Link Google Sheet:")); shf.addWidget(sheet_edit, 1)
shf.addWidget(colored_button("Đọc sheet", "#2196f3", read_sheet_clicked, 100))
setup_lay.addLayout(shf)

colf = QHBoxLayout()
for ed in (colm_edit, colv_edit):
    ed.setFixedWidth(55)
    ed.setAlignment(Qt.AlignmentFlag.AlignCenter)
hint_lb = QLabel("(nhập chữ cái cột, vd I, K)"); hint_lb.setStyleSheet(GRAY)
colf.addWidget(QLabel("Cột số máy:")); colf.addWidget(colm_edit); colf.addSpacing(16)
colf.addWidget(QLabel("Cột view:")); colf.addWidget(colv_edit); colf.addSpacing(16)
colf.addWidget(hint_lb); colf.addStretch(1)
setup_lay.addLayout(colf)

desc_lb = QLabel("Ô cột view RỖNG -> +1 folder cho máy ở cột số máy. Máy không có dòng nào = 0 folder. "
                 "DỌN: thiếu -> bù từ folder đã cut xong, thừa -> chuyển về folder đã cut xong.")
desc_lb.setStyleSheet(GRAY)
desc_lb.setWordWrap(True)
setup_lay.addWidget(desc_lb)
setup_lay.addWidget(sheet_info)
fc_scroll = make_scroll()
setup_lay.addWidget(fc_scroll)
setup_lay.addStretch(1)

def build_sheet_table():
    items = sorted(cfg["sheet_counts"].items(), key=lambda kv: int(kv[0]))
    inner = QWidget()
    gl = QGridLayout(inner)
    gl.setContentsMargins(4, 2, 4, 2)
    gl.setVerticalSpacing(2)
    if not items:
        sheet_info.setText("Chưa đọc sheet")
        lb = QLabel("(chưa có dữ liệu, nhập link rồi bấm Đọc sheet)")
        lb.setStyleSheet(GRAY)
        gl.addWidget(lb, 0, 0)
        gl.setColumnStretch(1, 1)
    else:
        sheet_info.setText(f"{len(items)} máy, tổng {sum(c for _, c in items)} folder (DỌN sẽ tự đọc lại sheet)")
        per = max((len(items) + 3) // 4, 1)              # chia 4 cột: mỗi cột `per` máy
        groups = (len(items) + per - 1) // per
        for g in range(groups):
            base = g * 3
            h1 = QLabel("Máy"); h1.setStyleSheet(BOLD)
            h2 = QLabel("Số folder"); h2.setStyleSheet(BOLD); h2.setAlignment(Qt.AlignmentFlag.AlignCenter)
            gl.addWidget(h1, 0, base)
            gl.addWidget(h2, 0, base + 1)
            gl.setColumnStretch(base + 2, 1)
        for i, (k, c) in enumerate(items):
            m = int(k)
            base = (i // per) * 3
            r = i % per + 1
            note = "" if m <= cfg["num_machines"] else " (ngoài số máy)"
            lb = QLabel(f"Máy {m}{note}")
            lb.setMinimumWidth(60)
            cl = QLabel(str(c)); cl.setAlignment(Qt.AlignmentFlag.AlignCenter); cl.setMinimumWidth(70)
            gl.addWidget(lb, r, base)
            gl.addWidget(cl, r, base + 1)
        gl.setRowStretch(per + 1, 1)
    set_scroll_content(fc_scroll, inner)

build_sheet_table()

# --- nút chạy ---
btns = QHBoxLayout()
btns.addStretch(1)
for text, bg, cb in (("DỌN", "#ff9800", lambda: confirm_run(do_clean, "DỌN")),
                     ("CHUYỂN", "#4caf50", lambda: confirm_run(do_send, "CHUYỂN")),
                     ("RETRY", "#f44336", lambda: run(do_retry)),
                     ("CLEAR IP", "#607d8b", clear_ips)):
    btns.addWidget(colored_button(text, bg, cb, 110))
btns.addStretch(1)
lay.addLayout(btns)

# --- ô log ---
txt = QPlainTextEdit()
txt.setReadOnly(True)
lay.addWidget(txt, 1)

pump_timer = QTimer(win)
pump_timer.timeout.connect(pump_log)
pump_timer.start(200)

win.show()
sys.exit(app.exec())

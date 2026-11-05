# pip install requests send2trash
import os, re, csv, io, json, time, shutil, threading, queue, mimetypes, requests
import tkinter as tk
from tkinter import filedialog, scrolledtext, messagebox, ttk
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

def pump_log():
    while not logq.empty():
        txt.insert(tk.END, logq.get() + "\n"); txt.see(tk.END)
    if sheet_dirty[0]:
        sheet_dirty[0] = False
        build_sheet_table()
    win.after(200, pump_log)

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

def confirm_run(fn, action):
    """Bấm DỌN / CHUYỂN: hỏi lại đường dẫn, chọn Có mới bắt đầu chạy."""
    r = root_var.get().strip() or "(chưa chọn)"
    c = src_var.get().strip() or "(chưa chọn)"
    ok = messagebox.askyesno("Xác nhận đường dẫn",
                             f"Folder xây kênh:\n{r}\n\nFolder đã cut xong:\n{c}\n\nĐúng đường dẫn chưa? Bắt đầu {action}?",
                             icon="question", default="no")
    if ok:
        run(fn)

# ===== GUI =====
win = tk.Tk(); win.title("VLC iPhone Sender"); win.geometry("900x620")

_save_job = [None]
def schedule_save():
    """Lưu config.json sau 0.5s kể từ lần sửa cuối (gõ IP xong là tự lưu)."""
    if _save_job[0]:
        win.after_cancel(_save_job[0])
    _save_job[0] = win.after(500, save_config)

# --- folder xây kênh ---
root_var = tk.StringVar(value=cfg["root"])
def on_root_change(*_):
    cfg["root"] = root_var.get()
    schedule_save()
root_var.trace_add("write", on_root_change)

def pick_root():
    p = filedialog.askdirectory(initialdir=root_var.get() or None)
    if not p:
        return
    if same_path(p, src_var.get()):
        messagebox.showwarning("Trùng folder", "Folder xây kênh không được trùng với folder đã cut xong.")
        return
    if not same_parent(p, src_var.get()):
        messagebox.showwarning("Khác folder cha", "Folder xây kênh và folder đã cut xong phải nằm chung 1 folder cha.")
        return
    root_var.set(p)

def pick_src():
    p = filedialog.askdirectory(initialdir=src_var.get() or None)
    if not p:
        return
    if same_path(p, root_var.get()):
        messagebox.showwarning("Trùng folder", "Folder đã cut xong không được trùng với folder xây kênh.")
        return
    if not same_parent(p, root_var.get()):
        messagebox.showwarning("Khác folder cha", "Folder đã cut xong và folder xây kênh phải nằm chung 1 folder cha.")
        return
    src_var.set(p)

top = tk.Frame(win); top.pack(fill="x", padx=8, pady=(6, 2))
tk.Label(top, text="Folder xây kênh:", width=19, anchor="w").pack(side="left")
tk.Entry(top, textvariable=root_var).pack(side="left", fill="x", expand=True)
tk.Button(top, text="Chọn folder xây kênh", width=24, command=pick_root).pack(side="left", padx=4)

# --- folder đã cut xong (lấy folder bù khi xoá folder rỗng) ---
src_var = tk.StringVar(value=cfg["source"])
def on_src_change(*_):
    cfg["source"] = src_var.get()
    schedule_save()
src_var.trace_add("write", on_src_change)

srcf = tk.Frame(win); srcf.pack(fill="x", padx=8, pady=(0, 6))
tk.Label(srcf, text="Folder đã cut xong:", width=19, anchor="w").pack(side="left")
tk.Entry(srcf, textvariable=src_var).pack(side="left", fill="x", expand=True)
tk.Button(srcf, text="Chọn folder đã cut xong", width=24, command=pick_src).pack(side="left", padx=4)

# --- cấu hình số máy / số video mỗi folder ---
nm_var = tk.StringVar(value=str(cfg["num_machines"]))
pf_var = tk.StringVar(value=str(cfg["per_folder"]))
ip_vars = []
_apply_job = [None]

def apply_counts():
    n = to_int(nm_var.get(), cfg["num_machines"], 1, MAX_MACHINES) if nm_var.get().strip().isdigit() else None
    p = to_int(pf_var.get(), cfg["per_folder"], 1, MAX_PER_FOLDER) if pf_var.get().strip().isdigit() else None
    rebuild = False
    if n is not None and n != cfg["num_machines"]:
        cfg["num_machines"] = n
        rebuild = True
    if p is not None:
        cfg["per_folder"] = p
    if rebuild:
        build_ip_grid()
        build_sheet_table()
    schedule_save()

def schedule_apply(*_):
    if _apply_job[0]:
        win.after_cancel(_apply_job[0])
    _apply_job[0] = win.after(600, apply_counts)

nm_var.trace_add("write", schedule_apply)
pf_var.trace_add("write", schedule_apply)

nb = ttk.Notebook(win); nb.pack(fill="x", padx=8, pady=2)
tab_main = tk.Frame(nb); tab_setup = tk.Frame(nb)
nb.add(tab_main, text="Chính"); nb.add(tab_setup, text="Setup kênh")

conf = tk.Frame(tab_main); conf.pack(fill="x", pady=2)
tk.Label(conf, text="Số máy (IP):").pack(side="left")
tk.Spinbox(conf, from_=1, to=MAX_MACHINES, textvariable=nm_var, width=5).pack(side="left", padx=(4, 16))
tk.Label(conf, text="Số video / folder:").pack(side="left")
tk.Spinbox(conf, from_=1, to=MAX_PER_FOLDER, textvariable=pf_var, width=5).pack(side="left", padx=4)
tk.Label(conf, text="(tự lưu vào config.json)", fg="#777").pack(side="left", padx=12)

tk.Label(tab_main, text="IP từng máy (máy N <-> folder N trong folder xây kênh; để trống = bỏ qua):").pack(anchor="w")

# --- khung IP có thanh cuộn ---
ipwrap = tk.Frame(tab_main); ipwrap.pack(fill="x", pady=2)
canvas = tk.Canvas(ipwrap, height=270, highlightthickness=0)
sb = tk.Scrollbar(ipwrap, orient="vertical", command=canvas.yview)
canvas.configure(yscrollcommand=sb.set)
sb.pack(side="right", fill="y"); canvas.pack(side="left", fill="both", expand=True)
grid = tk.Frame(canvas)
gwin = canvas.create_window((0, 0), window=grid, anchor="nw")
grid.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
canvas.bind("<Configure>", lambda e: canvas.itemconfigure(gwin, width=e.width))
def _wheel(e):
    canvas.yview_scroll(int(-e.delta / 120), "units")
canvas.bind("<Enter>", lambda e: canvas.bind_all("<MouseWheel>", _wheel))
canvas.bind("<Leave>", lambda e: canvas.unbind_all("<MouseWheel>"))

# ===== Ô nhập kiểu Bootstrap: viền xanh + vầng sáng (box-shadow) khi focus, góc vuông =====
BS_BORDER, BS_BLUE, BS_GLOW = "#ced4da", "#86b7fe", "#b6d4fe"
BS_RED, BS_RED_GLOW = "#dc3545", "#f1aeb5"
BS_GREEN, BS_GREEN_GLOW = "#198754", "#a3cfbb"
BS_GLOW_W = 3          # độ dày vầng sáng (px)

def bs_entry(parent, **kw):
    """Trả về (khung_ngoài, entry). Đem khung ngoài đi grid/pack.
    entry.bs_state(None | "ok" | "error") đổi màu viền (bình thường / đã lưu / sai)."""
    G = BS_GLOW_W
    page_bg = parent.cget("bg")
    cv = tk.Canvas(parent, bg=page_bg, bd=0, highlightthickness=0)
    e = tk.Entry(cv, bd=0, relief="flat", highlightthickness=0, bg="white", **kw)
    eh = e.winfo_reqheight()
    cv.configure(width=e.winfo_reqwidth() + 2 * (G + 1 + 6), height=eh + 2 * (G + 1 + 1))   # +1px đệm trên/dưới để ô nhập không đè lên viền
    win_id = cv.create_window(0, 0, window=e, anchor="nw")
    st = {"focus": False, "state": None}

    def paint(w=None, h=None):
        w = w or cv.winfo_width()
        h = h or cv.winfo_height()
        if w < 10:
            w = int(cv.cget("width"))
        if h < 10:
            h = int(cv.cget("height"))
        edge, glow = {None: (BS_BLUE, BS_GLOW), "ok": (BS_GREEN, BS_GREEN_GLOW),
                      "error": (BS_RED, BS_RED_GLOW)}[st["state"]]
        cv.delete("bs")
        if st["focus"]:                                  # vầng sáng (box-shadow)
            cv.create_rectangle(0, 0, w, h, fill=glow, outline="", tags="bs")
        edge_c = edge if (st["focus"] or st["state"]) else BS_BORDER
        cv.create_rectangle(G, G, w - G, h - G, fill=edge_c, outline="", tags="bs")        # viền: chữ nhật
        cv.create_rectangle(G + 1, G + 1, w - G - 1, h - G - 1, fill="white", outline="", tags="bs")
        cv.coords(win_id, G + 1 + 6, G + 1 + 1)           # toạ độ nguyên, neo góc trên-trái: không bị lệch nửa pixel
        cv.itemconfigure(win_id, width=max(w - 2 * (G + 1 + 6), 10), height=eh)

    def on_in(ev):
        st["focus"] = True; paint()

    def on_out(ev):
        st["focus"] = False; paint()

    def set_state(v):
        st["state"] = v; paint()

    cv.bind("<Configure>", lambda ev: paint(ev.width, ev.height))
    cv.bind("<Button-1>", lambda ev: (e.focus_set(), e.icursor(tk.END)))   # bấm vào mép cũng vào ô nhập
    e.bind("<FocusIn>", on_in, add="+")
    e.bind("<FocusOut>", on_out, add="+")
    e.bs_state = set_state
    paint()
    return cv, e

def on_ip_change(i, v):
    if i < len(cfg["ips"]):
        cfg["ips"][i] = v.get()
    schedule_save()

def build_ip_grid():
    for w in grid.winfo_children():
        w.destroy()
    ip_vars.clear()
    n = cfg["num_machines"]
    cfg["ips"] += [""] * (n - len(cfg["ips"]))
    per = max((n + 3) // 4, 1)                           # chia 4 cột: mỗi cột `per` máy
    for i in range(n):
        v = tk.StringVar(value=cfg["ips"][i])
        v.trace_add("write", lambda *_, i=i, v=v: on_ip_change(i, v))
        ip_vars.append(v)
        g = i // per
        row = i % per
        tk.Label(grid, text=f"Máy {i + 1}:", width=7, anchor="e").grid(row=row, column=g * 2, padx=(6, 0))
        outer, _e = bs_entry(grid, textvariable=v, width=20)
        outer.grid(row=row, column=g * 2 + 1, padx=(0, 8), sticky="we")
    for c in (1, 3, 5, 7):
        grid.columnconfigure(c, weight=1)
    grid.update_idletasks()
    canvas.configure(height=min(max(grid.winfo_reqheight(), 30), 300))
    canvas.yview_moveto(0)

def clear_ips():
    """Xoá hết IP trong các ô nhập và trong config.json."""
    for v in ip_vars:
        v.set("")
    cfg["ips"] = [""] * max(cfg["num_machines"], len(cfg["ips"]))
    save_config()
    log("Đã xoá hết IP")

build_ip_grid()

# --- tab Setup kênh: số folder kênh của từng máy, đọc từ Google Sheet ---
def make_scroll_grid(parent):
    wrap = tk.Frame(parent); wrap.pack(fill="x", pady=2)
    cv = tk.Canvas(wrap, height=270, highlightthickness=0)
    vsb = tk.Scrollbar(wrap, orient="vertical", command=cv.yview)
    cv.configure(yscrollcommand=vsb.set)
    vsb.pack(side="right", fill="y"); cv.pack(side="left", fill="both", expand=True)
    g = tk.Frame(cv)
    gw = cv.create_window((0, 0), window=g, anchor="nw")
    g.bind("<Configure>", lambda e: cv.configure(scrollregion=cv.bbox("all")))
    cv.bind("<Configure>", lambda e: cv.itemconfigure(gw, width=e.width))
    cv.bind("<Enter>", lambda e: cv.bind_all("<MouseWheel>", lambda ev: cv.yview_scroll(int(-ev.delta / 120), "units")))
    cv.bind("<Leave>", lambda e: cv.unbind_all("<MouseWheel>"))
    return cv, g

def build_sheet_table():
    for w in fc_grid.winfo_children():
        w.destroy()
    items = sorted(cfg["sheet_counts"].items(), key=lambda kv: int(kv[0]))
    if not items:
        sheet_info.set("Chưa đọc sheet")
        tk.Label(fc_grid, text="(chưa có dữ liệu, nhập link rồi bấm Đọc sheet)", fg="#777").grid(row=0, column=0, padx=6, pady=4, sticky="w")
    else:
        sheet_info.set(f"{len(items)} máy, tổng {sum(c for _, c in items)} folder (DỌN sẽ tự đọc lại sheet)")
        per = max((len(items) + 3) // 4, 1)             # chia 4 cột: mỗi cột `per` máy
        groups = (len(items) + per - 1) // per
        for g in range(groups):
            base = g * 3
            tk.Label(fc_grid, text="Máy", width=7, font=("", 9, "bold")).grid(row=0, column=base, pady=1)
            tk.Label(fc_grid, text="Số folder", width=9, font=("", 9, "bold")).grid(row=0, column=base + 1, pady=1)
        for i, (k, c) in enumerate(items):
            m = int(k)
            base = (i // per) * 3
            r = i % per + 1
            note = "" if m <= cfg["num_machines"] else " (ngoài số máy)"
            tk.Label(fc_grid, text=f"Máy {m}{note}", width=7 if not note else 20, anchor="w").grid(row=r, column=base, padx=(6, 2), pady=1, sticky="w")
            tk.Label(fc_grid, text=str(c), width=9, anchor="center").grid(row=r, column=base + 1, padx=(0, 12), pady=1)
    fc_grid.update_idletasks()
    fc_canvas.configure(height=min(max(fc_grid.winfo_reqheight(), 40), 300))
    fc_canvas.yview_moveto(0)

sheet_var = tk.StringVar(value=cfg["sheet_url"])
sheet_info = tk.StringVar(value="")
def on_sheet_change(*_):
    cfg["sheet_url"] = sheet_var.get().strip()
    schedule_save()
sheet_var.trace_add("write", on_sheet_change)

colm_var = tk.StringVar(value=cfg["sheet_col_machine"])
colv_var = tk.StringVar(value=cfg["sheet_col_view"])
def on_col_change(*_):
    cfg["sheet_col_machine"] = colm_var.get().strip().upper()
    cfg["sheet_col_view"] = colv_var.get().strip().upper()
    schedule_save()
colm_var.trace_add("write", on_col_change)
colv_var.trace_add("write", on_col_change)

def read_sheet_clicked():
    cfg["sheet_url"] = sheet_var.get().strip()
    on_col_change()
    threading.Thread(target=refresh_sheet, daemon=True).start()

shf = tk.Frame(tab_setup); shf.pack(fill="x", pady=(6, 2))
tk.Label(shf, text="Link Google Sheet:", anchor="w").pack(side="left")
tk.Entry(shf, textvariable=sheet_var).pack(side="left", fill="x", expand=True, padx=4)
tk.Button(shf, text="Đọc sheet", width=12, bg="#2196f3", fg="white", activebackground="#1976d2", activeforeground="white", command=read_sheet_clicked).pack(side="left", padx=4)
colf = tk.Frame(tab_setup); colf.pack(fill="x", pady=(2, 2))
tk.Label(colf, text="Cột số máy:").pack(side="left")
tk.Entry(colf, textvariable=colm_var, width=5, justify="center").pack(side="left", padx=(4, 16))
tk.Label(colf, text="Cột view:").pack(side="left")
tk.Entry(colf, textvariable=colv_var, width=5, justify="center").pack(side="left", padx=(4, 16))
tk.Label(colf, text="(nhập chữ cái cột, vd I, K)", fg="#777").pack(side="left")
tk.Label(tab_setup, text="Ô cột view RỖNG -> +1 folder cho máy ở cột số máy. Máy không có dòng nào = 0 folder. DỌN: thiếu -> bù từ folder đã cut xong, thừa -> chuyển về folder đã cut xong.",
         fg="#777", wraplength=840, justify="left").pack(anchor="w", pady=(2, 2))
tk.Label(tab_setup, textvariable=sheet_info, anchor="w").pack(anchor="w", pady=(2, 2))
fc_canvas, fc_grid = make_scroll_grid(tab_setup)
build_sheet_table()

btns = tk.Frame(win); btns.pack(pady=6)
tk.Button(btns, text="DỌN", width=14, bg="#ff9800", fg="white", command=lambda: confirm_run(do_clean, "DỌN")).pack(side="left", padx=4)
tk.Button(btns, text="CHUYỂN", width=14, bg="#4caf50", fg="white", command=lambda: confirm_run(do_send, "CHUYỂN")).pack(side="left", padx=4)
tk.Button(btns, text="RETRY", width=14, bg="#f44336", fg="white", command=lambda: run(do_retry)).pack(side="left", padx=4)
tk.Button(btns, text="CLEAR IP", width=14, bg="#607d8b", fg="white", command=clear_ips).pack(side="left", padx=4)

txt = scrolledtext.ScrolledText(win); txt.pack(fill="both", expand=True, padx=8, pady=6)

def on_close():
    apply_counts()
    save_config()
    save_state()
    win.destroy()

win.protocol("WM_DELETE_WINDOW", on_close)
pump_log()
win.mainloop()
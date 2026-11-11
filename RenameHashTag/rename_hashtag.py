"""Rename Hashtag - FILE DUY NHẤT (có giao diện). Thêm / tạo lại / xóa hashtag ở cuối tên video.

Chạy:  python rename_hashtag_gui.py
Tùy chọn: cài spaCy để nhận diện tên riêng tốt hơn (không bắt buộc):
  pip install spacy && python -m spacy download en_core_web_sm
File hashtag_blacklist.txt (cùng thư mục) chứa các từ không dùng làm hashtag; tự tạo nếu chưa có.
"""
import os
import re
import sys
import json
import time
import string

VIDEO_EXTS = ('.mp4', '.mkv', '.avi', '.mov', '.webm')
MAX_NAME_LEN = 240      # độ dài tên file
MAX_PATH_LEN = 255      # đường dẫn đầy đủ (Windows giới hạn ~260)
MAX_HASHTAGS = 3
MIN_EXTRA_SCORE = 0.9   # hashtag thứ 2, 3 chỉ thêm khi đủ điểm
BLACKLIST_FILE = 'hashtag_blacklist.txt'
HERE = (os.path.dirname(sys.executable) if getattr(sys, 'frozen', False)
        else os.path.dirname(os.path.abspath(__file__)))

DEFAULT_BLACKLIST = """// Mỗi dòng một từ KHÔNG được dùng để tạo hashtag (không phân biệt hoa/thường, có/không có dấu #).
// Dòng bắt đầu bằng // là ghi chú.
video
videos
full
official
episode
"""

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass


# ============================================================ blacklist
def load_blacklist():
    path = os.path.join(HERE, BLACKLIST_FILE)
    if not os.path.isfile(path):
        try:
            with open(path, 'w', encoding='utf-8') as f:
                f.write(DEFAULT_BLACKLIST)
        except OSError:
            pass
        raw = DEFAULT_BLACKLIST
    else:
        with open(path, 'r', encoding='utf-8-sig', errors='replace') as f:
            raw = f.read()
    out = set()
    for ln in raw.splitlines():
        ln = ln.strip()
        if ln and not ln.startswith('//'):
            out.add(ln.lstrip('#').lower())
    return out


# ============================================================ spaCy (tùy chọn)
_NLP = None
_NLP_TRIED = False


_NLP_ERR = ''


def _get_nlp():
    global _NLP, _NLP_TRIED, _NLP_ERR
    if not _NLP_TRIED:
        _NLP_TRIED = True
        try:
            import spacy
            _NLP = spacy.load('en_core_web_sm')
        except ImportError:
            _NLP = None
            _NLP_ERR = 'chưa cài spaCy (pip install spacy)'
        except Exception as ex:
            _NLP = None
            _NLP_ERR = f'có spaCy nhưng không tải được model en_core_web_sm ({ex}); chạy: python -m spacy download en_core_web_sm'
    return _NLP


def spacy_status():
    """Chuỗi mô tả spaCy có đang được dùng hay không."""
    if _get_nlp() is not None:
        import spacy
        return f'spaCy {spacy.__version__} + en_core_web_sm: ĐANG DÙNG'
    return f'spaCy: KHÔNG dùng được - {_NLP_ERR}'


def require_spacy():
    """spaCy là BẮT BUỘC: thiếu thì báo lỗi, không chạy tiếp."""
    if _get_nlp() is None:
        raise RuntimeError('Bắt buộc phải có spaCy. ' + _NLP_ERR)


def _spacy_doc(words):
    nlp = _get_nlp()
    if nlp is None or not words:
        return None
    try:
        from spacy.tokens import Doc
        doc = Doc(nlp.vocab, words=list(words))
        for _, proc in nlp.pipeline:
            doc = proc(doc)
        return doc
    except Exception:
        return None


# ============================================================ từ điển
_STOP = set("""
a an the and or but nor so yet of in on at to for from by with without about against between into through during
before after above below up down out off over under again further then once here there when where why how all any
both each few more most other some such no not only own same than too very can will just don dont now is are was
were be been being am do does did doing have has had having i me my we our you your he him his she her it its they
them their what which who whom this that these those would could might must shall may also still even ever never
always often really actually if as while because like likes liked
found find finds finding discovered discover discovers live lives living lived made make makes making get gets got
getting go goes went going gone come comes came take takes took taken see sees saw seen look looks looked know knew
known tell tells told say says said show shows showed shown want wants wanted need needs needed try tried tries
trying let lets put puts keep kept give gave given use used uses turn turns turned
build builds built building buy bought launch launched caught catch happen happens happened raced race
one two three four five six seven eight nine ten new old big small good bad great best worst real first last next
many much every everyone everything something anything nothing someone anyone thing things way ways people person
day days time times year years part pt episode ep video videos full official vs versus
today tonight tomorrow yesterday instead entire whole complete finally
heavy giant huge tiny little mini inside outside biggest largest smallest fastest strongest highest lowest
longest shortest cheapest greatest powerful incredible amazing impossible extreme dangerous expensive
world ever again anymore minutes minute hours hour seconds second thousands millions
start finish finished work works worked functional fully smart insane crazy simple easy
""".split())

TOPIC_TAGS = {
    'underground': ['#underground', '#urbanexploration'], 'tunnel': ['#tunnel', '#underground'],
    'bunker': ['#bunker', '#survival'], 'cave': ['#cave', '#exploring'], 'abandoned': ['#abandoned', '#urbex'],
    'haunted': ['#haunted', '#paranormal'], 'ghost': ['#ghost', '#paranormal'],
    'paranormal': ['#paranormal', '#scary'], 'scary': ['#scary', '#horror'], 'horror': ['#horror', '#scarystories'],
    'creepy': ['#creepy', '#scary'], 'disturbing': ['#disturbing', '#creepy'], 'mystery': ['#mystery', '#unsolved'],
    'unsolved': ['#unsolved', '#mystery'], 'crime': ['#truecrime', '#crime'], 'murder': ['#truecrime', '#murder'],
    'secret': ['#secret', '#mystery'], 'hidden': ['#hidden', '#mystery'], 'survival': ['#survival', '#survivalskills'],
    'history': ['#history', '#historyfacts'], 'ancient': ['#ancient', '#history'], 'war': ['#war', '#history'],
    'space': ['#space', '#astronomy'], 'ocean': ['#ocean', '#deepsea'], 'shark': ['#shark', '#ocean'],
    'science': ['#science', '#sciencefacts'], 'fact': ['#facts', '#didyouknow'], 'ai': ['#ai', '#artificialintelligence'],
    'tech': ['#tech', '#technology'], 'technology': ['#technology', '#tech'], 'money': ['#money', '#business'],
    'travel': ['#travel', '#explore'], 'food': ['#food', '#foodie'], 'diy': ['#diy', '#howto'],
    'animal': ['#animals', '#wildlife'], 'dog': ['#dog', '#pets'], 'cat': ['#cat', '#pets'],
    'house': ['#house', '#home'], 'city': ['#city', '#urbanexploration'], 'island': ['#island', '#travel'],
    'treasure': ['#treasure', '#adventure'], 'alien': ['#alien', '#ufo'], 'ufo': ['#ufo', '#alien'],
    'conspiracy': ['#conspiracy', '#mystery'], 'experiment': ['#experiment', '#science'],
    'camping': ['#camping', '#outdoors'], 'lego': ['#lego', '#toys'], 'restoration': ['#restoration', '#restore'],
    'construction': ['#construction', '#building'], 'woodworking': ['#woodworking', '#diy'],
    'minecraft': ['#minecraft', '#gaming'], 'submarine': ['#submarine', '#ocean'],
    'airplane': ['#airplane', '#aviation'], 'plane': ['#plane', '#aviation'], 'aircraft': ['#aircraft', '#aviation'],
    'engine': ['#engine', '#mechanic'], 'generator': ['#generator', '#energy'], 'timelapse': ['#timelapse', '#history'],
    'mountain': ['#mountain', '#nature'], 'evolution': ['#evolution', '#science'],
}

_ENT_LABELS = {'PERSON', 'ORG', 'GPE', 'EVENT', 'PRODUCT'}
_PUNCT = string.punctuation + '“”‘’…«»'
HASHTAG_TOKEN = re.compile(r'#(?=\w*[^\W\d_])\w+', re.UNICODE)  # '#1', 'C#' không phải hashtag
_INDEX_WORD = re.compile(r'(?i)^(pt|part|ep|episode)\.?$')
_NUM = re.compile(r'^\d+$')


def _stem(w):
    if w.endswith('ies') and len(w) > 4:
        return w[:-3] + 'y'
    if w.endswith(('sses', 'xes', 'ches', 'shes')):
        return w[:-2]
    if w.endswith('s') and not w.endswith(('ss', 'us', 'is')) and len(w) > 3:
        return w[:-1]
    return w


def _core(word):
    """Bỏ dấu câu ở hai đầu (cả dấu full-width như ： ？ ｜) và đuôi sở hữu 's."""
    core = re.sub(r'^\W+|\W+$', '', word, flags=re.UNICODE)
    return re.sub(r"['’]s$", '', core)


# ============================================================ tách hashtag / số tập
def _is_index_pair(tokens, i):
    return i + 1 < len(tokens) and _INDEX_WORD.match(tokens[i]) and _NUM.match(tokens[i + 1].strip('.'))


def split_hashtags(title):
    """(tên đã bỏ hashtag cuối, [hashtag]).
    - Hashtag đứng ở CUỐI tên (có thể trước 'Pt. N') -> bỏ khỏi tên, đem làm danh sách hashtag.
    - Hashtag nằm GIỮA câu ('#LEGO Helicopter') -> giữ lại chữ (bỏ dấu #) để câu không mất nghĩa.
    - Trùng số tập ('Pt 1 Pt. 1') -> chỉ giữ cái cuối."""
    tokens = title.split()
    n = len(tokens)
    trailing = [False] * n
    i = n - 1
    while i >= 0:   # quét ngược qua vùng gồm hashtag + "Pt. N"
        if HASHTAG_TOKEN.fullmatch(tokens[i]):
            trailing[i] = True
            i -= 1
        elif i >= 1 and _NUM.match(tokens[i].strip('.')) and _INDEX_WORD.match(tokens[i - 1]):
            i -= 2
        else:
            break
    tags, seen, kept = [], set(), []
    for idx, tok in enumerate(tokens):
        if HASHTAG_TOKEN.fullmatch(tok):
            if tok.lower() not in seen:
                seen.add(tok.lower())
                tags.append(tok)
            if not trailing[idx]:
                kept.append(tok[1:])
        else:
            kept.append(tok)
    clean = ' '.join(kept).strip(' |-–—:,;·•/')
    # trùng số tập: giữ cái cuối cùng
    pat = re.compile(r'(?i)\b(?:pt|part)\.?\s*(\d+)\b')
    ms = list(pat.finditer(clean))
    if len(ms) > 1:
        last = ms[-1].group(1)
        for m in reversed(ms[:-1]):
            if m.group(1) == last:
                clean = clean[:m.start()] + clean[m.end():]
        clean = ' '.join(clean.split())
    return clean, tags


def strip_trailing_hashtags(title):
    """Chỉ xóa hashtag ở CUỐI tên (kể cả trước 'Pt. N'). Hashtag giữa câu giữ nguyên, kể cả dấu #."""
    tokens = title.split()
    drop = set()
    i = len(tokens) - 1
    while i >= 0:
        if HASHTAG_TOKEN.fullmatch(tokens[i]):
            drop.add(i)
            i -= 1
        elif i >= 1 and _NUM.match(tokens[i].strip('.')) and _INDEX_WORD.match(tokens[i - 1]):
            i -= 2
        else:
            break
    return ' '.join(t for k, t in enumerate(tokens) if k not in drop)


def _index_suffix(clean):
    """Tách đuôi 'Pt. N' khỏi tên để tạo hashtag chỉ từ phần nội dung."""
    return re.sub(r'(?i)\s*\b(?:pt|part|ep|episode)\.?\s*\d+\s*$', '', clean)


# ============================================================ tạo hashtag
def generate_hashtags(title, max_tags=MAX_HASHTAGS, blocked=None):
    max_tags = max(1, min(MAX_HASHTAGS, int(max_tags)))
    blocked = load_blacklist() if blocked is None else blocked
    words = _index_suffix(title).split()
    n = len(words)
    doc = _spacy_doc(tuple(words)) if n else None
    cands = {}

    def add(tag, score, rank):
        key = tag.lower()
        body = key[1:]
        letters = sum(ch.isalpha() for ch in body)
        if not 3 <= len(key) <= 30 or body in blocked or letters < 3 or letters < len(body) - letters:
            return   # quá ngắn/dài, trong blacklist, hoặc toàn số (#30000ft)
        item = (rank, -score, tag)
        if key not in cands or item < cands[key]:
            cands[key] = item

    covered = set()
    if doc is not None:   # tên riêng nhiều từ -> 1 hashtag ghép (#elonmusk)
        for ent in doc.ents:
            if ent.label_ not in _ENT_LABELS or not 2 <= len(ent) <= 3:
                continue
            cores = [_core(words[i]) for i in range(ent.start, ent.end)]
            if any(_stem(c.lower()) in TOPIC_TAGS or c.lower() in TOPIC_TAGS for c in cores):
                continue
            if all(c.lower() in _STOP for c in cores):
                continue
            add('#' + re.sub(r'[\W_]+', '', ''.join(cores)).lower(), 1.8, 0)
            covered.update(range(ent.start, ent.end))

    for i, w in enumerate(words):
        if i in covered:
            continue
        core = _core(w)
        if not core or not re.search(r'[^\W\d_]', core):
            continue
        if re.search(r"['’]", core):          # don't, it's, I'm... -> bỏ
            continue
        parts = [p for p in re.split(r'[-‐–—]', core) if p]
        if len(parts) > 2:                    # Air-to-Air, Step-by-Step... -> bỏ
            continue
        cands_words = [''.join(parts)] if len(parts) == 2 and all(len(p) >= 3 for p in parts) else parts
        for cw in cands_words:
            low = cw.lower()
            stem = _stem(low)
            if low in blocked or stem in blocked:
                continue
            topic = TOPIC_TAGS.get(low) or TOPIC_TAGS.get(stem)
            if not topic and (low in _STOP or stem in _STOP or len(low) < 3):
                continue
            if doc is not None and len(cands_words) == 1 and len(parts) == 1:
                base = {'PROPN': 1.2, 'NOUN': 1.0, 'ADJ': 0.4, 'VERB': 0.2}.get(doc[i].pos_, 0.2)
            else:
                base = 0.65
                if low.endswith(('ed', 'ly')) and not topic:   # động từ quá khứ / trạng từ
                    base -= 0.35
                elif low.endswith('ing') and not topic:
                    base -= 0.1
            score = base + min(len(low), 10) / 20.0 + (1.0 - i / max(n, 1)) * 0.2
            if topic:
                score += 1.0
                for r, t in enumerate(topic):
                    add(t, score * (1.0 if r == 0 else 0.7), r)
            else:
                add('#' + re.sub(r'[\W_]+', '', low), score, 0)

    tags = []
    for rank, neg_score, tag in sorted(cands.values()):
        if len(tags) >= max_tags:
            break
        if tags and -neg_score < MIN_EXTRA_SCORE:
            continue
        tags.append(tag)
    if tags:
        return tags
    for w in words:   # không từ nào đạt -> lấy từ đầu tiên dùng được
        core = re.sub(r'[\W_]+', '', _core(w)).lower()
        if 3 <= len(core) <= 29 and core not in blocked and re.search(r'[^\W\d_]', core):
            return ['#' + core]
    return [] if 'shorts' in blocked else ['#shorts']


def prepare_title_and_hashtags(title, max_tags=MAX_HASHTAGS, retag=False, blocked=None):
    """(tên sạch, [hashtag], nguồn). retag=True: bỏ hashtag cũ và tạo lại."""
    clean, existing = split_hashtags(title)
    if not clean:
        clean = ' '.join(title.replace('#', ' ').split())
    if existing and not retag:
        return clean, existing[:MAX_HASHTAGS], 'title'
    return clean, generate_hashtags(clean, max_tags, blocked), 'auto'


# ============================================================ đổi tên file
def collect_videos(root, recursive=True):
    if os.path.isfile(root):
        if root.lower().endswith(VIDEO_EXTS):
            yield root
        return
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for f in sorted(filenames):
            if f.lower().endswith(VIDEO_EXTS):
                yield os.path.join(dirpath, f)
        if not recursive:
            break


def build_plan(files, max_tags, retag=False, strip=False, strip_end=False):
    require_spacy()
    jobs, skipped = [], []
    taken = set()
    blocked = load_blacklist()
    for path in files:
        folder, name = os.path.split(path)
        stem, ext = os.path.splitext(name)
        if strip_end:
            clean, tags, src = (strip_trailing_hashtags(stem) or stem), [], 'strip'
        elif strip:
            clean, _old = split_hashtags(stem)
            clean, tags, src = (clean or stem), [], 'strip'
        else:
            clean, tags, src = prepare_title_and_hashtags(stem, max_tags, retag, blocked)
        new_stem = f"{clean} {' '.join(tags)}" if tags else clean
        if new_stem == stem:
            skipped.append((path, 'đã đúng định dạng'))
            continue
        new_name = new_stem + ext
        target = os.path.join(folder, new_name)
        if len(new_name) > MAX_NAME_LEN or len(os.path.abspath(target)) > MAX_PATH_LEN:
            skipped.append((path, 'tên/đường dẫn mới quá dài'))
            continue
        key = os.path.normcase(target)
        same_file = os.path.exists(target) and os.path.samefile(path, target)
        if key in taken or (os.path.exists(target) and not same_file):
            skipped.append((path, f'trùng tên với file đã có: {new_name}'))
            continue
        taken.add(key)
        jobs.append({'old': path, 'new': target, 'tags': tags, 'src': src})
    return jobs, skipped


def undo(log_path):
    with open(log_path, 'r', encoding='utf-8') as f:
        entries = json.load(f)
    ok = fail = 0
    for e in reversed(entries):
        try:
            if os.path.exists(e['new']) and not os.path.exists(e['old']):
                os.rename(e['new'], e['old'])
                ok += 1
            else:
                print(f"  ⚠ Bỏ qua (không thấy file hoặc tên cũ đã bị chiếm): {e['new']}")
                fail += 1
        except OSError as ex:
            print(f"  ✘ Lỗi: {e['new']}: {ex}")
            fail += 1
    print(f'Hoàn tác xong: {ok} file, bỏ qua/lỗi: {fail}')


# ============================================================ GIAO DIỆN
import io
import queue
import threading
import contextlib
import tkinter as tk
from tkinter import ttk, filedialog, messagebox


MODES = [
    ('Thêm hashtag (giữ hashtag có sẵn, chưa có thì tự tạo)', 'add'),
    ('Tạo lại hashtag (bỏ hashtag cũ, tạo mới)', 'retag'),
    ('Xóa hashtag ở CUỐI tên (giữ hashtag giữa câu)', 'strip_end'),
    ('Xóa hết hashtag (hashtag giữa câu chỉ bỏ dấu #)', 'strip'),
]


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title('Rename Hashtag')
        self.geometry('1000x680')
        self.minsize(800, 520)
        self.q = queue.Queue()
        self.busy = False
        self.active = 0   # số luồng đang chạy

        self.path = tk.StringVar()
        self.mode = tk.StringVar(value='add')
        self.max_tags = tk.IntVar(value=3)
        self.recursive = tk.BooleanVar(value=True)
        self.status = tk.StringVar(value='Chọn thư mục rồi bấm "Xem trước".')

        self._build_ui()
        self.after(100, self._poll)
        self.log('Python ' + sys.version.split()[0] + ' | ' + sys.executable)
        threading.Thread(target=self._check_spacy, daemon=True).start()

    # ------------------------------------------------------------ UI
    def _build_ui(self):
        pad = {'padx': 8, 'pady': 4}
        top = ttk.Frame(self)
        top.pack(fill='x', **pad)
        ttk.Label(top, text='Thư mục / file:').pack(side='left')
        ttk.Entry(top, textvariable=self.path).pack(side='left', fill='x', expand=True, padx=6)
        ttk.Button(top, text='Chọn thư mục...', command=self.browse).pack(side='left')

        opt = ttk.LabelFrame(self, text='Chế độ')
        opt.pack(fill='x', **pad)
        for text, val in MODES:
            ttk.Radiobutton(opt, text=text, value=val, variable=self.mode).pack(anchor='w', padx=8)
        row = ttk.Frame(opt)
        row.pack(fill='x', padx=8, pady=4)
        ttk.Label(row, text='Số hashtag tối đa:').pack(side='left')
        ttk.Spinbox(row, from_=1, to=3, width=4, textvariable=self.max_tags).pack(side='left', padx=6)
        ttk.Checkbutton(row, text='Quét cả thư mục con', variable=self.recursive).pack(side='left', padx=16)

        btns = ttk.Frame(self)
        btns.pack(fill='x', **pad)
        self.btn_preview = ttk.Button(btns, text='Xem trước', command=self.preview)
        self.btn_apply = ttk.Button(btns, text='Đổi tên', command=self.apply)
        self.btn_undo = ttk.Button(btns, text='Hoàn tác (chọn file log)...', command=self.undo)
        for b in (self.btn_preview, self.btn_apply, self.btn_undo):
            b.pack(side='left', padx=(0, 6))

        mid = ttk.Frame(self)
        mid.pack(fill='both', expand=True, **pad)
        cols = ('old', 'new', 'src')
        self.tree = ttk.Treeview(mid, columns=cols, show='headings', selectmode='browse')
        self.tree.heading('old', text='Tên cũ (đường dẫn tương đối)')
        self.tree.heading('new', text='Tên mới')
        self.tree.heading('src', text='Nguồn')
        self.tree.column('old', width=380)
        self.tree.column('new', width=440)
        self.tree.column('src', width=90, anchor='center')
        ys = ttk.Scrollbar(mid, orient='vertical', command=self.tree.yview)
        xs = ttk.Scrollbar(mid, orient='horizontal', command=self.tree.xview)
        self.tree.configure(yscrollcommand=ys.set, xscrollcommand=xs.set)
        self.tree.grid(row=0, column=0, sticky='nsew')
        ys.grid(row=0, column=1, sticky='ns')
        xs.grid(row=1, column=0, sticky='ew')
        mid.rowconfigure(0, weight=1)
        mid.columnconfigure(0, weight=1)

        self.logbox = tk.Text(self, height=6, state='disabled', wrap='word')
        self.logbox.pack(fill='x', **pad)
        ttk.Label(self, textvariable=self.status, anchor='w').pack(fill='x', padx=8, pady=(0, 6))

    def _check_spacy(self):
        msg = spacy_status()
        self.q.put(('log', msg))
        self.q.put(('status', msg))

    # ------------------------------------------------------------ tiện ích
    def browse(self):
        p = filedialog.askdirectory(title='Chọn thư mục chứa video')
        if p:
            self.path.set(p)

    def log(self, text):
        self.logbox.configure(state='normal')
        self.logbox.insert('end', text + '\n')
        self.logbox.see('end')
        self.logbox.configure(state='disabled')

    def set_busy(self, busy):
        self.busy = busy
        st = 'disabled' if busy else 'normal'
        for b in (self.btn_preview, self.btn_apply, self.btn_undo):
            b.configure(state=st)

    def _get_opts(self):
        root = self.path.get().strip().strip('"')
        if not root or not os.path.exists(root):
            messagebox.showwarning('Thiếu thư mục', 'Hãy chọn thư mục (hoặc file video) hợp lệ.')
            return None
        try:
            mt = max(1, min(3, int(self.max_tags.get())))
        except (tk.TclError, ValueError):
            mt = 3
        m = self.mode.get()
        return dict(root=root, max_tags=mt, recursive=self.recursive.get(),
                    retag=(m == 'retag'), strip=(m == 'strip'), strip_end=(m == 'strip_end'))

    def run_thread(self, fn, *args):
        self.active += 1
        self.set_busy(True)
        threading.Thread(target=self._worker, args=(fn, *args), daemon=True).start()

    def _worker(self, fn, *args):
        try:
            fn(*args)
        except Exception as ex:   # báo lỗi về giao diện
            self.q.put(('error', str(ex)))
        finally:
            self.q.put(('idle', None))

    def _poll(self):
        try:
            while True:
                kind, data = self.q.get_nowait()
                if kind == 'log':
                    self.log(data)
                elif kind == 'status':
                    self.status.set(data)
                elif kind == 'plan':
                    self.fill_tree(*data)
                elif kind == 'confirm':
                    self._confirm_apply(*data)
                elif kind == 'error':
                    self.log('✘ Lỗi: ' + data)
                    self.status.set('Có lỗi, xem khung nhật ký.')
                elif kind == 'idle':
                    self.active = max(0, self.active - 1)
                    if self.active == 0:
                        self.set_busy(False)
        except queue.Empty:
            pass
        self.after(100, self._poll)

    def fill_tree(self, root, jobs, skipped):
        self.tree.delete(*self.tree.get_children())
        isdir = os.path.isdir(root)
        for j in jobs:
            old = os.path.relpath(j['old'], root) if isdir else j['old']
            label = {'title': 'có sẵn', 'strip': 'xóa'}.get(j['src'], 'tự tạo')
            self.tree.insert('', 'end', values=(old, os.path.basename(j['new']), label))
        others = [(p, w) for p, w in skipped if w != 'đã đúng định dạng']
        for p, w in others:
            self.log(f'⚠ Bỏ qua {p}: {w}')
        n_ok = len(skipped) - len(others)
        self.status.set(f'Sẽ đổi: {len(jobs)} | đã đúng: {n_ok} | bỏ qua khác: {len(others)}')

    # ------------------------------------------------------------ xem trước
    def _make_plan(self, o):
        files = list(collect_videos(o['root'], recursive=o['recursive']))
        self.q.put(('status', f'Đang quét {len(files)} video...'))
        jobs, skipped = build_plan(files, o['max_tags'], o['retag'], o['strip'], o['strip_end'])
        return files, jobs, skipped

    def preview(self):
        o = self._get_opts()
        if o:
            self.run_thread(self._preview, o)

    def _preview(self, o):
        files, jobs, skipped = self._make_plan(o)
        self.q.put(('log', f'Tìm thấy {len(files)} video trong: {o["root"]}'))
        self.q.put(('log', spacy_status()))
        self.q.put(('plan', (o['root'], jobs, skipped)))

    # ------------------------------------------------------------ đổi tên
    def apply(self):
        o = self._get_opts()
        if o:
            self.run_thread(self._prepare_apply, o)

    def _prepare_apply(self, o):
        files, jobs, skipped = self._make_plan(o)
        self.q.put(('plan', (o['root'], jobs, skipped)))
        if not jobs:
            self.q.put(('log', 'Không có file nào cần đổi.'))
            return
        self.q.put(('confirm', (jobs,)))

    def _confirm_apply(self, jobs):
        # chạy ở luồng giao diện (được gọi từ _poll)
        if not messagebox.askyesno('Xác nhận', f'Đổi tên {len(jobs)} file?\n(Có file log để hoàn tác.)'):
            self.log('Đã hủy.')
            return
        self.run_thread(self._do_rename, jobs)

    def _do_rename(self, jobs):
        done = []
        log_path = os.path.join(HERE, time.strftime('rename_hashtag_log_%Y%m%d_%H%M%S.json'))
        try:
            for k, j in enumerate(jobs, 1):
                try:
                    os.rename(j['old'], j['new'])
                    done.append({'old': j['old'], 'new': j['new']})
                except OSError as ex:
                    self.q.put(('log', f"✘ Lỗi đổi tên {j['old']}: {ex}"))
                if k % 50 == 0:
                    self.q.put(('status', f'Đang đổi tên {k}/{len(jobs)}...'))
        finally:
            with open(log_path, 'w', encoding='utf-8') as f:
                json.dump(done, f, ensure_ascii=False, indent=2)
        self.q.put(('log', f'Đã đổi tên {len(done)}/{len(jobs)} file. Log hoàn tác: {log_path}'))
        self.q.put(('status', f'Xong: {len(done)}/{len(jobs)} file.'))

    # ------------------------------------------------------------ hoàn tác
    def undo(self):
        p = filedialog.askopenfilename(title='Chọn file log để hoàn tác', initialdir=HERE,
                                       filetypes=[('Log JSON', '*.json'), ('Tất cả', '*.*')])
        if not p:
            return
        if not messagebox.askyesno('Xác nhận', f'Hoàn tác theo log:\n{os.path.basename(p)}?'):
            return
        self.run_thread(self._do_undo, p)

    def _do_undo(self, path):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            undo(path)
        for line in buf.getvalue().splitlines():
            self.q.put(('log', line))
        self.q.put(('status', 'Đã hoàn tác.'))


if __name__ == '__main__':
    if _get_nlp() is None:   # bắt buộc có spaCy, không có thì báo lỗi rồi thoát
        _r = tk.Tk()
        _r.withdraw()
        messagebox.showerror(
            'Thiếu spaCy',
            'Chương trình BẮT BUỘC phải có spaCy.\n\n' + _NLP_ERR +
            '\n\nCài đặt (dùng đúng Python này):\n' + sys.executable + ' -m pip install spacy\n' +
            sys.executable + ' -m spacy download en_core_web_sm')
        _r.destroy()
        sys.exit(1)
    App().mainloop()

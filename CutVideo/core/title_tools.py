"""Công cụ xử lý tiêu đề cho CutVideo Pro.

1) smart_wrap(): ngắt dòng thông minh, tối đa N dòng (mặc định 3).
   - Cụm từ trong protected_phrases.txt không bao giờ bị ngắt giữa chừng (trừ khi bất khả kháng).
   - Có spaCy (en_core_web_sm) thì dùng thêm phân tích ngữ pháp; KHÔNG có spaCy vẫn chạy bằng luật đơn giản.
2) Hashtag cho tên file:
   - split_hashtags(): tách hashtag có sẵn trong tiêu đề ra khỏi chữ.
   - generate_hashtags(): tự tạo 1-3 hashtag, bỏ qua các từ trong hashtag_blacklist.txt.
   - prepare_title_and_hashtags(): gộp 2 bước trên (dùng hashtag có sẵn nếu có, không thì tự tạo).

Hai file .txt tự được tạo (kèm nội dung mẫu) ở thư mục gốc dự án nếu chưa có, và được đọc lại mỗi khi
bạn sửa file (không cần khởi động lại phần mềm). Dòng bắt đầu bằng // là ghi chú.
"""
import os
import re
import sys
import string
import threading
from functools import lru_cache
from itertools import combinations

PROTECTED_FILE = 'protected_phrases.txt'
BLACKLIST_FILE = 'hashtag_blacklist.txt'
TITLE_MAX_LINES = 3
MAX_HASHTAGS = 3
MIN_EXTRA_SCORE = 0.9  # hashtag thứ 2, thứ 3 chỉ được thêm khi đủ "đáng giá"

DEFAULT_PROTECTED = """// Mỗi dòng là một cụm từ KHÔNG được ngắt xuống dòng (không phân biệt hoa/thường).
// Dòng bắt đầu bằng // là ghi chú. Mỗi cụm cần từ 2 từ trở lên.
HOW TO
TOP 10
TOP 5
PART 1
PART 2
PART 3
PT. 1
PT. 2
PT. 3
SECRET TUNNEL
UNDERGROUND CITY
ABANDONED HOUSE
HIDDEN CITY
REAL LIFE
NO ONE
IN THE
OF THE
A SECRET
THE SECRET
100 FAMILIES
217 PEOPLE
"""

DEFAULT_BLACKLIST = """// Mỗi dòng một từ KHÔNG được dùng để tạo hashtag (không phân biệt hoa/thường, có hay không có dấu # đều được).
// Dòng bắt đầu bằng // là ghi chú.
// Các từ nối thông dụng (the, and, of, in...) và động từ/tính từ chung chung (found, new, best...) đã được
// loại sẵn trong code; file này để bạn thêm những từ riêng của mình.
video
videos
full
official
episode
"""


# ---------------------------------------------------------------- đọc file danh sách
def _data_dirs():
    here = os.path.dirname(os.path.abspath(__file__))
    if getattr(sys, 'frozen', False):
        dirs = [os.path.dirname(sys.executable)]
        if getattr(sys, '_MEIPASS', None):
            dirs.append(sys._MEIPASS)
        return dirs
    return [os.path.dirname(here), here]  # thư mục gốc dự án trước, rồi thư mục core/


def data_file_path(name, default_text=None):
    """Đường dẫn file danh sách; chưa có thì tạo file mẫu ở thư mục gốc dự án."""
    dirs = _data_dirs()
    for d in dirs:
        p = os.path.join(d, name)
        if os.path.isfile(p):
            return p
    path = os.path.join(dirs[0], name)
    if default_text is None:
        default_text = {PROTECTED_FILE: DEFAULT_PROTECTED, BLACKLIST_FILE: DEFAULT_BLACKLIST}.get(name, '')
    try:
        with open(path, 'w', encoding='utf-8') as f:
            f.write(default_text)
    except OSError:
        pass
    return path


_LIST_CACHE = {}
_LIST_LOCK = threading.Lock()


def _read_list(name, default_text):
    path = data_file_path(name, default_text)
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        mtime = None
    with _LIST_LOCK:
        hit = _LIST_CACHE.get(path)
        if hit and hit[0] == mtime:
            return hit[1]
    try:
        with open(path, 'r', encoding='utf-8-sig', errors='replace') as f:
            raw = f.read()
    except OSError:
        raw = default_text
    lines = [ln.strip() for ln in raw.splitlines() if ln.strip() and not ln.strip().startswith('//')]
    with _LIST_LOCK:
        _LIST_CACHE[path] = (mtime, lines)
    return lines


_PUNCT = string.punctuation + '“”‘’…«»'


def _norm_word(w):
    return w.lower().strip(_PUNCT)


def load_protected_phrases():
    """Danh sách cụm từ không được ngắt dòng, mỗi cụm là tuple các từ đã chuẩn hóa."""
    phrases = []
    for line in _read_list(PROTECTED_FILE, DEFAULT_PROTECTED):
        ws = tuple(w for w in (_norm_word(x) for x in line.split()) if w)
        if len(ws) >= 2:
            phrases.append(ws)
    return phrases


def load_hashtag_blacklist():
    out = set()
    for line in _read_list(BLACKLIST_FILE, DEFAULT_BLACKLIST):
        w = line.strip().lstrip('#').lower()
        if w:
            out.add(w)
    return out


# ---------------------------------------------------------------- spaCy (tùy chọn)
_NLP = None
_NLP_TRIED = False
_NLP_LOCK = threading.Lock()


def _get_nlp():
    global _NLP, _NLP_TRIED
    with _NLP_LOCK:
        if not _NLP_TRIED:
            _NLP_TRIED = True
            try:
                import spacy
                _NLP = spacy.load('en_core_web_sm')
            except Exception:
                _NLP = None  # không có spaCy / model -> dùng luật đơn giản
        return _NLP


@lru_cache(maxsize=256)
def _spacy_doc(words):
    """Doc của spaCy khớp 1-1 với danh sách từ (tách theo khoảng trắng) hoặc None."""
    nlp = _get_nlp()
    if nlp is None or not words:
        return None
    try:
        from spacy.tokens import Doc
        doc = Doc(nlp.vocab, words=list(words))
        with _NLP_LOCK:
            for _, proc in nlp.pipeline:
                doc = proc(doc)
        return doc
    except Exception:
        return None


# ---------------------------------------------------------------- ngắt dòng thông minh
_NO_END = {
    'a', 'an', 'the', 'this', 'that', 'these', 'those', 'my', 'your', 'his', 'her', 'its', 'our', 'their',
    'of', 'in', 'on', 'at', 'to', 'for', 'from', 'by', 'with', 'without', 'about', 'into', 'over', 'under',
    'and', 'or', 'but', 'nor', 'so', 'as', 'than', 'if', 'because', 'while', 'when', 'where', 'who', 'whose',
    'is', 'are', 'was', 'were', 'be', 'been', 'am', 'do', 'does', 'did', 'has', 'have', 'had',
    'will', 'would', 'can', 'could', 'should', 'must', 'not', 'no', 'i', 'we', 'you', 'they', 'he', 'she',
}
_INDEX_WORDS = {'pt', 'part', 'ep', 'episode', 'vol', 'volume', 'chapter', 'season', 'day', 'no'}
_GOOD_BREAK_END = (',', ':', ';', '?', '!', '|', '.', '…', '–', '—')


def _builtin_spans(words, norm):
    """Cụm luôn đi cùng nhau: 'Pt. 1', 'Part 2', '(1)', '1편'... (đoạn cuối tên)."""
    n = len(words)
    spans = []
    for i in range(n - 1):
        if norm[i] in _INDEX_WORDS and re.fullmatch(r'\d+|[ivx]+', norm[i + 1] or ''):
            spans.append((i, i + 2))
    if n >= 3 and re.fullmatch(r'[\(\[#\-]?\d+[\)\]]?(편|화|부)?', words[-1]):
        spans.append((n - 2, n))
    return spans


def _protected_spans(norm, phrases):
    spans = []
    for ph in phrases:
        k = len(ph)
        for i in range(len(norm) - k + 1):
            if tuple(norm[i:i + k]) == ph:
                spans.append((i, i + k))
    return spans


def _heuristic_penalty(words, i):
    """Phạt 0..1 khi ngắt dòng TRƯỚC từ thứ i (không cần spaCy)."""
    left_raw, right_raw = words[i - 1], words[i]
    left = _norm_word(left_raw)
    if left_raw == '|':
        return 0.0
    if not any(ch.isalnum() for ch in left_raw):          # '&', '+', '-' ở cuối dòng
        return 1.0
    if not any(ch.isalnum() for ch in right_raw):         # dòng mới bắt đầu bằng dấu câu
        return 1.0
    if left in _NO_END and not left_raw.endswith(_GOOD_BREAK_END):
        return 1.0
    if left.isdigit():                                    # '217 | People'
        return 0.8
    if left_raw.endswith(_GOOD_BREAK_END):                # ngắt sau dấu phẩy, hai chấm... là tự nhiên nhất
        return 0.0
    return 0.2


@lru_cache(maxsize=256)
def _break_penalties(words):
    """pens[i] = mức phạt (0..1) khi xuống dòng ngay trước từ thứ i."""
    n = len(words)
    pens = [0.0] * (n + 1)
    for i in range(1, n):
        pens[i] = _heuristic_penalty(words, i)
    doc = _spacy_doc(words)
    if doc is None:
        return tuple(pens)
    chunks = []
    try:
        chunks += [(c.start, c.end) for c in doc.noun_chunks if len(c) > 1]
    except Exception:
        pass
    chunks += [(e.start, e.end) for e in doc.ents if len(e) > 1]
    for i in range(1, n):
        left, right = doc[i - 1], doc[i]
        s = 0.0
        if left.pos_ in ('DET', 'ADP', 'CCONJ', 'SCONJ', 'PART'):
            s = 1.0
        elif left.pos_ == 'AUX':
            s = 0.8
        elif left.pos_ == 'PRON':
            s = 0.6
        if left.pos_ == 'ADJ' and right.pos_ in ('NOUN', 'PROPN'):
            s = max(s, 0.7)
        if left.like_num and right.pos_ in ('NOUN', 'PROPN'):
            s = max(s, 0.8)
        if any(a < i < b for a, b in chunks):
            s = max(s, 0.9)
        if words[i - 1].endswith(_GOOD_BREAK_END):
            s = 0.0
        pens[i] = min(1.0, max(pens[i], s))
    return tuple(pens)


def smart_wrap(text, measure, max_width, max_lines=TITLE_MAX_LINES, protected=None,
               min_width_ratio=0.25, balance_weight=0.45, length_weight=0.20,
               word_weight=0.10, semantic_weight=0.25):
    """Chia tiêu đề thành 1..max_lines dòng, mỗi dòng rộng <= max_width (đơn vị của measure).

    measure(str) -> độ rộng. protected: kết quả của load_protected_phrases().
    Ưu tiên: ít dòng nhất mà không ngắt cụm bảo vệ / không ngắt chỗ xấu (sau 'the', 'of', 'and'...),
    trong số đó chọn cách chia cân đối nhất. Không có cách nào thì nới dần các ràng buộc.
    Trả về None nếu không thể chia vừa trong max_lines dòng (để nơi gọi giảm cỡ chữ).
    """
    words = text.split()
    n = len(words)
    if n == 0:
        return []
    full = ' '.join(words)
    if measure(full) <= max_width:
        return [full]
    max_lines = max(1, min(int(max_lines), n))
    if n == 1 or max_lines < 2:
        return None

    cache = {}

    def width(a, b):
        if (a, b) not in cache:
            cache[(a, b)] = measure(' '.join(words[a:b]))
        return cache[(a, b)]

    if any(width(i, i + 1) > max_width for i in range(n)):
        return None  # có từ dài hơn cả dòng

    norm = [_norm_word(w) for w in words]
    spans = _builtin_spans(words, norm)
    if protected:
        spans += _protected_spans(norm, protected)
    bad_break = {b for b in range(1, n) if any(a < b < c for a, c in spans)}
    pens = _break_penalties(tuple(words))

    # level 0: tránh cả cụm bảo vệ lẫn chỗ ngắt xấu | level 1: chỉ tránh cụm bảo vệ | level 2: nới hết
    for level in (0, 1, 2):
        ratio = min_width_ratio if level < 2 else 0.0
        for k in range(2, max_lines + 1):
            best, best_score = None, float('inf')
            for brk in combinations(range(1, n), k - 1):
                if level < 2 and any(b in bad_break for b in brk):
                    continue
                if level < 1 and any(pens[b] >= 1.0 for b in brk):
                    continue
                pos = (0,) + brk + (n,)
                ws, ok = [], True
                for a, b in zip(pos, pos[1:]):
                    w = width(a, b)
                    if w > max_width or w < ratio * max_width:
                        ok = False
                        break
                    ws.append(w)
                if not ok:
                    continue
                max_w, min_w = max(ws), min(ws)
                wc = [b - a for a, b in zip(pos, pos[1:])]
                score = (balance_weight * (max_w - min_w) / max_w
                         + length_weight * (max_w - sum(ws) / len(ws)) / max_w
                         + word_weight * (max(wc) - min(wc)) / max(wc)
                         + semantic_weight * sum(pens[b] for b in brk) / len(brk)
                         + 0.04 * len(brk))
                if level == 2:
                    score += 2.0 * sum(1 for a, c in spans if any(a < b < c for b in brk))
                if score < best_score:
                    best, best_score = pos, score
            if best is not None:
                return [' '.join(words[a:b]) for a, b in zip(best, best[1:])]
    return None


# ---------------------------------------------------------------- hashtag
HASHTAG_RE = re.compile(r'(?<![\w#])#(?=\w*[^\W\d_])\w+', re.UNICODE)

_STOP = set("""
a an the and or but nor so yet of in on at to for from by with without about against between into through during
before after above below up down out off over under again further then once here there when where why how all any
both each few more most other some such no not only own same than too very can will just don should now is are was
were be been being am do does did doing have has had having i me my we our you your he him his she her it its they
them their what which who whom this that these those would could might must shall may also still even ever never
always often really actually if as while because
found find finds finding discovered discover discovers live lives living lived made make makes making get gets got
getting go goes went going gone come comes came take takes took taken see sees saw seen look looks looked know knew
known tell tells told say says said show shows showed shown want wants wanted need needs needed try tried tries
let lets put puts keep kept give gave given use used uses
one two three four five six seven eight nine ten new old big small good bad great best worst real first last next
many much every everyone everything something anything nothing someone anyone thing things way ways people person
day days time times year years part pt episode ep video videos full official vs versus
""".split())

TOPIC_TAGS = {  # từ khóa -> hashtag phổ biến (cái đầu tiên là chính); sửa/ thêm tùy kênh của bạn
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
}

_ENT_LABELS = {'PERSON', 'ORG', 'GPE', 'EVENT', 'PRODUCT'}


def _stem(w):
    if w.endswith('ies') and len(w) > 4:
        return w[:-3] + 'y'
    if w.endswith(('sses', 'xes', 'ches', 'shes')):
        return w[:-2]
    if w.endswith('s') and not w.endswith(('ss', 'us', 'is')) and len(w) > 3:
        return w[:-1]
    return w


def split_hashtags(title):
    """(tiêu đề đã bỏ hashtag, [hashtag có sẵn]). '#1' hay 'C#' không bị coi là hashtag."""
    tags, seen = [], set()
    for m in HASHTAG_RE.finditer(title):
        t = m.group(0)
        if t.lower() not in seen:
            seen.add(t.lower())
            tags.append(t)
    clean = HASHTAG_RE.sub(' ', title)
    clean = ' '.join(clean.split()).strip(' |-–—:,;·•/')
    return clean, tags


def _core(word):
    core = word.strip(_PUNCT)
    return re.sub(r"['’]s$", '', core)


def generate_hashtags(title, max_tags=MAX_HASHTAGS):
    """Tự tạo 1..max_tags hashtag (tối đa 3) từ tiêu đề; bỏ qua từ trong hashtag_blacklist.txt."""
    max_tags = max(1, min(MAX_HASHTAGS, int(max_tags)))
    blocked = load_hashtag_blacklist()
    words = title.split()
    n = len(words)
    doc = _spacy_doc(tuple(words)) if n else None
    cands = {}  # tag(lower) -> (rank, -score, tag)

    def add(tag, score, rank):
        key = tag.lower()
        if not 3 <= len(key) <= 30 or key[1:] in blocked:  # quá ngắn / quá dài thì bỏ
            return
        item = (rank, -score, tag)
        if key not in cands or item < cands[key]:
            cands[key] = item

    covered = set()
    if doc is not None:  # tên người/tổ chức/địa danh nhiều từ -> một hashtag ghép (#elonmusk)
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
        low = core.lower()
        stem = _stem(low)
        if low in blocked or stem in blocked:
            continue
        topic = TOPIC_TAGS.get(low) or TOPIC_TAGS.get(stem)
        if not topic and (low in _STOP or stem in _STOP or len(low) < 3):
            continue
        if doc is not None:
            base = {'PROPN': 1.2, 'NOUN': 1.0, 'ADJ': 0.5, 'VERB': 0.3}.get(doc[i].pos_, 0.2)
        else:
            base = 0.8
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
    for w in words:  # không có từ khóa nào đạt -> lấy từ đầu tiên dùng được
        core = re.sub(r'[\W_]+', '', _core(w)).lower()
        if 3 <= len(core) <= 29 and core not in blocked and re.search(r'[^\W\d_]', core):
            return ['#' + core]
    return [] if 'shorts' in blocked else ['#shorts']


def prepare_title_and_hashtags(title, max_tags=MAX_HASHTAGS):
    """Trả về (tiêu đề sạch để chèn lên video, [hashtag], nguồn).
    nguồn: 'title' (hashtag có sẵn trong tiêu đề, dùng luôn tối đa 3 cái) hoặc 'auto' (tự tạo)."""
    clean, existing = split_hashtags(title)
    if not clean:  # tiêu đề chỉ toàn hashtag -> giữ chữ, bỏ dấu #
        clean = ' '.join(title.replace('#', ' ').split())
    if existing:
        return clean, existing[:MAX_HASHTAGS], 'title'
    return clean, generate_hashtags(clean, max_tags), 'auto'

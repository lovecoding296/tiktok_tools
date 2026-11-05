import os, re, json, time, random, asyncio, unicodedata, urllib.parse
from difflib import SequenceMatcher
from playwright.async_api import async_playwright

# ================== CẤU HÌNH ==================
TITLES_FILE = "titles.txt"
PROFILE_DIR = "tiktok_profile"        # dùng TÀI KHOẢN TIKTOK RIÊNG, không dùng tài khoản kênh chính
OUT_OK = "titles_chua_dang.txt"
OUT_REPORT = "ket_qua.json"
CACHE_FILE = "cache.json"

HEADLESS = False
CONCURRENCY = 4                       # số tab song song (3-5 cho 900 tiêu đề/ngày)
BATCH = 24                            # số tiêu đề mỗi lô
PAUSE_BATCH = (45, 90)                # nghỉ ngẫu nhiên giữa các lô (giây)
BLOCK_COOLDOWN = 600                  # nghỉ khi nghi bị chặn (giây)
MAX_ROUNDS = 3                        # số vòng thử lại tiêu đề LOI
SCROLLS = 4
QUERY_WORDS = 8                       # tìm bằng N từ đầu của tiêu đề (0 = dùng cả tiêu đề)

EXACT, NEAR, TOP_N = 0.85, 0.55, 3
FALLBACK_DUP_THRESHOLD = 0.80         # không có API key: >= mức này coi là trùng


# ================== SO KHỚP ==================
def norm(s):
    s = unicodedata.normalize("NFKD", s.lower())
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"#\w+", " ", s)
    s = re.sub(r"[^\w\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def score(a, b):
    na, nb = norm(a), norm(b)
    if not na or not nb:
        return 0.0
    if na in nb or nb in na:
        return 1.0
    sa, sb = set(na.split()), set(nb.split())
    return max(SequenceMatcher(None, na, nb).ratio(), len(sa & sb) / len(sa) * 0.9)


def top_matches(title, results):
    seen, sc = set(), []
    for r in results:
        if r["url"] in seen:
            continue
        seen.add(r["url"])
        sc.append((score(title, r["title"]), r))
    sc.sort(key=lambda x: x[0], reverse=True)
    return sc[:TOP_N]


def decide(title, top):
    if top and top[0][0] >= THRESHOLD:
        return "DA_CO", top[0][1]
    return "CHUA_DANG", None


# ================== TIKTOK ==================
JS_EXTRACT = """() => {
    const seen = new Set(), out = [];
    document.querySelectorAll('a[href*="/video/"]').forEach(a => {
        if (seen.has(a.href)) return; seen.add(a.href);
        const box = a.closest('[data-e2e="search_top-item"], [data-e2e="search_video-item"], div') || a;
        const img = a.querySelector('img');
        out.push({url: a.href, title: ((img && img.alt) || box.innerText || a.innerText || '').trim()});
    });
    return out;
}"""


async def is_logged_in(ctx):
    return any(c["name"] == "sessionid" for c in await ctx.cookies("https://www.tiktok.com"))


async def ensure_login(ctx, page, timeout=300):
    await page.goto("https://www.tiktok.com/")
    await asyncio.sleep(3)
    if await is_logged_in(ctx):
        return
    print("Chưa đăng nhập: hãy đăng nhập trong cửa sổ Chrome (chờ tối đa 5 phút)...")
    t0 = time.time()
    while time.time() - t0 < timeout:
        if await is_logged_in(ctx):
            print("Đã đăng nhập.")
            return
        await asyncio.sleep(2)
    raise SystemExit("Hết thời gian chờ đăng nhập.")


def make_query(title):
    w = title.split()
    return " ".join(w[:QUERY_WORDS]) if QUERY_WORDS else title


async def search_tiktok(page, title):
    await page.goto("https://www.tiktok.com/search/video?q=" + urllib.parse.quote(make_query(title)),
                    wait_until="domcontentloaded")
    await asyncio.sleep(random.uniform(3.5, 5))
    for _ in range(SCROLLS):
        await page.mouse.wheel(0, 3000)
        await asyncio.sleep(random.uniform(1.2, 2.2))
    return await page.evaluate(JS_EXTRACT)


async def check_title(page, t):
    try:
        results = await search_tiktok(page, t)
        if not results:                      # thử lại 1 lần
            await asyncio.sleep(8)
            results = await search_tiktok(page, t)
        if not results:                      # 0 kết quả = nghi bị chặn, KHÔNG coi là chưa đăng
            status, item = "LOI", None
        else:
            status, item = decide(t, top_matches(t, results))
    except Exception as e:
        print("  Lỗi:", t[:50], e)
        status, item, results = "LOI", None, []

    print(f"[{status}] {t[:80]}" + (f"\n    -> {item['url']}" if item else ""))
    return {"title": t, "status": status, "scanned": len(results),
            "match_url": item["url"] if item else None,
            "match_caption": item["title"][:150] if item else None}


async def run_batch(pages, batch):
    """Mỗi tab (worker) lấy tiêu đề từ hàng đợi, giữa các tiêu đề nghỉ ngẫu nhiên."""
    q = asyncio.Queue()
    for t in batch:
        q.put_nowait(t)
    out = []

    async def worker(page, delay):
        await asyncio.sleep(delay)
        while True:
            try:
                t = q.get_nowait()
            except asyncio.QueueEmpty:
                return
            out.append(await check_title(page, t))
            await asyncio.sleep(random.uniform(2, 5))

    await asyncio.gather(*[worker(p, i * 1.5) for i, p in enumerate(pages)])
    return out


# ================== CACHE ==================
def load_cache():
    try:
        with open(CACHE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def save_cache(cache):
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=1)


# ================== MAIN ==================
async def main():
    with open(TITLES_FILE, encoding="utf-8") as f:
        titles = list(dict.fromkeys(l.strip() for l in f if l.strip()))   # bỏ trùng
    cache = load_cache()

    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            PROFILE_DIR, headless=HEADLESS,
            args=["--disable-blink-features=AutomationControlled"],
            viewport={"width": 1280, "height": 900})
        first = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await ensure_login(ctx, first)
        pages = [first] + [await ctx.new_page() for _ in range(CONCURRENCY - 1)]

        for rnd in range(1, MAX_ROUNDS + 1):
            todo = [t for t in titles if cache.get(t, {}).get("status") in (None, "LOI")]
            if not todo:
                break
            print(f"\n=== Vòng {rnd}: còn {len(todo)}/{len(titles)} tiêu đề ===")

            for i in range(0, len(todo), BATCH):
                res = await run_batch(pages, todo[i:i + BATCH])
                for r in res:
                    cache[r["title"]] = r
                save_cache(cache)            # lưu sau mỗi lô -> ngắt giữa chừng vẫn chạy tiếp được

                bad = sum(r["status"] == "LOI" for r in res)
                if bad > len(res) * 0.5:
                    print(f"!! Nghi bị chặn ({bad}/{len(res)} lỗi). Nghỉ {BLOCK_COOLDOWN // 60} phút. "
                          "Nếu thấy captcha trong Chrome hãy giải thủ công.")
                    await asyncio.sleep(BLOCK_COOLDOWN)
                else:
                    await asyncio.sleep(random.uniform(*PAUSE_BATCH))
        await ctx.close()

    ok = [t for t in titles if cache.get(t, {}).get("status") == "CHUA_DANG"]
    dup = [t for t in titles if cache.get(t, {}).get("status") == "DA_CO"]
    err = [t for t in titles if cache.get(t, {}).get("status") in (None, "LOI")]
    with open(OUT_OK, "w", encoding="utf-8") as f:
        f.write("\n".join(ok))
    with open(OUT_REPORT, "w", encoding="utf-8") as f:
        json.dump([cache[t] for t in titles if t in cache], f, ensure_ascii=False, indent=1)
    print(f"\nXong: {len(ok)} chưa đăng | {len(dup)} đã có | {len(err)} chưa kiểm tra được")
    print(f"-> {OUT_OK}")


if __name__ == "__main__":
    asyncio.run(main())
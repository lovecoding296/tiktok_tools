import os
import sys

# Thư mục chứa exe (hoặc file .py khi chạy thường)
BASE = os.path.dirname(sys.executable if getattr(sys, "frozen", False)
                       else os.path.abspath(__file__))
BROWSERS_DIR = os.path.join(BASE, "browsers")
if os.path.isdir(BROWSERS_DIR):
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = BROWSERS_DIR
elif getattr(sys, "frozen", False):
    # exe mà thiếu thư mục browsers: dùng bản cài sẵn của máy (nếu có)
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = os.path.join(
        os.environ.get("LOCALAPPDATA", ""), "ms-playwright")

import configparser
import json
import random
import re
import time

import gspread
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# ================= CẤU HÌNH =================
CONFIG_FILE = os.path.join(BASE, "config.ini")
SERVICE_ACCOUNT_FILE = os.path.join(BASE, "service_account.json")
PROFILE_DIR = os.path.join(BASE, "tiktok_profile_firefox")  # profile riêng cho Firefox
DELAY = (0.3, 0.8)           # nghỉ ngẫu nhiên giữa các kênh (giây)
BATCH_SIZE = 10              # ghi lên sheet mỗi N dòng
# ============================================

READY_SELECTOR = '#__UNIVERSAL_DATA_FOR_REHYDRATION__, [data-e2e="followers-count"]'
NOT_FOUND_RE = re.compile(r"Couldn.t find this account|Không thể tìm thấy tài khoản", re.I)


def col_to_index(value: str, name: str) -> int:
    """'I' -> 9, 'aa' -> 27, '9' -> 9"""
    value = (value or "").strip()
    if value.isdigit() and int(value) >= 1:
        return int(value)
    if re.fullmatch(r"[A-Za-z]{1,3}", value):
        return gspread.utils.a1_to_rowcol(value.upper() + "1")[1]
    raise SystemExit(f"Cột '{name}' trong {CONFIG_FILE} không hợp lệ: '{value}' (dùng chữ như I, J hoặc số như 9, 10)")


def load_config(path: str) -> dict:
    cp = configparser.ConfigParser(interpolation=None)
    if not cp.read(path, encoding="utf-8-sig"):
        raise SystemExit(f"Không tìm thấy file {path}, hãy tạo file cấu hình cùng thư mục với script.")
    try:
        url = cp.get("sheet", "url").strip()
        if not url:
            raise ValueError
    except Exception:
        raise SystemExit(f"Mục [sheet] url trong {path} đang trống.")

    note = cp.get("columns", "note", fallback="").strip()
    try:
        headless = cp.getboolean("browser", "headless", fallback=False)
    except ValueError:
        raise SystemExit(f"Mục [browser] headless trong {path} không hợp lệ (dùng true hoặc false).")
    return {
        "headless": headless,
        "url": url,
        "worksheet": cp.get("sheet", "worksheet", fallback="").strip() or None,
        "link_col": col_to_index(cp.get("columns", "link", fallback="I"), "link"),
        "out_col": col_to_index(cp.get("columns", "output", fallback="J"), "output"),
        "note_col": col_to_index(note, "note") if note else None,
        "start_row": cp.getint("columns", "start_row", fallback=2),
    }


def extract_username(link: str) -> str:
    link = (link or "").strip()
    m = re.search(r"tiktok\.com/@([A-Za-z0-9._]+)", link, re.I)
    if m:
        return m.group(1)
    m = re.fullmatch(r"@?([A-Za-z0-9._]{2,})", link)
    return m.group(1) if m else ""


def parse_count(text: str):
    """'96.4M' -> 96400000, '15,3K' -> 15300, '8,245' -> 8245"""
    m = re.fullmatch(r"\s*([\d.,]+)\s*([KMB]?)\s*", text or "", re.I)
    if not m:
        return None
    num, suf = m.group(1), m.group(2).upper()
    if suf:
        try:
            return int(round(float(num.replace(",", ".")) * {"K": 1e3, "M": 1e6, "B": 1e9}[suf]))
        except ValueError:
            return None
    digits = re.sub(r"[.,]", "", num)
    return int(digits) if digits.isdigit() else None


def get_followers(page, username: str):
    """Trả về (giá_trị_cột_output, ghi_chú).
    Thành công: (số follower, "" hoặc "Số làm tròn").
    Lỗi: (nội dung lỗi, "")."""
    url = f"https://www.tiktok.com/@{username}"
    for attempt in range(2):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=20000)
            page.wait_for_selector(READY_SELECTOR, state="attached", timeout=15000)
        except PWTimeout:
            pass
        except Exception:
            time.sleep(1)

        # Cách 1: JSON nhúng (số chính xác)
        try:
            raw = page.evaluate(
                "document.getElementById('__UNIVERSAL_DATA_FOR_REHYDRATION__')?.textContent")
            if raw:
                detail = json.loads(raw).get("__DEFAULT_SCOPE__", {}).get("webapp.user-detail", {})
                stats = (detail.get("userInfo") or {}).get("stats")
                if stats and "followerCount" in stats:
                    return int(stats["followerCount"]), ""
        except Exception:
            pass

        # Cách 2: thẻ hiển thị trên giao diện (dự phòng, có thể là số làm tròn)
        try:
            el = page.query_selector('[data-e2e="followers-count"]')
            if el:
                raw_txt = (el.get_attribute("title") or el.inner_text()).strip()
                n = parse_count(raw_txt)
                if n is not None:
                    return n, ("Số làm tròn" if re.search(r"[KMB]$", raw_txt, re.I) else "")
        except Exception:
            pass

        # Cách 3: kiểm tra "không tìm thấy"
        try:
            body = page.inner_text("body", timeout=3000)
        except Exception:
            body = ""
        if NOT_FOUND_RE.search(body):
            return "Lỗi: Không tìm thấy kênh", ""

        time.sleep(1.5)

    return "Lỗi: Không đọc được dữ liệu", ""


def write_batch(ws, rows, out_col, note_col):
    """rows: list of (row_number, value, note) -> ghi 1 lần lên sheet"""
    if not rows:
        return
    data = []
    for r, value, note in rows:
        data.append({"range": gspread.utils.rowcol_to_a1(r, out_col), "values": [[value]]})
        if note_col:
            data.append({"range": gspread.utils.rowcol_to_a1(r, note_col), "values": [[note]]})
    try:
        ws.batch_update(data)
    except Exception as e:
        print("Lỗi ghi sheet, thử lại sau 10s:", e)
        time.sleep(10)
        ws.batch_update(data)


def main():
    # Dùng: python tiktok_followers.py [dòng_đầu] [dòng_cuối]
    cfg = load_config(CONFIG_FILE)
    first = int(sys.argv[1]) if len(sys.argv) > 1 else cfg["start_row"]
    last_arg = int(sys.argv[2]) if len(sys.argv) > 2 else None
    out_col, note_col = cfg["out_col"], cfg["note_col"]

    gc = gspread.service_account(filename=SERVICE_ACCOUNT_FILE)
    sh = gc.open_by_url(cfg["url"])
    ws = sh.worksheet(cfg["worksheet"]) if cfg["worksheet"] else sh.sheet1

    links = ws.col_values(cfg["link_col"])
    last = min(last_arg, len(links)) if last_arg else len(links)

    pending = []
    done = 0

    with sync_playwright() as p:
        ctx = p.firefox.launch_persistent_context(
            PROFILE_DIR,
            headless=cfg["headless"],
            locale="en-US",
            viewport={"width": 1280, "height": 800},
            firefox_user_prefs={"dom.webdriver.enabled": False},
        )

        # Chặn ảnh/video/font/css cho nhanh (vẫn giữ script để qua trang WAF)
        def block_heavy(route):
            if route.request.resource_type in ("image", "media", "font", "stylesheet"):
                route.abort()
            else:
                route.continue_()

        ctx.route("**/*", block_heavy)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        try:
            for r in range(first, last + 1):
                link = links[r - 1].strip() if r - 1 < len(links) else ""
                if not link or re.search(r"tiktok\.com/@?$", link, re.I):
                    continue  # dòng trống

                username = extract_username(link)
                if not username:
                    value, note = "Lỗi: Link không hợp lệ", ""
                else:
                    value, note = get_followers(page, username)

                pending.append((r, value, note))
                done += 1
                print(f"[{r}] @{username or '?'} -> {value}")

                if len(pending) >= BATCH_SIZE:
                    write_batch(ws, pending, out_col, note_col)
                    pending.clear()

                time.sleep(random.uniform(*DELAY))
        finally:
            write_batch(ws, pending, out_col, note_col)  # ghi nốt phần còn lại
            ctx.close()

    print(f"Hoàn tất {done} dòng.")


if __name__ == "__main__":
    try:
        main()
    except SystemExit as e:
        if e.code not in (None, 0):
            print(e.code)
    except Exception:
        import traceback
        traceback.print_exc()
    input("\nNhấn Enter để thoát...")
"""Đổi tên video hàng loạt: thêm 1-3 hashtag vào cuối tên file, GIỮ NGUYÊN vị trí file.

Dùng chung logic với CutVideo Pro (core/title_tools.py):
  - Tên đã có #hashtag  -> dùng luôn các hashtag đó (tối đa 3), dời chúng xuống cuối tên.
  - Tên chưa có hashtag -> tự tạo từ khóa phổ biến, bỏ qua các từ trong hashtag_blacklist.txt.
Chạy lại nhiều lần vẫn an toàn: file đã có hashtag ở cuối thì được bỏ qua.

Cách dùng (đặt file này ở thư mục gốc dự án, cạnh thư mục core/):
  python rename_hashtag.py                      -> hiện hộp thoại chọn thư mục
  python rename_hashtag.py "D:\\xay kenh\\US"     -> quét thư mục (cả thư mục con)
  python rename_hashtag.py "D:\\..." --dry-run    -> chỉ xem trước, không đổi tên
  python rename_hashtag.py "D:\\..." --yes        -> đổi luôn, không hỏi xác nhận
  python rename_hashtag.py --undo rename_hashtag_log_20260101_120000.json   -> hoàn tác một lần đổi
Tùy chọn khác: --max-tags 1..3 (mặc định 3), --no-recursive (không quét thư mục con).
"""
import os
import sys
import json
import time
import argparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from core.title_tools import prepare_title_and_hashtags  # noqa: E402

VIDEO_EXTS = ('.mp4', '.mkv', '.avi', '.mov', '.webm')
MAX_NAME_LEN = 240  # giới hạn độ dài tên file (Windows tối đa 255)

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass


def pick_folder():
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.attributes('-topmost', True)
        path = filedialog.askdirectory(title='Chọn thư mục chứa video cần thêm hashtag')
        root.destroy()
        return path
    except Exception:
        return input('Nhập đường dẫn thư mục: ').strip().strip('"')


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


def build_plan(files, max_tags):
    """Trả về (danh sách việc cần đổi, danh sách bỏ qua kèm lý do)."""
    jobs, skipped = [], []
    taken = set()  # các đường dẫn đích đã dùng trong đợt này
    for path in files:
        folder, name = os.path.split(path)
        stem, ext = os.path.splitext(name)
        clean, tags, src = prepare_title_and_hashtags(stem, max_tags)
        new_stem = f"{clean} {' '.join(tags)}" if tags else clean
        if new_stem == stem:
            skipped.append((path, 'đã đúng định dạng'))
            continue
        new_name = new_stem + ext
        if len(new_name) > MAX_NAME_LEN:
            skipped.append((path, 'tên mới quá dài'))
            continue
        target = os.path.join(folder, new_name)
        key = os.path.normcase(target)
        same_file = os.path.exists(target) and os.path.samefile(path, target)  # chỉ đổi hoa/thường
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


def main():
    ap = argparse.ArgumentParser(description='Thêm hashtag vào cuối tên video hàng loạt (giữ nguyên vị trí file).')
    ap.add_argument('path', nargs='?', help='thư mục (hoặc 1 file video) cần xử lý')
    ap.add_argument('--max-tags', type=int, default=3, help='số hashtag tối đa, 1-3 (mặc định 3)')
    ap.add_argument('--no-recursive', action='store_true', help='không quét thư mục con')
    ap.add_argument('--dry-run', action='store_true', help='chỉ xem trước, không đổi tên')
    ap.add_argument('--yes', action='store_true', help='không hỏi xác nhận')
    ap.add_argument('--undo', metavar='LOG', help='hoàn tác theo file log của lần đổi trước')
    args = ap.parse_args()

    if args.undo:
        undo(args.undo)
        return

    root = args.path or pick_folder()
    if not root or not os.path.exists(root):
        print('Không có thư mục hợp lệ.')
        return
    files = list(collect_videos(root, recursive=not args.no_recursive))
    print(f'Tìm thấy {len(files)} video trong: {root}')
    jobs, skipped = build_plan(files, args.max_tags)

    for j in jobs:
        label = 'có sẵn' if j['src'] == 'title' else 'tự tạo'
        print(f"\n  {os.path.relpath(j['old'], root) if os.path.isdir(root) else j['old']}")
        print(f"  -> {os.path.basename(j['new'])}   [{label}]")
    for path, why in skipped:
        if why != 'đã đúng định dạng':
            print(f'\n  ⚠ Bỏ qua {path}: {why}')
    n_ok = sum(1 for _, w in skipped if w == 'đã đúng định dạng')
    print(f'\nSẽ đổi tên: {len(jobs)} | đã có hashtag đúng: {n_ok} | bỏ qua khác: {len(skipped) - n_ok}')

    if not jobs or args.dry_run:
        if args.dry_run:
            print('(Chế độ xem trước - chưa đổi tên file nào)')
        return
    if not args.yes and input('Tiến hành đổi tên? [y/N] ').strip().lower() != 'y':
        print('Đã hủy.')
        return

    done = []
    for j in jobs:
        try:
            os.rename(j['old'], j['new'])
            done.append({'old': j['old'], 'new': j['new']})
        except OSError as ex:
            print(f"  ✘ Lỗi đổi tên {j['old']}: {ex}")
    log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            time.strftime('rename_hashtag_log_%Y%m%d_%H%M%S.json'))
    with open(log_path, 'w', encoding='utf-8') as f:
        json.dump(done, f, ensure_ascii=False, indent=2)
    print(f'\nĐã đổi tên {len(done)}/{len(jobs)} file. Log hoàn tác: {log_path}')
    print(f'Hoàn tác: python rename_hashtag.py --undo "{os.path.basename(log_path)}"')


if __name__ == '__main__':
    main()

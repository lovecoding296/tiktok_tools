
import os
import sys
import shutil
import re
import tkinter as tk

from tkinter import filedialog, messagebox


# ==================================================
# CONFIGURATION
# ==================================================

# Xác định thư mục chứa file Python hoặc EXE
if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(
        os.path.abspath(sys.executable)
    )
else:
    BASE_DIR = os.path.dirname(
        os.path.abspath(__file__)
    )

# File chứa từ khóa
KEYWORD_FILE = os.path.join(
    BASE_DIR,
    "keywords.txt"
)

# Folder lưu các folder vi phạm
DEST_DIR = os.path.join(
    BASE_DIR,
    "kiemtralai"
)

# Các định dạng video được kiểm tra
VIDEO_EXTENSIONS = {
    ".mp4",
    ".mkv",
    ".avi",
    ".mov",
    ".wmv",
    ".flv",
    ".webm",
    ".m4v",
    ".mpeg",
    ".mpg",
    ".ts",
    ".3gp"
}


# ==================================================
# LOAD KEYWORDS
# ==================================================

def load_keywords():

    if not os.path.isfile(KEYWORD_FILE):

        messagebox.showerror(
            "Error",
            f"Khong tim thay file keywords.txt:\n\n"
            f"{KEYWORD_FILE}"
        )

        return None

    try:

        with open(
            KEYWORD_FILE,
            "r",
            encoding="utf-8-sig"
        ) as file:

            keywords = [
                line.strip()
                for line in file
                if line.strip()
                and not line.strip().startswith("#")
            ]

        # Loại bỏ từ khóa trùng lặp
        keywords = list(dict.fromkeys(
            keyword.lower()
            for keyword in keywords
        ))

        if not keywords:

            messagebox.showerror(
                "Error",
                "File keywords.txt khong co tu khoa!"
            )

            return None

        return keywords

    except Exception as e:

        messagebox.showerror(
            "Error",
            f"Loi doc keywords.txt:\n{e}"
        )

        return None


# ==================================================
# SELECT SOURCE FOLDER
# ==================================================

def select_source_folder():

    return filedialog.askdirectory(
        title="Chon folder NGUON can kiem tra"
    )


# ==================================================
# SCAN VIDEOS
# ==================================================

def scan_videos(source_dir, dest_dir, pattern):

    folders_to_move = set()

    print("\n========== BAT DAU QUET ==========\n")

    dest_abs = os.path.abspath(dest_dir)

    for current_root, dirs, files in os.walk(source_dir):

        # Bỏ qua folder đích và toàn bộ folder con
        dirs[:] = [
            d for d in dirs
            if os.path.abspath(
                os.path.join(current_root, d)
            ) != dest_abs
            and not os.path.abspath(
                os.path.join(current_root, d)
            ).startswith(dest_abs + os.sep)
        ]

        for filename in files:

            name, ext = os.path.splitext(filename)

            # Chỉ kiểm tra file video
            if ext.lower() not in VIDEO_EXTENSIONS:
                continue

            # Kiểm tra từ khóa trong tên video
            matches = pattern.findall(name)

            if matches:

                folder_path = os.path.abspath(current_root)

                # Không di chuyển folder nguồn
                if folder_path == os.path.abspath(source_dir):

                    print(f"[BO QUA ROOT] {filename}")
                    continue

                folders_to_move.add(folder_path)

                print(f"[PHAT HIEN] {filename}")
                print(f"[TU KHOA] {', '.join(set(matches))}")
                print(f"[FOLDER] {folder_path}\n")

    return folders_to_move


# ==================================================
# FILTER NESTED FOLDERS
# ==================================================

def filter_parent_folders(folders):

    selected_folders = []

    for folder in sorted(
        folders,
        key=lambda x: (x.count(os.sep), x.lower())
    ):

        is_inside_selected = False

        for parent in selected_folders:

            try:

                if os.path.commonpath(
                    [folder, parent]
                ) == parent:

                    is_inside_selected = True
                    break

            except ValueError:
                continue

        if not is_inside_selected:

            selected_folders.append(folder)

    return selected_folders


# ==================================================
# MOVE FOLDERS
# ==================================================

def move_folders(folders, dest_dir):

    success = 0
    errors = 0

    print("\n========== BAT DAU DI CHUYEN ==========\n")

    for folder_path in folders:

        folder_name = os.path.basename(folder_path)

        destination = os.path.join(
            dest_dir,
            folder_name
        )

        # Tránh ghi đè folder trùng tên
        counter = 1

        while os.path.exists(destination):

            destination = os.path.join(
                dest_dir,
                f"{folder_name}_{counter}"
            )

            counter += 1

        try:

            shutil.move(
                folder_path,
                destination
            )

            print(f"[THANH CONG] {folder_path}")
            print(f"       -> {destination}\n")

            success += 1

        except Exception as e:

            print(f"[LOI] {folder_path}")
            print(f"      {e}\n")

            errors += 1

    return success, errors


# ==================================================
# MAIN
# ==================================================

def main():

    root = tk.Tk()
    root.withdraw()

    try:

        # 1. Đọc từ khóa
        keywords = load_keywords()

        if keywords is None:
            return

        # 2. Tạo regex kiểm tra nguyên từ
        pattern = re.compile(
            r"\b(?:"
            + "|".join(
                re.escape(keyword)
                for keyword in keywords
            )
            + r")\b",
            re.IGNORECASE
        )

        # 3. Chọn folder nguồn duy nhất
        source_dir = select_source_folder()

        if not source_dir:
            return

        source_dir = os.path.abspath(source_dir)

        # 4. Kiểm tra folder đích
        source_real = os.path.normcase(
            os.path.realpath(source_dir)
        )

        dest_real = os.path.normcase(
            os.path.realpath(DEST_DIR)
        )

        if source_real == dest_real:

            messagebox.showerror(
                "Error",
                "Folder nguon khong duoc la folder kiemtralai!"
            )

            return

        if os.path.commonpath(
            [source_real, dest_real]
        ) == source_real:

            messagebox.showerror(
                "Error",
                "Folder kiemtralai khong duoc nam trong folder nguon!"
            )

            return

        # 5. Tạo folder đích cạnh file PY hoặc EXE
        os.makedirs(
            DEST_DIR,
            exist_ok=True
        )

        print("=" * 60)
        print("VIDEO FILTER")
        print("=" * 60)

        print(f"Folder nguon : {source_dir}")
        print(f"Folder dich  : {DEST_DIR}")
        print(f"So tu khoa   : {len(keywords)}")

        print("\nDanh sach tu khoa:")

        for keyword in keywords:
            print(f" - {keyword}")

        # 6. Quét video
        folders = scan_videos(
            source_dir,
            DEST_DIR,
            pattern
        )

        if not folders:

            messagebox.showinfo(
                "Ket qua",
                "Khong tim thay video chua tu khoa nhay cam!"
            )

            return

        # 7. Loại bỏ folder con trùng folder cha
        selected_folders = filter_parent_folders(
            folders
        )

        print("\n========== KET QUA ==========")

        print(f"So folder phat hien: {len(folders)}")
        print(f"So folder can di chuyen: {len(selected_folders)}")

        for folder in selected_folders:
            print(f" - {folder}")

        # 8. Xác nhận di chuyển
        confirm = messagebox.askyesno(
            "Xac nhan di chuyen",

            f"Tim thay {len(folders)} folder chua video nhay cam.\n\n"

            f"So folder se di chuyen: {len(selected_folders)}\n\n"

            f"Folder dich:\n{DEST_DIR}\n\n"

            "Ban co muon tiep tuc?"
        )

        if not confirm:

            print("\n[DA HUY] Khong di chuyen folder.")

            return

        # 9. Di chuyển folder
        success, errors = move_folders(
            selected_folders,
            DEST_DIR
        )

        # 10. Thông báo kết quả
        print("\n" + "=" * 60)
        print("HOAN THANH")
        print(f"Thanh cong: {success}")
        print(f"Loi: {errors}")
        print("=" * 60)

        messagebox.showinfo(
            "Hoan thanh",

            f"Da hoan thanh kiem tra!\n\n"

            f"So folder phat hien: {len(folders)}\n"
            f"So folder da di chuyen: {success}\n"
            f"So folder loi: {errors}\n\n"

            f"Folder dich:\n{DEST_DIR}"
        )

    except Exception as e:

        messagebox.showerror(
            "Loi chuong trinh",
            str(e)
        )

    finally:

        root.destroy()


# ==================================================
# ENTRY POINT
# ==================================================

if __name__ == "__main__":
    main()

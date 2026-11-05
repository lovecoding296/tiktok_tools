import tkinter as tk
from tkinter import ttk, messagebox


def parse_line(line: str):
    """x1|x2|x3|x4|x5|x6|x7@...|x8 -> x1\tx2\tx3\tx4\tx3|x4|x5|x6\tx7"""
    parts = line.strip().split("|")
    if len(parts) < 7:
        return None
    x1, x2, x3, x4, x5, x6, x7 = [p.strip() for p in parts[:7]]
    x7 = x7.split("@", 1)[0]  # bỏ @ trở về sau
    return "\t".join([x1, x2, x3, x4, f"{x3}|{x4}|{x5}|{x6}", x7])


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Parse dữ liệu")
        self.geometry("900x650")

        ttk.Label(self, text="Input (mỗi dòng: x1|x2|x3|x4|x5|x6|x7@...|x8)").pack(anchor="w", padx=8, pady=(8, 0))
        self.txt_in = tk.Text(self, height=14, wrap="none", undo=True)
        self.txt_in.pack(fill="both", expand=True, padx=8, pady=4)

        bar = ttk.Frame(self)
        bar.pack(fill="x", padx=8, pady=4)
        ttk.Button(bar, text="Parse", command=self.parse).pack(side="left")
        ttk.Button(bar, text="Copy kết quả", command=self.copy).pack(side="left", padx=6)
        ttk.Button(bar, text="Dán từ clipboard", command=self.paste).pack(side="left")
        ttk.Button(bar, text="Xóa", command=self.clear).pack(side="left", padx=6)
        self.lbl = ttk.Label(bar, text="")
        self.lbl.pack(side="right")

        ttk.Label(self, text="Output (phân tách bằng Tab)").pack(anchor="w", padx=8)
        self.txt_out = tk.Text(self, height=14, wrap="none")
        self.txt_out.pack(fill="both", expand=True, padx=8, pady=(4, 8))

        self.bind("<Control-Return>", lambda e: self.parse())

    def parse(self):
        lines = self.txt_in.get("1.0", "end").splitlines()
        out, bad = [], []
        for i, line in enumerate(lines, 1):
            if not line.strip():
                continue
            r = parse_line(line)
            if r is None:
                bad.append(i)
            else:
                out.append(r)
        self.txt_out.delete("1.0", "end")
        self.txt_out.insert("1.0", "\n".join(out))
        msg = f"OK: {len(out)} dòng"
        if bad:
            msg += f" | Lỗi dòng: {', '.join(map(str, bad))}"
        self.lbl.config(text=msg)

    def copy(self):
        data = self.txt_out.get("1.0", "end").rstrip("\n")
        if not data:
            messagebox.showinfo("Thông báo", "Chưa có kết quả")
            return
        self.clipboard_clear()
        self.clipboard_append(data)
        self.lbl.config(text="Đã copy")

    def paste(self):
        try:
            self.txt_in.delete("1.0", "end")
            self.txt_in.insert("1.0", self.clipboard_get())
        except tk.TclError:
            pass

    def clear(self):
        self.txt_in.delete("1.0", "end")
        self.txt_out.delete("1.0", "end")
        self.lbl.config(text="")


if __name__ == "__main__":
    App().mainloop()

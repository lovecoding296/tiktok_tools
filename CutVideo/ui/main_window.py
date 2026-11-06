import os
import sys
import glob
import time
import json
import io
import traceback
import subprocess
import threading
import send2trash
from collections import deque
from concurrent.futures import ThreadPoolExecutor
CONFIG_PATH = os.path.join(os.path.dirname(sys.executable if getattr(sys, 'frozen', False) else os.path.abspath(__file__)), 'settings.json')
def load_settings():
    defaults = {'min_len': 120, 'max_len': 180, 'speed': 1.1, 'resolution': '1080x1080 (1:1 Vuông FHD)', 'delete_original': False, 'input_dir': '', 'output_dir': '', 'title_enabled': True, 'title_size': 42, 'title_color': '#000000', 'title_halign': 'center', 'box_enabled': True, 'box_color': '#FFFFFF', 'box_opacity': 100, 'box_radius': 30, 'line_pad_x': 55, 'line_gap': 0, 'line_spacing': 176, 'line_pad_y': 38, 'margin_x': 240, 'title_scale': 100, 'limit_enabled': False, 'limit_count': 2, 'even_enabled': False, 'even_count': 4, 'parallel': 2, 'name_style': 'paren', 'volume_db': 0.0, 'live_preview': True}
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
                saved = json.load(f)
                defaults.update(saved)
                # Cấu hình cũ: 'line_height' (khoảng cách baseline), 'box_h_pct' (chiều cao nền), 'box_pad_x' (chiều rộng nền)
                # -> đệm trái/phải + đệm trên/dưới của từng dòng + khoảng hở giữa các ô (hở = khoảng cách dòng - chiều cao ô).
                legacy_pitch = int(saved['line_height']) if 'line_height' in saved else 100 + int(saved.get('box_pad_y', 75))
                legacy_box_h = int(saved.get('box_h_pct', 175 if 'line_height' in saved else legacy_pitch))
                if 'line_pad_x' not in saved:
                    defaults['line_pad_x'] = int(saved.get('box_pad_x', 55))
                if 'line_pad_y' not in saved:
                    defaults['line_pad_y'] = (legacy_box_h - 100 + 1) // 2
                if 'line_gap' not in saved:
                    defaults['line_gap'] = legacy_pitch - (100 + 2 * int(defaults['line_pad_y']))
                if 'line_spacing' not in saved:
                    defaults['line_spacing'] = 100 + 2 * int(defaults['line_pad_y']) + int(defaults['line_gap'])
                defaults['line_spacing'] = min(max(int(defaults['line_spacing']), 20), 600)
                # Chỉ đổi chữ trắng -> đen với cấu hình CŨ (trước khi có ô nền, chưa có khóa 'box_color').
                # Cấu hình mới thì tôn trọng màu người dùng đã chọn, kể cả trắng.
                if 'box_color' not in saved and str(defaults.get('title_color', '')).upper() == '#FFFFFF':
                    defaults['title_color'] = '#000000'
        except Exception:
            pass
    return defaults
def save_settings(data):
    try:
        with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        return None


from PyQt6.QtCore import QTimer, Qt, QThread, pyqtSignal, QSize, QPropertyAnimation, QEasingCurve
from PyQt6.QtGui import QFont, QIcon, QTextCursor, QColor, QPixmap
from PyQt6.QtWidgets import QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton, QFileDialog, QSpinBox, QDoubleSpinBox, QComboBox, QProgressBar, QTextEdit, QFrame, QGroupBox, QMessageBox, QGridLayout, QSplitter, QCheckBox, QColorDialog, QTabWidget, QStackedWidget, QButtonGroup, QLayout, QApplication
from core.utils import check_h264_encoder, cleanup_temp_dir
from core.video_engine import find_break_points, render_segment, get_video_duration, reset_encoder_state, render_title_preview, CANVAS_W, CANVAS_H, TIKTOK_SAFE_TOP, _find_title_font, font_search_report
DARK_STYLESHEET = '\nQMainWindow {\n    background-color: #121318;\n}\nQWidget {\n    color: #e0e6ed;\n    font-family: \'Segoe UI\', Arial, sans-serif;\n    font-size: 13px;\n}\nQFrame.card {\n    background-color: #1a1c24;\n    border-radius: 10px;\n    border: 1px solid #2a2d3d;\n    padding: 12px;\n}\nQLabel {\n    color: #c5cedb;\n}\nQLabel.title {\n    font-size: 20px;\n    font-weight: bold;\n    color: #00d2ff;\n}\nQLabel.subtitle {\n    font-size: 12px;\n    color: #8a96a8;\n}\nQLabel.gpu-badge {\n    background-color: #0d3b2e;\n    color: #00ffaa;\n    border: 1px solid #00aa66;\n    border-radius: 6px;\n    padding: 4px 8px;\n    font-weight: bold;\n    font-size: 11px;\n}\nQLineEdit, QSpinBox, QDoubleSpinBox {\n    background-color: #121318;\n    border: 1px solid #32374a;\n    border-radius: 6px;\n    padding: 6px 10px;\n    color: #ffffff;\n    selection-background-color: #0088cc;\n}\nQLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus {\n    border: 1px solid #00d2ff;\n}\nQComboBox {\n    background-color: #1a1c24;\n    border: 1px solid #32374a;\n    border-radius: 6px;\n    padding: 6px 10px;\n    color: #ffffff;\n    font-weight: bold;\n}\nQComboBox:focus {\n    border: 1px solid #00d2ff;\n}\nQComboBox QAbstractItemView {\n    background-color: #1a1c24;\n    color: #ffffff;\n    border: 1px solid #00d2ff;\n    selection-background-color: #0088cc;\n    selection-color: #ffffff;\n    padding: 4px;\n    outline: 0px;\n}\nQComboBox QAbstractItemView::item {\n    color: #ffffff;\n    background-color: #1a1c24;\n    padding: 8px 12px;\n    min-height: 26px;\n}\nQComboBox QAbstractItemView::item:hover,\nQComboBox QAbstractItemView::item:selected {\n    background-color: #0088cc;\n    color: #ffffff;\n    font-weight: bold;\n}\nQCheckBox {\n    color: #e0e6ed;\n    font-weight: bold;\n}\nQCheckBox::indicator {\n    width: 18px;\n    height: 18px;\n    border-radius: 4px;\n    border: 1px solid #32374a;\n    background-color: #121318;\n}\nQCheckBox::indicator:checked {\n    background-color: #00d2ff;\n    border-color: #00d2ff;\n}\nQPushButton {\n    background-color: #242938;\n    color: #ffffff;\n    border: 1px solid #3a4158;\n    border-radius: 6px;\n    padding: 8px 16px;\n    font-weight: bold;\n}\nQPushButton:hover {\n    background-color: #2f364a;\n    border-color: #00d2ff;\n}\nQPushButton:pressed {\n    background-color: #1c202e;\n}\nQPushButton.btn-primary {\n    background-color: #0099e6;\n    color: #ffffff;\n    border: none;\n    font-size: 14px;\n    padding: 10px 24px;\n}\nQPushButton.btn-primary:hover {\n    background-color: #00b3ff;\n}\nQPushButton.btn-danger {\n    background-color: #8b0000;\n    color: #ffffff;\n    border: 1px solid #ff4444;\n}\nQPushButton.btn-danger:hover {\n    background-color: #cc0000;\n}\nQProgressBar {\n    background-color: #121318;\n    border: 1px solid #2a2d3d;\n    border-radius: 6px;\n    text-align: center;\n    color: #ffffff;\n    font-weight: bold;\n}\nQProgressBar::chunk {\n    background-color: #00d2ff;\n    border-radius: 5px;\n}\nQTextEdit {\n    background-color: #0e0f14;\n    border: 1px solid #222533;\n    border-radius: 6px;\n    color: #a0aec0;\n    font-family: \'Consolas\', \'Courier New\', monospace;\n    font-size: 12px;\n}\n'
DARK_STYLESHEET += """
QTabWidget::pane { border: none; background: transparent; top: 0px; }
QTabBar::tab { background-color: #1a1c24; color: #8a96a8; border: 1px solid #2a2d3d; border-bottom: none; border-top-left-radius: 8px; border-top-right-radius: 8px; padding: 8px 18px; margin-right: 4px; font-weight: bold; }
QTabBar::tab:selected { background-color: #242938; color: #00d2ff; border-color: #00d2ff; }
QTabBar::tab:hover:!selected { background-color: #20232f; color: #ffffff; }
"""
DARK_STYLESHEET += """
QPushButton[tab="true"] { background-color: #1a1c24; color: #8a96a8; border: 1px solid #2a2d3d; border-bottom: none; border-top-left-radius: 8px; border-top-right-radius: 8px; border-bottom-left-radius: 0px; border-bottom-right-radius: 0px; padding: 8px 18px; font-weight: bold; }
QPushButton[tab="true"]:hover { background-color: #20232f; color: #ffffff; border-color: #2a2d3d; }
QPushButton[tab="true"]:checked { background-color: #242938; color: #00d2ff; border-color: #00d2ff; }
"""
# Thông số tiêu đề gán cứng (theo settings.json), không hiển thị trên giao diện
TITLE_FIXED = {'font_weight': 700, 'font_width': 100, 'min_gap': 0, 'merge_step': 2.0, 'text_dy': 0, 'supersample': 4}
NAME_STYLES = (('Pt. 1, Pt. 2, ...', 'pt'), ('-1, -2, ...', 'dash'), ('Tên(1), Tên(2), ... (liền, không cách)', 'paren'), ('1편, 2편, ...', 'pyeon'))
def format_segment_name(base_name, index, style):
    """Tên file (không đuôi) của đoạn thứ `index` (đếm từ 1) theo kiểu đã chọn."""
    if style == 'dash':
        return f'{base_name}-{index}'
    if style == 'paren':
        return f'{base_name}({index})'
    if style == 'pyeon':
        return f'{base_name} {index}편'
    return f'{base_name} Pt. {index}'
class ProcessingWorker(QThread):
    log_signal = pyqtSignal(str, str)
    progress_signal = pyqtSignal(int, int)
    file_progress_signal = pyqtSignal(str, int)
    finished_signal = pyqtSignal()
    def __init__(self, input_dir, output_dir, min_len, max_len, speed, resolution, encoder, delete_original, title_opts=None, max_segments=None, parallel=2, name_style='pt', volume_db=0.0, even_only=False):
        super().__init__()
        self.input_dir = input_dir
        self.output_dir = output_dir
        self.min_len = min_len
        self.max_len = max_len
        self.speed = speed
        self.resolution = resolution
        self.encoder = encoder
        self.delete_original = delete_original
        self.title_opts = title_opts
        self.even_only = bool(even_only)  # True: chỉ lấy số đoạn CHẴN (không đủ thì lấy số chẵn gần nhất bên dưới)
        self.max_segments = max_segments  # None = cắt hết; N = chỉ cắt N đoạn đầu của mỗi video
        self.parallel = max(1, int(parallel))  # số ffmpeg chạy cùng lúc
        self.name_style = name_style  # 'pt' | 'dash' | 'paren' | 'pyeon'
        self.volume_db = float(volume_db)  # chỉnh âm lượng (dB): dương = tăng, âm = giảm
        self.is_running = True
        self._procs = set()
        self._proc_lock = threading.Lock()
    def _make_proc_cb(self):
        """Mỗi lần render có callback riêng để theo dõi tiến trình ffmpeg của nó (nhiều ffmpeg chạy song song)."""
        holder = {}
        def cb(proc):
            with self._proc_lock:
                if proc is not None:
                    holder['p'] = proc
                    self._procs.add(proc)
                    if not self.is_running:  # bấm dừng đúng lúc ffmpeg vừa khởi động
                        try:
                            proc.kill()
                        except Exception:
                            pass
                else:
                    p = holder.pop('p', None)
                    if p is not None:
                        self._procs.discard(p)
        return cb
    def stop(self):
        self.is_running = False
        with self._proc_lock:
            for p in list(self._procs):
                try:
                    p.kill()
                except Exception:
                    pass
    def _collect_videos(self):
        video_exts = ('.mp4', '.mkv', '.avi', '.mov', '.webm')
        video_files = []
        if os.path.isfile(self.input_dir):
            if self.input_dir.lower().endswith(video_exts):
                video_files.append(self.input_dir)
            return video_files
        out_abs = os.path.normcase(os.path.abspath(self.output_dir))
        for root, dirs, files in os.walk(self.input_dir):
            # Không quét vào thư mục xuất (nếu nằm trong thư mục nguồn) để khỏi cắt lại video đã cắt
            dirs[:] = [d for d in dirs if os.path.normcase(os.path.abspath(os.path.join(root, d))) != out_abs]
            for f in files:
                if f.lower().endswith(video_exts):
                    video_files.append(os.path.join(root, f))
        return video_files
    def _output_subdir(self, video_path):
        """Thư mục xuất của một video: giữ nguyên toàn bộ cây thư mục (cha, ông...) tính từ thư mục nguồn.
        Video nằm thẳng trong thư mục nguồn (hoặc nguồn là 1 file) thì dùng tên thư mục chứa nó như cũ."""
        parent = os.path.dirname(video_path)
        if os.path.isdir(self.input_dir):
            try:
                rel = os.path.relpath(parent, self.input_dir)
            except ValueError:  # khác ổ đĩa
                rel = '.'
            if rel != '.' and not rel.startswith('..'):
                return os.path.join(self.output_dir, rel)
        return os.path.join(self.output_dir, os.path.basename(parent))
    def _render_task(self, video_path, start_t, seg_duration, out_path, out_name, seg_title):
        if not self.is_running:
            return None
        render_segment(video_path=video_path, start_time=start_t, segment_duration=seg_duration,
                       output_path=out_path, speed=self.speed, resolution_preset=self.resolution,
                       encoder=self.encoder, process_callback=self._make_proc_cb(),
                       frame_opts=seg_title, is_cancelled=lambda: not self.is_running,
                       volume_db=self.volume_db)
        if not self.is_running:
            return None
        self.log_signal.emit(f'   + Đã xuất: {out_name} ({seg_duration:.1f}s)', 'success')
        return out_path
    def run(self):
        self.log_signal.emit('=== BẮT ĐẦU QUY TRÌNH CẮT VIDEO HÀNG ĐẠT TỐC ĐỘ CAO ===', 'info')
        start_batch_time = time.time()
        reset_encoder_state()
        video_files = self._collect_videos()
        total_files = len(video_files)
        if total_files == 0:
            self.log_signal.emit(f'Không tìm thấy file video nào trong: {self.input_dir}', 'warning')
            self.finished_signal.emit()
            return

        self.log_signal.emit(f'Tìm thấy tổng cộng {total_files} file video cần xử lý.', 'success')
        if self.parallel > 1:
            self.log_signal.emit(f'Render song song: {self.parallel} luồng (phân tích video kế tiếp trong lúc đang render).', 'info')
        self.progress_signal.emit(0, total_files)
        temp_dir = os.path.join(self.output_dir, '_temp_audio_cache')
        counters = {'processed': 0, 'success': 0, 'error': 0, 'skipped': 0}

        def mark_done():
            counters['processed'] += 1
            self.progress_signal.emit(counters['processed'], total_files)

        def finalize(job):
            """Chờ các đoạn của một video render xong, rồi ghi log / xóa video gốc."""
            n = job['n']
            file_name = job['name']
            rendered = 0
            try:
                for k, fut in enumerate(job['futures'], 1):
                    if fut.result() is not None:
                        rendered += 1
                    if self.is_running:
                        self.file_progress_signal.emit(f'Đã xuất {k}/{n}: {file_name}', int(20 + k / n * 80))
            except Exception as e:
                for f in job['futures']:
                    f.cancel()
                if not self.is_running:
                    return
                err_str = str(e)
                self.log_signal.emit(f'✘ LỖI xử lý {file_name}: {err_str}', 'error')
                counters['error'] += 1
                mark_done()
                return
            if not self.is_running or rendered != n:
                return
            elapsed = time.time() - job['t0']
            self.log_signal.emit(f'✔ HOÀN THÀNH {file_name} trong {elapsed:.2f} giây!', 'success')
            counters['success'] += 1
            if self.delete_original:
                try:
                    send2trash.send2trash(job['path'])
                    self.log_signal.emit(f'   🗑 Đã xóa video gốc vào Thùng Rác: {file_name}', 'warning')
                except Exception:
                    try:
                        os.remove(job['path'])
                        self.log_signal.emit(f'   🗑 Đã xóa vĩnh viễn video gốc: {file_name}', 'warning')
                    except Exception as e_del:
                        self.log_signal.emit(f'   ⚠️ Không thể xóa video gốc: {e_del}', 'error')
            mark_done()

        pool = ThreadPoolExecutor(max_workers=self.parallel)
        pending = deque()  # các video đã phân tích và đang render
        max_inflight = max(2, self.parallel)
        try:
            for idx, video_path in enumerate(video_files, 1):
                # Giữ tối đa max_inflight video đang chờ: GPU luôn có việc mà không phân tích dồn cả danh sách
                while self.is_running and len(pending) >= max_inflight:
                    finalize(pending.popleft())
                if not self.is_running:
                    break
                file_name = os.path.basename(video_path)
                channel_name = os.path.basename(os.path.dirname(video_path))
                self.log_signal.emit(f'\n[{idx}/{total_files}] Đang xử lý: {file_name} ({channel_name})', 'info')
                self.file_progress_signal.emit(f'Phân tích Audio: {file_name}', 10)
                t0 = time.time()
                futures = []
                try:
                    duration, break_points = find_break_points(video_path, temp_dir=temp_dir, min_len=self.min_len, max_len=self.max_len, max_segments=self.max_segments)
                    num_segments = len(break_points) - 1
                    if num_segments <= 0:
                        self.log_signal.emit(f'-> Thời lượng video ({duration:.1f}s) không đủ phân đoạn.', 'warning')
                        num_segments = 1
                        break_points = [0, duration]
                    if self.even_only:
                        even_n = num_segments // 2 * 2
                        if even_n != num_segments:
                            self.log_signal.emit(f'-> Chế độ số chẵn: video chỉ có {num_segments} đoạn -> lấy {even_n} đoạn đầu.', 'warning')
                        if even_n == 0:
                            self.log_signal.emit(f'-> Bỏ qua {file_name}: không đủ 2 đoạn để lấy số chẵn.', 'warning')
                            counters['skipped'] += 1
                            mark_done()
                            continue
                        num_segments = even_n
                        break_points = break_points[:even_n + 1]
                    partial = self.max_segments is not None and break_points[-1] < duration - 0.5
                    note = f' (chỉ cắt {num_segments} đoạn đầu)' if partial else ''
                    self.log_signal.emit(f'-> Thời lượng: {duration:.2f}s | Số đoạn cắt: {num_segments}{note}', 'success')
                    for s_i in range(num_segments):
                        seg_len = break_points[s_i + 1] - break_points[s_i]
                        self.log_signal.emit(f'   [Đoạn {s_i + 1}] {break_points[s_i]:.1f}s -> {break_points[s_i + 1]:.1f}s (Độ dài: {seg_len:.1f}s)', 'default')

                    base_name = os.path.splitext(file_name)[0]
                    name_base = base_name  # chữ chèn lên video = tên gốc, giữ nguyên
                    save_dir = self._output_subdir(video_path)
                    os.makedirs(save_dir, exist_ok=True)

                    for seg_idx in range(num_segments):
                        start_t = break_points[seg_idx]
                        seg_duration = break_points[seg_idx + 1] - start_t
                        if num_segments == 1 and not partial:
                            seg_stem = name_base
                        else:
                            seg_stem = format_segment_name(name_base, seg_idx + 1, self.name_style)
                        out_segment_name = seg_stem + '.mp4'
                        seg_title = dict(self.title_opts, text=seg_stem) if self.title_opts is not None else None
                        futures.append(pool.submit(self._render_task, video_path, start_t, seg_duration,
                                                   os.path.join(save_dir, out_segment_name), out_segment_name, seg_title))
                    pending.append({'path': video_path, 'name': file_name, 'futures': futures, 'n': num_segments,
                                    'duration': duration, 't0': t0})
                except Exception as e:
                    for f in futures:
                        f.cancel()
                    if not self.is_running:
                        break
                    err_str = str(e)
                    self.log_signal.emit(f'✘ LỖI xử lý {file_name}: {err_str}', 'error')
                    counters['error'] += 1
                    mark_done()
            while self.is_running and pending:
                finalize(pending.popleft())
        finally:
            pool.shutdown(wait=True, cancel_futures=True)

        if not self.is_running:
            self.log_signal.emit('🛑 ĐÃ DỪNG TIẾN TRÌNH THEO YÊU CẦU NGƯỜI DÙNG.', 'warning')
        cleanup_temp_dir(temp_dir)
        total_elapsed = time.time() - start_batch_time
        self.log_signal.emit('\n==================================================', 'info')
        self.log_signal.emit(f"TỔNG KẾT: Hoàn tất {counters['success']}/{total_files} video thành công. Lỗi: {counters['error']}. Bỏ qua: {counters['skipped']}. Tổng thời gian: {total_elapsed:.2f}s", 'success')
        self.finished_signal.emit()


class TitlePreviewWindow(QWidget):
    """Cửa sổ xem trước: canvas CANVAS_W x CANVAS_H nền đen + tiêu đề. Ảnh được thu nhỏ vừa cửa sổ (giữ tỉ lệ), kéo to cửa sổ để xem rõ hơn."""
    def __init__(self, pil_image, text, font_name='', info=''):
        super().__init__(None)
        self._font_name = font_name
        self._info = info
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.setWindowFlag(Qt.WindowType.Window, True)
        buf = io.BytesIO()
        pil_image.save(buf, format='PNG')
        self._pix = QPixmap()
        self._pix.loadFromData(buf.getvalue(), 'PNG')
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.lbl = QLabel()
        self.lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl.setStyleSheet('background-color: #000000;')
        self.lbl.setToolTip(text)
        self.lbl.setMinimumSize(100, 100)
        lay.addWidget(self.lbl)
        self._refresh_title_and_pixmap()
    def _refresh_title_and_pixmap(self):
        size = self.lbl.size()
        if size.width() > 0 and size.height() > 0:
            scaled = self._pix.scaled(size, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
            self.lbl.setPixmap(scaled)
            pct = int(round(scaled.width() * 100.0 / max(1, self._pix.width())))
        else:
            pct = 100
        self.setWindowTitle(f'Xem trước tiêu đề ({CANVAS_W}x{CANVAS_H}, hiển thị {pct}%) - font: {self._font_name or "?"} | {self._info}')
    def set_content(self, pil_image, text, font_name='', info=''):
        self._font_name = font_name
        self._info = info
        buf = io.BytesIO()
        pil_image.save(buf, format='PNG')
        pix = QPixmap()
        pix.loadFromData(buf.getvalue(), 'PNG')
        self._pix = pix
        self.lbl.setToolTip(text)
        self._refresh_title_and_pixmap()
    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._refresh_title_and_pixmap()

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle('CutVideo Pro v3.0 - Phần Mềm Cắt Video Siêu Tốc GPU')
        self.resize(960, 840)
        self.setStyleSheet(DARK_STYLESHEET)
        self.worker = None
        self.init_ui()
        self.detect_system_gpu()
    def init_ui(self):
        main_widget = QWidget()
        self.setCentralWidget(main_widget)
        main_layout = QVBoxLayout(main_widget)
        main_layout.setSpacing(12)
        main_layout.setContentsMargins(16, 16, 16, 16)
        header_card = QFrame()
        header_card.setProperty('class', 'card')
        header_layout = QHBoxLayout(header_card)
        title_vbox = QVBoxLayout()
        lbl_title = QLabel('CutVideo Pro v3.0')
        lbl_title.setProperty('class', 'title')
        lbl_sub = QLabel('Công nghệ trích xuất audio FFmpeg Direct & Phân tích khoảng lặng Vectorized NumPy')
        lbl_sub.setProperty('class', 'subtitle')
        title_vbox.addWidget(lbl_title)
        title_vbox.addWidget(lbl_sub)
        self.lbl_gpu_badge = QLabel('⚡ Kiểm tra GPU...')
        self.lbl_gpu_badge.setProperty('class', 'gpu-badge')
        self.lbl_gpu_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        header_layout.addLayout(title_vbox)
        header_layout.addStretch()
        header_layout.addWidget(self.lbl_gpu_badge)
        main_layout.addWidget(header_card)
        # Thanh tab tự dựng: [Cắt video] [>>] -> bấm >> thì 3 tab cài đặt trượt ra và có nút << ở bên phải chúng
        self.tabs = QStackedWidget()
        tab_row = QHBoxLayout()
        tab_row.setSpacing(4)
        tab_row.setContentsMargins(0, 0, 0, 0)
        main_layout.addLayout(tab_row)
        main_layout.addWidget(self.tabs, 1)
        page_main = QWidget()
        page_general = QWidget()
        page_video = QWidget()
        page_settings = QWidget()
        for page in (page_main, page_general, page_video, page_settings):
            self.tabs.addWidget(page)
        self.tab_group = QButtonGroup(self)

        def make_tab(text, page_index=None):
            btn = QPushButton(text)
            btn.setProperty('tab', 'true')
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            if page_index is not None:
                btn.setCheckable(True)
                self.tab_group.addButton(btn, page_index)
            return btn

        self.btn_tab_main = make_tab('✂ Cắt video', 0)
        self.btn_tab_main.setChecked(True)
        self.btn_expand = make_tab('>>')
        self.btn_expand.setToolTip('Hiện các tab cài đặt')
        self.btn_collapse = make_tab('<<')
        self.btn_collapse.setToolTip('Ẩn các tab cài đặt')
        self.btn_collapse.hide()
        self.settings_tabs_box = QWidget()
        box_layout = QHBoxLayout(self.settings_tabs_box)
        box_layout.setSpacing(4)
        box_layout.setContentsMargins(0, 0, 0, 0)
        box_layout.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
        box_layout.addWidget(make_tab('⚙ Cài đặt chung', 1))
        box_layout.addWidget(make_tab('🎬 Cài đặt video', 2))
        box_layout.addWidget(make_tab('🎨 Cài đặt tiêu đề', 3))
        self.settings_tabs_box.setMaximumWidth(0)  # ban đầu ẩn (rộng 0), animation sẽ mở rộng ra
        tab_row.addWidget(self.btn_tab_main)
        tab_row.addWidget(self.btn_expand)
        tab_row.addWidget(self.settings_tabs_box)
        tab_row.addWidget(self.btn_collapse)
        tab_row.addStretch()
        self.settings_tabs_visible = False
        self.tab_anim = QPropertyAnimation(self.settings_tabs_box, b'maximumWidth', self)
        self.tab_anim.setDuration(260)
        self.tab_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.tab_anim.finished.connect(self.on_tab_anim_finished)
        self.tab_group.idClicked.connect(self.tabs.setCurrentIndex)
        self.btn_expand.clicked.connect(lambda: self.set_settings_tabs_visible(True))
        self.btn_collapse.clicked.connect(lambda: self.set_settings_tabs_visible(False))
        general_page_layout = QVBoxLayout(page_general)
        general_page_layout.setSpacing(12)
        general_page_layout.setContentsMargins(0, 12, 0, 0)
        video_page_layout = QVBoxLayout(page_video)
        video_page_layout.setSpacing(12)
        video_page_layout.setContentsMargins(0, 12, 0, 0)
        settings_page_layout = QVBoxLayout(page_settings)
        settings_page_layout.setSpacing(12)
        settings_page_layout.setContentsMargins(0, 12, 0, 0)
        main_layout = QVBoxLayout(page_main)
        main_layout.setSpacing(12)
        main_layout.setContentsMargins(0, 12, 0, 0)
        dir_card = QFrame()
        dir_card.setProperty('class', 'card')
        dir_grid = QGridLayout(dir_card)
        dir_grid.setSpacing(8)
        lbl_in = QLabel('Thư mục Video Nguồn:')
        self.txt_input_dir = QLineEdit()
        self.txt_input_dir.setPlaceholderText('Chọn thư mục chứa video hoặc thư mục cha kênh (Ví dụ: E:\\Videos\\Channel1)...')
        btn_browse_in = QPushButton('📁 Chọn Thư Mục')
        btn_browse_in.clicked.connect(self.browse_input_dir)
        lbl_out = QLabel('Thư mục Xuất Kết Quả:')
        self.txt_output_dir = QLineEdit()
        self.txt_output_dir.setPlaceholderText('Chọn thư mục lưu video đã cắt...')
        btn_browse_out = QPushButton('📂 Chọn Thư Mục')
        btn_browse_out.clicked.connect(self.browse_output_dir)
        btn_open_out = QPushButton('🔗 Mở Thư Mục Out')
        btn_open_out.clicked.connect(self.open_output_dir)
        dir_grid.addWidget(lbl_in, 0, 0)
        dir_grid.addWidget(self.txt_input_dir, 0, 1)
        dir_grid.addWidget(btn_browse_in, 0, 2)
        dir_grid.addWidget(lbl_out, 1, 0)
        dir_grid.addWidget(self.txt_output_dir, 1, 1)
        out_btns_layout = QHBoxLayout()
        out_btns_layout.addWidget(btn_browse_out)
        out_btns_layout.addWidget(btn_open_out)
        dir_grid.addLayout(out_btns_layout, 1, 2)
        main_layout.addWidget(dir_card)
        settings_card = QFrame()
        settings_card.setProperty('class', 'card')
        settings_grid = QGridLayout(settings_card)
        settings_grid.setSpacing(10)
        settings_grid.addWidget(QLabel('Độ dài tối thiểu (Min seconds):'), 0, 0)
        self.spn_min_len = QSpinBox()
        self.spn_min_len.setRange(10, 3600)
        self.spn_min_len.setValue(220)
        settings_grid.addWidget(self.spn_min_len, 0, 1)
        settings_grid.addWidget(QLabel('Độ dài tối đa (Max seconds):'), 0, 2)
        self.spn_max_len = QSpinBox()
        self.spn_max_len.setRange(20, 7200)
        self.spn_max_len.setValue(360)
        settings_grid.addWidget(self.spn_max_len, 0, 3)
        settings_grid.addWidget(QLabel('Tốc độ Video (Speed):'), 1, 0)
        self.spn_speed = QDoubleSpinBox()
        self.spn_speed.setRange(0.5, 3.0)
        self.spn_speed.setSingleStep(0.05)
        self.spn_speed.setValue(1.0)
        settings_grid.addWidget(self.spn_speed, 1, 1)
        settings_grid.addWidget(QLabel('Khung hình / Resolution:'), 1, 2)
        self.cmb_resolution = QComboBox()
        self.cmb_resolution.addItems(['960x720', '1080x1920 (9:16 Shorts/TikTok)', '1920x1080 (16:9 Standard)', '1440x1080 (4:3 FHD)', '1080x1080 (1:1 Vuông FHD)', '720x720 (1:1 Vuông HD)', 'Original'])
        settings_grid.addWidget(self.cmb_resolution, 1, 3)
        settings_grid.addWidget(QLabel('Bộ Mã Hóa (Encoder):'), 2, 0)
        self.cmb_encoder = QComboBox()
        self.cmb_encoder.addItem('Tự động chọn (Auto GPU/CPU)', 'auto')
        self.cmb_encoder.addItem('Nvidia NVENC (h264_nvenc)', 'h264_nvenc')
        self.cmb_encoder.addItem('AMD AMF (h264_amf)', 'h264_amf')
        self.cmb_encoder.addItem('Intel QSV (h264_qsv)', 'h264_qsv')
        self.cmb_encoder.addItem('CPU Standard (libx264)', 'libx264')
        settings_grid.addWidget(self.cmb_encoder, 2, 1)
        self.chk_delete_original = QCheckBox('🗑 Tự động xóa video gốc (chuyển vào Thùng Rác) sau khi cắt thành công')
        settings_grid.addWidget(self.chk_delete_original, 3, 0, 1, 4)
        main_layout.addWidget(settings_card)
        # --- Tab Cài đặt chung ---
        general_card = QFrame()
        general_card.setProperty('class', 'card')
        general_grid = QGridLayout(general_card)
        general_grid.setSpacing(10)
        general_grid.addWidget(QLabel('Render song song (luồng):'), 0, 0)
        self.spn_parallel = QSpinBox()
        self.spn_parallel.setRange(1, 4)
        self.spn_parallel.setValue(2)
        self.spn_parallel.setToolTip('Số ffmpeg chạy cùng lúc. 2-3 thường nhanh nhất với GPU; card đời cũ giới hạn số phiên NVENC nên đặt thấp. CPU yếu thì để 1.')
        general_grid.addWidget(self.spn_parallel, 0, 1)
        general_grid.addWidget(QLabel('Kiểu đặt tên đoạn:'), 1, 0)
        self.cmb_name_style = QComboBox()
        for label, key in NAME_STYLES:
            self.cmb_name_style.addItem(label, key)
        self.cmb_name_style.setToolTip('Áp dụng cho tên file và cả tiêu đề trên video. Video chỉ có 1 đoạn thì giữ nguyên tên gốc.')
        general_grid.addWidget(self.cmb_name_style, 1, 1)
        self.chk_limit = QCheckBox('⚡ Chỉ cắt N đoạn đầu của mỗi video (tăng tốc, bỏ qua phần còn lại):')
        general_grid.addWidget(self.chk_limit, 2, 0)
        self.spn_limit = QSpinBox()
        self.spn_limit.setRange(1, 99)
        self.spn_limit.setValue(2)
        self.spn_limit.setSuffix(' đoạn')
        general_grid.addWidget(self.spn_limit, 2, 1)
        self.chk_limit.toggled.connect(self.spn_limit.setEnabled)
        self.chk_even = QCheckBox('🔢 Chỉ cắt N đoạn đầu với N chẵn (không đủ N thì lấy số chẵn gần nhất bên dưới):')
        self.chk_even.setToolTip('Ví dụ chọn 4: video cắt được 5+ đoạn -> lấy 4 đoạn đầu; chỉ cắt được 3 đoạn -> lấy 2 đoạn đầu; chỉ 1 đoạn -> bỏ qua video.')
        general_grid.addWidget(self.chk_even, 3, 0)
        self.spn_even = QSpinBox()
        self.spn_even.setRange(2, 98)
        self.spn_even.setSingleStep(2)
        self.spn_even.setKeyboardTracking(False)
        self.spn_even.setValue(4)
        self.spn_even.setSuffix(' đoạn')
        self.spn_even.setEnabled(False)
        general_grid.addWidget(self.spn_even, 3, 1)
        self.chk_even.toggled.connect(self.spn_even.setEnabled)
        self.spn_even.valueChanged.connect(self._snap_even)
        # Hai chế độ giới hạn loại trừ nhau
        self.chk_limit.toggled.connect(lambda on: on and self.chk_even.setChecked(False))
        self.chk_even.toggled.connect(lambda on: on and self.chk_limit.setChecked(False))
        general_grid.setColumnStretch(0, 1)
        general_grid.setColumnStretch(1, 1)
        general_page_layout.addWidget(general_card)
        general_page_layout.addStretch()
        # --- Tab Cài đặt video ---
        video_card = QFrame()
        video_card.setProperty('class', 'card')
        video_grid = QGridLayout(video_card)
        video_grid.setSpacing(10)
        video_grid.addWidget(QLabel('Chỉnh âm thanh (dB):'), 0, 0)
        self.spn_volume = QDoubleSpinBox()
        self.spn_volume.setRange(-60.0, 40.0)
        self.spn_volume.setDecimals(1)
        self.spn_volume.setSingleStep(0.5)
        self.spn_volume.setValue(0.0)
        self.spn_volume.setSuffix(' dB')
        self.spn_volume.setToolTip('Số dương = tăng âm lượng, số âm = giảm âm lượng, 0 = giữ nguyên. Ví dụ: +6 dB ≈ gấp đôi biên độ, -6 dB ≈ giảm một nửa.')
        video_grid.addWidget(self.spn_volume, 0, 1)
        lbl_vol_hint = QLabel('Số dương (+) tăng âm thanh, số âm (-) giảm âm thanh, 0 = giữ nguyên.')
        lbl_vol_hint.setProperty('class', 'subtitle')
        video_grid.addWidget(lbl_vol_hint, 1, 0, 1, 2)
        video_grid.setColumnStretch(0, 1)
        video_grid.setColumnStretch(1, 1)
        video_page_layout.addWidget(video_card)
        video_page_layout.addStretch()
        title_card = QFrame()
        title_card.setProperty('class', 'card')
        title_grid = QGridLayout(title_card)
        title_grid.setSpacing(10)
        self.chk_title = QCheckBox('📱 Khung 9:16 (1080x1920) nền đen: video cắt theo tỉ lệ ở mục Resolution, đặt giữa; tiêu đề (tên đoạn -1, -2...) luôn nằm giữa vùng an toàn phía trên của TikTok (240px) và mép trên video')
        title_grid.addWidget(self.chk_title, 0, 0, 1, 4)
        title_grid.addWidget(QLabel('Cỡ chữ (px):'), 1, 0)
        self.spn_title_size = QSpinBox()
        self.spn_title_size.setRange(1, 300)
        self.spn_title_size.setValue(12)
        title_grid.addWidget(self.spn_title_size, 1, 1)
        title_grid.addWidget(QLabel('Màu chữ:'), 1, 2)
        self.btn_title_color = QPushButton('#000000')
        self.btn_title_color.clicked.connect(self.pick_title_color)
        title_grid.addWidget(self.btn_title_color, 1, 3)
        title_grid.addWidget(QLabel('Căn ngang:'), 2, 0)
        self.cmb_title_halign = QComboBox()
        self.cmb_title_halign.addItem('Giữa', 'center')
        self.cmb_title_halign.addItem('Trái', 'left')
        self.cmb_title_halign.addItem('Phải', 'right')
        title_grid.addWidget(self.cmb_title_halign, 2, 1)
        title_grid.addWidget(QLabel('Tỉ lệ tiêu đề (%):'), 2, 2)
        self.spn_title_scale = QSpinBox()
        self.spn_title_scale.setRange(10, 400)
        self.spn_title_scale.setValue(100)
        self.spn_title_scale.setSingleStep(5)
        self.spn_title_scale.setToolTip('Phóng to / thu nhỏ cả tiêu đề (chữ + nền). 100 = giữ nguyên. Tiêu đề luôn được căn giữa giữa vùng an toàn và mép trên video.')
        title_grid.addWidget(self.spn_title_scale, 2, 3)
        self.chk_box = QCheckBox('🔲 Thêm nền (ô) phía sau chữ tiêu đề')
        self.chk_box.setChecked(True)
        title_grid.addWidget(self.chk_box, 3, 0, 1, 4)
        title_grid.addWidget(QLabel('Màu nền ô:'), 4, 0)
        self.btn_box_color = QPushButton('#FFFFFF')
        self.btn_box_color.clicked.connect(self.pick_box_color)
        title_grid.addWidget(self.btn_box_color, 4, 1)
        title_grid.addWidget(QLabel('Độ đục nền (%):'), 4, 2)
        self.spn_box_opacity = QSpinBox()
        self.spn_box_opacity.setRange(0, 100)
        self.spn_box_opacity.setValue(100)
        title_grid.addWidget(self.spn_box_opacity, 4, 3)
        title_grid.addWidget(QLabel('Độ bo góc (%):'), 5, 0)
        self.spn_box_radius = QSpinBox()
        self.spn_box_radius.setRange(0, 50)
        self.spn_box_radius.setValue(30)
        self.spn_box_radius.setToolTip('% chiều cao ô nền của một dòng. 0 = vuông, 50 = bo tròn hoàn toàn (viên thuốc)')
        title_grid.addWidget(self.spn_box_radius, 5, 1)
        title_grid.addWidget(QLabel('Lề hai bên (px):'), 5, 2)
        self.spn_margin_x = QSpinBox()
        self.spn_margin_x.setRange(0, 400)
        self.spn_margin_x.setValue(180)
        self.spn_margin_x.setToolTip('Khoảng cách từ ô tới mép trái/phải khung. Lề càng lớn thì ô tiêu đề càng hẹp (xuống dòng nhiều hơn), lề càng nhỏ thì ô càng rộng. Tiêu đề dài hơn 3 dòng sẽ tự giảm lề dần (tối thiểu 120px) cho tới khi còn 3 dòng')
        title_grid.addWidget(self.spn_margin_x, 5, 3)
        title_grid.addWidget(QLabel('Khoảng cách dòng (% cỡ chữ):'), 6, 0)
        self.spn_line_spacing = QSpinBox()
        self.spn_line_spacing.setRange(20, 600)
        self.spn_line_spacing.setValue(176)
        self.spn_line_spacing.setToolTip('Khoảng cách giữa 2 dòng chữ (từ đường chân chữ dòng trên tới dòng dưới), % cỡ chữ. ĐỘC LẬP với chiều cao ô nền: đổi đệm trên/dưới thì chữ KHÔNG nhúc nhích. Nhỏ hơn chiều cao ô (100 + 2 x đệm trên/dưới) = các ô chồng lên nhau (góc bo vẫn tự nhiên); bằng = các ô chạm nhau thành bậc thang; lớn hơn = mỗi dòng một ô riêng.')
        title_grid.addWidget(self.spn_line_spacing, 6, 1)
        title_grid.addWidget(QLabel('Đệm trên/dưới (% cỡ chữ):'), 6, 2)
        self.spn_line_pad_y = QSpinBox()
        self.spn_line_pad_y.setRange(-50, 300)
        self.spn_line_pad_y.setValue(38)
        self.spn_line_pad_y.setToolTip('Chiều cao ô nền của từng dòng = cỡ chữ + 2 x đệm (% cỡ chữ). Ô luôn đối xứng quanh tâm dòng chữ: tăng đệm thì mép trên và mép dưới của MỖI ô cùng kéo ra, tâm ô và chữ đứng yên, khoảng cách giữa các dòng không đổi (chỉnh ở "Khoảng cách dòng"). Ô chồng nhau thì tự hợp nhất và bo góc tự nhiên. Giá trị âm = nền ôm sát chữ hơn.')
        title_grid.addWidget(self.spn_line_pad_y, 6, 3)
        title_grid.addWidget(QLabel('Đệm trái/phải (% cỡ chữ):'), 7, 0)
        self.spn_line_pad_x = QSpinBox()
        self.spn_line_pad_x.setRange(0, 300)
        self.spn_line_pad_x.setValue(55)
        self.spn_line_pad_x.setToolTip('Đệm TRÁI và PHẢI mỗi dòng chữ, % cỡ chữ: nền nở ra cả hai bên từng dòng chữ. CHỈ đổi bề rộng nền: không đổi chỗ xuống dòng, không dịch chữ.')
        title_grid.addWidget(self.spn_line_pad_x, 7, 1)
        self.chk_live = QCheckBox('🔄 Xem trước tự cập nhật khi đổi thông số (khi cửa sổ xem trước đang mở)')
        self.chk_live.setChecked(True)
        title_grid.addWidget(self.chk_live, 8, 0, 1, 4)
        title_grid.addWidget(QLabel('Tiêu đề test:'), 9, 0)
        self.txt_title_test = QLineEdit('How Fast Is IShowSpeed Actually? (2)')
        self.txt_title_test.setPlaceholderText('Nhập tiêu đề để xem trước...')
        title_grid.addWidget(self.txt_title_test, 9, 1, 1, 2)
        self.btn_title_preview = QPushButton('👁 Xem trước')
        self.btn_title_preview.setToolTip(f'Mở cửa sổ canvas {CANVAS_W}x{CANVAS_H} nền đen với tiêu đề test theo các thông số bên trên. Đổi thông số rồi bấm lại để tạo lại.')
        self.btn_title_preview.clicked.connect(self.show_title_preview)
        self.txt_title_test.returnPressed.connect(self.show_title_preview)
        title_grid.addWidget(self.btn_title_preview, 9, 3)
        self._preview_windows = []
        self._preview_timer = QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.setInterval(250)
        self._preview_timer.timeout.connect(lambda: self.show_title_preview(auto=True))
        self.title_color = '#000000'
        self.box_color = '#FFFFFF'
        self.chk_title.toggled.connect(self.update_title_controls)
        self.chk_box.toggled.connect(self.update_title_controls)
        settings_page_layout.addWidget(title_card)
        settings_page_layout.addStretch()
        progress_card = QFrame()
        progress_card.setProperty('class', 'card')
        progress_layout = QVBoxLayout(progress_card)
        self.lbl_status = QLabel('Trạng thái: Sẵn sàng')
        self.lbl_status.setFont(QFont('Segoe UI', 10, QFont.Weight.Bold))
        self.bar_file_progress = QProgressBar()
        self.bar_file_progress.setValue(0)
        self.bar_file_progress.setFormat('File hiện tại: %p%')
        self.bar_batch_progress = QProgressBar()
        self.bar_batch_progress.setValue(0)
        self.bar_batch_progress.setFormat('Tổng tiến trình danh sách: %v/%m video')
        btn_layout = QHBoxLayout()
        self.btn_start = QPushButton('🚀 BẮT ĐẦU CẮT VIDEO TỐC ĐỘ CAO')
        self.btn_start.setProperty('class', 'btn-primary')
        self.btn_start.clicked.connect(self.start_processing)
        self.btn_stop = QPushButton('🛑 DỪNG NGAY LẬP TỨC')
        self.btn_stop.setProperty('class', 'btn-danger')
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self.stop_processing)
        btn_layout.addWidget(self.btn_start, 3)
        btn_layout.addWidget(self.btn_stop, 1)
        progress_layout.addWidget(self.lbl_status)
        progress_layout.addWidget(self.bar_file_progress)
        progress_layout.addWidget(self.bar_batch_progress)
        progress_layout.addLayout(btn_layout)
        main_layout.addWidget(progress_card)
        self.txt_log = QTextEdit()
        self.txt_log.setReadOnly(True)
        main_layout.addWidget(self.txt_log, 1)
        saved_cfg = load_settings()
        self.spn_min_len.setValue(saved_cfg.get('min_len', 120))
        self.spn_max_len.setValue(saved_cfg.get('max_len', 180))
        self.spn_speed.setValue(saved_cfg.get('speed', 1.1))
        saved_res = saved_cfg.get('resolution', '1080x1080 (1:1 Vuông FHD)')
        res_idx = self.cmb_resolution.findText(saved_res)
        if res_idx >= 0:
            self.cmb_resolution.setCurrentIndex(res_idx)
        else:
            for i in range(self.cmb_resolution.count()):
                if '1080x1080' in self.cmb_resolution.itemText(i):
                    self.cmb_resolution.setCurrentIndex(i)
                    break
        idx_ns = self.cmb_name_style.findData(saved_cfg.get('name_style', 'paren'))
        self.cmb_name_style.setCurrentIndex(idx_ns if idx_ns >= 0 else 0)
        self.chk_delete_original.setChecked(saved_cfg.get('delete_original', False))
        self.chk_limit.setChecked(bool(saved_cfg.get('limit_enabled', False)))
        self.spn_limit.setValue(int(saved_cfg.get('limit_count', 2)))
        self.spn_limit.setEnabled(self.chk_limit.isChecked())
        self.chk_even.setChecked(bool(saved_cfg.get('even_enabled', False)))
        self.spn_even.setValue(max(2, int(saved_cfg.get('even_count', 4)) // 2 * 2))
        self.spn_even.setEnabled(self.chk_even.isChecked())
        self.spn_parallel.setValue(int(saved_cfg.get('parallel', 2)))
        self.spn_volume.setValue(float(saved_cfg.get('volume_db', 0.0)))
        self.chk_title.setChecked(bool(saved_cfg.get('title_enabled', True)))
        self.spn_title_size.setValue(int(saved_cfg.get('title_size', 42)))
        self.set_title_color(saved_cfg.get('title_color', '#000000'))
        self.chk_box.setChecked(bool(saved_cfg.get('box_enabled', True)))
        self.set_box_color(saved_cfg.get('box_color', '#FFFFFF'))
        self.spn_box_opacity.setValue(int(saved_cfg.get('box_opacity', 100)))
        self.spn_box_radius.setValue(int(saved_cfg.get('box_radius', 30)))
        self.spn_margin_x.setValue(int(saved_cfg.get('margin_x', 240)))
        self.spn_title_scale.setValue(int(saved_cfg.get('title_scale', 100)))
        self.spn_line_pad_x.setValue(int(saved_cfg.get('line_pad_x', 55)))
        self.spn_line_spacing.setValue(int(saved_cfg.get('line_spacing', 176)))
        self.spn_line_pad_y.setValue(int(saved_cfg.get('line_pad_y', 38)))
        self.chk_live.setChecked(bool(saved_cfg.get('live_preview', True)))
        idx_h = self.cmb_title_halign.findData(saved_cfg.get('title_halign', 'center'))
        self.cmb_title_halign.setCurrentIndex(idx_h if idx_h >= 0 else 0)
        self.update_title_controls()
        if saved_cfg.get('input_dir'):
            self.txt_input_dir.setText(saved_cfg.get('input_dir'))
        if saved_cfg.get('output_dir'):
            self.txt_output_dir.setText(saved_cfg.get('output_dir'))
        self.spn_min_len.valueChanged.connect(self.save_current_settings)
        self.spn_max_len.valueChanged.connect(self.save_current_settings)
        self.spn_speed.valueChanged.connect(self.save_current_settings)
        self.cmb_resolution.currentIndexChanged.connect(self.save_current_settings)
        self.cmb_name_style.currentIndexChanged.connect(self.save_current_settings)
        self.chk_delete_original.stateChanged.connect(self.save_current_settings)
        self.chk_limit.stateChanged.connect(self.save_current_settings)
        self.spn_limit.valueChanged.connect(self.save_current_settings)
        self.chk_even.stateChanged.connect(self.save_current_settings)
        self.spn_even.valueChanged.connect(self.save_current_settings)
        self.spn_parallel.valueChanged.connect(self.save_current_settings)
        self.spn_volume.valueChanged.connect(self.save_current_settings)
        self.txt_input_dir.textChanged.connect(self.save_current_settings)
        self.txt_output_dir.textChanged.connect(self.save_current_settings)
        self.chk_title.stateChanged.connect(self.save_current_settings)
        self.chk_box.stateChanged.connect(self.save_current_settings)
        self.spn_title_size.valueChanged.connect(self.save_current_settings)
        self.cmb_title_halign.currentIndexChanged.connect(self.save_current_settings)
        for spn in (self.spn_box_opacity, self.spn_box_radius, self.spn_margin_x, self.spn_line_pad_x, self.spn_line_spacing, self.spn_line_pad_y, self.spn_title_scale):
            spn.valueChanged.connect(self.save_current_settings)
        self.chk_live.stateChanged.connect(self.save_current_settings)
        # Xem trước tự cập nhật (debounce 250ms) khi đổi bất kỳ thông số tiêu đề nào
        live_spins = (self.spn_title_size, self.spn_box_opacity, self.spn_box_radius, self.spn_margin_x,
                      self.spn_line_pad_x, self.spn_line_spacing, self.spn_line_pad_y, self.spn_title_scale)
        for spn in live_spins:
            spn.valueChanged.connect(self._schedule_preview)
        self.cmb_title_halign.currentIndexChanged.connect(self._schedule_preview)
        self.cmb_resolution.currentIndexChanged.connect(self._schedule_preview)
        self.chk_box.stateChanged.connect(self._schedule_preview)
        self.chk_title.stateChanged.connect(self._schedule_preview)
        self.txt_title_test.textChanged.connect(self._schedule_preview)
    def _schedule_preview(self, *_):
        if hasattr(self, 'chk_live') and self.chk_live.isChecked():
            self._preview_timer.start()
    def _alive_preview(self):
        for w in self._preview_windows:
            try:
                if w.isVisible():
                    return w
            except RuntimeError:
                pass
        return None
    def _snap_even(self, v):
        """Ép giá trị N luôn chẵn (lẻ -> lùi xuống số chẵn gần nhất)."""
        if v % 2:
            self.spn_even.setValue(max(2, v - 1))
    def set_settings_tabs_visible(self, visible):
        """Hiện/ẩn 3 tab cài đặt bằng animation trượt (đổi chiều rộng của khung chứa)."""
        self.settings_tabs_visible = visible
        box = self.settings_tabs_box
        if visible:
            self.btn_expand.hide()
            self.btn_collapse.show()
        elif self.tab_group.checkedId() != 0:
            # Đang ở một tab cài đặt mà ẩn đi -> quay về tab Cắt video
            self.btn_tab_main.setChecked(True)
            self.tabs.setCurrentIndex(0)
        self.tab_anim.stop()
        full = box.layout().sizeHint().width()
        cur = box.maximumWidth()
        self.tab_anim.setStartValue(full if cur >= 16777215 else cur)
        self.tab_anim.setEndValue(full if visible else 0)
        self.tab_anim.start()
    def on_tab_anim_finished(self):
        if self.settings_tabs_visible:
            self.settings_tabs_box.setMaximumWidth(16777215)  # bỏ giới hạn để tab không bị cắt khi đổi cỡ cửa sổ
        else:
            self.btn_collapse.hide()
            self.btn_expand.show()

    def detect_system_gpu(self):
        enc_code, enc_name = check_h264_encoder()
        self.detected_encoder = enc_code
        self.lbl_gpu_badge.setText(f'⚡ {enc_name}')
        if 'GPU' in enc_name:
            self.lbl_gpu_badge.setStyleSheet('background-color: #0d3b2e; color: #00ffaa; border: 1px solid #00aa66;')
        else:
            self.lbl_gpu_badge.setStyleSheet('background-color: #3b2e0d; color: #ffaa00; border: 1px solid #aa6600;')
    def browse_input_dir(self):
        path = QFileDialog.getExistingDirectory(self, 'Chọn thư mục chứa video')
        if path:
            self.txt_input_dir.setText(path)
    def browse_output_dir(self):
        path = QFileDialog.getExistingDirectory(self, 'Chọn thư mục xuất kết quả')
        if path:
            self.txt_output_dir.setText(path)
    def open_output_dir(self):
        out_path = self.txt_output_dir.text().strip()
        if out_path and os.path.exists(out_path):
            os.startfile(out_path)
        else:
            QMessageBox.warning(self, 'Cảnh báo', 'Thư mục đầu ra chưa tồn tại!')
    def append_log(self, text, color_type='default'):
        cursor = self.txt_log.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        color_map = {'info': '#00d2ff', 'success': '#00ffaa', 'warning': '#ffbb00', 'error': '#ff5555', 'default': '#a0aec0'}
        hex_color = color_map.get(color_type, '#a0aec0')
        formatted_html = f'<span style=\'color: {hex_color};\'>{text}</span><br>'
        cursor.insertHtml(formatted_html)
        self.txt_log.setTextCursor(cursor)
        self.txt_log.ensureCursorVisible()
    def start_processing(self):
        in_dir = self.txt_input_dir.text().strip()
        out_dir = self.txt_output_dir.text().strip()
        if not in_dir or not os.path.exists(in_dir):
            QMessageBox.critical(self, 'Lỗi', 'Vui lòng chọn thư mục video nguồn hợp lệ!')
            return
        else:
            if not out_dir:
                QMessageBox.critical(self, 'Lỗi', 'Vui lòng chọn thư mục lưu kết quả!')
                return
            else:
                min_l = self.spn_min_len.value()
                max_l = self.spn_max_len.value()
                if min_l >= max_l:
                    QMessageBox.critical(self, 'Lỗi', 'Độ dài tối thiểu phải nhỏ hơn độ dài tối đa!')
                    return
                else:
                    speed = self.spn_speed.value()
                    res = self.cmb_resolution.currentText()
                    delete_orig = self.chk_delete_original.isChecked()
                    selected_enc = self.cmb_encoder.currentData()
                    if selected_enc == 'auto' or not selected_enc:
                        encoder = self.detected_encoder
                    else:
                        encoder = selected_enc
                    if self.chk_even.isChecked():
                        max_seg = max(2, self.spn_even.value() // 2 * 2)
                    elif self.chk_limit.isChecked():
                        max_seg = self.spn_limit.value()
                    else:
                        max_seg = None
                    self.save_current_settings()
                    self.btn_start.setEnabled(False)
                    self.btn_stop.setEnabled(True)
                    self.txt_log.clear()
                    self.worker = ProcessingWorker(input_dir=in_dir, output_dir=out_dir, min_len=min_l, max_len=max_l, speed=speed, resolution=res, encoder=encoder, delete_original=delete_orig, title_opts=self.get_title_opts(), max_segments=max_seg, even_only=self.chk_even.isChecked(), parallel=self.spn_parallel.value(), name_style=self.cmb_name_style.currentData(), volume_db=self.spn_volume.value())
                    self.worker.log_signal.connect(self.append_log)
                    self.worker.progress_signal.connect(self.update_batch_progress)
                    self.worker.file_progress_signal.connect(self.update_file_progress)
                    self.worker.finished_signal.connect(self.processing_finished)
                    self.worker.start()
    def update_title_controls(self):
        enabled = self.chk_title.isChecked()
        box_on = enabled and self.chk_box.isChecked()
        for w in (self.spn_title_size, self.btn_title_color, self.cmb_title_halign,
                  self.spn_margin_x, self.spn_title_scale, self.spn_line_spacing, self.chk_box, self.chk_live, self.btn_title_preview, self.txt_title_test):
            w.setEnabled(enabled)
        for w in (self.btn_box_color, self.spn_box_opacity, self.spn_box_radius,
                  self.spn_line_pad_x, self.spn_line_pad_y):
            w.setEnabled(box_on)
    def set_title_color(self, hex_color):
        color = QColor(str(hex_color))
        if not color.isValid():
            color = QColor('#000000')
        self.title_color = color.name().upper()
        text_color = '#000000' if color.lightness() > 128 else '#FFFFFF'
        self.btn_title_color.setText(self.title_color)
        self.btn_title_color.setStyleSheet(f'background-color: {self.title_color}; color: {text_color};')
    def pick_title_color(self):
        color = QColorDialog.getColor(QColor(self.title_color), self, 'Chọn màu chữ tiêu đề')
        if color.isValid():
            self.set_title_color(color.name())
            self.save_current_settings()
            self._schedule_preview()
    def set_box_color(self, hex_color):
        color = QColor(str(hex_color))
        if not color.isValid():
            color = QColor('#FFFFFF')
        self.box_color = color.name().upper()
        text_color = '#000000' if color.lightness() > 128 else '#FFFFFF'
        self.btn_box_color.setText(self.box_color)
        self.btn_box_color.setStyleSheet(f'background-color: {self.box_color}; color: {text_color};')
    def pick_box_color(self):
        color = QColorDialog.getColor(QColor(self.box_color), self, 'Chọn màu nền ô tiêu đề')
        if color.isValid():
            self.set_box_color(color.name())
            self.save_current_settings()
            self._schedule_preview()
    def get_title_opts(self):
        if not self.chk_title.isChecked():
            return None
        return self._build_title_opts()
    def _preview_info(self):
        o = self._build_title_opts()
        info = (f"cỡ {o['size']} | đệm trái/phải {o['line_pad_x']} | đệm trên/dưới {o['line_pad_y']} | cách dòng {o['line_spacing']} | bo {o['box_radius']} | lề {o['margin_x']} | tỉ lệ {o['title_scale']}%")
        _eng = sys.modules.get(render_title_preview.__module__)
        info += f" | engine: {getattr(_eng, 'TITLE_LAYOUT', 'CŨ')} @ {os.path.abspath(getattr(_eng, '__file__', '?'))}"
        return info
    def show_title_preview(self, auto=False):
        if auto:
            win = self._alive_preview()
            text = self.txt_title_test.text().strip()
            if win is None or not text:
                return
            try:
                img = render_title_preview(self._build_title_opts(), text, self.cmb_resolution.currentText())
            except Exception:
                return
            fp = _find_title_font()
            win.set_content(img, text, os.path.basename(fp) if fp else '', self._preview_info())
            return
        # Đóng cửa sổ xem trước cũ rồi tạo lại 1 cửa sổ theo thông số hiện tại
        for w in self._preview_windows:
            try:
                w.close()
            except RuntimeError:
                pass
        self._preview_windows = []
        text = self.txt_title_test.text().strip()
        if not text:
            QMessageBox.information(self, 'Xem trước tiêu đề', 'Hãy nhập tiêu đề test trước.')
            return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            img = render_title_preview(self._build_title_opts(), text, self.cmb_resolution.currentText())
        except Exception as e:
            QApplication.restoreOverrideCursor()
            QMessageBox.warning(self, 'Xem trước tiêu đề', f'Không tạo được ảnh xem trước:\n{e}')
            return
        QApplication.restoreOverrideCursor()
        geo = self.screen().availableGeometry()
        frame_extra = 40  # chừa chỗ thanh tiêu đề cửa sổ
        scale = min((geo.height() - frame_extra) * 0.95 / CANVAS_H, geo.width() * 0.95 / CANVAS_W, 1.0)
        w_win, h_win = max(120, int(CANVAS_W * scale)), max(200, int(CANVAS_H * scale))
        font_path = _find_title_font()
        info = self._preview_info()
        win = TitlePreviewWindow(img, text, os.path.basename(font_path) if font_path else '', info)
        win.lbl.setToolTip(f'Font: {font_path}')
        win.resize(w_win, h_win)
        win.move(geo.x() + max(0, (geo.width() - w_win) // 2), geo.y() + max(0, (geo.height() - h_win - frame_extra) // 2))
        win.show()
        self._preview_windows.append(win)
        if not font_path or 'tiktok' not in os.path.basename(font_path).lower():
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Icon.Warning)
            box.setWindowTitle('Không tìm thấy font TikTok Sans')
            box.setText('Không tìm thấy font TikTok Sans nên đang dùng font dự phòng: ' + (os.path.basename(font_path) if font_path else '(không có)') + '.\nBấm "Show Details" để xem các thư mục đã tìm và file font thấy được.')
            box.setDetailedText(font_search_report())
            box.exec()
    def _build_title_opts(self):
        opts = {'size': self.spn_title_size.value(), 'color': self.title_color,
                'halign': self.cmb_title_halign.currentData(),
                'box_enabled': self.chk_box.isChecked(), 'box_color': self.box_color, 'box_opacity': self.spn_box_opacity.value() if self.chk_box.isChecked() else 0, 'box_radius': self.spn_box_radius.value(),
                'line_pad_x': self.spn_line_pad_x.value(), 'line_spacing': self.spn_line_spacing.value(), 'line_pad_y': self.spn_line_pad_y.value(), 'margin_x': self.spn_margin_x.value(),
                'title_scale': self.spn_title_scale.value()}
        opts.update(TITLE_FIXED)
        return opts
    def save_current_settings(self):
        cfg = {'min_len': self.spn_min_len.value(), 'max_len': self.spn_max_len.value(), 'speed': self.spn_speed.value(), 'resolution': self.cmb_resolution.currentText(), 'delete_original': self.chk_delete_original.isChecked(), 'input_dir': self.txt_input_dir.text().strip(), 'output_dir': self.txt_output_dir.text().strip(), 'title_enabled': self.chk_title.isChecked(), 'title_size': self.spn_title_size.value(), 'title_color': self.title_color, 'title_halign': self.cmb_title_halign.currentData(), 'box_enabled': self.chk_box.isChecked(), 'box_color': self.box_color, 'box_opacity': self.spn_box_opacity.value(), 'box_radius': self.spn_box_radius.value(), 'line_pad_x': self.spn_line_pad_x.value(), 'line_spacing': self.spn_line_spacing.value(), 'line_pad_y': self.spn_line_pad_y.value(), 'margin_x': self.spn_margin_x.value(), 'title_scale': self.spn_title_scale.value(), 'limit_enabled': self.chk_limit.isChecked(), 'limit_count': self.spn_limit.value(), 'even_enabled': self.chk_even.isChecked(), 'even_count': self.spn_even.value(), 'parallel': self.spn_parallel.value(), 'name_style': self.cmb_name_style.currentData(), 'volume_db': self.spn_volume.value(),
               'live_preview': self.chk_live.isChecked()}
        save_settings(cfg)
    def closeEvent(self, event):
        self.save_current_settings()
        if self.worker and self.worker.isRunning():
            self.worker.stop()
            self.worker.wait(8000)
        super().closeEvent(event)
    def stop_processing(self):
        if self.worker and self.worker.isRunning():
                self.append_log('🛑 Đang dừng tiến trình ngay lập tức...', 'warning')
                self.worker.stop()
                self.btn_stop.setEnabled(False)
    def update_batch_progress(self, current, total):
        self.bar_batch_progress.setMaximum(total)
        self.bar_batch_progress.setValue(current)
        self.lbl_status.setText(f'Trạng thái: Đang xử lý video {current}/{total}')
    def update_file_progress(self, status, pct):
        self.lbl_status.setText(f'Trạng thái: {status}')
        self.bar_file_progress.setValue(pct)
    def processing_finished(self):
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.lbl_status.setText('Trạng thái: Đã dừng / Hoàn tất')
        self.bar_file_progress.setValue(100)
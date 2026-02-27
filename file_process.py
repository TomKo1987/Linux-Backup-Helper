from pathlib import Path
from options import Options
from PyQt6.QtGui import QColor
from samba_password import SambaPasswordManager
import os, re, time, shutil, psutil, tempfile, subprocess
from global_style import get_current_style as _get_global_style
from PyQt6.QtCore import (QThread, QTimer, QElapsedTimer, QMutex, QMutexLocker, QWaitCondition, QDateTime, pyqtSignal,
                          QAbstractListModel, QModelIndex, Qt, QCoreApplication)
from PyQt6.QtWidgets import (QDialog, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QProgressBar, QTabWidget, QLineEdit,
                             QDialogButtonBox, QInputDialog, QMessageBox, QListView, QGraphicsDropShadowEffect)

from logging_config import setup_logger
logger = setup_logger(__name__)


# noinspection PyUnresolvedReferences
class FileProcessDialog(QDialog):
    UPDATE_TIMER_INTERVAL = 350
    ELAPSED_UPDATE_INTERVAL = 250

    SMB_MOUNT_TIMEOUT = 10
    SMB_UNMOUNT_TIMEOUT = 2
    SMB_OPERATION_DELAY = 0.5
    SMB_CLEANUP_DELAY = 0.5

    TAB_CONFIG = {
        'summary': {'index': 0, 'color': '#6ffff5', 'display': 'Summary'},
        'copied': {'index': 1, 'color': 'lightgreen', 'display': 'Copied'},
        'skipped': {'index': 2, 'color': '#ffff7f', 'display': 'Skipped'},
        'error': {'index': 3, 'color': '#ff8587', 'display': 'Errors'}
    }

    def __init__(self, parent, checkbox_dirs, operation_type):
        super().__init__(parent)
        self.operation_type = operation_type
        self.checkbox_dirs = checkbox_dirs
        self.setWindowTitle(operation_type)
        self._last_summary_update_time = 0
        self.sudo_password = None
        self.sudo_password_event = QWaitCondition()
        self.sudo_password_mutex = QMutex()
        self.sudo_dialog_open = False
        self.status_label = QLabel(f"{self.operation_type} in progress...\n")
        self.status_label.setStyleSheet(
            "color: #6ffff5; font-weight: bold; font-size: 20px; background-color: transparent;")
        self.current_file_label = QLabel(f"Preparing:\n{self.operation_type}")
        self.current_file_label.setStyleSheet("font-weight: bold; font-size: 17px;")
        self.elapsed_time_label = QLabel("\nElapsed time:\n00s\n")
        self.elapsed_time_label.setStyleSheet("font-weight: bold; font-size: 17px;")
        self.progress_bar = QProgressBar()
        self.progress_bar.setValue(0)
        self.progress_bar.setStyleSheet(_get_global_style())
        self.tab_widget = QTabWidget()
        self.copied_tab = VirtualLogTabWidget()
        self.skipped_tab = VirtualLogTabWidget()
        self.error_tab = VirtualLogTabWidget()
        self.summary_tab = QWidget()
        self.timer = QElapsedTimer()
        self.paused_elapsed = 0
        self.update_timer = QTimer(self)
        self.thread = FileCopyThread(self.checkbox_dirs, self.operation_type)
        self.thread.set_parent_dialog(self)
        self.container = QWidget(self)
        self.container.setProperty("class", "container")
        self.info_layout = QVBoxLayout()
        self.cancel_button = None
        self.summary_table = QWidget()
        self.summary_layout = QVBoxLayout(self.summary_table)
        self.shadow = QGraphicsDropShadowEffect()
        self.shadow.setBlurRadius(100)
        self.shadow.setColor(QColor(0, 0, 0, 250))
        self.shadow.setOffset(0.0, 1.0)
        self.total_bytes_copied = 0
        self.total_files = 0
        self.copied_count = 0
        self.skipped_count = 0
        self.error_count = 0
        self.color_step = 0
        self.cancelled = False
        self._smb_error_occurred = False
        self._error_keys = set()
        self.color_timer = QTimer(self)
        self.setup_ui_layout()
        self.setup_tabs()
        self.setup_connections()
        self.start_process()

    def handle_sudo_password_request(self):
        if self.sudo_dialog_open:
            return
        self.sudo_dialog_open = True
        try:
            if self.timer.isValid():
                self.paused_elapsed += self.timer.elapsed()
            self.update_timer.stop()
            password, ok = QInputDialog.getText(
                self, "Sudo Password", "Enter sudo password for mounting SMB shares:", QLineEdit.EchoMode.Password)
            with QMutexLocker(self.sudo_password_mutex):
                self.sudo_password = password if ok and password else None
                self.sudo_password_event.wakeAll()
            self.timer.restart()
            self.update_timer.start(self.UPDATE_TIMER_INTERVAL)
        finally:
            self.sudo_dialog_open = False

    def get_sudo_password(self):
        with QMutexLocker(self.sudo_password_mutex):
            if self.sudo_password is not None:
                return self.sudo_password
            self.thread.sudo_password_requested.emit()
            self.sudo_password_event.wait(self.sudo_password_mutex)
            if self.sudo_password is None:
                raise RuntimeError("Sudo password required for mounting SMB shares")
            return self.sudo_password

    def setup_connections(self):
        t = self.thread
        t.workers_ready.connect(lambda: self.cancel_button.setEnabled(True))
        t.file_copied.connect(self.log_file_copied)
        t.file_skipped.connect(self.log_file_skipped)
        t.file_error.connect(self.log_file_error)
        t.progress_updated.connect(self.update_progress)
        t.operation_completed.connect(self.operation_completed)
        t.smb_error_cancel.connect(self._on_smb_error_cancel)
        t.sudo_password_requested.connect(self.handle_sudo_password_request)
        self.update_timer.timeout.connect(self.update_elapsed_time)

    def setup_ui_layout(self):
        main_layout = QVBoxLayout(self)
        self.info_layout.addWidget(self.current_file_label)
        self.info_layout.addWidget(self.elapsed_time_label)
        self.info_layout.addWidget(self.progress_bar)
        button_box = QDialogButtonBox()
        self.cancel_button = button_box.addButton("Cancel", QDialogButtonBox.ButtonRole.RejectRole)
        self.cancel_button.setFixedSize(125, 35)
        self.cancel_button.clicked.connect(self.cancel_operation)
        self.cancel_button.setEnabled(False)
        self.cancel_button.setStyleSheet(_get_global_style())
        container_layout = QVBoxLayout(self.container)
        container_layout.addWidget(self.status_label)
        container_layout.addWidget(self.tab_widget)
        container_layout.addWidget(button_box)
        main_layout.addWidget(self.container)
        self.setFixedSize(1200, 700)

    def start_process(self):
        self.timer.start()
        self.update_timer.start(self.UPDATE_TIMER_INTERVAL)
        self.thread.start()

    def setup_tabs(self):
        tab_style = "font-family: FiraCode Nerd Font Mono; font-size: 14px; padding: 10px;"
        tab_bar = self.tab_widget.tabBar()
        for tab_type, config in self.TAB_CONFIG.items():
            if tab_type == 'summary':
                self.setup_summary_tab()
                self.tab_widget.addTab(self.summary_tab, config['display'])
            else:
                tab = getattr(self, f"{tab_type}_tab")
                tab.setStyleSheet(f"color: {config['color']}; {tab_style}")
                self.tab_widget.addTab(tab, f"{config['display']} (0)")
            tab_bar.setTabTextColor(config['index'], QColor(config['color']))

    def setup_summary_tab(self):
        layout = QVBoxLayout(self.summary_tab)
        center_wrapper = QWidget()
        center_layout = QHBoxLayout(center_wrapper)
        self.summary_layout.setContentsMargins(5, 5, 5, 5)
        center_layout.addStretch(1)
        center_layout.addWidget(self.summary_table)
        center_layout.addStretch(1)
        center_wrapper.setGraphicsEffect(self.shadow)
        layout.addStretch(1)
        layout.addWidget(center_wrapper)
        layout.addStretch(1)
        layout.addLayout(self.info_layout)
        self.summary_tab.setStyleSheet("background-color: #2c3042;")
        center_wrapper.setContentsMargins(10, 10, 10, 10)

    @staticmethod
    def create_summary_row(label_text, value_text, text_color, bg_color):
        row_layout = QHBoxLayout()
        row_layout.setContentsMargins(5, 5, 5, 5)
        base_style = (
            f"font-family: 'FiraCode Nerd Font Mono', 'Fira Code', monospace; padding: 2px 2px; border-radius: 5px; "
            f"font-size: 18px; background-color: {bg_color}; color: {text_color}; border: 2px solid rgba(0, 0, 0, 50%);")
        label = QLabel(label_text)
        label.setStyleSheet(base_style + " qproperty-alignment: AlignLeft;")
        label.setFixedWidth(500)
        value = QLabel(value_text)
        value.setStyleSheet(base_style + " qproperty-alignment: AlignCenter;")
        value.setFixedWidth(500)
        row_layout.addWidget(label)
        row_layout.addWidget(value)
        return row_layout

    def update_summary_widget(self, copied: int, skipped: int, error: int):
        self.clear_layout(self.summary_layout)
        total = copied + skipped + error
        size_formatted = self.format_file_size(self.total_bytes_copied)
        copied_size_text = f"({size_formatted})" if copied else "(0.00 MB)"
        rows = [
            ("Processed files/directories:", f"{total}", "#c1ffe3", "#2c2f33"),
            ("Copied:", f"{copied} {copied_size_text}", "#55ff55", "#1f3a1f"),
            ("Skipped (Up to date, protected file...):", f"{skipped}", "#ffff7f", "#3a3a1f"),
            ("Errors:", f"{error}", "#ff8587", "#3a1f1f")
        ]
        for label, value, text_color, bg_color in rows:
            self.summary_layout.addLayout(self.create_summary_row(label, value, text_color, bg_color))

    @staticmethod
    def format_file_size(size_bytes):
        units = ["bytes", "KB", "MB", "GB", "TB"]
        size = float(size_bytes)
        unit_index = 0
        while size >= 1024 and unit_index < len(units) - 1:
            size /= 1024
            unit_index += 1
        return f"{size:.2f} {units[unit_index]}"

    @staticmethod
    def clear_layout(layout):
        while layout.count():
            item = layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
            elif item.layout():
                FileProcessDialog.clear_layout(item.layout())

    def update_progress(self, progress, status_text):
        if not self.cancelled:
            self.progress_bar.setValue(progress)
            self.current_file_label.setText(status_text)

    def update_elapsed_time(self):
        if self.timer.isValid():
            total_elapsed = (self.paused_elapsed + self.timer.elapsed()) // 1000
            h, rem = divmod(total_elapsed, 3600)
            m, s = divmod(rem, 60)
            time_text = f"\nElapsed time:\n{h:02}h {m:02}m {s:02}s\n" if h \
                else f"\nElapsed time:\n{m:02}m {s:02}s\n" if m else f"\nElapsed time:\n{s:02}s\n"
            self.elapsed_time_label.setText(time_text)
        self.update_summary()

    def update_summary(self):
        now = QDateTime.currentMSecsSinceEpoch()
        if hasattr(self,
                   'thread') and self.thread and self.thread.isRunning() and now - self._last_summary_update_time < 250:
            return
        self._last_summary_update_time = now
        self.update_summary_widget(self.copied_count, self.skipped_count, self.error_count)

    def log_file_copied(self, source, destination, file_size=0):
        self.copied_count += 1
        self.total_bytes_copied += file_size
        entry = f"{self.copied_count}:\n'{source}'\nCopied to ⤵ \n'{destination}'\n"
        self.copied_tab.add_entry(entry, "copied")
        self.tab_widget.setTabText(1, f"Copied ({self.copied_count})")

    def log_file_skipped(self, source, reason=""):
        self.skipped_count += 1
        entry = f"{self.skipped_count}:\n'{source}'\nSkipped {reason}\n"
        self.skipped_tab.add_entry(entry, "skipped")
        self.tab_widget.setTabText(2, f"Skipped ({self.skipped_count})")

    def log_file_error(self, source, error=""):
        key = f"{source}::{error}"
        if key in self._error_keys:
            return
        self._error_keys.add(key)
        self.error_count += 1
        entry = f"{self.error_count}:\n'{source}'\nError: {error}\n"
        self.error_tab.add_entry(entry, "error")
        self.tab_widget.setTabText(3, f"Errors ({self.error_count})")

    def _on_smb_error_cancel(self):
        self._smb_error_occurred = True
        self.cancelled = True
        if self.thread and self.thread.isRunning():
            self.thread.cancel()

    def operation_completed(self):
        self.update_timer.stop()
        if self.thread:
            self.thread.cleanup_resources()
        if self.cancelled:
            self.status_label.setText(f"{self.operation_type} canceled!\n")
            self.status_label.setStyleSheet(
                "color: #ff8587; font-weight: bold; font-size: 20px; background-color: transparent;")
            text = "✖ \nProcess aborted due to samba file error." if self._smb_error_occurred else "✖ \nProcess aborted by user."
            self.current_file_label.setText(text)
            err_style = "color: #ff8587; font-weight: bold; font-size: 17px;"
            self.current_file_label.setStyleSheet(err_style)
            self.elapsed_time_label.setStyleSheet(err_style)
            self.progress_bar.setStyleSheet(
                f"""{_get_global_style()} QProgressBar::chunk {{background-color: qlineargradient(spread:pad, x1:0, y1:0, x2:1, 
                y2:0, stop:0 #fd7e14, stop:1 #ff8587); border-radius: 2px;}}""")
        else:
            self.status_label.setText(f"{self.operation_type} successfully completed!\n")
            self.current_file_label.setText("⇪ \nCheck details above.")
            self.progress_bar.setValue(100)
            self.animate_text_effect()
        self.cancel_button.setText("Close")
        try:
            self.cancel_button.clicked.disconnect()
        except RuntimeError:
            pass
        self.cancel_button.clicked.connect(self.accept)
        self.cancel_button.setFocus()
        self.update_summary()
        for tab in (self.copied_tab, self.skipped_tab, self.error_tab):
            tab.flush_entries()
            tab.sort_entries()

    def animate_text_effect(self) -> None:
        try:
            self.color_timer.timeout.disconnect(self.update_label_color)
        except (RuntimeError, TypeError):
            pass
        self.color_timer.timeout.connect(self.update_label_color)
        self.color_timer.start(50)

    def update_label_color(self):
        self.color_step = (self.color_step + 0.0175) % 1
        start_color, end_color = (102, 255, 245), (85, 255, 85)
        r = int(start_color[0] + (end_color[0] - start_color[0]) * self.color_step)
        g = int(start_color[1] + (end_color[1] - start_color[1]) * self.color_step)
        b = int(start_color[2] + (end_color[2] - start_color[2]) * self.color_step)
        color_hex = f"#{r:02x}{g:02x}{b:02x}"
        style = f"color: {color_hex}; font-weight: bold;"
        self.status_label.setStyleSheet(f"{style} font-size: 20px; background-color: transparent;")
        self.current_file_label.setStyleSheet(f"{style} font-size: 17px;")
        self.elapsed_time_label.setStyleSheet(f"{style} font-size: 17px;")

    def cancel_operation(self):
        confirm_box = QMessageBox(QMessageBox.Icon.Question,
                                  "Confirm Cancellation", f"Are you sure you want to cancel the "
                                                          f"{self.operation_type} process?",
                                  QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, self)
        confirm_box.setDefaultButton(QMessageBox.StandardButton.No)
        if confirm_box.exec() == QMessageBox.StandardButton.Yes:
            if self.thread and self.thread.isRunning():
                self.cancelled = True
                self.thread.cancel()
                self.status_label.setText(f"Cancelling {self.operation_type}...\n")
                self.status_label.setStyleSheet(
                    "color: #ff8587; font-weight: bold; font-size: 20px; background-color: transparent;")
                self.current_file_label.setText("Please wait while operations are being cancelled...\n")
                self.progress_bar.setStyleSheet(f"""{_get_global_style()} QProgressBar::chunk 
                {{background-color: qlineargradient(spread:pad, x1:0, y1:0, x2:1, y2:0, stop:0 #fd7e14, stop:1 
                #ff8587); border-radius: 2px;}}""")
                QCoreApplication.processEvents()
                if not self.thread.wait(1000):
                    self.thread.terminate()
                    self.thread.wait(500)

    def closeEvent(self, event):
        if self.thread and self.thread.isRunning():
            confirm_box = QMessageBox(QMessageBox.Icon.Question, "Confirm Close",
                                      f"The {self.operation_type} process is still running. "
                                      f"Are you sure you want to close?",
                                      QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, self)
            confirm_box.setDefaultButton(QMessageBox.StandardButton.No)
            if confirm_box.exec() == QMessageBox.StandardButton.Yes:
                self.cancelled = True
                self.thread.cancel()
                if not self.thread.wait(100):
                    self.thread.terminate()
                    if not self.thread.wait(100):
                        logger.warning("WARNING: Thread could not be terminated")
                event.accept()
            else:
                event.ignore()
        else:
            event.accept()


# noinspection PyUnresolvedReferences
class FileCopyThread(QThread):
    progress_updated = pyqtSignal(int, str)
    workers_ready = pyqtSignal()
    file_copied = pyqtSignal(str, str, int)
    file_skipped = pyqtSignal(str, str)
    file_error = pyqtSignal(str, str)
    smb_error_cancel = pyqtSignal()
    operation_completed = pyqtSignal()
    sudo_password_requested = pyqtSignal()

    SKIP_PATTERNS = ["Singleton", "SingletonCookie", "SingletonLock", "lockfile", "lock", "Cache-Control",
                     ".parentlock", "cookies.sqlite-wal", "cookies.sqlite-shm", "places.sqlite-wal",
                     "places.sqlite-shm", "SingletonSocket"]

    def __init__(self, checkbox_dirs, operation_type):
        super().__init__()
        self.checkbox_dirs = checkbox_dirs
        self.operation_type = operation_type
        self.cancelled = False
        self.total_files = 0
        self.processed_files = 0
        self.file_batches = []
        self.batch_index = 0
        self.worker_threads = []
        self.mutex = QMutex()
        self.buffer_size = min(64 * 1024 * 1024, max(4 * 1024 * 1024, int(psutil.virtual_memory().available * 0.1)))
        self.num_workers = min(os.cpu_count() or 4, 8)
        self.total_bytes = 0
        self.processed_bytes = 0
        self.file_sizes = {}
        self._smb_handler = None
        self._samba_password_manager = None
        self._smb_credentials = None
        self.parent_dialog = None

    def set_parent_dialog(self, dialog):
        self.parent_dialog = dialog

    def run(self):
        try:
            if self.cancelled:
                return
            self.collect_files()
            if self.total_files == 0:
                self.progress_updated.emit(100, "No files to process")
                return
            workers_count = min(self.num_workers, os.cpu_count() or 4)
            for i in range(workers_count):
                worker = FileWorkerThread(self, i)
                self.worker_threads.append(worker)
                worker.start()
            self.workers_ready.emit()
            for worker in self.worker_threads:
                worker.wait()
        except Exception as e:
            logger.error("Error in file copy thread: %s", e)
        finally:
            self.operation_completed.emit()

    def collect_files(self):
        all_files = []
        file_sizes = {}
        total_bytes = 0
        for checkbox, sources, destinations, _ in self.checkbox_dirs:
            sources = sources if isinstance(sources, list) else [sources]
            destinations = destinations if isinstance(destinations, list) else [destinations]
            for source, destination in zip(sources, destinations):
                if SmbFileHandler.is_smb_path(source):
                    if self.smb_handler.is_directory(source):
                        for smb_file in self.smb_handler.list_smb_directory_recursive(source):
                            full_smb_path = source.rstrip("/") + "/" + smb_file
                            if not self._should_skip_file(full_smb_path):
                                try:
                                    rel = Path(str(smb_file))
                                    dst_file = str(Path(str(destination)) / rel)
                                except (TypeError, ValueError):
                                    dst_file = str(Path(str(destination)) / Path(str(smb_file)).name)
                                file_size = self._get_file_size(full_smb_path)
                                all_files.append((full_smb_path, dst_file))
                                file_sizes[full_smb_path] = file_size
                                total_bytes += file_size
                            else:
                                self.file_skipped.emit(smb_file, "(Protected/locked file)")
                    else:
                        file_size = self._get_file_size(source)
                        all_files.append((source, destination))
                        file_sizes[source] = file_size
                        total_bytes += file_size
                else:
                    src = Path(source)
                    if not src.exists():
                        continue
                    if src.is_file():
                        if not self._should_skip_file(str(src)):
                            file_size = self._get_file_size(str(src))
                            all_files.append((str(src), str(destination)))
                            file_sizes[str(src)] = file_size
                            total_bytes += file_size
                        else:
                            self.file_skipped.emit(str(src), "(Protected/locked file)")
                    else:
                        for dirpath, _, filenames in os.walk(src):
                            rel_path = Path(dirpath).relative_to(src)
                            for file in filenames:
                                src_file = Path(dirpath) / file
                                src_file_str = str(src_file)
                                if not self._should_skip_file(src_file_str):
                                    dst_file = Path(destination) / rel_path / file
                                    file_size = self._get_file_size(src_file_str)
                                    all_files.append((src_file_str, str(dst_file)))
                                    file_sizes[src_file_str] = file_size
                                    total_bytes += file_size
                                else:
                                    self.file_skipped.emit(src_file_str, "(Protected/locked file)")
        self.total_files = len(all_files)
        self.file_sizes = file_sizes
        self.total_bytes = total_bytes
        workers = max(1, min(self.num_workers, os.cpu_count() or 4))
        batch_size = max(50, min(200, (self.total_files // (workers * 2)) or 100))
        self.file_batches = [all_files[i:i + batch_size] for i in range(0, len(all_files), batch_size)]
        self.batch_index = 0

    def get_next_batch(self):
        with QMutexLocker(self.mutex):
            if hasattr(self, 'file_batches') and self.file_batches and self.batch_index < len(self.file_batches):
                batch = self.file_batches[self.batch_index]
                self.batch_index += 1
                return batch
            return None

    def _should_skip_file(self, file_path):
        return Path(file_path).name in self.SKIP_PATTERNS

    def _skip_file(self, source_file, reason):
        self.mutex.lock()
        try:
            self.processed_files += 1
            file_size = self.file_sizes.get(source_file, 0)
            self.processed_bytes += file_size
            progress = int((self.processed_bytes / self.total_bytes) * 100) if self.total_bytes > 0 else 0
            self.file_skipped.emit(source_file, reason)
            self.progress_updated.emit(progress, f"Skipping {reason}:\n{Path(source_file).name}")
        finally:
            self.mutex.unlock()

    def copy_file(self, source_file, dest_file):
        if self.cancelled:
            return
        file_name = Path(source_file).name
        dest_path = Path(dest_file)
        if SmbFileHandler.is_smb_path(source_file) or SmbFileHandler.is_smb_path(dest_file):
            if self.cancelled:
                return
            self.smb_handler.copy_file(source_file, dest_file, lambda success, smb_file_name, size_or_error:
            (self._handle_smb_result(success, source_file, dest_file, smb_file_name,
                                     size_or_error) if not self.cancelled else None))
            return
        try:
            if not Path(source_file).exists():
                self.handle_file_error(source_file, "Source file not found")
                return
            src_stat = Path(source_file).stat()
            file_size = src_stat.st_size
            if dest_path.exists():
                dest_stat = dest_path.stat()
                if src_stat.st_size == dest_stat.st_size and src_stat.st_mtime <= dest_stat.st_mtime:
                    self._update_file_progress(False, source_file, dest_file, file_name, file_size)
                    return
            self.fast_copy(source_file, dest_file)
            self._update_file_progress(True, source_file, dest_file, file_name, file_size)
        except (OSError, FileNotFoundError) as e:
            error_msg = str(e).lower()
            if any(pattern.lower() in error_msg for pattern in self.SKIP_PATTERNS):
                self._skip_file(source_file, "(Protected/locked file)")
            else:
                self.handle_file_error(source_file, f"Source error: {e}")
        except Exception as e:
            self.handle_file_error(source_file, str(e))

    def fast_copy(self, source, destination, file_size=None):
        if self.cancelled:
            return
        try:
            if file_size is None:
                file_size = os.path.getsize(source)
            if file_size >= 64 * 1024 * 1024:
                buffer_size = 1024 * 1024
            elif file_size >= 1 * 1024 * 1024:
                buffer_size = 64 * 1024
            else:
                buffer_size = min(8 * 1024, self.buffer_size)
            dest_dir = os.path.dirname(destination)
            if dest_dir and not os.path.exists(dest_dir):
                os.makedirs(dest_dir, exist_ok=True)
            try:
                if hasattr(os, 'sendfile') and os.name == 'posix' and file_size > (1 * 1024 * 1024):
                    with open(source, 'rb') as fsrc, open(destination, 'wb') as fdst:
                        src_fd = fsrc.fileno()
                        dst_fd = fdst.fileno()
                        actual_size = os.fstat(src_fd).st_size
                        sent_total = 0
                        chunk = 1024 * 1024
                        while sent_total < actual_size and not self.cancelled:
                            to_send = min(chunk, actual_size - sent_total)
                            sent = os.sendfile(dst_fd, src_fd, None, to_send)
                            if sent == 0:
                                break
                            sent_total += sent
                    if self.cancelled:
                        try:
                            os.unlink(destination)
                        except OSError:
                            pass
                        return
                    if sent_total != actual_size:
                        raise OSError(f"sendfile incomplete: {sent_total}/{actual_size} bytes transferred")
                    shutil.copystat(source, destination)
                    return
            except OSError as e:
                logger.debug("sendfile fallback engaged (OSError) for: %s %s", source, e)
            except Exception as e:
                logger.debug("sendfile fallback engaged for: %s %s", source, e)
            try:
                buf = bytearray(buffer_size)
                with open(source, 'rb') as fsrc, open(destination, 'wb') as fdst:
                    mv = memoryview(buf)
                    while not self.cancelled:
                        n = fsrc.readinto(mv)
                        if not n:
                            break
                        fdst.write(mv[:n])
                if self.cancelled:
                    try:
                        os.unlink(destination)
                    except OSError:
                        pass
                    return
                shutil.copystat(source, destination)
            except MemoryError:
                shutil.copy2(source, destination)
        except (OSError, IOError) as e:
            if Path(source).name in self.SKIP_PATTERNS:
                raise OSError("Skipping access-protected file")
            else:
                raise OSError(f"Failed to copy {source} to {destination}:\n{str(e)}")

    def _get_file_size(self, file_path):
        try:
            if SmbFileHandler.is_smb_path(file_path):
                return self.smb_handler.get_smb_file_size(file_path)
            else:
                return Path(file_path).stat().st_size
        except Exception as e:
            logger.error("Error getting file size for %s: %s", file_path, e)
            if self._smb_handler is not None and SmbFileHandler.is_smb_path(file_path):
                self.file_error.emit(file_path, f"File size cannot be determined. {e}")
                self.smb_error_cancel.emit()
            return 0

    def _update_file_progress(self, should_copy, source_file, dest_file, file_name, file_size):
        self.mutex.lock()
        try:
            self.processed_files += 1
            self.processed_bytes += self.file_sizes.get(source_file, file_size)
            progress = int((self.processed_bytes / self.total_bytes) * 100) if self.total_bytes > 0 else 0
            if should_copy:
                self.file_copied.emit(source_file, dest_file, file_size)
                self.progress_updated.emit(progress, f"Copying:\n{file_name}")
            else:
                self.file_skipped.emit(source_file, "(Up to date)")
                self.progress_updated.emit(progress, f"Skipping (Up to date):\n{file_name}")
        except Exception as e:
            self.mutex.unlock()
            self.handle_file_error(source_file, str(e))
            return
        self.mutex.unlock()

    def handle_file_error(self, source_file, error_msg):
        self.mutex.lock()
        try:
            self.processed_files += 1
            if source_file in self.file_sizes:
                self.processed_bytes += self.file_sizes[source_file]
            progress = int((self.processed_bytes / self.total_bytes) * 100) if self.total_bytes > 0 else 0
            self.file_error.emit(source_file, error_msg)
            self.progress_updated.emit(progress, f"Error copying:\n{Path(source_file).name}")
        finally:
            self.mutex.unlock()

    def _handle_smb_result(self, success, source_file, dest_file, file_name, size_or_error):
        self.mutex.lock()
        try:
            self.processed_files += 1
            try:
                file_size = int(size_or_error) if isinstance(size_or_error, (int, str)) else 0
            except (ValueError, TypeError):
                file_size = 0
            self.processed_bytes += file_size
            progress = int((self.processed_bytes / self.total_bytes) * 100) if self.total_bytes > 0 else 0
            if success:
                self.file_copied.emit(source_file, dest_file, file_size)
                self.progress_updated.emit(progress, f"Copying:\n{file_name}")
            else:
                self.file_error.emit(source_file, str(size_or_error))
                self.progress_updated.emit(progress, f"Error copying:\n{file_name}")
                self.smb_error_cancel.emit()
        finally:
            self.mutex.unlock()

    @property
    def smb_handler(self):
        if self._smb_handler is None:
            self._smb_handler = SmbFileHandler(self.samba_password_manager, self)
        return self._smb_handler

    @property
    def samba_password_manager(self):
        if self._samba_password_manager is None:
            self._samba_password_manager = SambaPasswordManager()
        return self._samba_password_manager

    def get_smb_credentials(self):
        with QMutexLocker(self.mutex):
            return self._smb_credentials

    def set_smb_credentials(self, credentials):
        with QMutexLocker(self.mutex):
            self._smb_credentials = credentials

    def cleanup_resources(self):
        for worker in list(self.worker_threads):
            if worker and worker.isRunning():
                worker.requestInterruption()
                if not worker.wait(100):
                    worker.terminate()
                    worker.wait(100)
        self.worker_threads.clear()
        if self._smb_handler:
            try:
                self._smb_handler.force_cleanup()
            except Exception as e:
                logger.warning("Warning during SMB cleanup: %s", e)
            finally:
                self._smb_handler = None
                self._smb_credentials = None

    def cancel(self):
        self.cancelled = True
        for worker in self.worker_threads:
            worker.requestInterruption()
        if self._smb_handler:
            self._smb_handler.force_cleanup()
        for worker in self.worker_threads:
            if not worker.wait(200):
                worker.terminate()
        self.cleanup_resources()


class FileWorkerThread(QThread):
    def __init__(self, main_thread, worker_id):
        super().__init__()
        self.main_thread = main_thread
        self.worker_id = worker_id
        self.setObjectName(f"FileWorker-{worker_id}")

    def run(self):
        while not self.main_thread.cancelled and not self.isInterruptionRequested():
            batch = self.main_thread.get_next_batch()
            if batch is None:
                break
            for source_file, dest_file in batch:
                if self.main_thread.cancelled or self.isInterruptionRequested():
                    return
                try:
                    self.main_thread.copy_file(source_file, dest_file)
                except Exception as e:
                    if not self.main_thread.cancelled:
                        self.main_thread.handle_file_error(source_file, str(e))


class SmbFileHandler:
    def __init__(self, samba_password_manager, thread=None):
        self.samba_password_manager = samba_password_manager
        self.thread = thread
        self._smb_credentials = None
        self._sudo_password = None
        self.mutex = QMutex()
        self._mount_wait_conditions = {}
        self._mounted_shares = {}
        self._mounting_shares = set()

    def initialize(self):
        if self._smb_credentials:
            return
        with QMutexLocker(self.mutex):
            if self._smb_credentials:
                return
            creds = getattr(self.thread, 'get_smb_credentials', lambda: None)() if self.thread else None
            valid_creds = (
                creds if isinstance(creds, (list, tuple)) and len(creds) >= 2 and all(creds[:2])
                else None
            )
            self._smb_credentials = valid_creds or self.samba_password_manager.get_samba_credentials()
            if self.thread and hasattr(self.thread, 'set_smb_credentials') and not valid_creds:
                self.thread.set_smb_credentials(self._smb_credentials)

    @staticmethod
    def is_smb_path(path):
        return str(path).startswith(("smb:", "//"))

    @staticmethod
    def parse_smb_url(path):
        path = str(path)
        if path.startswith("smb:/") and not path.startswith("smb://"):
            path = f"smb://{path[5:]}"
        if path.startswith("//"):
            path = f"smb://{path[2:]}"
        m = re.match(r"smb://([^/]+)/([^/]+)(/?.*)", path)
        if not m:
            raise ValueError(f"Invalid SMB-URL: {path}")
        return m.group(1), m.group(2), m.group(3).lstrip('/')

    def _get_sudo_password(self):
        if self._sudo_password:
            return self._sudo_password
        if self.thread and getattr(self.thread, 'parent_dialog', None):
            self._sudo_password = self.thread.parent_dialog.get_sudo_password()
            return self._sudo_password
        raise RuntimeError("Cannot request sudo password - no parent dialog available")

    def _mount_smb_share(self, server, share):
        if self.thread and getattr(self.thread, 'cancelled', False):
            raise RuntimeError("Operation cancelled!")
        if not server or not share:
            raise ValueError("Server and share must be specified!")
        key = (server, share)
        with QMutexLocker(self.mutex):
            if key in self._mounted_shares and os.path.ismount(self._mounted_shares[key]):
                return self._mounted_shares[key]
            self._mounted_shares.pop(key, None)
            if key in self._mounting_shares:
                if key not in self._mount_wait_conditions:
                    self._mount_wait_conditions[key] = QWaitCondition()
                wait_condition = self._mount_wait_conditions[key]
                wait_count = 0
                while key in self._mounting_shares and wait_count < 10:
                    if self.thread and getattr(self.thread, 'cancelled', False):
                        raise RuntimeError("Operation cancelled during mount wait!")
                    wait_condition.wait(self.mutex, 500)
                    wait_count += 1
                if key in self._mounted_shares and os.path.ismount(self._mounted_shares[key]):
                    return self._mounted_shares[key]
                if key in self._mounting_shares:
                    raise RuntimeError("Mount operation timed out - another thread is still mounting")
            self._mounting_shares.add(key)
            if key not in self._mount_wait_conditions:
                self._mount_wait_conditions[key] = QWaitCondition()

        mount_point = tempfile.mkdtemp(prefix=f"smb_{server}_{share}_")
        cred_file_path: str | None = None
        try:
            if self.thread and getattr(self.thread, 'cancelled', False):
                raise RuntimeError("Operation cancelled before mount")
            self.initialize()
            if not self._smb_credentials or len(self._smb_credentials) < 2:
                raise RuntimeError("SMB credentials are unavailable or incomplete.")
            username, password = self._smb_credentials[:2]
            domain = self._smb_credentials[2] if len(self._smb_credentials) > 2 else None

            with tempfile.NamedTemporaryFile(
                mode='w', prefix='smb_cred_', suffix='.tmp',
                delete=False, encoding='utf-8'
            ) as cred_file:
                cred_file_path = cred_file.name
                cred_file.write(f"username={username}\npassword={password}\n")
                if domain:
                    cred_file.write(f"domain={domain}\n")
            os.chmod(cred_file_path, 0o600)

            cmd = ['sudo', 'mount.cifs', f'//{server}/{share}', mount_point]
            opts = [f'credentials={cred_file_path}',
                    f'uid={os.getuid()}', f'gid={os.getgid()}', 'iocharset=utf8']
            cmd.extend(['-o', ','.join(opts)])
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            if proc.returncode == 0:
                with QMutexLocker(self.mutex):
                    self._mounted_shares[key] = mount_point
            else:
                if self.thread and getattr(self.thread, 'cancelled', False):
                    raise RuntimeError("Operation cancelled")
                sudo_password = self._get_sudo_password()
                cmd = ['sudo', '-S', 'mount.cifs', f'//{server}/{share}', mount_point, '-o', ','.join(opts)]
                proc_2 = subprocess.run(cmd, input=f"{sudo_password}\n", capture_output=True, text=True, timeout=10)
                if proc_2.returncode != 0:
                    if os.path.exists(mount_point):
                        try:
                            os.rmdir(mount_point)
                        except Exception as e:
                            logger.warning("%s", e)
                    raise RuntimeError(f"Mount failed: {proc_2.stderr}")
                with QMutexLocker(self.mutex):
                    self._mounted_shares[key] = mount_point
            return mount_point
        except subprocess.TimeoutExpired:
            if os.path.exists(mount_point):
                try:
                    os.rmdir(mount_point)
                except Exception as rmdir_error:
                    logger.warning("Could not remove temp mount point after timeout: %s", rmdir_error)
            raise RuntimeError("Mount operation timed out")
        except Exception as e:
            logger.warning("%s", e)
            if os.path.exists(mount_point):
                try:
                    os.rmdir(mount_point)
                except Exception as rmdir_error:
                    logger.warning("Could not remove temp mount point: %s", rmdir_error)
            raise
        finally:
            if cred_file_path and os.path.exists(cred_file_path):
                try:
                    os.unlink(cred_file_path)
                except OSError as exc:
                    logger.warning("Could not remove SMB credentials file: %s", exc)
            with QMutexLocker(self.mutex):
                self._mounting_shares.discard(key)
                if key in self._mount_wait_conditions:
                    self._mount_wait_conditions[key].wakeAll()

    @staticmethod
    def _unmount_smb_share(mount_point, sudo_password=None):
        if not mount_point or not os.path.exists(mount_point):
            return
        commands_to_try = [
            (['sudo', 'umount', mount_point], None),
            (['sudo', '-S', 'umount', mount_point], sudo_password),
            (['sudo', 'umount', '-l', mount_point], None)
        ]

        for cmd, input_data in commands_to_try:
            try:
                input_str = f"{input_data}\n" if input_data else None
                result = subprocess.run(cmd, input=input_str, capture_output=True, text=True, timeout=5)
                if result.returncode == 0:
                    break
            except subprocess.TimeoutExpired as e:
                logger.warning("Warning during unmount attempt (timeout): %s", e)
            except Exception as e:
                logger.warning("Warning during unmount attempt: %s", e)

        try:
            if os.path.exists(mount_point) and not os.listdir(mount_point):
                os.rmdir(mount_point)
        except Exception as e:
            logger.warning("Could not remove mount point %s: %s", mount_point, e)

    def _smb_path_to_local(self, smb_path):
        server, share, path = self.parse_smb_url(smb_path)
        mount_point = self._mount_smb_share(server, share)
        return os.path.join(mount_point, path) if path else mount_point

    def copy_file(self, source, destination, progress_callback=None):
        if self.thread and getattr(self.thread, "cancelled", False):
            return None
        src_is_smb = self.is_smb_path(source)
        dst_is_smb = self.is_smb_path(destination)

        try:
            if src_is_smb and not dst_is_smb:
                return self._copy_smb_to_local(source, destination, progress_callback)
            if not src_is_smb and dst_is_smb:
                return self._copy_local_to_smb(source, destination, progress_callback)
            return self._copy_local(source, destination, progress_callback)
        except Exception as e:
            fn = Path(source).name
            if progress_callback:
                progress_callback(False, fn, str(e))
            raise

    def _copy_smb_to_local(self, source, destination, progress_callback):
        if self.thread and getattr(self.thread, "cancelled", False):
            return None
        local_source = self._smb_path_to_local(source)
        fn = Path(source).name or "directory"
        try:
            if os.path.isdir(local_source):
                dest_path = Path(destination) / Path(local_source).name
                shutil.copytree(local_source, dest_path, dirs_exist_ok=True)
                total = sum(f.stat().st_size for f in dest_path.rglob('*') if f.is_file())
            else:
                Path(destination).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(local_source, destination)
                total = os.path.getsize(destination)
            if progress_callback:
                progress_callback(True, fn, total)
            return total
        except Exception as e:
            if progress_callback:
                progress_callback(False, fn, str(e))
            raise

    def _copy_local_to_smb(self, source, destination, progress_callback):
        if self.thread and getattr(self.thread, "cancelled", False):
            return None
        local_destination = self._smb_path_to_local(destination)
        fn = Path(source).name
        try:
            if os.path.isdir(source):
                dest_path = os.path.join(local_destination, os.path.basename(source))
                shutil.copytree(source, dest_path, dirs_exist_ok=True)
                total = sum(f.stat().st_size for f in Path(source).rglob('*') if f.is_file())
            else:
                dest_dir = os.path.dirname(local_destination)
                if dest_dir:
                    os.makedirs(dest_dir, exist_ok=True)
                shutil.copy2(source, local_destination)
                total = os.path.getsize(source)
            if progress_callback:
                progress_callback(True, fn, total)
            return total
        except Exception as e:
            if progress_callback:
                progress_callback(False, fn, str(e))
            raise

    def _copy_local(self, source, destination, progress_callback):
        if self.thread and getattr(self.thread, "cancelled", False):
            return None
        fn = Path(source).name
        try:
            dest_dir = os.path.dirname(destination)
            if dest_dir:
                os.makedirs(dest_dir, exist_ok=True)
            if Path(source).is_dir():
                shutil.copytree(source, destination, dirs_exist_ok=True)
                total = sum(f.stat().st_size for f in Path(destination).rglob('*') if f.is_file())
            else:
                shutil.copy2(source, destination)
                total = os.path.getsize(destination)
            if progress_callback:
                progress_callback(True, fn, total)
            return total
        except Exception as e:
            if progress_callback:
                progress_callback(False, fn, str(e))
            raise

    def is_directory(self, path):
        if self.is_smb_path(path):
            try:
                return os.path.isdir(self._smb_path_to_local(path))
            except Exception as e:
                logger.error("Error in is_directory: %s", e)
                return False
        return Path(path).is_dir()

    def get_smb_file_size(self, path):
        if not self.is_smb_path(path):
            return 0
        try:
            local_path = self._smb_path_to_local(path)
            if os.path.isfile(local_path):
                return os.path.getsize(local_path)
            if os.path.isdir(local_path):
                return sum(f.stat().st_size for f in Path(local_path).rglob('*') if f.is_file())
            return 0
        except Exception as e:
            logger.error("Error in get_smb_file_size: %s", e)
            return 0

    def list_smb_directory(self, path):
        if not self.is_smb_path(path):
            return []
        try:
            local_path = self._smb_path_to_local(path)
            return [f for f in os.listdir(local_path) if not f.startswith('.')] if os.path.isdir(local_path) else []
        except Exception as e:
            logger.error("Error in list_smb_directory: %s", e)
            return []

    def list_smb_directory_recursive(self, path):
        if not self.is_smb_path(path):
            return []
        try:
            local_path = self._smb_path_to_local(path)
            if not os.path.isdir(local_path):
                return []
            result = []
            for dirpath, dirnames, filenames in os.walk(local_path):
                dirnames[:] = [d for d in dirnames if not d.startswith('.')]
                for fname in filenames:
                    if fname.startswith('.'):
                        continue
                    abs_path = os.path.join(dirpath, fname)
                    rel = os.path.relpath(str(abs_path), str(local_path))
                    result.append(rel)
            return result
        except Exception as e:
            logger.error("Error in list_smb_directory_recursive: %s", e)
            return []

    def cleanup(self):
        for (_server, _share), mount_point in list(self._mounted_shares.items()):
            try:
                time.sleep(0.5)
                self._unmount_smb_share(mount_point, self._sudo_password)
            except Exception as e:
                logger.warning("Cleanup warning: %s", e)
        self._mounted_shares.clear()
        self._mounting_shares.clear()
        self._mount_wait_conditions.clear()

    def force_cleanup(self):
        try:
            for (_server, _share), mount_point in list(self._mounted_shares.items()):
                try:
                    subprocess.run(['sudo', 'umount', '-l', mount_point], timeout=2, capture_output=True)
                    if os.path.exists(mount_point):
                        try:
                            os.rmdir(mount_point)
                        except Exception as e:
                            logger.warning("%s", e)
                except Exception as e:
                    logger.exception("Force cleanup error (ignored): %s", e)
            self._mounted_shares.clear()
            self._mounting_shares.clear()
            with QMutexLocker(self.mutex):
                for condition in self._mount_wait_conditions.values():
                    condition.wakeAll()
                self._mount_wait_conditions.clear()
        except Exception as e:
            logger.exception("Force cleanup error (ignored): %s", e)


class LogEntryListModel(QAbstractListModel):
    def __init__(self, entries, entry_types, parent=None):
        super().__init__(parent)
        self._entries = entries
        self._types = entry_types
        self.filter = ""
        self._filtered_indices = list(range(len(entries)))

    def rowCount(self, parent=QModelIndex()):
        return len(self._filtered_indices)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        row = self._filtered_indices[index.row()]
        if role == Qt.ItemDataRole.DisplayRole:
            return self._entries[row]
        if role == Qt.ItemDataRole.ForegroundRole:
            t = self._types[row]
            colors = {"error": Qt.GlobalColor.red, "skipped": Qt.GlobalColor.yellow, "copied": Qt.GlobalColor.green}
            return colors.get(t)
        return None

    def set_filter(self, text):
        self.filter = text.lower()
        self._filtered_indices = [i for i, e in enumerate(self._entries) if
                                  self.filter in e.lower()] if self.filter else list(range(len(self._entries)))
        self.layoutChanged.emit()

    def add_entry(self, entry, entry_type):
        self.beginInsertRows(QModelIndex(), len(self._entries), len(self._entries))
        self._entries.append(entry)
        self._types.append(entry_type)
        self.endInsertRows()
        self.set_filter(self.filter)

    def sort_entries(self):
        if not self._entries:
            return
        try:
            self.beginResetModel()
            replaced = []
            for entry, entry_type in zip(self._entries, self._types):
                e = entry
                for old, new in getattr(Options, 'text_replacements', []):
                    e = e.replace(old, new)
                replaced.append((e, entry_type))

            def extract_path(sorted_entry):
                try:
                    m = re.match(r"^\d+:(?:<br>)?'([^']+)'", sorted_entry)
                    if m:
                        return m.group(1).lower()
                    lines = sorted_entry.split('<br>' if '<br>' in sorted_entry else '\n')
                    for line in lines:
                        if '/' in line and not line.strip().isdigit():
                            return line.strip().lower()
                    return sorted_entry.lower()
                except Exception as error:
                    logger.error("Error in sort_entries: %s", error)
                    return sorted_entry.lower()

            replaced_sorted = sorted(replaced, key=lambda x: extract_path(x[0]))
            new_entries, new_types = [], []
            for i, (entry, entry_type) in enumerate(replaced_sorted):
                try:
                    if '<br>' in entry:
                        entry = re.sub(r"^\d+:<br>", f"{i + 1}:<br>", entry, count=1)
                    else:
                        entry = re.sub(r"^\d+:", f"{i + 1}:", entry, count=1)
                except Exception as e:
                    logger.error("Error in sort_entries: %s", e)
                    entry = f"{i + 1}: {entry}"
                new_entries.append(entry)
                new_types.append(entry_type)
            self._entries[:] = new_entries
            self._types[:] = new_types
            self.endResetModel()
            self.set_filter(self.filter)
        except Exception as e:
            self.endResetModel()
            logger.exception("Error in sort_entries: %s", e)


# noinspection PyUnresolvedReferences
class VirtualLogTabWidget(QWidget):
    FLUSH_TIMER_INTERVAL = 300

    def __init__(self, parent=None):
        super().__init__(parent)
        self.entries = []
        self.entry_types = []
        self.pending_entries = []
        self._mutex = QMutex()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(5, 5, 5, 5)
        search_label = QLabel("Search:")
        search_label.setStyleSheet("color: #e0e0e0;")
        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText("Filter entries...")
        layout.addWidget(search_label)
        layout.addWidget(self.search_box)

        self.model = LogEntryListModel(self.entries, self.entry_types)
        self.list_view = QListView()
        self.list_view.setModel(self.model)
        self.list_view.setEditTriggers(QListView.EditTrigger.NoEditTriggers)
        self.list_view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list_view.setWordWrap(True)
        layout.addWidget(self.list_view)

        self.search_box.textChanged.connect(self.model.set_filter)
        self._flush_timer = QTimer(self)
        self._flush_timer.setInterval(self.FLUSH_TIMER_INTERVAL)
        self._flush_timer.setSingleShot(True)
        self._flush_timer.timeout.connect(self.flush_entries)

    def add_entry(self, entry, entry_type):
        with QMutexLocker(self._mutex):
            self.pending_entries.append((entry, entry_type))
        if not self._flush_timer.isActive():
            self._flush_timer.start()

    def flush_entries(self):
        entries_to_add = []
        has_remaining = False
        with QMutexLocker(self._mutex):
            if not self.pending_entries:
                return
            entries_to_add = self.pending_entries[:100]
            self.pending_entries = self.pending_entries[100:]
            has_remaining = bool(self.pending_entries)
        if entries_to_add:
            start = len(self.entries)
            entries, types = zip(*entries_to_add)
            self.model.beginInsertRows(QModelIndex(), start, start + len(entries) - 1)
            self.entries.extend(entries)
            self.entry_types.extend(types)
            self.model.endInsertRows()
            if self.model.filter:
                self.model.set_filter(self.model.filter)
        if has_remaining:
            self._flush_timer.start()

    def sort_entries(self):
        self.model.sort_entries()

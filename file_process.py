from __future__ import annotations
from pathlib import Path
from options import Options
from PyQt6.QtGui import QColor
from samba_password import SambaPasswordManager
import os, re, shutil, tempfile, subprocess, psutil
from global_style import get_current_style as _style
from PyQt6.QtCore import (Qt, QThread, QTimer, QElapsedTimer, QMutex, QMutexLocker, QWaitCondition, QDateTime,
                          QAbstractListModel, QModelIndex, QCoreApplication, pyqtSignal)
from PyQt6.QtWidgets import (QDialog, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QProgressBar, QTabWidget, QLineEdit,
                             QDialogButtonBox, QInputDialog, QMessageBox, QListView, QGraphicsDropShadowEffect)

from logging_config import setup_logger
logger = setup_logger(__name__)


# noinspection PyUnresolvedReferences
class FileProcessDialog(QDialog):
    UPDATE_TIMER_INTERVAL  = 350
    SMB_MOUNT_TIMEOUT      = 10
    SMB_UNMOUNT_TIMEOUT    = 2
    SMB_OPERATION_DELAY    = 0.5

    TAB_CONFIG = {
        'summary': {'index': 0, 'color': '#6ffff5',  'display': 'Summary'},
        'copied':  {'index': 1, 'color': 'lightgreen','display': 'Copied'},
        'skipped': {'index': 2, 'color': '#ffff7f',   'display': 'Skipped'},
        'error':   {'index': 3, 'color': '#ff8587',   'display': 'Errors'},
    }

    def __init__(self, parent, checkbox_dirs, operation_type):
        super().__init__(parent)
        self.operation_type = operation_type
        self.checkbox_dirs  = checkbox_dirs
        self.setWindowTitle(operation_type)

        self.sudo_password        = None
        self.sudo_password_event  = QWaitCondition()
        self.sudo_password_mutex  = QMutex()
        self.sudo_dialog_open     = False

        self.total_bytes_copied   = 0
        self.copied_count = self.skipped_count = self.error_count = 0
        self.colour_step  = 0
        self.cancelled    = False
        self._smb_error   = False
        self._error_keys: set[str] = set()
        self._last_summary_ts = 0
        self.paused_elapsed   = 0

        self.status_label = QLabel(f"{operation_type} in progress…\n")
        self.status_label.setStyleSheet("color:#6ffff5;font-weight:bold;font-size:20px;background:transparent;")
        self.current_file_label = QLabel(f"Preparing:\n{operation_type}")
        self.current_file_label.setStyleSheet("font-weight:bold;font-size:17px;")
        self.elapsed_time_label = QLabel("\nElapsed time:\n00s\n")
        self.elapsed_time_label.setStyleSheet("font-weight:bold;font-size:17px;")
        self.progress_bar   = QProgressBar()
        self.progress_bar.setValue(0)
        self.progress_bar.setStyleSheet(_style())
        self.tab_widget     = QTabWidget()
        self.copied_tab     = VirtualLogTabWidget()
        self.skipped_tab    = VirtualLogTabWidget()
        self.error_tab      = VirtualLogTabWidget()
        self.summary_tab    = QWidget()
        self.summary_table  = QWidget()
        self.summary_layout = QVBoxLayout(self.summary_table)
        self.cancel_button  = None
        self.container      = QWidget(self)
        self.container.setProperty("class", "container")
        self.info_layout    = QVBoxLayout()

        self.timer        = QElapsedTimer()
        self.update_timer = QTimer(self)
        self.colour_timer = QTimer(self)

        self.thread = FileCopyThread(self.checkbox_dirs, self.operation_type)
        self.thread.set_parent_dialog(self)

        self._setup_ui()
        self._setup_tabs()
        self._setup_connections()
        self._start_process()

    def handle_sudo_password_request(self):
        if self.sudo_dialog_open:
            return
        self.sudo_dialog_open = True
        try:
            if self.timer.isValid():
                self.paused_elapsed += self.timer.elapsed()
            self.update_timer.stop()
            password, ok = QInputDialog.getText(
                self, "Sudo Password",
                "Enter sudo password for mounting SMB shares:",
                QLineEdit.EchoMode.Password,
            )
            with QMutexLocker(self.sudo_password_mutex):
                self.sudo_password = password if ok and password else None
                self.sudo_password_event.wakeAll()
            self.timer.restart()
            self.update_timer.start(self.UPDATE_TIMER_INTERVAL)
        finally:
            self.sudo_dialog_open = False

    def get_sudo_password(self) -> str:
        with QMutexLocker(self.sudo_password_mutex):
            if self.sudo_password is not None:
                return self.sudo_password
            self.thread.sudo_password_requested.emit()
            self.sudo_password_event.wait(self.sudo_password_mutex)
            if self.sudo_password is None:
                raise RuntimeError("Sudo password required for mounting SMB shares")
            return self.sudo_password

    def _setup_connections(self):
        t = self.thread
        t.workers_ready.connect(lambda: self.cancel_button.setEnabled(True))
        t.file_copied.connect(self._on_file_copied)
        t.file_skipped.connect(self._on_file_skipped)
        t.file_error.connect(self._on_file_error)
        t.progress_updated.connect(self._on_progress_updated)
        t.operation_completed.connect(self._on_operation_completed)
        t.smb_error_cancel.connect(self._on_smb_error_cancel)
        t.sudo_password_requested.connect(self.handle_sudo_password_request)
        self.update_timer.timeout.connect(self._update_elapsed_time)

    def _setup_ui(self):
        main_layout = QVBoxLayout(self)
        self.info_layout.addWidget(self.current_file_label)
        self.info_layout.addWidget(self.elapsed_time_label)
        self.info_layout.addWidget(self.progress_bar)

        button_box        = QDialogButtonBox()
        self.cancel_button = button_box.addButton("Cancel", QDialogButtonBox.ButtonRole.RejectRole)
        self.cancel_button.setFixedSize(125, 35)
        self.cancel_button.clicked.connect(self._cancel_operation)
        self.cancel_button.setEnabled(False)
        self.cancel_button.setStyleSheet(_style())

        container_layout = QVBoxLayout(self.container)
        container_layout.addWidget(self.status_label)
        container_layout.addWidget(self.tab_widget)
        container_layout.addWidget(button_box)
        main_layout.addWidget(self.container)
        self.setFixedSize(1200, 700)

    def _setup_tabs(self):
        tab_style = "font-family:FiraCode Nerd Font Mono;font-size:14px;padding:10px;"
        tab_bar   = self.tab_widget.tabBar()
        for key, cfg in self.TAB_CONFIG.items():
            if key == "summary":
                self._setup_summary_tab()
                self.tab_widget.addTab(self.summary_tab, cfg["display"])
            else:
                tab = getattr(self, f"{key}_tab")
                tab.setStyleSheet(f"color:{cfg['color']};{tab_style}")
                self.tab_widget.addTab(tab, f"{cfg['display']} (0)")
            tab_bar.setTabTextColor(cfg["index"], QColor(cfg["color"]))

    def _setup_summary_tab(self):
        layout         = QVBoxLayout(self.summary_tab)
        center_wrapper = QWidget()
        center_layout  = QHBoxLayout(center_wrapper)
        self.summary_layout.setContentsMargins(5, 5, 5, 5)
        center_layout.addStretch(1)
        center_layout.addWidget(self.summary_table)
        center_layout.addStretch(1)
        shadow = QGraphicsDropShadowEffect(center_wrapper)
        shadow.setBlurRadius(100)
        shadow.setColor(QColor(0, 0, 0, 250))
        shadow.setOffset(0.0, 1.0)
        center_wrapper.setGraphicsEffect(shadow)
        layout.addStretch(1)
        layout.addWidget(center_wrapper)
        layout.addStretch(1)
        layout.addLayout(self.info_layout)
        self.summary_tab.setStyleSheet("background-color:#2c3042;")
        center_wrapper.setContentsMargins(10, 10, 10, 10)

    def _start_process(self):
        self.timer.start()
        self.update_timer.start(self.UPDATE_TIMER_INTERVAL)
        self.thread.start()

    @staticmethod
    def _create_summary_row(label_text, value_text, text_color, bg_color):
        row = QHBoxLayout()
        row.setContentsMargins(5, 5, 5, 5)
        base = (
            f"font-family:'FiraCode Nerd Font Mono','Fira Code',monospace;"
            f"padding:2px;border-radius:5px;font-size:18px;"
            f"background-color:{bg_color};color:{text_color};"
            f"border:2px solid rgba(0,0,0,50%);"
        )
        lbl = QLabel(label_text)
        lbl.setStyleSheet(base + "qproperty-alignment:AlignLeft;")
        lbl.setFixedWidth(500)
        val = QLabel(value_text)
        val.setStyleSheet(base + "qproperty-alignment:AlignCenter;")
        val.setFixedWidth(500)
        row.addWidget(lbl)
        row.addWidget(val)
        return row

    def _update_summary_widget(self, copied, skipped, error):
        self._clear_layout(self.summary_layout)
        total           = copied + skipped + error
        size_str        = self._fmt_size(self.total_bytes_copied)
        copied_size_txt = f"({size_str})" if copied else "(0.00 MB)"
        rows = [
            ("Processed files/directories:", f"{total}",                    "#c1ffe3", "#2c2f33"),
            ("Copied:",                      f"{copied} {copied_size_txt}", "#55ff55", "#1f3a1f"),
            ("Skipped (Up to date…):",       f"{skipped}",                  "#ffff7f", "#3a3a1f"),
            ("Errors:",                      f"{error}",                    "#ff8587", "#3a1f1f"),
        ]
        for label, value, tc, bc in rows:
            self.summary_layout.addLayout(self._create_summary_row(label, value, tc, bc))

    @staticmethod
    def _fmt_size(size_bytes: int) -> str:
        units = ["bytes", "KB", "MB", "GB", "TB"]
        size  = float(size_bytes)
        idx   = 0
        while size >= 1024 and idx < len(units) - 1:
            size /= 1024
            idx  += 1
        return f"{size:.2f} {units[idx]}"

    @staticmethod
    def _clear_layout(layout):
        while layout.count():
            item = layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
            elif item.layout():
                FileProcessDialog._clear_layout(item.layout())

    def _on_progress_updated(self, progress, status_text):
        if not self.cancelled:
            self.progress_bar.setValue(progress)
            self.current_file_label.setText(status_text)

    def _update_elapsed_time(self):
        if self.timer.isValid():
            total = (self.paused_elapsed + self.timer.elapsed()) // 1000
            h, r  = divmod(total, 3600)
            m, s  = divmod(r, 60)
            txt   = (f"\nElapsed time:\n{h:02}h {m:02}m {s:02}s\n" if h
                     else f"\nElapsed time:\n{m:02}m {s:02}s\n" if m
                     else f"\nElapsed time:\n{s:02}s\n")
            self.elapsed_time_label.setText(txt)
        self._update_summary()

    def _update_summary(self):
        now = QDateTime.currentMSecsSinceEpoch()
        if (self.thread and self.thread.isRunning()
                and now - self._last_summary_ts < 250):
            return
        self._last_summary_ts = now
        self._update_summary_widget(self.copied_count, self.skipped_count, self.error_count)

    def _on_file_copied(self, source, destination, file_size=0):
        self.copied_count       += 1
        self.total_bytes_copied += file_size
        entry = f"{self.copied_count}:\n'{source}'\nCopied to ⤵ \n'{destination}'\n"
        self.copied_tab.add_entry(entry, "copied")
        self.tab_widget.setTabText(1, f"Copied ({self.copied_count})")

    def _on_file_skipped(self, source, reason=""):
        self.skipped_count += 1
        entry = f"{self.skipped_count}:\n'{source}'\nSkipped {reason}\n"
        self.skipped_tab.add_entry(entry, "skipped")
        self.tab_widget.setTabText(2, f"Skipped ({self.skipped_count})")

    def _on_file_error(self, source, error=""):
        key = f"{source}::{error}"
        if key in self._error_keys:
            return
        self._error_keys.add(key)
        self.error_count += 1
        entry = f"{self.error_count}:\n'{source}'\nError: {error}\n"
        self.error_tab.add_entry(entry, "error")
        self.tab_widget.setTabText(3, f"Errors ({self.error_count})")

    def _on_smb_error_cancel(self):
        self._smb_error = True
        self.cancelled  = True
        if self.thread and self.thread.isRunning():
            self.thread.cancel()

    def _on_operation_completed(self):
        self.update_timer.stop()
        if self.thread:
            self.thread.cleanup_resources()

        if self.cancelled:
            self.status_label.setText(f"{self.operation_type} canceled!\n")
            self.status_label.setStyleSheet(
                "color:#ff8587;font-weight:bold;font-size:20px;background:transparent;")
            text = ("✖ \nProcess aborted due to samba file error." if self._smb_error
                    else "✖ \nProcess aborted by user.")
            self.current_file_label.setText(text)
            err_style = "color:#ff8587;font-weight:bold;font-size:17px;"
            self.current_file_label.setStyleSheet(err_style)
            self.elapsed_time_label.setStyleSheet(err_style)
            self.progress_bar.setStyleSheet(
                f"{_style()} QProgressBar::chunk {{"
                f"background-color:qlineargradient(spread:pad,x1:0,y1:0,x2:1,y2:0,"
                f"stop:0 #fd7e14,stop:1 #ff8587);border-radius:2px;}}"
            )
        else:
            self.status_label.setText(f"{self.operation_type} successfully completed!\n")
            self.current_file_label.setText("⇪ \nCheck details above.")
            self.progress_bar.setValue(100)
            self._animate_text()

        self.cancel_button.setText("Close")
        try:
            self.cancel_button.clicked.disconnect()
        except RuntimeError:
            pass
        self.cancel_button.clicked.connect(self.accept)
        self.cancel_button.setFocus()
        self._update_summary()
        for tab in (self.copied_tab, self.skipped_tab, self.error_tab):
            tab.flush_entries()
            tab.sort_entries()

    def _animate_text(self):
        try:
            self.colour_timer.timeout.disconnect(self._update_label_colour)
        except (RuntimeError, TypeError):
            pass
        self.colour_timer.timeout.connect(self._update_label_colour)
        self.colour_timer.start(50)

    def _update_label_colour(self):
        self.colour_step = (self.colour_step + 0.0175) % 1
        s, e = (102, 255, 245), (85, 255, 85)
        r = int(s[0] + (e[0] - s[0]) * self.colour_step)
        g = int(s[1] + (e[1] - s[1]) * self.colour_step)
        b = int(s[2] + (e[2] - s[2]) * self.colour_step)
        hex_col = f"#{r:02x}{g:02x}{b:02x}"
        bold    = f"color:{hex_col};font-weight:bold;"
        self.status_label.setStyleSheet(f"{bold}font-size:20px;background:transparent;")
        self.current_file_label.setStyleSheet(f"{bold}font-size:17px;")
        self.elapsed_time_label.setStyleSheet(f"{bold}font-size:17px;")

    def _cancel_operation(self):
        box = QMessageBox(
            QMessageBox.Icon.Question, "Confirm Cancellation",
            f"Are you sure you want to cancel the {self.operation_type} process?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, self,
        )
        box.setDefaultButton(QMessageBox.StandardButton.No)
        if box.exec() != QMessageBox.StandardButton.Yes:
            return
        if self.thread and self.thread.isRunning():
            self.cancelled = True
            self.thread.cancel()
            self.status_label.setText(f"Cancelling {self.operation_type}…\n")
            self.status_label.setStyleSheet(
                "color:#ff8587;font-weight:bold;font-size:20px;background:transparent;")
            self.current_file_label.setText("Please wait while operations are being cancelled…\n")
            self.progress_bar.setStyleSheet(
                f"{_style()} QProgressBar::chunk {{"
                f"background-color:qlineargradient(spread:pad,x1:0,y1:0,x2:1,y2:0,"
                f"stop:0 #fd7e14,stop:1 #ff8587);border-radius:2px;}}"
            )
            QCoreApplication.processEvents()
            if not self.thread.wait(3000):
                logger.warning("Thread did not stop cleanly after cancel, forcing terminate")
                self.thread.terminate()
                self.thread.wait(1000)

    def closeEvent(self, event):
        if self.thread and self.thread.isRunning():
            box = QMessageBox(
                QMessageBox.Icon.Question, "Confirm Close",
                f"The {self.operation_type} process is still running. Close anyway?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, self,
            )
            box.setDefaultButton(QMessageBox.StandardButton.No)
            if box.exec() == QMessageBox.StandardButton.Yes:
                self.cancelled = True
                self.thread.cancel()
                if not self.thread.wait(3000):
                    logger.warning("Thread did not stop in closeEvent, forcing terminate")
                    self.thread.terminate()
                    if not self.thread.wait(1000):
                        logger.warning("Thread could not be terminated.")
                self.thread.cleanup_resources()
                event.accept()
            else:
                event.ignore()
        else:
            event.accept()


class FileCopyThread(QThread):
    progress_updated    = pyqtSignal(int, str)
    workers_ready       = pyqtSignal()
    file_copied         = pyqtSignal(str, str, int)
    file_skipped        = pyqtSignal(str, str)
    file_error          = pyqtSignal(str, str)
    smb_error_cancel    = pyqtSignal()
    operation_completed = pyqtSignal()
    sudo_password_requested = pyqtSignal()

    SKIP_NAMES = frozenset({
        "Singleton", "SingletonCookie", "SingletonLock", "lockfile", "lock",
        "Cache-Control", ".parentlock", "cookies.sqlite-wal", "cookies.sqlite-shm",
        "places.sqlite-wal", "places.sqlite-shm", "SingletonSocket",
    })

    def __init__(self, checkbox_dirs, operation_type):
        super().__init__()
        self.checkbox_dirs  = checkbox_dirs
        self.operation_type = operation_type
        self.cancelled      = False
        self.mutex          = QMutex()

        self.total_files     = 0
        self.processed_files = 0
        self.total_bytes     = 0
        self.processed_bytes = 0
        self.file_sizes: dict[str, int] = {}
        self.file_batches: list[list]   = []
        self.batch_index     = 0
        self.worker_threads: list[FileWorkerThread] = []

        self._smb_src_stats:   dict[str, os.stat_result] = {}
        self._smb_local_paths: dict[str, str]             = {}

        mem_avail = psutil.virtual_memory().available
        self.buffer_size = min(64 * 1024 * 1024, max(4 * 1024 * 1024, int(mem_avail * 0.1)))
        self.num_workers = min(os.cpu_count() or 4, 8)

        self._smb_handler:          SmbFileHandler | None  = None
        self._samba_password_mgr:   SambaPasswordManager | None = None
        self._smb_credentials                               = None
        self.parent_dialog                                  = None

    def set_parent_dialog(self, dialog):
        self.parent_dialog = dialog

    def run(self):
        try:
            if self.cancelled:
                return
            self._collect_files()
            if self.total_files == 0:
                self.progress_updated.emit(100, "No files to process")
                return
            workers = min(self.num_workers, os.cpu_count() or 4)
            for i in range(workers):
                w = FileWorkerThread(self, i)
                self.worker_threads.append(w)
                w.start()
            self.workers_ready.emit()
            for w in self.worker_threads:
                w.wait()
        except Exception as exc:
            logger.error("FileCopyThread.run error: %s", exc)
        finally:
            self.operation_completed.emit()

    def _collect_files(self):
        all_files:  list[tuple[str, str]] = []
        file_sizes: dict[str, int]        = {}
        total_bytes = 0

        for checkbox, sources, destinations, _ in self.checkbox_dirs:
            sources      = sources      if isinstance(sources,      list) else [sources]
            destinations = destinations if isinstance(destinations, list) else [destinations]
            for source, destination in zip(sources, destinations):
                items, sizes, nbytes = self._enumerate_source(source, destination)
                all_files   += items
                file_sizes.update(sizes)
                total_bytes += nbytes

        self.total_files = len(all_files)
        self.file_sizes  = file_sizes
        self.total_bytes = total_bytes

        workers    = max(1, min(self.num_workers, os.cpu_count() or 4))
        batch_size = max(50, min(200, (self.total_files // (workers * 2)) or 100))
        self.file_batches = [all_files[i:i + batch_size]
                             for i in range(0, len(all_files), batch_size)]
        self.batch_index  = 0

    def _enumerate_source(self, source: str, destination: str
                           ) -> tuple[list[tuple[str, str]], dict[str, int], int]:
        files:  list[tuple[str, str]] = []
        sizes:  dict[str, int]        = {}
        nbytes = 0

        if SmbFileHandler.is_smb_path(source):
            try:
                server, share, rel_root = SmbFileHandler.parse_smb_url(source)
                local_root = self.smb_handler.mount_share(server, share)
                local_src  = os.path.join(local_root, rel_root) if rel_root else local_root
            except Exception as exc:
                logger.error("SMB mount/enumerate error for '%s': %s", source, exc)
                self.file_error.emit(source, f"Mount error: {exc}")
                return files, sizes, nbytes

            if os.path.isfile(local_src):
                if not self._should_skip(local_src):
                    try:
                        st = os.stat(local_src)
                        files.append((source, destination))
                        sizes[source] = st.st_size
                        self._smb_src_stats[source]   = st
                        self._smb_local_paths[source] = local_src
                        nbytes += st.st_size
                    except OSError as exc:
                        logger.error("stat error for '%s': %s", local_src, exc)
                else:
                    self.file_skipped.emit(source, "(Protected/locked file)")

            elif os.path.isdir(local_src):
                for dirpath, dirnames, filenames in os.walk(local_src):
                    dirnames[:] = [d for d in dirnames if not d.startswith(".")]
                    rel_dir = os.path.relpath(dirpath, local_src)
                    for fname in filenames:
                        if fname.startswith("."):
                            continue
                        local_file = os.path.join(dirpath, fname)
                        if self._should_skip(local_file):
                            smb_file = source.rstrip("/") + "/" + (
                                os.path.join(rel_dir, fname) if rel_dir != "." else fname
                            )
                            self.file_skipped.emit(smb_file, "(Protected/locked file)")
                            continue
                        try:
                            st = os.stat(local_file)
                        except OSError as exc:
                            logger.error("stat error for '%s': %s", local_file, exc)
                            continue
                        rel_file = fname if rel_dir == "." else os.path.join(rel_dir, fname)
                        smb_file = source.rstrip("/") + "/" + rel_file.replace(os.sep, "/")
                        try:
                            dst_file = str(Path(destination) / Path(rel_file))
                        except (TypeError, ValueError):
                            dst_file = str(Path(destination) / fname)
                        files.append((smb_file, dst_file))
                        sizes[smb_file] = st.st_size
                        self._smb_src_stats[smb_file]   = st
                        self._smb_local_paths[smb_file] = local_file
                        nbytes += st.st_size
            else:
                logger.warning("SMB source not found after mount: %s", local_src)
        else:
            src = Path(source)
            if not src.exists():
                return files, sizes, nbytes
            if src.is_file():
                if self._should_skip(str(src)):
                    self.file_skipped.emit(str(src), "(Protected/locked file)")
                else:
                    sz = self._get_file_size(str(src))
                    files.append((str(src), destination))
                    sizes[str(src)] = sz
                    nbytes += sz
            else:
                for dirpath, _, filenames in os.walk(src):
                    rel = Path(dirpath).relative_to(src)
                    for fname in filenames:
                        src_f = str(Path(dirpath) / fname)
                        if self._should_skip(src_f):
                            self.file_skipped.emit(src_f, "(Protected/locked file)")
                            continue
                        dst_f = str(Path(destination) / rel / fname)
                        sz    = self._get_file_size(src_f)
                        files.append((src_f, dst_f))
                        sizes[src_f] = sz
                        nbytes += sz
        return files, sizes, nbytes

    def get_next_batch(self) -> list | None:
        with QMutexLocker(self.mutex):
            if self.file_batches and self.batch_index < len(self.file_batches):
                batch = self.file_batches[self.batch_index]
                self.batch_index += 1
                return batch
            return None

    def _should_skip(self, path: str) -> bool:
        return Path(path).name in self.SKIP_NAMES

    def copy_file(self, source: str, dest: str):
        if self.cancelled:
            return

        if SmbFileHandler.is_smb_path(source) or SmbFileHandler.is_smb_path(dest):
            if not self.cancelled:
                self._copy_smb_file(source, dest)
            return

        try:
            if not Path(source).exists():
                self.handle_error(source, "Source file not found")
                return

            src_stat  = Path(source).stat()
            file_size = src_stat.st_size
            dest_path = Path(dest)

            if dest_path.exists():
                dst_stat = dest_path.stat()
                if src_stat.st_size == dst_stat.st_size and src_stat.st_mtime <= dst_stat.st_mtime:
                    self._record_progress(copied=False, source=source, dest=dest,
                                          name=Path(source).name, size=file_size)
                    return

            self._fast_copy(source, dest)
            self._record_progress(copied=True, source=source, dest=dest,
                                  name=Path(source).name, size=file_size)

        except (OSError, FileNotFoundError) as exc:
            msg = str(exc).lower()
            if any(s.lower() in msg for s in self.SKIP_NAMES):
                self._record_skip(source, "(Protected/locked file)")
            else:
                self.handle_error(source, f"Source error: {exc}")
        except Exception as exc:
            self.handle_error(source, str(exc))

    def _copy_smb_file(self, source: str, dest: str):

        if self.cancelled:
            return

        src_is_smb = SmbFileHandler.is_smb_path(source)
        dst_is_smb = SmbFileHandler.is_smb_path(dest)

        try:
            if src_is_smb:
                local_src = self._smb_local_paths.get(source) or (
                    self.smb_handler.resolve_local_path(source)
                )
                src_stat  = self._smb_src_stats.get(source) or os.stat(local_src)
            else:
                local_src = source
                src_stat  = os.stat(source)

            file_size = src_stat.st_size
            name      = Path(local_src).name

            if dst_is_smb:
                local_dst = self.smb_handler.resolve_local_path(dest)
            else:
                local_dst = dest

            if os.path.exists(local_dst) and not os.path.isdir(local_dst):
                try:
                    dst_stat = os.stat(local_dst)
                    if (src_stat.st_size == dst_stat.st_size
                            and src_stat.st_mtime <= dst_stat.st_mtime):
                        self._record_progress(
                            copied=False, source=source, dest=dest,
                            name=name, size=file_size,
                        )
                        return
                except OSError:
                    pass

            if self.cancelled:
                return
            os.makedirs(os.path.dirname(local_dst) or ".", exist_ok=True)
            self._fast_copy(local_src, local_dst)

            try:
                shutil.copystat(local_src, local_dst)
            except OSError:
                pass

            self._record_progress(
                copied=True, source=source, dest=dest,
                name=name, size=file_size,
            )

        except (OSError, FileNotFoundError) as exc:
            msg = str(exc).lower()
            if any(s.lower() in msg for s in self.SKIP_NAMES):
                self._record_skip(source, "(Protected/locked file)")
            else:
                self.handle_error(source, f"SMB copy error: {exc}")
        except Exception as exc:
            self.handle_error(source, str(exc))

    def _fast_copy(self, source: str, destination: str):

        if self.cancelled:
            return

        dest_dir = os.path.dirname(destination)
        if dest_dir:
            os.makedirs(dest_dir, exist_ok=True)

        buf = 256 * 1024
        try:
            buf = bytearray(buf)
            mv  = memoryview(buf)
            with open(source, "rb") as fsrc, open(destination, "wb") as fdst:
                while not self.cancelled:
                    n = fsrc.readinto(mv)
                    if not n:
                        break
                    fdst.write(mv[:n])
            if self.cancelled:
                _silent_unlink(destination)
                return
            shutil.copystat(source, destination)
        except MemoryError:
            old_flag = getattr(shutil, "_USE_CP_SENDFILE", None)
            try:
                if old_flag is not None:
                    shutil._USE_CP_SENDFILE = False
                shutil.copy2(source, destination)
            finally:
                if old_flag is not None:
                    shutil._USE_CP_SENDFILE = old_flag

    def _record_progress(self, *, copied: bool, source: str, dest: str, name: str, size: int):
        with QMutexLocker(self.mutex):
            self.processed_files += 1
            self.processed_bytes += self.file_sizes.get(source, size)
            pct = int(self.processed_bytes / self.total_bytes * 100) if self.total_bytes else 0
        if copied:
            self.file_copied.emit(source, dest, size)
            self.progress_updated.emit(pct, f"Copying:\n{name}")
        else:
            self.file_skipped.emit(source, "(Up to date)")
            self.progress_updated.emit(pct, f"Skipping (Up to date):\n{name}")

    def _record_skip(self, source: str, reason: str):
        with QMutexLocker(self.mutex):
            self.processed_files += 1
            self.processed_bytes += self.file_sizes.get(source, 0)
            pct = int(self.processed_bytes / self.total_bytes * 100) if self.total_bytes else 0
        self.file_skipped.emit(source, reason)
        self.progress_updated.emit(pct, f"Skipping {reason}:\n{Path(source).name}")

    def handle_error(self, source: str, msg: str):
        with QMutexLocker(self.mutex):
            self.processed_files += 1
            self.processed_bytes += self.file_sizes.get(source, 0)
            pct = int(self.processed_bytes / self.total_bytes * 100) if self.total_bytes else 0
        self.file_error.emit(source, msg)
        self.progress_updated.emit(pct, f"Error copying:\n{Path(source).name}")

    def _handle_smb_result(self, success: bool, source: str, dest: str, name: str, size_or_err):
        with QMutexLocker(self.mutex):
            self.processed_files += 1
            try:
                size = int(size_or_err) if isinstance(size_or_err, (int, str)) else 0
            except (ValueError, TypeError):
                size = 0
            self.processed_bytes += size
            pct = int(self.processed_bytes / self.total_bytes * 100) if self.total_bytes else 0
        if success:
            self.file_copied.emit(source, dest, size)
            self.progress_updated.emit(pct, f"Copying:\n{name}")
        else:
            self.file_error.emit(source, str(size_or_err))
            self.progress_updated.emit(pct, f"Error copying:\n{name}")
            self.smb_error_cancel.emit()

    def _get_file_size(self, path: str) -> int:
        try:
            if SmbFileHandler.is_smb_path(path):
                return self.smb_handler.get_smb_file_size(path)
            return Path(path).stat().st_size
        except Exception as exc:
            logger.error("Error getting file size for '%s': %s", path, exc)
            if self._smb_handler and SmbFileHandler.is_smb_path(path):
                self.file_error.emit(path, f"File size error: {exc}")
                self.smb_error_cancel.emit()
            return 0

    @property
    def smb_handler(self) -> "SmbFileHandler":
        if self._smb_handler is None:
            self._smb_handler = SmbFileHandler(self.samba_password_mgr, self)
        return self._smb_handler

    @property
    def samba_password_mgr(self) -> SambaPasswordManager:
        if self._samba_password_mgr is None:
            self._samba_password_mgr = SambaPasswordManager()
        return self._samba_password_mgr

    def get_smb_credentials(self):
        with QMutexLocker(self.mutex):
            return self._smb_credentials

    def set_smb_credentials(self, creds):
        with QMutexLocker(self.mutex):
            self._smb_credentials = creds

    def cleanup_resources(self):
        for w in list(self.worker_threads):
            if w and w.isRunning():
                w.requestInterruption()
                if not w.wait(3000):
                    logger.warning("Worker thread did not stop cleanly, forcing terminate")
                    w.terminate()
                    w.wait(1000)
        self.worker_threads.clear()
        if self._smb_handler:
            try:
                self._smb_handler.force_cleanup()
            except Exception as exc:
                logger.warning("SMB cleanup warning: %s", exc)
            finally:
                self._smb_handler   = None
                self._smb_credentials = None

    def cancel(self):
        self.cancelled = True
        for w in self.worker_threads:
            w.requestInterruption()
        if self._smb_handler:
            self._smb_handler.force_cleanup()
        for w in self.worker_threads:
            if not w.wait(3000):
                logger.warning("Worker thread did not respond to cancel, forcing terminate")
                w.terminate()
                w.wait(1000)
        self.cleanup_resources()


class FileWorkerThread(QThread):
    def __init__(self, main_thread: FileCopyThread, worker_id: int):
        super().__init__()
        self.main_thread = main_thread
        self.setObjectName(f"FileWorker-{worker_id}")

    def run(self):
        while not self.main_thread.cancelled and not self.isInterruptionRequested():
            batch = self.main_thread.get_next_batch()
            if batch is None:
                break
            for source, dest in batch:
                if self.main_thread.cancelled or self.isInterruptionRequested():
                    return
                try:
                    self.main_thread.copy_file(source, dest)
                except Exception as exc:
                    if not self.main_thread.cancelled:
                        self.main_thread.handle_error(source, str(exc))


class SmbFileHandler:
    def __init__(self, samba_password_manager: SambaPasswordManager, thread=None):
        self.samba_password_manager = samba_password_manager
        self.thread                 = thread
        self._smb_credentials       = None
        self._sudo_password         = None
        self.mutex                  = QMutex()
        self._mount_wait_conditions: dict = {}
        self._mounted_shares:        dict = {}
        self._mounting_shares:       set  = set()

    def initialize(self):
        if self._smb_credentials:
            return
        with QMutexLocker(self.mutex):
            if self._smb_credentials:
                return
            creds       = (getattr(self.thread, "get_smb_credentials", lambda: None)()
                           if self.thread else None)
            valid_creds = (creds
                           if isinstance(creds, (list, tuple))
                           and len(creds) >= 2 and all(creds[:2])
                           else None)
            self._smb_credentials = valid_creds or self.samba_password_manager.get_samba_credentials()
            if self.thread and hasattr(self.thread, "set_smb_credentials") and not valid_creds:
                self.thread.set_smb_credentials(self._smb_credentials)

    @staticmethod
    def is_smb_path(path) -> bool:
        return str(path).startswith(("smb:", "//"))

    @staticmethod
    def parse_smb_url(path: str) -> tuple[str, str, str]:
        path = str(path)
        if path.startswith("smb:/") and not path.startswith("smb://"):
            path = f"smb://{path[5:]}"
        if path.startswith("//"):
            path = f"smb://{path[2:]}"
        m = re.match(r"smb://([^/]+)/([^/]+)(/?.*)", path)
        if not m:
            raise ValueError(f"Invalid SMB URL: {path}")
        return m.group(1), m.group(2), m.group(3).lstrip("/")

    def resolve_local_path(self, smb_path: str) -> str:
        return self._smb_path_to_local(smb_path)

    def mount_share(self, server: str, share: str) -> str:
        return self._mount_smb_share(server, share)

    def _smb_path_to_local(self, smb_path: str) -> str:
        server, share, rel = self.parse_smb_url(smb_path)
        mp = self._mount_smb_share(server, share)
        return os.path.join(mp, rel) if rel else mp

    def _get_sudo_password(self) -> str:
        if self._sudo_password:
            return self._sudo_password
        if self.thread and getattr(self.thread, "parent_dialog", None):
            self._sudo_password = self.thread.parent_dialog.get_sudo_password()
            return self._sudo_password
        raise RuntimeError("Cannot request sudo password — no parent dialog available")

    def _mount_smb_share(self, server: str, share: str) -> str:
        if self.thread and getattr(self.thread, "cancelled", False):
            raise RuntimeError("Operation cancelled")
        if not server or not share:
            raise ValueError("Server and share must be specified")

        key = (server, share)
        with QMutexLocker(self.mutex):
            if key in self._mounted_shares and os.path.ismount(self._mounted_shares[key]):
                return self._mounted_shares[key]
            self._mounted_shares.pop(key, None)

            if key in self._mounting_shares:
                if key not in self._mount_wait_conditions:
                    self._mount_wait_conditions[key] = QWaitCondition()
                wc = self._mount_wait_conditions[key]
                for _ in range(10):
                    if self.thread and getattr(self.thread, "cancelled", False):
                        raise RuntimeError("Cancelled during mount wait")
                    wc.wait(self.mutex, 500)
                    if key not in self._mounting_shares:
                        break
                if key in self._mounted_shares and os.path.ismount(self._mounted_shares[key]):
                    return self._mounted_shares[key]
                if key in self._mounting_shares:
                    raise RuntimeError("Mount timed out waiting for another thread")
            self._mounting_shares.add(key)
            self._mount_wait_conditions.setdefault(key, QWaitCondition())

        mp = tempfile.mkdtemp(prefix=f"smb_{server}_{share}_")
        cred_file_path: str | None = None
        try:
            if self.thread and getattr(self.thread, "cancelled", False):
                raise RuntimeError("Cancelled before mount")
            self.initialize()
            if not self._smb_credentials or len(self._smb_credentials) < 2:
                raise RuntimeError("SMB credentials unavailable or incomplete")
            username, password = self._smb_credentials[:2]
            domain = (self._smb_credentials[2]
                      if len(self._smb_credentials) > 2 else None)

            with tempfile.NamedTemporaryFile(
                mode="w", prefix="smb_cred_", suffix=".tmp",
                delete=False, encoding="utf-8",
            ) as cf:
                cred_file_path = cf.name
                cf.write(f"username={username}\npassword={password}\n")
                if domain:
                    cf.write(f"domain={domain}\n")
            os.chmod(cred_file_path, 0o600)

            opts = [
                f"credentials={cred_file_path}",
                f"uid={os.getuid()}", f"gid={os.getgid()}", "iocharset=utf8",
            ]
            cmd = ["sudo", "mount.cifs", f"//{server}/{share}", mp, "-o", ",".join(opts)]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)

            if result.returncode != 0:
                if self.thread and getattr(self.thread, "cancelled", False):
                    raise RuntimeError("Cancelled")
                sudo_pw = self._get_sudo_password()
                cmd2    = ["sudo", "-S", "mount.cifs", f"//{server}/{share}", mp,
                           "-o", ",".join(opts)]
                result2 = subprocess.run(
                    cmd2, input=f"{sudo_pw}\n",
                    capture_output=True, text=True, timeout=10,
                )
                if result2.returncode != 0:
                    _silent_rmdir(mp)
                    raise RuntimeError(f"Mount failed: {result2.stderr}")

            with QMutexLocker(self.mutex):
                self._mounted_shares[key] = mp
            return mp

        except subprocess.TimeoutExpired:
            _silent_rmdir(mp)
            raise RuntimeError("Mount operation timed out")
        except Exception:
            _silent_rmdir(mp)
            raise
        finally:
            if cred_file_path and os.path.exists(cred_file_path):
                _silent_unlink(cred_file_path)
            with QMutexLocker(self.mutex):
                self._mounting_shares.discard(key)
                self._mount_wait_conditions.get(key, QWaitCondition()).wakeAll()

    @staticmethod
    def _unmount_smb_share(mount_point: str, sudo_password: str | None = None):
        if not mount_point or not os.path.exists(mount_point):
            return
        for cmd, inp in [
            (["sudo", "umount",      mount_point], None),
            (["sudo", "-S", "umount", mount_point], sudo_password),
            (["sudo", "umount", "-l", mount_point], None),
        ]:
            try:
                subprocess.run(
                    cmd,
                    input=f"{inp}\n" if inp else None,
                    capture_output=True, text=True, timeout=5,
                )
                break
            except Exception as exc:
                logger.warning("Unmount attempt warning: %s", exc)
        _silent_rmdir(mount_point)

    def copy_file(self, source: str, destination: str, progress_callback=None):
        if self.thread and getattr(self.thread, "cancelled", False):
            return None
        src_smb = self.is_smb_path(source)
        dst_smb = self.is_smb_path(destination)
        try:
            if src_smb and not dst_smb:
                return self._copy_smb_to_local(source, destination, progress_callback)
            if not src_smb and dst_smb:
                return self._copy_local_to_smb(source, destination, progress_callback)
            return self._copy_local(source, destination, progress_callback)
        except Exception as exc:
            if progress_callback:
                progress_callback(False, Path(source).name, str(exc))
            raise

    def _copy_smb_to_local(self, source, dest, cb):
        if self.thread and getattr(self.thread, "cancelled", False):
            return None
        local_src = self._smb_path_to_local(source)
        fn        = Path(source).name or "directory"
        try:
            if os.path.isdir(local_src):
                dst_p = Path(dest) / Path(local_src).name
                shutil.copytree(local_src, dst_p, dirs_exist_ok=True)
                total = sum(f.stat().st_size for f in dst_p.rglob("*") if f.is_file())
            else:
                Path(dest).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(local_src, dest)
                total = os.path.getsize(dest)
            if cb: cb(True, fn, total)
            return total
        except Exception as exc:
            if cb: cb(False, fn, str(exc))
            raise

    def _copy_local_to_smb(self, source, dest, cb):
        if self.thread and getattr(self.thread, "cancelled", False):
            return None
        local_dst = self._smb_path_to_local(dest)
        fn        = Path(source).name
        try:
            if os.path.isdir(source):
                dst_p = os.path.join(local_dst, os.path.basename(source))
                shutil.copytree(source, dst_p, dirs_exist_ok=True)
                total = sum(f.stat().st_size for f in Path(source).rglob("*") if f.is_file())
            else:
                os.makedirs(os.path.dirname(local_dst) or ".", exist_ok=True)
                shutil.copy2(source, local_dst)
                total = os.path.getsize(source)
            if cb: cb(True, fn, total)
            return total
        except Exception as exc:
            if cb: cb(False, fn, str(exc))
            raise

    def _copy_local(self, source, dest, cb):
        if self.thread and getattr(self.thread, "cancelled", False):
            return None
        fn = Path(source).name
        try:
            os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
            if Path(source).is_dir():
                shutil.copytree(source, dest, dirs_exist_ok=True)
                total = sum(f.stat().st_size for f in Path(dest).rglob("*") if f.is_file())
            else:
                shutil.copy2(source, dest)
                total = os.path.getsize(dest)
            if cb: cb(True, fn, total)
            return total
        except Exception as exc:
            if cb: cb(False, fn, str(exc))
            raise

    def is_directory(self, path: str) -> bool:
        if self.is_smb_path(path):
            try:
                return os.path.isdir(self._smb_path_to_local(path))
            except Exception as exc:
                logger.error("is_directory error: %s", exc)
                return False
        return Path(path).is_dir()

    def get_smb_file_size(self, path: str) -> int:
        if not self.is_smb_path(path):
            return 0
        try:
            local = self._smb_path_to_local(path)
            if os.path.isfile(local):
                return os.path.getsize(local)
            if os.path.isdir(local):
                return sum(f.stat().st_size for f in Path(local).rglob("*") if f.is_file())
            return 0
        except Exception as exc:
            logger.error("get_smb_file_size error: %s", exc)
            return 0

    def list_smb_directory(self, path: str) -> list[str]:
        if not self.is_smb_path(path):
            return []
        try:
            local = self._smb_path_to_local(path)
            return [f for f in os.listdir(local) if not f.startswith(".")] if os.path.isdir(local) else []
        except Exception as exc:
            logger.error("list_smb_directory error: %s", exc)
            return []

    def list_smb_directory_recursive(self, path: str) -> list[str]:
        if not self.is_smb_path(path):
            return []
        try:
            local = self._smb_path_to_local(path)
            if not os.path.isdir(local):
                return []
            result = []
            for dirpath, dirnames, filenames in os.walk(local):
                dirnames[:] = [d for d in dirnames if not d.startswith(".")]
                for fname in filenames:
                    if not fname.startswith("."):
                        rel = os.path.relpath(os.path.join(dirpath, fname), local)
                        result.append(rel)
            return result
        except Exception as exc:
            logger.error("list_smb_directory_recursive error: %s", exc)
            return []

    def cleanup(self):
        for _, mp in list(self._mounted_shares.items()):
            try:
                self._unmount_smb_share(mp, self._sudo_password)
            except Exception as exc:
                logger.warning("Cleanup warning: %s", exc)
        self._mounted_shares.clear()
        self._mounting_shares.clear()
        self._mount_wait_conditions.clear()

    def force_cleanup(self):
        try:
            for _, mp in list(self._mounted_shares.items()):
                try:
                    subprocess.run(["sudo", "umount", "-l", mp],
                                   timeout=2, capture_output=True)
                    _silent_rmdir(mp)
                except Exception as exc:
                    logger.exception("force_cleanup error (ignored): %s", exc)
            self._mounted_shares.clear()
            self._mounting_shares.clear()
            with QMutexLocker(self.mutex):
                for wc in self._mount_wait_conditions.values():
                    wc.wakeAll()
                self._mount_wait_conditions.clear()
        except Exception as exc:
            logger.exception("force_cleanup error (ignored): %s", exc)


class LogEntryListModel(QAbstractListModel):
    _COLOURS = {
        "error":   Qt.GlobalColor.red,
        "skipped": Qt.GlobalColor.yellow,
        "copied":  Qt.GlobalColor.green,
    }

    def __init__(self, entries: list, entry_types: list, parent=None):
        super().__init__(parent)
        self._entries  = entries
        self._types    = entry_types
        self.filter    = ""
        self._filtered = list(range(len(entries)))

    def rowCount(self, parent=QModelIndex()) -> int:
        return len(self._filtered)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        row = self._filtered[index.row()]
        if role == Qt.ItemDataRole.DisplayRole:
            return self._entries[row]
        if role == Qt.ItemDataRole.ForegroundRole:
            return self._COLOURS.get(self._types[row])
        return None

    def set_filter(self, text: str):
        self.filter    = text.lower()
        self._filtered = (
            [i for i, e in enumerate(self._entries) if self.filter in e.lower()]
            if self.filter else list(range(len(self._entries)))
        )
        self.layoutChanged.emit()

    def add_entry(self, entry: str, entry_type: str):
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
            pairs = list(zip(self._entries, self._types))
            replaced = []
            for entry, etype in pairs:
                e = entry
                for old, new in getattr(Options, "text_replacements", []):
                    e = e.replace(old, new)
                replaced.append((e, etype))

            def _sort_key(item):
                text = item[0]
                try:
                    m = re.match(r"^\d+:(?:<br>)?'([^']+)'", text)
                    if m:
                        return m.group(1).lower()
                    for line in text.split("<br>" if "<br>" in text else "\n"):
                        if "/" in line and not line.strip().isdigit():
                            return line.strip().lower()
                    return text.lower()
                except (ValueError, IndexError, re.error) as _exc:
                    logger.error("sort_entries renumbering error: %s", _exc)
                    return text.lower()

            replaced.sort(key=_sort_key)
            new_entries, new_types = [], []
            for i, (entry, etype) in enumerate(replaced):
                try:
                    sep = "<br>" if "<br>" in entry else "\n"
                    entry = re.sub(r"^\d+:" + (sep if sep == "<br>" else ""),
                                   f"{i + 1}:{sep}", entry, count=1)
                    entry = re.sub(r"^\d+:", f"{i + 1}:", entry, count=1)
                except Exception as exc:
                    logger.error("sort_entries renumbering error: %s", exc)
                    entry = f"{i + 1}: {entry}"
                new_entries.append(entry)
                new_types.append(etype)
            self._entries[:] = new_entries
            self._types[:]   = new_types
            self.endResetModel()
            self.set_filter(self.filter)
        except Exception as exc:
            self.endResetModel()
            logger.exception("sort_entries error: %s", exc)


class VirtualLogTabWidget(QWidget):
    FLUSH_INTERVAL = 300

    def __init__(self, parent=None):
        super().__init__(parent)
        self.entries:          list[str] = []
        self.entry_types:      list[str] = []
        self.pending_entries:  list[tuple[str, str]] = []
        self._mutex = QMutex()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(5, 5, 5, 5)

        search_label = QLabel("Search:")
        search_label.setStyleSheet("color:#e0e0e0;")
        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText("Filter entries…")
        layout.addWidget(search_label)
        layout.addWidget(self.search_box)

        self.model    = LogEntryListModel(self.entries, self.entry_types)
        self.list_view = QListView()
        self.list_view.setModel(self.model)
        self.list_view.setEditTriggers(QListView.EditTrigger.NoEditTriggers)
        self.list_view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list_view.setWordWrap(True)
        layout.addWidget(self.list_view)

        self.search_box.textChanged.connect(self.model.set_filter)

        self._flush_timer = QTimer(self)
        self._flush_timer.setInterval(self.FLUSH_INTERVAL)
        self._flush_timer.setSingleShot(True)
        self._flush_timer.timeout.connect(self.flush_entries)

    def add_entry(self, entry: str, entry_type: str):
        with QMutexLocker(self._mutex):
            self.pending_entries.append((entry, entry_type))
        if not self._flush_timer.isActive():
            self._flush_timer.start()

    def flush_entries(self):
        with QMutexLocker(self._mutex):
            if not self.pending_entries:
                return
            batch                = self.pending_entries[:100]
            del self.pending_entries[:100]
            has_more             = bool(self.pending_entries)

        if batch:
            start = len(self.entries)
            entries, types = zip(*batch)
            self.model.beginInsertRows(QModelIndex(), start, start + len(entries) - 1)
            self.entries.extend(entries)
            self.entry_types.extend(types)
            self.model.endInsertRows()
            if self.model.filter:
                self.model.set_filter(self.model.filter)

        if has_more:
            self._flush_timer.start()

    def sort_entries(self):
        self.model.sort_entries()


def _silent_unlink(path: str):
    try:
        os.unlink(path)
    except OSError:
        pass


def _silent_rmdir(path: str):
    if path and os.path.exists(path):
        try:
            if not os.listdir(path):
                os.rmdir(path)
        except OSError:
            pass

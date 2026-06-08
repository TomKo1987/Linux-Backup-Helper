import csv
import io
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

from PyQt6.QtCore import Qt, QThread, pyqtSignal, QPoint
from PyQt6.QtGui import QColor, QCloseEvent, QKeySequence, QShortcut
from PyQt6.QtWidgets import (
    QComboBox, QDialog, QFileDialog, QFrame, QHBoxLayout, QLabel,
    QLineEdit, QListWidget, QListWidgetItem, QMessageBox, QProgressBar,
    QPushButton, QVBoxLayout, QApplication,
)

from state import _HOME, logger
from themes import current_theme, font_sz
from ui_utils import _StandardKeysMixin

_PROC_MOUNTS_OCTAL_RE = re.compile(r"\\(\d{3})")
_SIZE_UNITS = ("B", "KB", "MB", "GB", "TB")

_R_PATH       = 0
_R_SIZE       = 1
_R_IS_DIR     = 2
_R_FILE_COUNT = 3
_R_IS_MOUNT   = 4


def _fmt_size(value: float) -> str:
    for unit in _SIZE_UNITS[:-1]:
        if value < 1024.0:
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} {unit}"
        value /= 1024.0
    return f"{value:.2f} {_SIZE_UNITS[-1]}"


def _fmt_count(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def _bar(fraction: float, width: int = 18) -> str:
    filled = max(0, min(width, int(fraction * width)))
    return "█" * filled + "░" * (width - filled)


def _get_disk_info(path: Path) -> dict:
    info: dict = {}
    try:
        st = os.statvfs(path)
        info["total"] = st.f_blocks * st.f_frsize
        info["free"]  = st.f_bavail * st.f_frsize
        info["used"]  = (st.f_blocks - st.f_bfree) * st.f_frsize
        info["fraction_used"] = info["used"] / max(info["total"], 1)
    except OSError:
        info["total"] = info["free"] = info["used"] = 0
        info["fraction_used"] = 0.0

    dev = fstype = mount = "?"

    try:
        out = subprocess.check_output(
            ["df", "-T", "--output=source,fstype,target", str(path)],
            stderr=subprocess.DEVNULL, text=True, timeout=3,
        ).splitlines()
        if len(out) >= 2:
            parts = out[1].split()
            dev    = parts[0] if len(parts) > 0 else "?"
            fstype = parts[1] if len(parts) > 1 else "?"
            mount  = parts[2] if len(parts) > 2 else "?"
    except (subprocess.SubprocessError, OSError, ValueError):
        try:
            out = subprocess.check_output(
                ["df", "-P", str(path)],
                stderr=subprocess.DEVNULL, text=True, timeout=3,
            ).splitlines()
            if len(out) >= 2:
                parts = out[1].split()
                dev   = parts[0] if len(parts) > 0 else "?"
                mount = parts[5] if len(parts) > 5 else "?"
        except (subprocess.SubprocessError, OSError, ValueError):
            pass

    if fstype == "?" or mount == "?" or dev == "?":
        try:
            real = os.path.realpath(str(path))
            best_mnt = ""
            with open("/proc/mounts", encoding="utf-8", errors="replace") as f:
                for line in f:
                    cols = line.split()
                    if len(cols) >= 3:
                        d = _PROC_MOUNTS_OCTAL_RE.sub(lambda x: chr(int(x.group(1), 8)), cols[0])
                        m = _PROC_MOUNTS_OCTAL_RE.sub(lambda x: chr(int(x.group(1), 8)), cols[1])
                        t_ = cols[2]
                        if real.startswith(m) and len(m) > len(best_mnt):
                            best_mnt = m
                            if dev   == "?": dev   = d
                            if mount == "?": mount = m
                            if fstype == "?": fstype = t_
        except (OSError, ValueError):
            pass

    info["device"] = dev
    info["fstype"] = fstype
    info["mount"]  = mount
    return info


class _ScanWorker(QThread):
    progress = pyqtSignal(str)
    finished = pyqtSignal(list, float, int)

    def __init__(self, root: Path, cancel: threading.Event) -> None:
        super().__init__()
        self._root   = root
        self._cancel = cancel

    def run(self) -> None:
        t0 = time.monotonic()
        results: list[tuple[Path, int, bool, int, bool]] = []
        skipped_mounts = 0

        mount_points: set[str] = set()
        try:
            with open("/proc/mounts", encoding="utf-8", errors="replace") as f:
                for line in f:
                    cols = line.split()
                    if len(cols) >= 2:
                        mount_points.add(os.path.realpath(cols[1]))
        except OSError:
            pass

        root_real = os.path.realpath(str(self._root))
        try:
            root_dev = os.lstat(self._root).st_dev
        except OSError:
            root_dev = -1

        sizes: dict[Path, int] = {}
        try:
            proc = subprocess.Popen(
                ["du", "-x", "--block-size=1", "--max-depth=1", str(self._root)],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
            )
            n_found = 0

            if proc.stdout:
                try:
                    for line in proc.stdout:
                        if self._cancel.is_set():
                            proc.kill()
                            break
                        line = line.rstrip("\n")
                        if not line:
                            continue
                        parts = line.split("\t", 1)
                        if len(parts) == 2:
                            try:
                                sz = int(parts[0])
                                path = Path(parts[1])
                                if path != self._root:
                                    sizes[path] = sz
                                    n_found += 1
                                    self.progress.emit(f"Scanning\u2026 {n_found} entries found")
                            except ValueError:
                                pass
                finally:
                    proc.stdout.close()
            proc.wait()
        except (OSError, subprocess.SubprocessError) as exc:
            logger.warning("DiskAnalyzer du: %s", exc)

        try:
            for entry in os.scandir(self._root):
                if self._cancel.is_set():
                    break
                try:
                    if entry.is_symlink():
                        continue
                    path = Path(entry.path)

                    if entry.is_file(follow_symlinks=False):
                        size = sizes.get(path, 0)
                        if size == 0:
                            try:
                                st   = entry.stat(follow_symlinks=False)
                                size = st.st_blocks * 512
                            except OSError:
                                pass
                        results.append((path, size, False, 1, False))

                    elif entry.is_dir(follow_symlinks=False):
                        entry_real = str(path.resolve())
                        is_mount = (
                            entry_real in mount_points
                            and entry_real != root_real
                        )
                        if is_mount:
                            results.append((path, 0, True, 0, True))
                            skipped_mounts += 1
                        else:
                            size       = sizes.get(path, 0)
                            file_count = self._count_files(path, root_dev)
                            results.append((path, size, True, file_count, False))

                except (OSError, PermissionError):
                    pass
        except (OSError, PermissionError) as exc:
            logger.warning("DiskAnalyzer scan: %s", exc)

        results.sort(key=lambda x: x[_R_SIZE], reverse=True)
        elapsed = time.monotonic() - t0
        self.finished.emit(results[:500], elapsed, skipped_mounts)

    def _count_files(self, path: Path, root_dev: int) -> int:
        count = 0
        try:
            for dirpath, dirnames, filenames in os.walk(
                path, followlinks=False, topdown=True, onerror=lambda e: None
            ):
                if self._cancel.is_set():
                    break
                if root_dev != -1:
                    keep = []
                    for d in dirnames:
                        try:
                            if os.lstat(os.path.join(dirpath, d)).st_dev == root_dev:
                                keep.append(d)
                        except OSError:
                            keep.append(d)
                    dirnames[:] = keep
                count += len(filenames)
        except OSError:
            pass
        return count



class _DiskInfoBar(QFrame):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        t = current_theme()
        self._t = t
        self.setStyleSheet(
            f"background:{t['bg2']};border-bottom:1px solid {t['header_sep']};"
        )
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 8, 14, 8)
        lay.setSpacing(4)

        self._lbl_dev   = QLabel("—")
        self._lbl_dev.setStyleSheet(
            f"color:{t['text_dim']};font-size:{font_sz(-1)}px;background:transparent;"
        )
        self._lbl_stats = QLabel("—")
        self._lbl_stats.setStyleSheet(
            f"color:{t['text']};font-size:{font_sz()}px;font-weight:bold;background:transparent;"
        )
        self._lbl_bar   = QLabel()
        self._lbl_bar.setStyleSheet(
            f"color:{t['accent']};font-family:monospace;font-size:{font_sz(-1)}px;background:transparent;"
        )

        lay.addWidget(self._lbl_dev)
        lay.addWidget(self._lbl_stats)
        lay.addWidget(self._lbl_bar)
        self.setVisible(False)

    def update_info(self, info: dict) -> None:
        t = self._t
        if not info.get("total"):
            self.setVisible(False)
            return

        dev   = info.get("device", "?")
        fs    = info.get("fstype", "?")
        mnt   = info.get("mount",  "?")
        self._lbl_dev.setText(
            f"Partition:  {dev}  │  Filesystem: {fs}  │  Mountpoint: {mnt}"
        )

        total = info["total"]
        used  = info["used"]
        free  = info["free"]
        pct   = info["fraction_used"] * 100

        self._lbl_stats.setText(
            f"Total: {_fmt_size(total)}   │   "
            f"Used: {_fmt_size(used)} ({pct:.1f}%)   │   "
            f"Free: {_fmt_size(free)}"
        )

        bar_width = 40
        bar = _bar(info["fraction_used"], bar_width)

        if pct > 90:
            bar_color = "#ff5555"
        elif pct > 70:
            bar_color = "#ffb86c"
        else:
            bar_color = t["accent"]

        self._lbl_bar.setStyleSheet(
            f"color:{bar_color};font-family:monospace;"
            f"font-size:{font_sz(-1)}px;background:transparent;"
        )
        self._lbl_bar.setText(f"[{bar}]  {pct:.1f}% used")
        self.setVisible(True)


# noinspection PyUnresolvedReferences
class DiskAnalyzerDialog(_StandardKeysMixin, QDialog):
    _SORT_SIZE_DESC = 0
    _SORT_SIZE_ASC  = 1
    _SORT_NAME_ASC  = 2
    _SORT_NAME_DESC = 3
    _SORT_TYPE      = 4

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Disk Usage Analyzer")
        self.setMinimumSize(1200, 1100)

        self._worker:       _ScanWorker | None = None
        self._cancel        = threading.Event()
        self._results:      list[tuple] = []
        self._item_paths:   list[Path]  = []
        self._scan_root:    Path        = _HOME
        self._disk_info:    dict        = {}
        self._elapsed:      float       = 0.0
        self._sort_mode:    int         = self._SORT_SIZE_DESC
        self._skipped_mounts: int       = 0

        self._nav_back:    list[Path] = []
        self._nav_forward: list[Path] = []

        self._build_ui()
        self._refresh_nav_buttons()

        QShortcut(QKeySequence("F5"), self).activated.connect(self._start_scan)

    def _build_ui(self) -> None:
        t   = current_theme()
        bg  = t["bg"];  bg2 = t["bg2"]; bg3 = t["bg3"]
        sep = t["header_sep"]; acc = t["accent"]; fg = t["text"]; dim = t["text_dim"]

        self.setStyleSheet(f"background:{bg};color:{fg};")

        header = QFrame()
        header.setStyleSheet(f"background:{bg2};border-bottom:1px solid {sep};")
        hl = QHBoxLayout(header)
        hl.setContentsMargins(14, 10, 14, 10)
        title = QLabel("💽  Disk Usage Analyzer")
        title.setStyleSheet(
            f"font-size:{font_sz(5)}px;font-weight:bold;color:{acc};background:transparent;"
        )
        hl.addWidget(title)
        hl.addStretch()
        kbd = QLabel("F5 = Refresh")
        kbd.setStyleSheet(f"color:{dim};font-size:{font_sz(-2)}px;background:transparent;")
        hl.addWidget(kbd)

        nav_frame = QFrame()
        nav_frame.setStyleSheet(f"background:{bg2};border-bottom:1px solid {sep};")
        nbl = QHBoxLayout(nav_frame)
        nbl.setContentsMargins(12, 6, 12, 6)
        nbl.setSpacing(6)

        self._back_btn = QPushButton("◀ Back")
        self._back_btn.setFixedHeight(28)
        self._back_btn.setStyleSheet(self._btn_ss(t))
        self._back_btn.setToolTip("Navigate back (Alt+Left)")
        self._back_btn.clicked.connect(self._nav_back_action)
        QShortcut(QKeySequence("Alt+Left"), self).activated.connect(self._nav_back_action)

        self._up_btn = QPushButton("↑ Up")
        self._up_btn.setFixedHeight(28)
        self._up_btn.setStyleSheet(self._btn_ss(t))
        self._up_btn.setToolTip("Go to parent directory (Alt+Up)")
        self._up_btn.clicked.connect(self._nav_up)
        QShortcut(QKeySequence("Alt+Up"), self).activated.connect(self._nav_up)

        self._fwd_btn = QPushButton("▶ Fwd")
        self._fwd_btn.setFixedHeight(28)
        self._fwd_btn.setStyleSheet(self._btn_ss(t))
        self._fwd_btn.setToolTip("Navigate forward (Alt+Right)")
        self._fwd_btn.clicked.connect(self._nav_forward_action)
        QShortcut(QKeySequence("Alt+Right"), self).activated.connect(self._nav_forward_action)

        self._breadcrumb = QLabel("")
        self._breadcrumb.setStyleSheet(
            f"color:{dim};font-size:{font_sz(-1)}px;background:transparent;"
        )

        nbl.addWidget(self._back_btn)
        nbl.addWidget(self._up_btn)
        nbl.addWidget(self._fwd_btn)
        nbl.addSpacing(8)
        nbl.addWidget(self._breadcrumb, 1)

        path_frame = QFrame()
        path_frame.setStyleSheet(f"background:{bg2};border-bottom:1px solid {sep};")
        pl = QHBoxLayout(path_frame)
        pl.setContentsMargins(12, 8, 12, 8)
        pl.setSpacing(8)

        self._path_edit = QLineEdit(str(_HOME))
        self._path_edit.setStyleSheet(
            f"QLineEdit{{background:{bg3};border:1px solid {sep};border-radius:4px;"
            f"color:{fg};padding:4px 8px;font-size:{font_sz()}px;}}"
            f"QLineEdit:focus{{border-color:{acc};}}"
        )
        self._path_edit.returnPressed.connect(self._start_scan)

        browse_btn = QPushButton("📁 Browse")
        browse_btn.setMinimumHeight(34)
        browse_btn.clicked.connect(self._browse)
        browse_btn.setStyleSheet(self._btn_ss(t))

        self._scan_btn = QPushButton("🔍 Scan")
        self._scan_btn.setMinimumHeight(34)
        self._scan_btn.setStyleSheet(self._btn_ss(t, primary=True))
        self._scan_btn.clicked.connect(self._start_scan)

        pl.addWidget(QLabel("Directory:"))
        pl.addWidget(self._path_edit, 1)
        pl.addWidget(browse_btn)
        pl.addWidget(self._scan_btn)
        pl.addSpacing(12)

        self._disk_bar = _DiskInfoBar()

        self._progress = QProgressBar()
        self._progress.setRange(0, 0)
        self._progress.setVisible(False)
        self._progress.setFixedHeight(4)
        self._progress.setStyleSheet(
            f"QProgressBar{{background:{bg3};border:none;}}"
            f"QProgressBar::chunk{{background:{acc};}}"
        )

        ctrl_frame = QFrame()
        ctrl_frame.setStyleSheet(f"background:{bg2};border-bottom:1px solid {sep};")
        cl = QHBoxLayout(ctrl_frame)
        cl.setContentsMargins(12, 6, 12, 6)
        cl.setSpacing(10)

        sort_lbl = QLabel("Sort:")
        sort_lbl.setStyleSheet(f"color:{dim};background:transparent;font-size:{font_sz(-1)}px;")

        self._sort_combo = QComboBox()
        self._sort_combo.addItems([
            "Size ↓ (largest first)",
            "Size ↑ (smallest first)",
            "Name A→Z",
            "Name Z→A",
            "Type (folders first)",
        ])
        self._sort_combo.setStyleSheet(
            f"QComboBox{{background:{bg3};border:1px solid {sep};border-radius:4px;"
            f"color:{fg};padding:3px 8px;font-size:{font_sz(-1)}px;}}"
            f"QComboBox::drop-down{{border:none;}}"
            f"QComboBox QAbstractItemView{{background:{bg2};color:{fg};"
            f"selection-background-color:{acc};}}"
        )
        self._sort_combo.currentIndexChanged.connect(self._on_sort_changed)

        filter_lbl = QLabel("Filter:")
        filter_lbl.setStyleSheet(f"color:{dim};background:transparent;font-size:{font_sz(-1)}px;")

        self._filter_edit = QLineEdit()
        self._filter_edit.setPlaceholderText("Filter results by name…")
        self._filter_edit.setStyleSheet(
            f"QLineEdit{{background:{bg3};border:1px solid {sep};border-radius:4px;"
            f"color:{fg};padding:3px 8px;font-size:{font_sz()}px;}}"
            f"QLineEdit:focus{{border-color:{acc};}}"
        )
        self._filter_edit.textChanged.connect(self._apply_filter)

        clear_btn = QPushButton("✕")
        clear_btn.setFixedSize(22, 22)
        clear_btn.setToolTip("Clear filter")
        clear_btn.setStyleSheet(
            f"QPushButton{{background:{bg3};border:1px solid {sep};border-radius:4px;"
            f"color:{dim};padding:0;font-size:{font_sz(-1)}px;}}"
            f"QPushButton:hover{{border-color:{acc};color:{fg};}}"
        )
        clear_btn.clicked.connect(self._filter_edit.clear)

        export_btn = QPushButton("📋 Copy CSV")
        export_btn.setFixedHeight(26)
        export_btn.setToolTip("Copy results as CSV to clipboard")
        export_btn.setStyleSheet(self._btn_ss(t))
        export_btn.clicked.connect(self._export_clipboard)

        cl.addWidget(sort_lbl)
        cl.addWidget(self._sort_combo)
        cl.addSpacing(12)
        cl.addWidget(filter_lbl)
        cl.addWidget(self._filter_edit, 1)
        cl.addWidget(clear_btn)
        cl.addSpacing(12)
        cl.addWidget(export_btn)

        self._list = QListWidget()
        self._list.setStyleSheet(
            f"QListWidget{{background:{bg3};border:1px solid {sep};border-radius:4px;"
            f"font-family:monospace;font-size:{font_sz(-1)}px;color:{fg};outline:none;}}"
            f"QListWidget::item{{padding:6px 8px;border-bottom:1px solid {sep};}}"
            f"QListWidget::item:selected{{background:{bg2};color:{acc};}}"
        )
        self._list.itemDoubleClicked.connect(self._on_item_double_clicked)
        self._list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._list.customContextMenuRequested.connect(self._show_context_menu)

        self._status = QLabel("Enter a directory and press Scan or F5.")
        self._status.setStyleSheet(f"color:{dim};font-size:{font_sz(-1)}px;padding:4px 12px;")

        self._legend = QLabel("")
        self._legend.setStyleSheet(f"color:{dim};font-size:{font_sz(-2)}px;padding:2px 12px;")

        bottom = QFrame()
        bottom.setStyleSheet(f"background:{bg2};border-top:1px solid {sep};")
        bl = QVBoxLayout(bottom)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.setSpacing(0)

        status_row = QFrame()
        status_row.setStyleSheet("background:transparent;")
        sr = QHBoxLayout(status_row)
        sr.setContentsMargins(0, 0, 12, 0)
        sr.addWidget(self._status)
        sr.addStretch()
        close_btn = QPushButton("Close")
        close_btn.setMinimumHeight(32)
        close_btn.setMinimumWidth(100)
        close_btn.setStyleSheet(self._btn_ss(t))
        close_btn.clicked.connect(self.accept)
        sr.addWidget(close_btn)

        bl.addWidget(status_row)
        bl.addWidget(self._legend)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(header)
        lay.addWidget(nav_frame)
        lay.addWidget(path_frame)
        lay.addWidget(self._disk_bar)
        lay.addWidget(self._progress)
        lay.addWidget(ctrl_frame)
        lay.addWidget(self._list, 1)
        lay.addWidget(bottom)

    @staticmethod
    def _btn_ss(t: dict, primary: bool = False) -> str:
        border = t["accent"] if primary else t["header_sep"]
        return (
            f"QPushButton{{background:{t['bg3']};border:1px solid {border};"
            f"border-radius:4px;color:{t['text']};padding:4px 14px;}}"
            f"QPushButton:hover{{background:{t['bg2']};border-color:{t['accent']};"
            f"color:{t['highlight']}}}"
            f"QPushButton:focus{{border-color:{t['accent']};color:{t['highlight']};"
            f"outline:none;}}"
            f"QPushButton:pressed{{background:{t['bg']};border-color:{t['accent2']};"
            f"color:{t['accent2']};}}"
            f"QPushButton:disabled{{color:{t['text_dim']};border-color:{t['bg3']};}}"
        )

    def _refresh_nav_buttons(self) -> None:
        self._back_btn.setEnabled(bool(self._nav_back))
        self._fwd_btn.setEnabled(bool(self._nav_forward))
        root = self._scan_root
        self._up_btn.setEnabled(root.parent != root)
        self._breadcrumb.setText(str(root))

    def _browse(self) -> None:
        path = QFileDialog.getExistingDirectory(
            self, "Select Directory", self._path_edit.text() or str(_HOME)
        )
        if path:
            self._path_edit.setText(path)

    def _nav_back_action(self) -> None:
        if not self._nav_back:
            return
        self._nav_forward.append(self._scan_root)
        dest = self._nav_back.pop()
        self._path_edit.setText(str(dest))
        self._start_scan(push_history=False)

    def _nav_forward_action(self) -> None:
        if not self._nav_forward:
            return
        self._nav_back.append(self._scan_root)
        dest = self._nav_forward.pop()
        self._path_edit.setText(str(dest))
        self._start_scan(push_history=False)

    def _nav_up(self) -> None:
        parent = self._scan_root.parent
        if parent == self._scan_root:
            return
        self._nav_back.append(self._scan_root)
        self._nav_forward.clear()
        self._path_edit.setText(str(parent))
        self._start_scan(push_history=False)

    def _drill_into(self, path: Path) -> None:
        if not path.is_dir():
            return
        self._nav_back.append(self._scan_root)
        self._nav_forward.clear()
        self._path_edit.setText(str(path))
        self._start_scan(push_history=False)

    def _start_scan(self, *, push_history: bool = True) -> None:
        if self._worker and self._worker.isRunning():
            self._cancel.set()
            self._worker.wait(500)
            try:
                self._worker.finished.disconnect()
                self._worker.progress.disconnect()
            except (RuntimeError, TypeError):
                pass

        raw  = self._path_edit.text().strip()
        root = Path(os.path.expandvars(os.path.expanduser(raw)))

        if not root.is_dir():
            QMessageBox.warning(self, "Invalid Directory", f"Not a directory:\n{root}")
            return

        if push_history and root != self._scan_root:
            self._nav_back.append(self._scan_root)
            self._nav_forward.clear()

        self._scan_root = root
        self._refresh_nav_buttons()

        self._disk_info = _get_disk_info(root)
        self._disk_bar.update_info(self._disk_info)

        self._cancel = threading.Event()
        self._filter_edit.blockSignals(True)
        self._filter_edit.clear()
        self._filter_edit.blockSignals(False)
        self._list.clear()
        self._results         = []
        self._skipped_mounts  = 0
        self._legend.setText("")
        self._progress.setVisible(True)
        self._scan_btn.setEnabled(False)
        self._status.setText("Scanning…")

        self._worker = _ScanWorker(root, self._cancel)
        self._worker.progress.connect(self._status.setText)
        self._worker.finished.connect(self._on_done)
        self._worker.start()

    def _on_done(self, results: list, elapsed: float, skipped_mounts: int) -> None:
        self._results        = results
        self._elapsed        = elapsed
        self._skipped_mounts = skipped_mounts
        self._progress.setVisible(False)
        self._scan_btn.setEnabled(True)
        self._apply_filter(self._filter_edit.text())

    def _sorted_results(self) -> list:
        r = list(self._results)
        m = self._sort_mode
        mounts = [x for x in r if x[_R_IS_MOUNT]]
        rest   = [x for x in r if not x[_R_IS_MOUNT]]
        if m == self._SORT_SIZE_DESC:
            rest.sort(key=lambda x: x[_R_SIZE], reverse=True)
        elif m == self._SORT_SIZE_ASC:
            rest.sort(key=lambda x: x[_R_SIZE])
        elif m == self._SORT_NAME_ASC:
            rest.sort(key=lambda x: x[_R_PATH].name.lower())
        elif m == self._SORT_NAME_DESC:
            rest.sort(key=lambda x: x[_R_PATH].name.lower(), reverse=True)
        elif m == self._SORT_TYPE:
            rest.sort(key=lambda x: (not x[_R_IS_DIR], x[_R_PATH].name.lower()))
        return rest + mounts

    def _apply_filter(self, text: str = "") -> None:
        if not self._results:
            return

        query = text.strip().lower()
        t = current_theme()
        fg = t["text"]
        acc = t["accent"]
        warn_col = "#ffb86c"

        sorted_r = self._sorted_results()

        disk_used = self._disk_info.get("used", 0)
        scan_total = sum(x[_R_SIZE] for x in sorted_r if not x[_R_IS_MOUNT])
        ref_total = scan_total

        self._list.clear()
        self._item_paths = []
        shown_total = 0
        shown_count = 0

        for entry in sorted_r:
            path = entry[_R_PATH]
            size = entry[_R_SIZE]
            is_dir = entry[_R_IS_DIR]
            file_count = entry[_R_FILE_COUNT]
            is_mount = entry[_R_IS_MOUNT]

            try:
                rel = path.relative_to(self._scan_root)
                display = str(rel)
            except ValueError:
                display = str(path)

            if query and query not in display.lower():
                continue

            if is_mount:
                icon = "💿 "
                line1 = f"{icon}{display}"
                line2 = "       [mount point — excluded from scan]"
                item = QListWidgetItem(f"{line1}\n{line2}")
                item.setForeground(QColor(warn_col))
                self._list.addItem(item)
                self._item_paths.append(path)
                continue

            bar_frac = size / max(ref_total, 1)
            bar = _bar(bar_frac)

            icon = "📁 " if is_dir else "📄 "
            size_str = _fmt_size(size)
            pct_scan = (size / max(ref_total, 1)) * 100

            count_str = f"  [{_fmt_count(file_count)} files]" if is_dir else ""

            line1 = f"{icon}{display}"
            line2 = (
                f"  {size_str:>10}   {pct_scan:5.1f}% of scan      {bar}{count_str}"
            )

            item = QListWidgetItem(f"{line1}\n{line2}")
            item.setForeground(QColor(acc if is_dir else fg))
            self._list.addItem(item)
            self._item_paths.append(path)
            shown_total += size
            shown_count += 1

        elapsed_str = f"  (Scan: {self._elapsed:.1f}s)" if self._elapsed else ""

        visible_mounts = sum(
            1 for x in sorted_r
            if x[_R_IS_MOUNT] and (not query or query in str(x[_R_PATH]).lower())
        )

        mount_note = ""
        if self._skipped_mounts or visible_mounts:
            total_excl = self._skipped_mounts + visible_mounts
            mount_note = f"  ·  ⚠ {total_excl} mount point(s) excluded"

        if query:
            self._status.setText(
                f"{shown_count} results for '{text}'  ·  "
                f"Shown: {_fmt_size(shown_total)}  ·  "
                f"Total entries: {len(self._results)}"
            )
        else:
            dir_count = sum(1 for x in self._results if x[_R_IS_DIR] and not x[_R_IS_MOUNT])
            file_count = sum(1 for x in self._results if not x[_R_IS_DIR])

            pct_of_disk = (shown_total / max(disk_used, 1)) * 100 if disk_used else 0
            self._status.setText(
                f"{shown_count} entries  ·  "
                f"{dir_count} folders · {file_count} files  ·  "
                f"Scan total: {_fmt_size(shown_total)} ({pct_of_disk:.1f}% of used disk space)"
                f"{mount_note}{elapsed_str}"
            )

        legend_parts = ["% = share of total scanned size", "Sizes = actual allocated blocks (same as `du -sh`)"]

        if self._skipped_mounts or visible_mounts:
            legend_parts.append(
                "💿 = mount point on different filesystem — excluded to avoid double-counting"
            )
        self._legend.setText("  ℹ  " + "  ·  ".join(legend_parts))

    def _on_sort_changed(self, index: int) -> None:
        self._sort_mode = index
        self._apply_filter(self._filter_edit.text())

    def _on_item_double_clicked(self, item: QListWidgetItem) -> None:
        row = self._list.row(item)
        if not (0 <= row < len(self._item_paths)):
            return
        path = self._item_paths[row]
        if path.is_dir():
            self._drill_into(path)
        else:
            try:
                subprocess.Popen(
                    ["xdg-open", str(path.parent)],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
            except OSError as exc:
                logger.warning("DiskAnalyzer xdg-open: %s", exc)

    def _show_context_menu(self, pos: QPoint) -> None:
        item = self._list.itemAt(pos)
        if item is None:
            return
        row = self._list.row(item)
        if not (0 <= row < len(self._item_paths)):
            return

        from PyQt6.QtWidgets import QMenu
        path   = self._item_paths[row]
        target = path if path.is_dir() else path.parent

        size_str = "?"
        for entry in self._results:
            if entry[_R_PATH] == path:
                size_str = _fmt_size(entry[_R_SIZE])
                break

        menu       = QMenu(self)
        act_drill  = menu.addAction("🔍  Scan this directory") if path.is_dir() else None
        act_fm     = menu.addAction("📁  Open in File Manager")
        act_term   = menu.addAction("🖥  Open Terminal here")
        menu.addSeparator()
        act_copy   = menu.addAction("📋  Copy path")
        act_size   = menu.addAction(f"📊  Size: {size_str}")
        act_size.setEnabled(False)

        global_pos = self._list.mapToGlobal(pos)
        if hasattr(global_pos, "toPoint"):
            global_pos = global_pos.toPoint()

        chosen = menu.exec(global_pos)
        if act_drill and chosen == act_drill:
            self._drill_into(path)
        elif chosen == act_fm:
            try:
                subprocess.Popen(
                    ["xdg-open", str(target)],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
            except OSError as exc:
                logger.warning("DiskAnalyzer xdg-open: %s", exc)
        elif chosen == act_term:
            self._open_terminal(target)
        elif chosen == act_copy:
            QApplication.clipboard().setText(str(path))

    def _open_terminal(self, path: Path) -> None:
        import shlex
        p          = str(path)
        candidates = [
            ("gnome-terminal", ["gnome-terminal", "--working-directory", p]),
            ("konsole",        ["konsole", "--workdir", p]),
            ("xfce4-terminal", ["xfce4-terminal", "--working-directory", p]),
            ("alacritty",      ["alacritty", "--working-directory", p]),
            ("kitty",          ["kitty", "--directory", p]),
            ("foot",           ["foot", "--working-directory", p]),
            ("wezterm",        ["wezterm", "start", "--cwd", p]),
            ("xterm",          ["xterm", "-e", f"bash -c 'cd {shlex.quote(p)} && exec $SHELL'"]),
        ]
        for term, cmd in candidates:
            if shutil.which(term):
                try:
                    subprocess.Popen(
                        cmd, start_new_session=True,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    )
                    return
                except OSError as exc:
                    logger.warning("DiskAnalyzer terminal %s: %s", term, exc)
        QMessageBox.information(
            self, "No terminal found",
            "No supported terminal emulator found.\n"
            "Please install one (e.g. gnome-terminal, konsole, alacritty, xterm).",
        )

    def _export_clipboard(self) -> None:
        if not self._results:
            return
        buf = io.StringIO()
        w   = csv.writer(buf)
        w.writerow(["Name", "Path", "Size (bytes)", "Size (human)", "Type", "Files"])
        for entry in self._sorted_results():
            path       = entry[_R_PATH]
            size       = entry[_R_SIZE]
            is_dir     = entry[_R_IS_DIR]
            file_count = entry[_R_FILE_COUNT]
            is_mount   = entry[_R_IS_MOUNT]
            try:
                name = str(path.relative_to(self._scan_root))
            except ValueError:
                name = str(path)
            kind = "mount" if is_mount else ("dir" if is_dir else "file")
            w.writerow([name, str(path), size, _fmt_size(size), kind, file_count])
        QApplication.clipboard().setText(buf.getvalue())
        self._status.setText("✓ Results copied to clipboard as CSV")

    def closeEvent(self, event: QCloseEvent) -> None:
        self._cancel.set()
        if self._worker and self._worker.isRunning():
            self._worker.wait(800)
        super().closeEvent(event)
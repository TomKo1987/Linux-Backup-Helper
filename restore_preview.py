import html as _html
import os
import threading
from pathlib import Path

from PyQt6.QtCore import Qt, QThread, pyqtSignal, QSortFilterProxyModel
from PyQt6.QtGui import QColor, QStandardItemModel, QStandardItem
from PyQt6.QtWidgets import (
    QDialog, QFrame, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QMessageBox, QProgressBar, QPushButton, QSplitter,
    QTabWidget, QTableView, QTextEdit, QVBoxLayout, QWidget,
    QAbstractItemView, QApplication, QHeaderView, QSizePolicy, QStackedWidget,
)

from copy_worker import _SKIP_RE
from dotfiles_manager import _colored_diff_html
from state import S
from themes import current_theme, font_sz
from ui_utils import _StandardKeysMixin

__all__ = ["RestorePreviewDialog"]

_DIFF_MAX_BYTES = 2 * 1024 * 1024
_BINARY_PROBE_BYTES = 8192


def _looks_binary(path: Path) -> bool:
    try:
        with open(path, "rb") as fh:
            chunk = fh.read(_BINARY_PROBE_BYTES)
        return b"\x00" in chunk
    except OSError:
        return True


def _read_text_safe(path: Path) -> list[str] | None:
    try:
        if path.stat().st_size > _DIFF_MAX_BYTES:
            return None
        if _looks_binary(path):
            return None
        return path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None


class _RestorePreviewWorker(QThread):
    progress   = pyqtSignal(int, int)
    entry_done = pyqtSignal(dict)
    finished   = pyqtSignal()

    def __init__(self, tasks: list[tuple[list[str], list[str], str, dict]]) -> None:
        super().__init__()
        self._tasks  = tasks
        self._cancel = threading.Event()

    def cancel(self) -> None:
        self._cancel.set()

    def run(self) -> None:
        total = len(self._tasks)
        for idx, task in enumerate(self._tasks):
            if self._cancel.is_set():
                break
            backup_sources, local_destinations, title = task[0], task[1], task[2]
            excludes = task[3] if len(task) > 3 else {}
            self.progress.emit(idx, total)
            self.entry_done.emit(self._analyse(backup_sources, local_destinations, title, excludes))
        self.progress.emit(total, total)
        self.finished.emit()

    def _analyse(self, backup_roots: list[str], local_roots: list[str], title: str,
                 excludes: dict | set | frozenset | None = None) -> dict:

        to_create:  list[tuple[str, str]] = []
        to_overwrite: list[tuple[str, str]] = []
        unchanged:  list[str] = []
        errors:     list[tuple[str, str]] = []
        overwrite_paths: dict[str, tuple[str, str]] = {}

        if not backup_roots or not local_roots:
            return dict(title=title, to_create=to_create, to_overwrite=to_overwrite,
                        unchanged=unchanged, errors=errors, src_total=0, _paths=overwrite_paths)

        if len(backup_roots) != len(local_roots):
            errors.append((
                title,
                f"Source/destination count mismatch ({len(backup_roots)} backup path(s) vs "
                f"{len(local_roots)} local path(s)) — extra entries were not checked",
            ))

        for backup_root, local_root in zip(backup_roots, local_roots):
            if self._cancel.is_set():
                break
            if not backup_root or not local_root:
                continue

            backup_p = Path(backup_root).expanduser()
            local_p  = Path(local_root).expanduser()

            if not backup_p.exists():
                errors.append((backup_root, "Backup source path does not exist"))
                continue

            backup_abs = str(backup_p.resolve())
            if isinstance(excludes, dict):
                excl_names = excludes.get(backup_abs) or excludes.get(backup_root) or excludes.get(str(backup_p)) or []
                excl_set = {os.path.join(backup_abs, n) for n in excl_names}
            elif isinstance(excludes, (set, frozenset, list, tuple)):
                excl_set = set(excludes)
            else:
                excl_set = set()

            for dirpath, dirs, files in os.walk(backup_p, followlinks=False):
                if self._cancel.is_set():
                    break
                dirpath_abs = str(Path(dirpath).resolve())
                dirs[:] = [
                    d for d in dirs
                    if not _SKIP_RE.search(d)
                    and os.path.join(dirpath_abs, d) not in excl_set
                ]
                for fname in files:
                    if _SKIP_RE.search(fname):
                        continue
                    backup_file = Path(dirpath) / fname
                    if str(backup_file.resolve()) in excl_set:
                        continue
                    try:
                        rel = backup_file.relative_to(backup_p)
                    except ValueError:
                        continue
                    local_file = local_p / rel
                    try:
                        backup_stat = backup_file.stat()
                    except OSError as e:
                        errors.append((str(rel), str(e)))
                        continue

                    if not local_file.exists():
                        to_create.append((str(rel), "new"))
                    else:
                        try:
                            local_stat = local_file.stat()
                            if (backup_stat.st_size != local_stat.st_size
                                    or abs(backup_stat.st_mtime_ns - local_stat.st_mtime_ns) > 2_000_000_000):
                                to_overwrite.append((str(rel), "modified"))
                                overwrite_paths[str(rel)] = (str(local_file), str(backup_file))
                            else:
                                unchanged.append(str(rel))
                        except OSError as e:
                            errors.append((str(rel), str(e)))

        return dict(
            title=title,
            to_create=to_create,
            to_overwrite=to_overwrite,
            unchanged=unchanged,
            errors=errors,
            src_total=len(to_create) + len(to_overwrite) + len(unchanged),
            _paths=overwrite_paths,
        )


class _SearchableList(QWidget):
    def __init__(self, items: list[str], color: str, parent=None) -> None:
        super().__init__(parent)
        t   = current_theme()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 4, 0, 0)
        lay.setSpacing(4)

        self._search = QLineEdit()
        self._search.setPlaceholderText("🔍  Filter…")
        self._search.setClearButtonEnabled(True)
        self._search.setStyleSheet(
            f"QLineEdit{{background:{t['bg2']};color:{t['text']};"
            f"border:1px solid {t['header_sep']};border-radius:4px;"
            f"padding:3px 8px;font-size:{font_sz(-1)}px;}}"
            f"QLineEdit:focus{{border-color:{t['accent']};}}"
        )
        lay.addWidget(self._search)

        self._list = QListWidget()
        self._list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._list.setStyleSheet(
            f"QListWidget{{background:{t['bg3']};border:1px solid {t['header_sep']};"
            f"color:{color};font-family:monospace;font-size:{font_sz(-2)}px;}}"
            f"QListWidget::item:selected{{background:{t['accent']}22;color:{t['text']};}}"
            f"QListWidget::item:hover{{background:{t['bg2']};}}"
        )
        self._all_items = items
        self._list.addItems(items)
        lay.addWidget(self._list, 1)

        count_style = f"color:{t['text_dim']};font-size:{font_sz(-2)}px;"
        self._count_lbl = QLabel(f"{len(items):,} items")
        self._count_lbl.setStyleSheet(count_style)
        lay.addWidget(self._count_lbl)

        self._search.textChanged.connect(self._filter)

    def _filter(self, text: str) -> None:
        needle = text.strip().lower()
        self._list.clear()
        hits = [s for s in self._all_items if needle in s.lower()] if needle else self._all_items
        self._list.addItems(hits)
        n_total = len(self._all_items)
        n_shown = len(hits)
        suffix = f" (showing {n_shown:,} of {n_total:,})" if needle else ""
        self._count_lbl.setText(f"{n_total:,} items{suffix}")

    def update_items(self, items: list[str]) -> None:
        self._all_items = items
        self._list.clear()
        self._list.addItems(items)
        self._count_lbl.setText(f"{len(items):,} items")
        current_filter = self._search.text()
        if current_filter:
            self._filter(current_filter)


class _OverwriteDiffPanel(QWidget):

    def __init__(self, overwrite: list[tuple[str, str]], paths: dict[str, tuple[str, str]], parent=None) -> None:
        super().__init__(parent)
        self._paths = paths
        t   = current_theme()
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 4, 0, 0)
        lay.setSpacing(6)

        splitter = QSplitter(Qt.Orientation.Horizontal)

        left = QWidget()
        left_lay = QVBoxLayout(left)
        left_lay.setContentsMargins(0, 0, 0, 0)
        left_lay.setSpacing(4)

        self._search = QLineEdit()
        self._search.setPlaceholderText("🔍  Filter…")
        self._search.setClearButtonEnabled(True)
        self._search.setStyleSheet(
            f"QLineEdit{{background:{t['bg2']};color:{t['text']};"
            f"border:1px solid {t['header_sep']};border-radius:4px;"
            f"padding:3px 8px;font-size:{font_sz(-1)}px;}}"
            f"QLineEdit:focus{{border-color:{t['accent']};}}"
        )
        self._search.textChanged.connect(self._filter)
        left_lay.addWidget(self._search)

        self._list = QListWidget()
        self._list.setStyleSheet(
            f"QListWidget{{background:{t['bg3']};border:1px solid {t['header_sep']};"
            f"color:{t['warning']};font-family:monospace;font-size:{font_sz(-2)}px;}}"
            f"QListWidget::item:selected{{background:{t['accent']}33;color:{t['text']};}}"
            f"QListWidget::item:hover{{background:{t['bg2']};}}"
        )
        self._all_rel = [rel for rel, _reason in overwrite]
        self._list.addItems(self._all_rel)
        self._list.currentTextChanged.connect(self._on_select)
        left_lay.addWidget(self._list, 1)

        self._count_lbl = QLabel(f"{len(self._all_rel):,} files would be overwritten")
        self._count_lbl.setStyleSheet(f"color:{t['text_dim']};font-size:{font_sz(-2)}px;")
        left_lay.addWidget(self._count_lbl)

        splitter.addWidget(left)

        right = QWidget()
        right_lay = QVBoxLayout(right)
        right_lay.setContentsMargins(0, 0, 0, 0)
        right_lay.setSpacing(4)

        self._diff_hdr = QLabel("Select a file on the left to preview the diff")
        self._diff_hdr.setStyleSheet(f"color:{t['text_dim']};font-size:{font_sz(-1)}px;")
        right_lay.addWidget(self._diff_hdr)

        self._diff_view = QTextEdit()
        self._diff_view.setReadOnly(True)
        self._diff_view.setStyleSheet(
            f"QTextEdit{{background:{t['bg']};border:1px solid {t['header_sep']};border-radius:4px;}}"
        )
        right_lay.addWidget(self._diff_view, 1)

        splitter.addWidget(right)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 2)
        lay.addWidget(splitter)

    def _filter(self, text: str) -> None:
        needle = text.strip().lower()
        self._list.clear()
        hits = [s for s in self._all_rel if needle in s.lower()] if needle else self._all_rel
        self._list.addItems(hits)

    def _on_select(self, rel: str) -> None:
        if not rel:
            return
        pair = self._paths.get(rel)
        t = current_theme()
        if not pair:
            self._diff_hdr.setText(rel)
            self._diff_view.setHtml(
                f"<p style='color:{t['text_dim']};'>No diff available for this entry.</p>")
            return
        local_path, backup_path = pair
        self._diff_hdr.setText(f"{rel}   —   local file would be overwritten by backup")

        local_lines  = _read_text_safe(Path(local_path))
        backup_lines = _read_text_safe(Path(backup_path))

        if local_lines is None or backup_lines is None:
            self._diff_view.setHtml(
                f"<p style='color:{t['text_dim']};font-family:monospace;'>"
                f"Binary or large file — diff preview not available.<br>"
                f"Local: {_html.escape(local_path)}<br>"
                f"Backup: {_html.escape(backup_path)}</p>"
            )
            return

        self._diff_view.setHtml(_colored_diff_html(backup_lines, local_lines, t))


class _EntryTabWidget(QWidget):

    def __init__(self, result: dict, parent=None) -> None:
        super().__init__(parent)
        t           = current_theme()
        to_create   = result["to_create"]
        to_overwrite = result["to_overwrite"]
        unchanged   = result["unchanged"]
        errors      = result["errors"]

        n_err = len(errors)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 8, 10, 8)
        lay.setSpacing(0)

        chip_row = QHBoxLayout()
        chip_row.setContentsMargins(0, 0, 0, 0)
        chip_row.setSpacing(0)

        self._chips: list[QPushButton] = []
        self._chip_colors: list[str]   = []
        chip_defs = [
            (f"🆕  Would create  {len(to_create):,}",      t["info"]    if to_create else t["text_dim"]),
            (f"⚠  Would overwrite  {len(to_overwrite):,}", t["warning"] if to_overwrite else t["text_dim"]),
            (f"✓  Unchanged  {len(unchanged):,}",           t["success"]),
            (f"✗  Errors  {n_err:,}",                       t["error"]   if n_err   else t["text_dim"]),
        ]
        for i, (label, color) in enumerate(chip_defs):
            btn = QPushButton(label)
            btn.setCheckable(False)
            btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            self._chips.append(btn)
            self._chip_colors.append(color)
            chip_row.addWidget(btn)

            def _make_cb(idx: int):
                return lambda: self._switch(idx)
            btn.clicked.connect(_make_cb(i))

        chip_row.addStretch()
        lay.addLayout(chip_row)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"background:{t['header_sep']};border:none;")
        sep.setFixedHeight(1)
        lay.addWidget(sep)

        self._stack = QStackedWidget()
        create_items = [f"[{reason}]  {rel}" for rel, reason in to_create]
        self._stack.addWidget(_SearchableList(create_items, t["text"]))
        self._stack.addWidget(_OverwriteDiffPanel(to_overwrite, result.get("_paths", {})))
        self._stack.addWidget(_SearchableList(unchanged, t["text_dim"]))
        err_items = [f"{rel}  →  {msg}" for rel, msg in errors]
        self._stack.addWidget(_SearchableList(err_items, t["error"]))
        lay.addWidget(self._stack, 1)

        self._active_idx = -1
        self._switch(3 if n_err else (1 if to_overwrite else 0))

    def _switch(self, idx: int) -> None:
        if idx == self._active_idx:
            return
        self._active_idx = idx
        self._stack.setCurrentIndex(idx)
        t = current_theme()
        for i, (btn, color) in enumerate(zip(self._chips, self._chip_colors)):
            if i == idx:
                btn.setStyleSheet(
                    f"QPushButton{{color:{color};font-size:{font_sz(-1)}px;font-weight:bold;"
                    f"background:{t['bg2']};border:1px solid {t['header_sep']};"
                    f"border-bottom:2px solid {color};"
                    f"border-radius:4px 4px 0 0;padding:5px 16px;}}"
                    f"QPushButton:hover{{background:{t['bg2']};}}"
                )
            else:
                btn.setStyleSheet(
                    f"QPushButton{{color:{t['text_dim']};font-size:{font_sz(-1)}px;font-weight:normal;"
                    f"background:{t['bg3']};border:1px solid {t['header_sep']};"
                    f"border-bottom:1px solid {t['header_sep']};"
                    f"border-radius:4px 4px 0 0;padding:5px 16px;}}"
                    f"QPushButton:hover{{color:{t['text']};background:{t['bg2']};}}"
                )


class _OverviewTab(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        t   = current_theme()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.setSpacing(6)

        self._model = QStandardItemModel(0, 5)
        self._model.setHorizontalHeaderLabels(["Entry", "Would create", "Would overwrite", "Unchanged", "Errors"])

        self._proxy = QSortFilterProxyModel()
        self._proxy.setSourceModel(self._model)
        self._proxy.setFilterCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self._proxy.setFilterKeyColumn(0)

        self._search = QLineEdit()
        self._search.setPlaceholderText("🔍  Filter entries…")
        self._search.setClearButtonEnabled(True)
        self._search.setStyleSheet(
            f"QLineEdit{{background:{t['bg2']};color:{t['text']};"
            f"border:1px solid {t['header_sep']};border-radius:4px;"
            f"padding:3px 8px;font-size:{font_sz(-1)}px;}}"
            f"QLineEdit:focus{{border-color:{t['accent']};}}"
        )
        self._search.textChanged.connect(self._proxy.setFilterFixedString)
        lay.addWidget(self._search)

        self._table = QTableView()
        self._table.setModel(self._proxy)
        self._table.setSortingEnabled(True)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setAlternatingRowColors(True)
        v_header = self._table.verticalHeader()
        if v_header is not None:
            v_header.setVisible(False)
        h_header = self._table.horizontalHeader()
        if h_header is None:
            return
        h_header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for col in (1, 2, 3, 4):
            h_header.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        self._table.setStyleSheet(
            f"QTableView{{background:{t['bg3']};alternate-background-color:{t['bg2']};"
            f"color:{t['text']};border:1px solid {t['header_sep']};gridline-color:{t['header_sep']};"
            f"font-size:{font_sz(-1)}px;}}"
            f"QTableView::item:selected{{background:{t['accent']}33;}}"
            f"QHeaderView::section{{background:{t['bg2']};color:{t['text_dim']};"
            f"font-size:{font_sz(-2)}px;font-weight:bold;padding:4px 8px;"
            f"border:none;border-bottom:1px solid {t['header_sep']};}}"
        )
        lay.addWidget(self._table, 1)

        self._count_lbl = QLabel("")
        self._count_lbl.setStyleSheet(f"color:{t['text_dim']};font-size:{font_sz(-2)}px;")
        lay.addWidget(self._count_lbl)

    def add_result(self, result: dict) -> None:
        t = current_theme()
        title        = result["title"].replace("<br>", " · ")
        n_create     = len(result["to_create"])
        n_overwrite  = len(result["to_overwrite"])
        n_unchanged  = len(result["unchanged"])
        n_err        = len(result["errors"])

        def _item(text: str, color: str | None = None,
                  align: Qt.AlignmentFlag = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                  numeric: bool = False) -> QStandardItem:
            it = QStandardItem()
            it.setText(text)
            if color:
                it.setForeground(QColor(color))
            it.setTextAlignment(align)
            if numeric:
                it.setData(int(text.replace(",", "")), Qt.ItemDataRole.UserRole)
            it.setFlags(it.flags() & ~Qt.ItemFlag.ItemIsEditable)
            return it

        center = Qt.AlignmentFlag.AlignCenter | Qt.AlignmentFlag.AlignVCenter

        col_create    = t["info"]    if n_create    else t["text_dim"]
        col_overwrite = t["warning"] if n_overwrite else t["text_dim"]
        col_err       = t["error"]   if n_err       else t["text_dim"]
        row_color     = t["error"] if n_err else (t["warning"] if n_overwrite else (t["info"] if n_create else t["success"]))

        row = [
            _item(title, row_color),
            _item(f"{n_create:,}",    col_create,    center, numeric=True),
            _item(f"{n_overwrite:,}", col_overwrite, center, numeric=True),
            _item(f"{n_unchanged:,}", t["success"],  center, numeric=True),
            _item(f"{n_err:,}",       col_err,       center, numeric=True),
        ]
        self._model.appendRow(row)
        self._update_count()

    def _update_count(self) -> None:
        self._count_lbl.setText(f"{self._model.rowCount():,} entries total")

    def clear(self) -> None:
        self._model.removeRows(0, self._model.rowCount())


# noinspection PyUnresolvedReferences
class RestorePreviewDialog(_StandardKeysMixin, QDialog):

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("🛟  Restore Preview")
        screen = QApplication.primaryScreen()
        geo    = screen.availableGeometry() if screen else None
        if geo:
            self.setMinimumSize(
                min(1600, int(geo.width() * 0.85)),
                min(900,  int(geo.height() * 0.85)),
            )
        else:
            self.setMinimumSize(1200, 700)

        self._worker: _RestorePreviewWorker | None = None
        self._results: list[dict] = []
        self._build()

    def closeEvent(self, event) -> None:
        if self._worker and self._worker.isRunning():
            self._worker.cancel()
            self._worker.wait(3000)
        event.accept()

    def _build(self) -> None:
        t   = current_theme()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(10)

        hdr = QLabel("🛟  Restore Preview — Preview only, nothing will be changed")
        hdr.setStyleSheet(
            f"font-size:{font_sz(3)}px;font-weight:bold;color:{t['accent']};"
        )
        lay.addWidget(hdr)

        info = QLabel(
            "Scans your backup destinations and compares them with the original local paths, "
            "as if a restore were run now. Files marked <b>would create</b> don't exist locally yet. "
            "Files marked <b>would overwrite</b> already exist locally and differ — select one to see a diff."
        )
        info.setTextFormat(Qt.TextFormat.RichText)
        info.setWordWrap(True)
        info.setStyleSheet(f"color:{t['text_dim']};font-size:{font_sz(-1)}px;")
        lay.addWidget(info)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"background:{t['header_sep']};border:none;")
        sep.setFixedHeight(1)
        lay.addWidget(sep)

        prog_row = QHBoxLayout()
        self._prog_label = QLabel("Press  ▶ Start Scan  to begin…")
        self._prog_label.setStyleSheet(f"color:{t['text_dim']};font-size:{font_sz(-1)}px;")
        self._prog_bar = QProgressBar()
        self._prog_bar.setRange(0, 100)
        self._prog_bar.setValue(0)
        self._prog_bar.setFixedHeight(8)
        self._prog_bar.setTextVisible(False)
        self._prog_bar.setStyleSheet(
            f"QProgressBar{{background:{t['pb_bg']};border-radius:4px;border:none;}}"
            f"QProgressBar::chunk{{background:{t['pb_chunk']};border-radius:4px;}}"
        )
        self._prog_bar.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        prog_row.addWidget(self._prog_label)
        prog_row.addWidget(self._prog_bar, 1)
        lay.addLayout(prog_row)

        summ_row = QHBoxLayout()
        summ_row.setSpacing(8)
        self._lbl_create    = self._stat_lbl("—  would create",    t["info"])
        self._lbl_overwrite = self._stat_lbl("—  would overwrite", t["warning"])
        self._lbl_unchanged = self._stat_lbl("—  unchanged",       t["success"])
        self._lbl_error     = self._stat_lbl("—  errors",          t["error"])
        for w in (self._lbl_create, self._lbl_overwrite, self._lbl_unchanged, self._lbl_error):
            summ_row.addWidget(w)
        summ_row.addStretch()
        lay.addLayout(summ_row)

        sep2 = QFrame()
        sep2.setFrameShape(QFrame.Shape.HLine)
        sep2.setStyleSheet(f"background:{t['header_sep']};border:none;")
        sep2.setFixedHeight(1)
        lay.addWidget(sep2)

        self._tabs = QTabWidget()
        self._tabs.setObjectName("RestorePreviewTabs")
        self._tabs.setMovable(False)
        self._tabs.setDocumentMode(False)
        self._tabs.setStyleSheet(
            f"QTabWidget#RestorePreviewTabs::pane{{"
            f"border:1px solid {t['header_sep']};background:{t['bg2']};"
            f"border-radius:0 4px 4px 4px;}}"
            f"QTabWidget#RestorePreviewTabs QTabBar::tab{{"
            f"background:{t['bg3']};color:{t['text_dim']};"
            f"font-size:{font_sz(-1)}px;font-weight:normal;"
            f"border:1px solid {t['header_sep']};border-bottom:none;"
            f"border-radius:4px 4px 0 0;"
            f"padding:6px 14px;margin-right:2px;}}"
            f"QTabWidget#RestorePreviewTabs QTabBar::tab:selected{{"
            f"background:{t['bg2']};color:{t['text']};font-weight:bold;"
            f"border-bottom:2px solid {t['accent']};}}"
            f"QTabWidget#RestorePreviewTabs QTabBar::tab:hover:!selected{{"
            f"background:{t['bg2']};color:{t['text']};}}"
            f"QTabWidget#RestorePreviewTabs QTabBar::scroller{{width:24px;}}"
        )
        lay.addWidget(self._tabs, 1)

        self._overview = _OverviewTab()
        self._tabs.addTab(self._overview, "📊  Overview")

        btn_row = QHBoxLayout()
        self._start_btn = QPushButton("▶  Start Scan")
        self._start_btn.setMinimumHeight(34)
        self._start_btn.clicked.connect(self._start)
        self._cancel_btn = QPushButton("⏹  Cancel")
        self._cancel_btn.setMinimumHeight(34)
        self._cancel_btn.setEnabled(False)
        self._cancel_btn.clicked.connect(self._cancel)
        close_btn = QPushButton("Close")
        close_btn.setMinimumHeight(34)
        close_btn.clicked.connect(self.accept)
        btn_row.addWidget(self._start_btn)
        btn_row.addWidget(self._cancel_btn)
        btn_row.addStretch()
        btn_row.addWidget(close_btn)
        lay.addLayout(btn_row)

    @staticmethod
    def _stat_lbl(text: str, color: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setStyleSheet(
            f"color:{color};font-size:{font_sz(1)}px;font-weight:bold;padding:4px 6px;"
        )
        return lbl

    def _start(self) -> None:
        tasks: list[tuple[list[str], list[str], str, dict]] = []
        for e in S.entries:
            details = e.get("details", {})
            if details.get("no_restore"):
                continue

            backup_src = e.get("destination", [])
            local_dst  = e.get("source", [])
            if isinstance(backup_src, str):
                backup_src = [backup_src]
            if isinstance(local_dst, str):
                local_dst = [local_dst]
            if backup_src and local_dst:
                tasks.append((backup_src, local_dst, e.get("title", "?"), details.get("exclude_paths", {})))

        if not tasks:
            QMessageBox.information(self, "Restore Preview", "No backup entries configured.")
            return

        from drive_utils import check_drives_to_mount, mount_required_drives
        all_paths: list[str] = []
        for src_list, dst_list, _title, _excl in tasks:
            all_paths.extend(src_list)
            all_paths.extend(dst_list)
        needed = check_drives_to_mount(all_paths)
        if needed and not mount_required_drives(needed, parent=self):
            return

        self._results.clear()
        while self._tabs.count() > 1:
            self._tabs.removeTab(1)
        self._overview.clear()
        self._tabs.setCurrentIndex(0)

        self._prog_bar.setValue(0)
        self._prog_bar.setRange(0, len(tasks))
        self._start_btn.setEnabled(False)
        self._cancel_btn.setEnabled(True)
        self._prog_label.setText(f"Scanning 0 / {len(tasks)} …")
        self._lbl_create.setText("Scanning…")
        self._lbl_overwrite.setText("")
        self._lbl_unchanged.setText("")
        self._lbl_error.setText("")

        self._worker = _RestorePreviewWorker(tasks)
        self._worker.progress.connect(self._on_progress)
        self._worker.entry_done.connect(self._on_entry_done)
        self._worker.finished.connect(self._on_finished)
        self._worker.start()

    def _cancel(self) -> None:
        if self._worker:
            self._worker.cancel()
        self._cancel_btn.setEnabled(False)
        self._prog_label.setText("Cancelling…")

    def _on_progress(self, done: int, total: int) -> None:
        self._prog_bar.setRange(0, max(total, 1))
        self._prog_bar.setValue(done)
        self._prog_label.setText(f"Scanning {done} / {total} …")

    def _on_entry_done(self, result: dict) -> None:
        self._results.append(result)
        self._add_entry_tab(result)
        self._overview.add_result(result)
        self._update_totals()

    def _on_finished(self) -> None:
        self._prog_label.setText(f"Scan complete — {len(self._results):,} entries checked.")
        self._prog_bar.setValue(self._prog_bar.maximum())
        self._start_btn.setEnabled(True)
        self._cancel_btn.setEnabled(False)
        for i, r in enumerate(self._results):
            if r["errors"]:
                self._tabs.setCurrentIndex(i + 1)
                break

    def _add_entry_tab(self, result: dict) -> None:
        t            = current_theme()
        title        = result["title"].replace("<br>", "\n")
        n_create     = len(result["to_create"])
        n_overwrite  = len(result["to_overwrite"])
        n_err        = len(result["errors"])

        widget = _EntryTabWidget(result)
        self._tabs.addTab(widget, title)
        idx = self._tabs.count() - 1

        if n_err:
            self._tabs.tabBar().setTabTextColor(idx, QColor(t["error"]))   # type: ignore[attr-defined]
        elif n_overwrite:
            self._tabs.tabBar().setTabTextColor(idx, QColor(t["warning"])) # type: ignore[attr-defined]
        elif n_create:
            self._tabs.tabBar().setTabTextColor(idx, QColor(t["info"]))    # type: ignore[attr-defined]
        else:
            self._tabs.tabBar().setTabTextColor(idx, QColor(t["success"])) # type: ignore[attr-defined]

        tip = f"{n_create:,} would create  |  {n_overwrite:,} would overwrite  |  {n_err:,} errors"
        self._tabs.tabBar().setTabToolTip(idx, tip)

    def _update_totals(self) -> None:
        t              = current_theme()
        total_create    = sum(len(r["to_create"])    for r in self._results)
        total_overwrite = sum(len(r["to_overwrite"]) for r in self._results)
        total_unchanged = sum(len(r["unchanged"])    for r in self._results)
        total_error     = sum(len(r["errors"])       for r in self._results)

        def _upd(lbl: QLabel, text: str, color: str) -> None:
            lbl.setText(text)
            lbl.setStyleSheet(f"color:{color};font-size:{font_sz(1)}px;font-weight:bold;padding:4px 6px;")

        _upd(self._lbl_create,    f"{total_create:,}  would create",     t["info"])
        _upd(self._lbl_overwrite, f"{total_overwrite:,}  would overwrite", t["warning"])
        _upd(self._lbl_unchanged, f"{total_unchanged:,}  unchanged",      t["success"])
        _upd(self._lbl_error,     f"{total_error:,}  errors",             t["error"])

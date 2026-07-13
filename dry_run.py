import os
import threading
from pathlib import Path

from PyQt6.QtCore import Qt, QThread, pyqtSignal, QSortFilterProxyModel
from PyQt6.QtGui import QColor, QStandardItemModel, QStandardItem
from PyQt6.QtWidgets import (
    QDialog, QFrame, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QMessageBox, QProgressBar, QPushButton, QTabWidget, QTableView,
    QVBoxLayout, QWidget, QAbstractItemView, QHeaderView,
    QSizePolicy, QStackedWidget,
)

from copy_worker import _SKIP_RE
from state import S
from themes import current_theme, font_sz
from ui_utils import _StandardKeysMixin, size_to_screen

__all__ = ["DryRunDialog", "DryRunModeDialog", "launch_dry_run"]


class _DryRunWorker(QThread):
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
            sources, destinations, title = task[0], task[1], task[2]
            excludes = task[3] if len(task) > 3 else {}
            self.progress.emit(idx, total)
            self.entry_done.emit(self._analyse(sources, destinations, title, excludes))
        self.progress.emit(total, total)
        self.finished.emit()

    def _analyse(self, sources: list[str], destinations: list[str], title: str, excludes: dict | set | frozenset | None = None) -> dict:
        to_copy: list[tuple[str, str]] = []
        to_skip: list[str] = []
        errors: list[tuple[str, str]] = []

        if not sources or not destinations:
            return dict(title=title, to_copy=to_copy,
                        to_skip=to_skip, errors=errors, src_total=0)

        if len(sources) != len(destinations):
            errors.append((
                title,
                f"Source/destination count mismatch ({len(sources)} source(s) vs "
                f"{len(destinations)} destination(s)) — extra entries were not checked",
            ))

        for src_root, dst_root in zip(sources, destinations):
            if self._cancel.is_set():
                break
            if not src_root or not dst_root:
                continue

            src_p = Path(src_root).expanduser()
            dst_p = Path(dst_root).expanduser()

            if not src_p.exists():
                errors.append((src_root, "Source path does not exist"))
                continue

            src_abs = str(src_p.resolve())
            if isinstance(excludes, dict):
                excl_names = excludes.get(src_abs) or excludes.get(src_root) or excludes.get(str(src_p)) or []
                excl_set = {os.path.join(src_abs, n) for n in excl_names}
            elif isinstance(excludes, (set, frozenset, list, tuple)):
                excl_set = set(excludes)
            else:
                excl_set = set()

            for dirpath, dirs, files in os.walk(src_p, followlinks=False):
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
                    src_file = Path(dirpath) / fname
                    src_file_str = str(src_file)
                    if src_file_str in excl_set or str(src_file.resolve()) in excl_set:
                        continue
                    try:
                        rel = src_file.relative_to(src_p)
                    except ValueError:
                        continue
                    dst_file = dst_p / rel

                    if os.path.islink(src_file_str):
                        try:
                            target = os.readlink(src_file_str)
                        except OSError as e:
                            errors.append((str(rel), str(e)))
                            continue

                        dst_file_str = str(dst_file)
                        if not os.path.lexists(dst_file_str):
                            to_copy.append((str(rel), "new"))
                        elif os.path.islink(dst_file_str) and os.readlink(dst_file_str) == target:
                            to_skip.append(str(rel))
                        else:
                            to_copy.append((str(rel), "modified"))
                        continue

                    try:
                        src_stat = src_file.stat()
                    except OSError as e:
                        errors.append((str(rel), str(e)))
                        continue

                    if not dst_file.exists():
                        to_copy.append((str(rel), "new"))
                    else:
                        try:
                            dst_stat = dst_file.stat()
                            if (src_stat.st_size != dst_stat.st_size
                                    or abs(src_stat.st_mtime_ns - dst_stat.st_mtime_ns) > 2_000_000_000):
                                to_copy.append((str(rel), "modified"))
                            else:
                                to_skip.append(str(rel))
                        except OSError as e:
                            errors.append((str(rel), str(e)))

        return dict(
            title=title,
            to_copy=to_copy,
            to_skip=to_skip,
            errors=errors,
            src_total=len(to_copy) + len(to_skip),
        )


def _style_chip_tabs(chips: list[QPushButton], colors: list[str], active_idx: int) -> None:
    t = current_theme()
    for i, (btn, color) in enumerate(zip(chips, colors)):
        if i == active_idx:
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


class _EntryTabWidget(QWidget):

    def __init__(self, result: dict, parent=None) -> None:
        super().__init__(parent)
        t       = current_theme()
        to_copy = result["to_copy"]
        to_skip = result["to_skip"]
        errors  = result["errors"]

        n_new = sum(1 for _, r in to_copy if r == "new")
        n_mod = sum(1 for _, r in to_copy if r == "modified")
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
            (f"📋  To copy  {len(to_copy):,}",    t["info"]    if to_copy else t["text_dim"]),
            (f"✓  Up-to-date  {len(to_skip):,}",  t["success"]),
            (f"⚠  Errors  {n_err:,}",              t["error"]   if n_err   else t["text_dim"]),
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
        if n_new:
            chip_row.addWidget(self._badge(f"new: {n_new:,}", t["accent"]))
        if n_mod:
            chip_row.addWidget(self._badge(f"modified: {n_mod:,}", t["warning"]))
        lay.addLayout(chip_row)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"background:{t['header_sep']};border:none;")
        sep.setFixedHeight(1)
        lay.addWidget(sep)

        self._stack = QStackedWidget()
        copy_items = [f"[{reason}]  {rel}" for rel, reason in to_copy]
        self._stack.addWidget(_SearchableList(copy_items, t["text"]))
        self._stack.addWidget(_SearchableList(to_skip,    t["text_dim"]))
        err_items = [f"{rel}  →  {msg}" for rel, msg in errors]
        self._stack.addWidget(_SearchableList(err_items,  t["error"]))
        lay.addWidget(self._stack, 1)

        self._active_idx = -1
        self._switch(2 if n_err else 0)

    def _switch(self, idx: int) -> None:
        if idx == self._active_idx:
            return
        self._active_idx = idx
        self._stack.setCurrentIndex(idx)
        _style_chip_tabs(self._chips, self._chip_colors, idx)

    @staticmethod
    def _badge(text: str, color: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setStyleSheet(
            f"color:{color};font-size:{font_sz(-2)}px;font-weight:bold;"
            f"background:{color}22;border-radius:4px;padding:1px 8px;"
        )
        return lbl


class _OverviewTab(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        t   = current_theme()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.setSpacing(6)

        self._model = QStandardItemModel(0, 4)
        self._model.setHorizontalHeaderLabels(["Entry", "To copy", "Up-to-date", "Errors"])

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
        for col in (1, 2, 3):
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
        title    = result["title"].replace("<br>", " · ")
        n_copy   = len(result["to_copy"])
        n_skip   = len(result["to_skip"])
        n_err    = len(result["errors"])

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

        col_copy  = t["info"]   if n_copy else t["text_dim"]
        col_skip  = t["success"]
        col_err   = t["error"]  if n_err  else t["text_dim"]
        row_color = t["error"]  if n_err  else (t["info"] if n_copy else t["success"])

        row = [
            _item(title, row_color),
            _item(f"{n_copy:,}",  col_copy,  center, numeric=True),
            _item(f"{n_skip:,}",  col_skip,  center, numeric=True),
            _item(f"{n_err:,}",   col_err,   center, numeric=True),
        ]
        self._model.appendRow(row)
        self._update_count()

    def _update_count(self) -> None:
        self._count_lbl.setText(f"{self._model.rowCount():,} entries total")

    def clear(self) -> None:
        self._model.removeRows(0, self._model.rowCount())


class _GlobalViewTab(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        t   = current_theme()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 8, 10, 8)
        lay.setSpacing(0)

        chip_row = QHBoxLayout()
        chip_row.setContentsMargins(0, 0, 0, 0)
        chip_row.setSpacing(0)

        self._chips: list[QPushButton] = []
        self._chip_colors: list[str]   = []
        chip_defs = [
            ("📋  To copy  0",    t["info"]),
            ("✓  Up-to-date  0",  t["success"]),
            ("⚠  Errors  0",      t["text_dim"]),
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
        self._copy_list = _SearchableList([], t["text"])
        self._skip_list = _SearchableList([], t["text_dim"])
        self._err_list  = _SearchableList([], t["error"])
        self._stack.addWidget(self._copy_list)
        self._stack.addWidget(self._skip_list)
        self._stack.addWidget(self._err_list)
        lay.addWidget(self._stack, 1)

        self._active_idx = -1
        self._switch(0)

    def update_data(self, results: list[dict]) -> None:
        t = current_theme()
        all_copy: list[str] = []
        all_skip: list[str] = []
        all_err:  list[str] = []
        for r in results:
            entry_title = r["title"].replace("<br>", " · ")
            for rel, reason in r["to_copy"]:
                all_copy.append(f"[{reason}]  {rel}  ←  {entry_title}")
            for rel in r["to_skip"]:
                all_skip.append(f"{rel}  ←  {entry_title}")
            for rel, msg in r["errors"]:
                all_err.append(f"{rel}  →  {msg}  ←  {entry_title}")

        n_copy = len(all_copy)
        n_skip = len(all_skip)
        n_err  = len(all_err)

        self._copy_list.update_items(all_copy)
        self._skip_list.update_items(all_skip)
        self._err_list.update_items(all_err)

        chip_defs = [
            (f"📋  To copy  {n_copy:,}",    t["info"]    if n_copy else t["text_dim"]),
            (f"✓  Up-to-date  {n_skip:,}",  t["success"]),
            (f"⚠  Errors  {n_err:,}",        t["error"]   if n_err  else t["text_dim"]),
        ]
        for i, (label, color) in enumerate(chip_defs):
            self._chips[i].setText(label)
            self._chip_colors[i] = color

        active = self._active_idx
        self._active_idx = -1
        self._switch(active if active >= 0 else 0)

    def show_page(self, idx: int) -> None:
        self._active_idx = -1
        self._switch(idx)

    def _switch(self, idx: int) -> None:
        if idx == self._active_idx:
            return
        self._active_idx = idx
        self._stack.setCurrentIndex(idx)
        _style_chip_tabs(self._chips, self._chip_colors, idx)


# noinspection PyUnresolvedReferences
class DryRunDialog(_StandardKeysMixin, QDialog):

    def __init__(self, parent=None, mode: str = "backup") -> None:
        super().__init__(parent)
        self._mode = mode  # "backup" or "restore"
        title_suffix = "Backup Preview" if mode == "backup" else "Restore Preview"
        self.setWindowTitle(f"🔎  Dry Run — {title_suffix}")
        size_to_screen(self, 1600, 900)

        self._worker: _DryRunWorker | None = None
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

        if self._mode == "backup":
            mode_icon  = "💾"
            mode_label = "Backup"
            mode_color = t["accent"]
            hdr_text   = "🔎  Backup Dry Run — Preview only, nothing will be changed"
            info_text  = (
                "Scans your backup source paths and compares them with the destinations. "
                "Files marked <b>new</b> don't exist at the destination yet. "
                "Files marked <b>modified</b> are newer or differ in size."
            )
        else:
            mode_icon  = "🔁"
            mode_label = "Restore"
            mode_color = t.get("warning", t["info"])
            hdr_text   = "🔁  Restore Dry Run — Preview only, nothing will be changed"
            info_text  = (
                "Scans your backup destinations and compares them with your local source paths. "
                "Files marked <b>new</b> would be newly created locally on restore. "
                "Files marked <b>modified</b> would overwrite a differing local file."
            )

        hdr_row = QHBoxLayout()
        hdr_row.setSpacing(12)

        hdr = QLabel(hdr_text)
        hdr.setStyleSheet(
            f"font-size:{font_sz(3)}px;font-weight:bold;color:{t['accent']};"
        )
        hdr_row.addWidget(hdr, 1)

        mode_pill = QLabel(f"{mode_icon}  {mode_label} Mode")
        mode_pill.setStyleSheet(
            f"color:{mode_color};font-size:{font_sz(-1)}px;font-weight:bold;"
            f"background:{mode_color}22;border:1px solid {mode_color}55;"
            f"border-radius:10px;padding:3px 12px;"
        )
        mode_pill.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hdr_row.addWidget(mode_pill)
        lay.addLayout(hdr_row)

        info = QLabel(info_text)
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
            f"QProgressBar::chunk{{background:{mode_color};border-radius:4px;}}"
        )
        self._prog_bar.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        prog_row.addWidget(self._prog_label)
        prog_row.addWidget(self._prog_bar, 1)
        lay.addLayout(prog_row)

        summ_row = QHBoxLayout()
        summ_row.setSpacing(8)
        self._lbl_copy  = self._stat_btn("—  to copy",     t["info"],    0)
        self._lbl_skip  = self._stat_btn("—  up-to-date",  t["success"], 1)
        self._lbl_error = self._stat_btn("—  errors",       t["error"],   2)
        for w in (self._lbl_copy, self._lbl_skip, self._lbl_error):
            summ_row.addWidget(w)
        summ_row.addStretch()
        lay.addLayout(summ_row)

        sep2 = QFrame()
        sep2.setFrameShape(QFrame.Shape.HLine)
        sep2.setStyleSheet(f"background:{t['header_sep']};border:none;")
        sep2.setFixedHeight(1)
        lay.addWidget(sep2)

        self._tabs = QTabWidget()
        self._tabs.setObjectName("DryRunTabs")
        self._tabs.setMovable(False)
        self._tabs.setDocumentMode(False)
        self._tabs.setStyleSheet(
            f"QTabWidget#DryRunTabs::pane{{"
            f"border:1px solid {t['header_sep']};background:{t['bg2']};"
            f"border-radius:0 4px 4px 4px;}}"
            f"QTabWidget#DryRunTabs QTabBar::tab{{"
            f"background:{t['bg3']};color:{t['text_dim']};"
            f"font-size:{font_sz(-1)}px;font-weight:normal;"
            f"border:1px solid {t['header_sep']};border-bottom:none;"
            f"border-radius:4px 4px 0 0;"
            f"padding:6px 14px;margin-right:2px;}}"
            f"QTabWidget#DryRunTabs QTabBar::tab:selected{{"
            f"background:{t['bg2']};color:{t['text']};font-weight:bold;"
            f"border-bottom:2px solid {mode_color};}}"
            f"QTabWidget#DryRunTabs QTabBar::tab:hover:!selected{{"
            f"background:{t['bg2']};color:{t['text']};}}"
            f"QTabWidget#DryRunTabs QTabBar::scroller{{width:24px;}}"
        )
        lay.addWidget(self._tabs, 1)

        self._overview = _OverviewTab()
        self._tabs.addTab(self._overview, "📊  Overview")

        self._global_view = _GlobalViewTab()
        self._tabs.addTab(self._global_view, "🌐  All Files")

        btn_row = QHBoxLayout()
        self._start_btn = QPushButton("▶  Start Scan")
        self._start_btn.setMinimumHeight(34)
        self._start_btn.clicked.connect(self._start)
        self._cancel_btn = QPushButton("⏹  Cancel")
        self._cancel_btn.setMinimumHeight(34)
        self._cancel_btn.setEnabled(False)
        self._cancel_btn.clicked.connect(self._cancel)

        self._switch_btn = QPushButton(
            "🔁  Switch to Restore Mode" if self._mode == "backup" else "💾  Switch to Backup Mode"
        )
        self._switch_btn.setMinimumHeight(34)
        self._switch_btn.setToolTip("Close this window and open Dry Run in the other mode")
        self._switch_btn.clicked.connect(self._switch_mode)
        switch_btn = self._switch_btn

        close_btn = QPushButton("Close")
        close_btn.setMinimumHeight(34)
        close_btn.clicked.connect(self.accept)
        btn_row.addWidget(self._start_btn)
        btn_row.addWidget(self._cancel_btn)
        btn_row.addWidget(switch_btn)
        btn_row.addStretch()
        btn_row.addWidget(close_btn)
        lay.addLayout(btn_row)

    def _switch_mode(self) -> None:
        if self._worker and self._worker.isRunning():
            self._worker.cancel()
            self._worker.wait(3000)
        new_mode = "restore" if self._mode == "backup" else "backup"
        self.accept()
        DryRunDialog(self.parent(), mode=new_mode).exec()

    def _stat_btn(self, text: str, color: str, page: int) -> QPushButton:
        btn = QPushButton(text)
        btn.setStyleSheet(
            f"QPushButton{{color:{color};font-size:{font_sz(1)}px;font-weight:bold;"
            f"background:transparent;border:none;padding:4px 6px;}}"
            f"QPushButton:hover{{background:{color}18;border-radius:4px;}}"
        )
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        btn.clicked.connect(lambda: self._goto_global(page))
        return btn

    def _goto_global(self, page: int) -> None:
        self._global_view.show_page(page)
        self._tabs.setCurrentIndex(1)

    def _start(self) -> None:
        tasks: list[tuple[list[str], list[str], str, dict]] = []
        is_restore = self._mode == "restore"
        skip_flag  = "no_restore" if is_restore else "no_backup"
        for e in S.entries:
            details = e.get("details", {})
            if details.get(skip_flag):
                continue
            src = e.get("source", [])
            dst = e.get("destination", [])
            if isinstance(src, str):
                src = [src]
            if isinstance(dst, str):
                dst = [dst]
            if src and dst:
                if is_restore:
                    tasks.append((dst, src, e.get("title", "?"), details.get("exclude_paths", {})))
                else:
                    tasks.append((src, dst, e.get("title", "?"), details.get("exclude_paths", {})))

        if not tasks:
            label = "restore" if is_restore else "backup"
            QMessageBox.information(self, "Dry Run", f"No {label} entries configured.")
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
        while self._tabs.count() > 2:
            self._tabs.removeTab(2)
        self._overview.clear()
        self._global_view.update_data([])
        self._tabs.setCurrentIndex(0)

        self._prog_bar.setValue(0)
        self._prog_bar.setRange(0, len(tasks))
        self._start_btn.setEnabled(False)
        self._cancel_btn.setEnabled(True)
        self._switch_btn.setEnabled(False)
        verb = "Scanning (restore direction)" if is_restore else "Scanning"
        self._prog_label.setText(f"{verb} 0 / {len(tasks)} …")
        self._lbl_copy.setText("Scanning…")
        self._lbl_skip.setText("")
        self._lbl_error.setText("")

        self._worker = _DryRunWorker(tasks)
        self._worker.progress.connect(self._on_progress)
        self._worker.entry_done.connect(self._on_entry_done)
        self._worker.finished.connect(self._on_finished)
        self._worker.start()

    def _cancel(self) -> None:
        if self._worker:
            self._worker.cancel()
        self._cancel_btn.setEnabled(False)
        self._switch_btn.setEnabled(True)
        self._prog_label.setText("Cancelling…")

    def _on_progress(self, done: int, total: int) -> None:
        self._prog_bar.setRange(0, max(total, 1))
        self._prog_bar.setValue(done)
        verb = "Scanning (restore direction)" if self._mode == "restore" else "Scanning"
        self._prog_label.setText(f"{verb} {done} / {total} …")

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
        self._switch_btn.setEnabled(True)
        for i, r in enumerate(self._results):
            if r["errors"]:
                self._tabs.setCurrentIndex(i + 2)
                break

    def _add_entry_tab(self, result: dict) -> None:
        t         = current_theme()
        title     = result["title"].replace("<br>", "\n")
        n_copy    = len(result["to_copy"])
        n_skip    = len(result["to_skip"])
        n_err     = len(result["errors"])

        widget = _EntryTabWidget(result)
        self._tabs.addTab(widget, title)
        idx = self._tabs.count() - 1

        if n_err:
            self._tabs.tabBar().setTabTextColor(idx, QColor(t["error"]))   # type: ignore[attr-defined]
        elif n_copy:
            self._tabs.tabBar().setTabTextColor(idx, QColor(t["info"]))    # type: ignore[attr-defined]
        else:
            self._tabs.tabBar().setTabTextColor(idx, QColor(t["success"])) # type: ignore[attr-defined]

        tip = f"{n_copy:,} to copy  |  {n_skip:,} up-to-date  |  {n_err:,} errors"
        self._tabs.tabBar().setTabToolTip(idx, tip)

    def _update_totals(self) -> None:
        t           = current_theme()
        total_copy  = sum(len(r["to_copy"]) for r in self._results)
        total_skip  = sum(len(r["to_skip"]) for r in self._results)
        total_error = sum(len(r["errors"])  for r in self._results)

        def _upd(btn: QPushButton, text: str, color: str) -> None:
            btn.setText(text)
            btn.setStyleSheet(
                f"QPushButton{{color:{color};font-size:{font_sz(1)}px;font-weight:bold;"
                f"background:transparent;border:none;padding:4px 6px;}}"
                f"QPushButton:hover{{background:{color}18;border-radius:4px;}}"
            )

        _upd(self._lbl_copy,  f"{total_copy:,}  to copy",     t["info"])
        _upd(self._lbl_skip,  f"{total_skip:,}  up-to-date",  t["success"])
        _upd(self._lbl_error, f"{total_error:,}  errors",      t["error"])

        self._global_view.update_data(self._results)


class _ModeCard(QWidget):

    def __init__(self, icon: str, title: str, subtitle: str, color: str, parent=None) -> None:
        super().__init__(parent)
        t = current_theme()
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._color = color

        self._frame = QFrame(self)
        self._frame.setObjectName("ModeCard")
        self._frame.setStyleSheet(
            f"QFrame#ModeCard{{background:{t['bg2']};border:2px solid {t['header_sep']};"
            f"border-radius:10px;padding:6px;}}"
            f"QFrame#ModeCard:hover{{border-color:{color};}}"
        )

        inner = QVBoxLayout(self._frame)
        inner.setContentsMargins(20, 18, 20, 18)
        inner.setSpacing(6)
        inner.setAlignment(Qt.AlignmentFlag.AlignCenter)

        icon_lbl = QLabel(icon)
        icon_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon_lbl.setStyleSheet("font-size:36px;background:transparent;border:none;")
        inner.addWidget(icon_lbl)

        title_lbl = QLabel(title)
        title_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title_lbl.setStyleSheet(
            f"font-size:{font_sz(2)}px;font-weight:bold;color:{color};"
            f"background:transparent;border:none;"
        )
        inner.addWidget(title_lbl)

        sub_lbl = QLabel(subtitle)
        sub_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        sub_lbl.setWordWrap(True)
        sub_lbl.setStyleSheet(
            f"font-size:{font_sz(-1)}px;color:{t['text_dim']};"
            f"background:transparent;border:none;"
        )
        inner.addWidget(sub_lbl)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._frame)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            parent = self.parent()
            if isinstance(parent, DryRunModeDialog):
                parent.card_clicked(self)
        super().mousePressEvent(event)


class DryRunModeDialog(_StandardKeysMixin, QDialog):

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("🔎  Dry Run — Select Mode")
        self.setFixedSize(850, 550)
        self._chosen: str | None = None
        self._build()

    def _build(self) -> None:
        t = current_theme()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(28, 24, 28, 20)
        lay.setSpacing(16)

        hdr = QLabel("🔎  Dry Run")
        hdr.setStyleSheet(
            f"font-size:{font_sz(4)}px;font-weight:bold;color:{t['accent']};"
        )
        hdr.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(hdr)

        sub = QLabel("Select the direction to simulate.\nNothing will be written to disk.")
        sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
        sub.setWordWrap(True)
        sub.setStyleSheet(f"color:{t['text_dim']};font-size:{font_sz(0)}px;")
        lay.addWidget(sub)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"background:{t['header_sep']};border:none;")
        sep.setFixedHeight(1)
        lay.addWidget(sep)

        cards_row = QHBoxLayout()
        cards_row.setSpacing(20)

        self._backup_card = _ModeCard(
            "💾",
            "Backup Dry Run",
            "Preview what would be copied\nfrom your sources to the backup.",
            t["accent"],
            self,
        )
        self._restore_card = _ModeCard(
            "🔁",
            "Restore Dry Run",
            "Preview what would be restored\nfrom the backup to your local paths.",
            t.get("warning", t["info"]),
            self,
        )
        cards_row.addWidget(self._backup_card)
        cards_row.addWidget(self._restore_card)
        lay.addLayout(cards_row, 1)

        cancel_row = QHBoxLayout()
        cancel_row.addStretch()
        cancel_btn = QPushButton("Cancel")
        cancel_btn.setMinimumHeight(30)
        cancel_btn.clicked.connect(self.reject)
        cancel_row.addWidget(cancel_btn)
        lay.addLayout(cancel_row)

    def card_clicked(self, card: "_ModeCard") -> None:
        if card is self._backup_card:
            self._chosen = "backup"
        else:
            self._chosen = "restore"
        self.accept()

    def chosen_mode(self) -> str | None:
        return self._chosen


def launch_dry_run(parent=None) -> None:
    chooser = DryRunModeDialog(parent)
    if chooser.exec() != QDialog.DialogCode.Accepted:
        return
    mode = chooser.chosen_mode()
    if mode:
        DryRunDialog(parent, mode=mode).exec()

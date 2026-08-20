import os
import threading
from pathlib import Path

from PyQt6.QtCore import Qt, QThread, pyqtSignal, QSortFilterProxyModel
from PyQt6.QtGui import QColor, QStandardItemModel, QStandardItem, QCloseEvent, QMouseEvent
from PyQt6.QtWidgets import (
    QDialog, QFrame, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QMessageBox, QProgressBar, QPushButton, QTabWidget, QTableView,
    QVBoxLayout, QWidget, QAbstractItemView, QHeaderView,
    QSizePolicy, QStackedWidget,
)

from copy_worker_core import _SKIP_RE
from copy_worker import _is_up_to_date_local, _is_symlink_up_to_date
from drive_utils import is_smb, is_ssh
from state import S
from themes import current_theme, font_sz
from translations import tr
from ui_utils import color_style, _StandardKeysMixin, size_to_screen

__all__ = ["DryRunDialog", "DryRunModeDialog", "launch_dry_run"]


def _hline(color: str) -> QFrame:
    line = QFrame()
    line.setFrameShape(QFrame.Shape.HLine)
    line.setStyleSheet(f"background:{color};border:none;")
    line.setFixedHeight(1)
    return line


class _ChipStackMixin:
    """Shared chip-tab/stacked-widget switching behavior for tab widgets that
    show a row of colored chip buttons above a QStackedWidget.

    Classes using this mixin must set: self._active_idx (int), self._stack
    (QStackedWidget), self._chips (list[QPushButton]), self._chip_colors (list[str]).
    """

    _active_idx: int
    _stack: QStackedWidget
    _chips: list
    _chip_colors: list

    def _switch(self, idx: int) -> None:
        if idx == self._active_idx:
            return
        self._active_idx = idx
        self._stack.setCurrentIndex(idx)
        _style_chip_tabs(self._chips, self._chip_colors, idx)


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
                tr("Source/destination count mismatch ({n_src} source(s) vs "
                   "{n_dst} destination(s)) — extra entries were not checked",
                   n_src=len(sources), n_dst=len(destinations)),
            ))

        for src_root, dst_root in zip(sources, destinations):
            if self._cancel.is_set():
                break
            if not src_root or not dst_root:
                continue

            if is_smb(src_root) or is_ssh(src_root) or is_smb(dst_root) or is_ssh(dst_root):
                remote = src_root if (is_smb(src_root) or is_ssh(src_root)) else dst_root
                errors.append((
                    remote,
                    tr("Remote (SMB/SSH) path — not simulated in Dry Run (preview is "
                       "local-filesystem only; this is not a failure)."),
                ))
                continue

            src_p = Path(src_root).expanduser()
            dst_p = Path(dst_root).expanduser()

            if not src_p.exists():
                errors.append((src_root, tr("Source path does not exist")))
                continue

            src_abs = str(src_p.resolve())
            if isinstance(excludes, dict):
                excl_names = excludes.get(src_abs) or excludes.get(src_root) or excludes.get(str(src_p)) or []
                excl_set = {os.path.join(src_abs, n) for n in excl_names}
            elif isinstance(excludes, (set, frozenset, list, tuple)):
                excl_set = set(excludes)
            else:
                excl_set = set()

            if src_p.is_file() or src_p.is_symlink():
                self._classify_file(src_p, src_p.name, dst_p, to_copy, to_skip, errors)
                continue

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
                    self._classify_file(src_file, str(rel), dst_file, to_copy, to_skip, errors)

        return dict(
            title=title,
            to_copy=to_copy,
            to_skip=to_skip,
            errors=errors,
            src_total=len(to_copy) + len(to_skip),
        )

    @staticmethod
    def _classify_file(
            src_file: Path,
        rel_name: str,
        dst_file: Path,
        to_copy: list[tuple[str, str]],
        to_skip: list[str],
        errors: list[tuple[str, str]],
    ) -> None:
        src_file_str = str(src_file)

        if os.path.islink(src_file_str):
            try:
                target = os.readlink(src_file_str)
            except OSError as e:
                errors.append((rel_name, str(e)))
                return

            dst_file_str = str(dst_file)
            if not os.path.lexists(dst_file_str):
                to_copy.append((rel_name, "new"))
            elif _is_symlink_up_to_date(dst_file_str, target):
                to_skip.append(rel_name)
            else:
                to_copy.append((rel_name, "modified"))
            return

        try:
            src_stat = src_file.stat()
        except OSError as e:
            errors.append((rel_name, str(e)))
            return

        if not dst_file.exists():
            to_copy.append((rel_name, "new"))
        else:
            try:
                if _is_up_to_date_local(str(dst_file), src_stat):
                    to_skip.append(rel_name)
                else:
                    to_copy.append((rel_name, "modified"))
            except OSError as e:
                errors.append((rel_name, str(e)))


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
        self._search.setPlaceholderText(tr("🔍  Filter…"))
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
        self._count_lbl = QLabel(tr("{n:,} items", n=len(items)))
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
        suffix = tr(" (showing {n_shown:,} of {n_total:,})", n_shown=n_shown, n_total=n_total) if needle else ""
        self._count_lbl.setText(tr("{n_total:,} items{suffix}", n_total=n_total, suffix=suffix))

    def update_items(self, items: list[str]) -> None:
        self._all_items = items
        self._list.clear()
        self._list.addItems(items)
        self._count_lbl.setText(tr("{n:,} items", n=len(items)))
        current_filter = self._search.text()
        if current_filter:
            self._filter(current_filter)


class _EntryTabWidget(_ChipStackMixin, QWidget):

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
            (tr("📋  To copy  {n:,}", n=len(to_copy)),    t["info"]    if to_copy else t["text_dim"]),
            (tr("✓  Up-to-date  {n:,}", n=len(to_skip)),  t["success"]),
            (tr("⚠  Errors  {n:,}", n=n_err),              t["error"]   if n_err   else t["text_dim"]),
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
            chip_row.addWidget(self._badge(tr("new: {n:,}", n=n_new), t["accent"]))
        if n_mod:
            chip_row.addWidget(self._badge(tr("modified: {n:,}", n=n_mod), t["warning"]))
        lay.addLayout(chip_row)
        lay.addWidget(_hline(t["header_sep"]))

        self._stack = QStackedWidget()
        _reason_label = {"new": tr("new"), "modified": tr("modified")}
        copy_items = [f"[{_reason_label.get(reason, str(reason))}]  {rel}" for rel, reason in to_copy]
        self._stack.addWidget(_SearchableList(copy_items, t["text"]))
        self._stack.addWidget(_SearchableList(to_skip,    t["text_dim"]))
        err_items = [f"{rel}  →  {msg}" for rel, msg in errors]
        self._stack.addWidget(_SearchableList(err_items,  t["error"]))
        lay.addWidget(self._stack, 1)

        self._active_idx = -1
        self._switch(2 if n_err else 0)

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
        self._model.setHorizontalHeaderLabels([tr("Entry"), tr("To copy"), tr("Up-to-date"), tr("Errors")])

        self._proxy = QSortFilterProxyModel()
        self._proxy.setSourceModel(self._model)
        self._proxy.setFilterCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self._proxy.setFilterKeyColumn(0)

        self._search = QLineEdit()
        self._search.setPlaceholderText(tr("🔍  Filter entries…"))
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
        self._count_lbl.setStyleSheet(color_style(t['text_dim'], font_sz(-2)))
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
        self._count_lbl.setText(tr("{n:,} entries total", n=self._model.rowCount()))

    def clear(self) -> None:
        self._model.removeRows(0, self._model.rowCount())


class _GlobalViewTab(_ChipStackMixin, QWidget):
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
            (tr("📋  To copy  {n}", n=0),    t["info"]),
            (tr("✓  Up-to-date  {n}", n=0),  t["success"]),
            (tr("⚠  Errors  {n}", n=0),      t["text_dim"]),
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
        lay.addWidget(_hline(t["header_sep"]))

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
            (tr("📋  To copy  {n:,}", n=n_copy),    t["info"]    if n_copy else t["text_dim"]),
            (tr("✓  Up-to-date  {n:,}", n=n_skip),  t["success"]),
            (tr("⚠  Errors  {n:,}", n=n_err),        t["error"]   if n_err  else t["text_dim"]),
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


# noinspection PyUnresolvedReferences
class DryRunDialog(_StandardKeysMixin, QDialog):

    def __init__(self, parent=None, mode: str = "backup") -> None:
        super().__init__(parent)
        self._mode = mode
        title_suffix = tr("Backup Preview") if mode == "backup" else tr("Restore Preview")
        self.setWindowTitle(tr("🔎  Dry Run — {suffix}", suffix=title_suffix))
        size_to_screen(self, 1600, 900)

        self._worker: _DryRunWorker | None = None
        self._results: list[dict] = []
        self._build()

    def closeEvent(self, a0: QCloseEvent | None) -> None:
        if self._worker and self._worker.isRunning():
            self._worker.cancel()
            self._worker.wait(3000)
        if a0 is not None:
            a0.accept()

    def _build(self) -> None:
        t   = current_theme()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(10)

        if self._mode == "backup":
            mode_icon  = "💾"
            mode_label = tr("Backup")
            mode_color = t["accent"]
            hdr_text   = tr("🔎  Backup Dry Run — Preview only, nothing will be changed")
            info_text  = tr(
                "Scans your backup source paths and compares them with the destinations. "
                "Files marked <b>new</b> don't exist at the destination yet. "
                "Files marked <b>modified</b> are newer or differ in size."
            )
        else:
            mode_icon  = "🔁"
            mode_label = tr("Restore")
            mode_color = t.get("warning", t["info"])
            hdr_text   = tr("🔁  Restore Dry Run — Preview only, nothing will be changed")
            info_text  = tr(
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

        mode_pill = QLabel(tr("{icon}  {label} Mode", icon=mode_icon, label=mode_label))
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
        info.setStyleSheet(color_style(t['text_dim'], font_sz(-1)))
        lay.addWidget(info)
        lay.addWidget(_hline(t["header_sep"]))

        prog_row = QHBoxLayout()
        self._prog_label = QLabel(tr("Press  ▶ Start Scan  to begin…"))
        self._prog_label.setStyleSheet(color_style(t['text_dim'], font_sz(-1)))
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
        self._lbl_copy  = self._stat_btn(tr("—  to copy"),     t["info"],    0)
        self._lbl_skip  = self._stat_btn(tr("—  up-to-date"),  t["success"], 1)
        self._lbl_error = self._stat_btn(tr("—  errors"),       t["error"],   2)
        for w in (self._lbl_copy, self._lbl_skip, self._lbl_error):
            summ_row.addWidget(w)
        summ_row.addStretch()
        lay.addLayout(summ_row)
        lay.addWidget(_hline(t["header_sep"]))

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
        self._tabs.addTab(self._overview, tr("📊  Overview"))

        self._global_view = _GlobalViewTab()
        self._tabs.addTab(self._global_view, tr("🌐  All Files"))

        btn_row = QHBoxLayout()
        self._start_btn = QPushButton(tr("▶  Start Scan"))
        self._start_btn.setMinimumHeight(34)
        self._start_btn.clicked.connect(self._start)
        self._cancel_btn = QPushButton(tr("⏹  Cancel"))
        self._cancel_btn.setMinimumHeight(34)
        self._cancel_btn.setEnabled(False)
        self._cancel_btn.clicked.connect(self._cancel)

        self._switch_btn = QPushButton(
            tr("🔁  Switch to Restore Mode") if self._mode == "backup" else tr("💾  Switch to Backup Mode")
        )
        self._switch_btn.setMinimumHeight(34)
        self._switch_btn.setToolTip(tr("Close this window and open Dry Run in the other mode"))
        self._switch_btn.clicked.connect(self._switch_mode)
        switch_btn = self._switch_btn

        close_btn = QPushButton(tr("Close"))
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
        _parent = self.parent()
        _parent_widget = _parent if isinstance(_parent, QWidget) else None
        DryRunDialog(_parent_widget, mode=new_mode).exec()

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
        from advanced_copy import restore_exclude_paths

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
                    tasks.append((dst, src, e.get("title", "?"), restore_exclude_paths(e)))
                else:
                    tasks.append((src, dst, e.get("title", "?"), details.get("exclude_paths", {})))

        if not tasks:
            label = tr("restore") if is_restore else tr("backup")
            QMessageBox.information(self, tr("Dry Run"), tr("No {label} entries configured.", label=label))
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
        verb = tr("Scanning (restore direction)") if is_restore else tr("Scanning")
        self._prog_label.setText(tr("{verb} 0 / {total} …", verb=verb, total=len(tasks)))
        self._lbl_copy.setText(tr("Scanning…"))
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
        self._prog_label.setText(tr("Cancelling…"))

    def _on_progress(self, done: int, total: int) -> None:
        self._prog_bar.setRange(0, max(total, 1))
        self._prog_bar.setValue(done)
        verb = tr("Scanning (restore direction)") if self._mode == "restore" else tr("Scanning")
        self._prog_label.setText(tr("{verb} {done} / {total} …", verb=verb, done=done, total=total))

    def _on_entry_done(self, result: dict) -> None:
        self._results.append(result)
        self._add_entry_tab(result)
        self._overview.add_result(result)
        self._update_totals()

    def _on_finished(self) -> None:
        self._prog_label.setText(tr("Scan complete — {n:,} entries checked.", n=len(self._results)))
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

        tip = tr("{n_copy:,} to copy  |  {n_skip:,} up-to-date  |  {n_err:,} errors",
                 n_copy=n_copy, n_skip=n_skip, n_err=n_err)
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

        _upd(self._lbl_copy,  tr("{n:,}  to copy", n=total_copy),     t["info"])
        _upd(self._lbl_skip,  tr("{n:,}  up-to-date", n=total_skip),  t["success"])
        _upd(self._lbl_error, tr("{n:,}  errors", n=total_error),      t["error"])

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
        title_lbl.setWordWrap(True)
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

    def mousePressEvent(self, a0: QMouseEvent | None) -> None:
        if a0 is not None and a0.button() == Qt.MouseButton.LeftButton:
            parent = self.parent()
            if isinstance(parent, DryRunModeDialog):
                parent.card_clicked(self)
        super().mousePressEvent(a0)


class DryRunModeDialog(_StandardKeysMixin, QDialog):

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(tr("🔎  Dry Run — Select Mode"))
        self.setMinimumSize(850, 550)
        self._chosen: str | None = None
        self._build()
        self.adjustSize()
        hint = self.sizeHint()
        self.resize(max(850, hint.width()), max(550, hint.height()))

    def _build(self) -> None:
        t = current_theme()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(28, 24, 28, 20)
        lay.setSpacing(16)

        hdr = QLabel(tr("🔎  Dry Run"))
        hdr.setStyleSheet(
            f"font-size:{font_sz(4)}px;font-weight:bold;color:{t['accent']};"
        )
        hdr.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(hdr)

        sub = QLabel(tr("Select the direction to simulate.\nNothing will be written to disk."))
        sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
        sub.setWordWrap(True)
        sub.setStyleSheet(color_style(t['text_dim'], font_sz(0)))
        lay.addWidget(sub)
        lay.addWidget(_hline(t["header_sep"]))

        cards_row = QHBoxLayout()
        cards_row.setSpacing(20)

        self._backup_card = _ModeCard(
            "💾",
            tr("Backup Dry Run"),
            tr("Preview what would be copied\nfrom your sources to the backup."),
            t["accent"],
            self,
        )
        self._restore_card = _ModeCard(
            "🔁",
            tr("Restore Dry Run"),
            tr("Preview what would be restored\nfrom the backup to your local paths."),
            t.get("warning", t["info"]),
            self,
        )
        cards_row.addWidget(self._backup_card)
        cards_row.addWidget(self._restore_card)
        lay.addLayout(cards_row, 1)

        cancel_row = QHBoxLayout()
        cancel_row.addStretch()
        cancel_btn = QPushButton(tr("Cancel"))
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

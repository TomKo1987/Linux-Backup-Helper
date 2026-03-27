from pathlib import Path
from typing import Optional
from datetime import datetime
import copy, os, shutil, subprocess, threading, tarfile, json

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QColor, QTextCursor
from PyQt6.QtWidgets import (
    QFormLayout, QHBoxLayout, QLabel, QLineEdit, QListWidget, QWidget, QVBoxLayout,
    QListWidgetItem, QMessageBox, QPlainTextEdit, QPushButton, QSizePolicy, QSplitter,
    QCheckBox, QColorDialog, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QTextEdit,
)

from themes import current_theme, font_scale, font_sz
from state import (
    _norm_paths, list_profiles, load_profile, save_profile, logger, State,
    S, _HOME, _LOG_FILE, _PROFILES_DIR, _PROFILE_RE, _atomic_write, apply_replacements,
)


def _sep() -> QWidget:
    w = QWidget()
    w.setFixedHeight(1)
    w.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
    w.setStyleSheet(f"background:{current_theme()['header_sep']};")
    return w


def _hdr_label(text: str, color: str = "", size: int | None = None) -> QLabel:
    lbl = QLabel(text)
    sz  = size if size is not None else font_sz(3)
    lbl.setStyleSheet(
        f"font-size:{sz}px;font-weight:bold;"
        f"color:{color or current_theme()['accent']};padding:4px 0;"
    )
    lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
    return lbl


def _ok_cancel_buttons(dialog: QDialog, ok_fn, ok_label: str = "Save", cancel_label: str = "Cancel") -> QDialogButtonBox:
    bb         = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)  # type: ignore
    ok_btn     = bb.button(QDialogButtonBox.StandardButton.Ok)
    cancel_btn = bb.button(QDialogButtonBox.StandardButton.Cancel)
    if ok_btn:     ok_btn.setText(ok_label)
    if cancel_btn: cancel_btn.setText(cancel_label)
    bb.accepted.connect(ok_fn)
    bb.rejected.connect(dialog.reject)
    return bb


def _btn_row(buttons: list[tuple[str, object]]) -> QHBoxLayout:
    row = QHBoxLayout()
    for label, fn in buttons:
        b = QPushButton(label)
        b.clicked.connect(fn)
        row.addWidget(b)
    return row


def _browse_buttons(parent: QWidget, editor, home: Path = _HOME) -> QHBoxLayout:
    row = QHBoxLayout()
    row.setContentsMargins(0, 0, 0, 0)
    row.addStretch()
    for label, mode in [("📄 File", "file"), ("📁 Directory", "dir")]:
        b = QPushButton(label)
        b.setFixedHeight(28)
        b.clicked.connect(lambda _c=False, _m=mode: _do_browse(parent, editor, _m, home))
        row.addWidget(b)
    return row


def _do_browse(parent: QWidget, editor, mode: str, home: Path = _HOME) -> None:
    path = (QFileDialog.getExistingDirectory(parent, "Select directory", str(home))
            if mode == "dir"
            else QFileDialog.getOpenFileName(parent, "Select file", str(home))[0])
    if not path:
        return
    if hasattr(editor, "setPlainText"):
        editor.setPlainText(path)
        cur = editor.textCursor()
        cur.movePosition(QTextCursor.MoveOperation.End)
        editor.setTextCursor(cur)
    else:
        editor.setText(path)


def _ask_text(parent, title: str, label: str, default: str = "", min_width: int = 440) -> tuple[str, bool]:
    dlg = QDialog(parent)
    dlg.setWindowTitle(title)
    dlg.setMinimumWidth(min_width)
    layout = QVBoxLayout(dlg)
    layout.setSpacing(10)
    layout.setContentsMargins(16, 16, 16, 16)
    layout.addWidget(QLabel(label))
    edit = QLineEdit(default)
    edit.selectAll()
    layout.addWidget(edit)
    bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)  # type: ignore
    bb.accepted.connect(dlg.accept)
    bb.rejected.connect(dlg.reject)
    layout.addWidget(bb)
    edit.setFocus()
    accepted = dlg.exec() == QDialog.DialogCode.Accepted
    return edit.text(), accepted


def _ask_profile_name(title: str, default: str, parent: Optional[QWidget] = None) -> Optional[str]:
    while True:
        name, ok = _ask_text(parent, title, "Profile name:", default=default)
        if not ok:
            return None
        name = name.strip()
        if not name:
            QMessageBox.warning(parent, "Invalid Name", "Name must not be empty.")
            continue
        if not _PROFILE_RE.match(name):
            QMessageBox.warning(
                parent, "Invalid Name",
                "Only letters, digits, spaces, hyphens, underscores and dots are allowed.",
            )
            continue
        return name


class _ListDialog(QDialog):

    def __init__(self, parent, title: str, size: tuple[int, int], hdr_text: str,
                 btn_specs: list[tuple[str, str]], close_label: str = "✕  Close"):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumSize(*size)

        layout = QVBoxLayout(self)
        layout.addWidget(_hdr_label(hdr_text))
        layout.addWidget(_sep())
        self.item_list = QListWidget()
        layout.addWidget(self.item_list, 1)
        layout.addLayout(_btn_row([(lbl, getattr(self, fn)) for lbl, fn in btn_specs]))
        layout.addWidget(_sep())
        close_btn = QPushButton(close_label)
        close_btn.clicked.connect(self.accept)
        layout.addWidget(close_btn)
        self._refresh()

    def _refresh(self) -> None:
        raise NotImplementedError

    def _selected_data(self):
        item = self.item_list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None


class _TextViewDialog(QDialog):

    def __init__(self, parent, title: str, min_size: tuple[int, int],
                 font_size: int | None = None, extra_buttons: list[tuple[str, object]] = ()):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumSize(*min_size)
        t  = current_theme()
        fs = font_size if font_size is not None else font_sz()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(5, 5, 5, 5)
        layout.setSpacing(5)

        self.view = QTextEdit()
        self.view.setReadOnly(True)
        self.view.setStyleSheet(f"font-family:monospace;font-size:{fs}px;border:none;border-radius:0;")
        layout.addWidget(self.view, 1)

        bot = QWidget()
        bot.setStyleSheet(f"background:{t['bg2']};border-top:1px solid {t['header_sep']};")
        bl  = QHBoxLayout(bot)
        bl.setContentsMargins(12, 8, 12, 8)
        bl.setSpacing(8)
        for label, fn in [*extra_buttons, ("✕ Close", self.accept)]:
            b = QPushButton(label)
            b.setFixedHeight(36)
            b.clicked.connect(fn)
            bl.addWidget(b)
        layout.addWidget(bot)


# noinspection PyUnresolvedReferences
class EntryDialog(QDialog):
    _COL_CLEAR = QColor(0, 0, 0, 0)

    def __init__(self, parent, entry: Optional[dict], *, stacked: bool = False,
                 _pairs: Optional[list[list[str]]] = None):
        super().__init__(parent)
        self.result: dict           = {}
        self.pairs: list[list[str]] = list(_pairs) if _pairs is not None else []
        self.stacked: bool          = stacked
        self._suppress_sync: bool   = False
        self._entry_snapshot: dict  = entry or {}
        self._show_full_paths: bool = False
        self._pairs_provided: bool  = _pairs is not None
        t = current_theme()
        self._COL_ACTIVE_BG  = QColor(t["info"])
        self._COL_ACTIVE_FG  = QColor(t["bg"])
        self._COL_PARTNER_BG = QColor(t["warning"])
        self._COL_PARTNER_FG = QColor(t["bg"])
        self.setWindowTitle("Edit Entry" if entry else "New Entry")
        self._build(self._entry_snapshot)

    @property
    def snapshot(self) -> dict:
        return self._entry_snapshot

    def _compute_size(self) -> tuple[int, int]:
        from PyQt6.QtGui import QFontMetrics, QFont
        from PyQt6.QtWidgets import QApplication
        fm     = QFontMetrics(QFont("monospace", 15))
        max_px = max((fm.horizontalAdvance(p) for pair in self.pairs for p in pair), default=400)
        scr    = QApplication.primaryScreen()
        screen = scr.availableGeometry() if scr else None
        if screen is None:
            return 1200, 800
        pad = 80
        if self.stacked:
            return (max(1110, min(max_px + 140, screen.width() - pad)),
                    max(900, min(screen.height() - pad, 1100)) + 10)
        return (max(1200, min(max_px * 2 + 150, screen.width() - pad)),
                max(800, min(screen.height() - pad, 950)) + 10)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if not event.spontaneous():
            w, h = self._compute_size()
            self.setMinimumSize(w, h)
            self.resize(w, h)

    def _build(self, e: dict) -> None:
        from PyQt6.QtWidgets import QAbstractItemView
        t  = current_theme()
        fs = font_scale()

        src_paths = _norm_paths(e.get("source", []))
        dst_paths = _norm_paths(e.get("destination", []))
        if not self._pairs_provided and not self.pairs:
            n          = max(len(src_paths), len(dst_paths))
            self.pairs = [[src_paths[i] if i < len(src_paths) else "",
                           dst_paths[i] if i < len(dst_paths) else ""] for i in range(n)]

        root = QVBoxLayout(self)
        root.setSpacing(8)
        root.setContentsMargins(12, 12, 12, 12)

        title_row  = QHBoxLayout()
        title_row.addWidget(_hdr_label("Edit Entry" if e else "New Entry"))
        title_row.addStretch()
        layout_btn = QPushButton("Side-by-Side View" if self.stacked else "Stacked View")
        layout_btn.setFixedHeight(28)
        layout_btn.setToolTip("Toggle between side-by-side and stacked layout")
        layout_btn.clicked.connect(self._toggle_layout)
        title_row.addWidget(layout_btn)
        root.addLayout(title_row)
        root.addWidget(_sep())

        form = QFormLayout()
        form.setSpacing(6)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        self.hdr = QComboBox()
        self.hdr.setEditable(True)
        header_keys = list(S.headers.keys())
        self.hdr.addItems(header_keys)
        saved_hdr = e.get("header", "")
        if saved_hdr:
            self.hdr.setCurrentText(saved_hdr)
        elif header_keys:
            self.hdr.setCurrentIndex(0)

        self.title_edit = QLineEdit(e.get("title", ""))
        self.title_edit.setPlaceholderText("Entry name (use <br> for line break)")
        form.addRow("Header:", self.hdr)
        form.addRow("Title:",  self.title_edit)
        root.addLayout(form)
        root.addWidget(_sep())

        lw_style = (f"QListWidget {{ font-family:monospace;font-size:{fs['xl']}px; }}"
                    f"QListWidget::item {{ padding:6px 6px; }}")
        self._src_list = QListWidget()
        self._dst_list = QListWidget()
        for lw in (self._src_list, self._dst_list):
            lw.setStyleSheet(lw_style)
            lw.setAlternatingRowColors(True)
            lw.setMinimumHeight(180)
            lw.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
            lw.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)

        hint_html = (
            f"<div style='color:{t['muted']};font-family:monospace;font-size:{fs['sm']}px;"
            f"line-height:180%;text-align:center;'>"
            f"Click <b style='color:{t['accent']};'>'➕ Add Pair'</b> to add source and destination pairs."
            f"<table cellspacing='0' cellpadding='4' align='center' style='text-align:left;'>"
            f"<tr><td style='white-space:nowrap;padding-right:20px;vertical-align:top;'>Local file:</td>"
            f"    <td>~/Documents/notes.txt<br>"
            f"        <span style='color:{t['muted']};'> or /home/user/Documents/notes.txt</span></td></tr>"
            f"<tr><td style='white-space:nowrap;padding-right:20px;vertical-align:top;'>Local folder:</td>"
            f"    <td>~/.config/app/<br>"
            f"        <span style='color:{t['muted']};'>or /home/user/.config/app/</span></td></tr>"
            f"<tr><td colspan='2' style='font-size:{fs['xs']}px;padding-top:4px;padding-bottom:8px;'>"
            f"(Replace 'user' with your actual username if using full paths)<br></td></tr>"
            f"<tr><td style='white-space:nowrap;padding-right:20px;vertical-align:top;'>Samba Shares:</td>"
            f"    <td>smb://192.168.0.53/share/data/</td></tr>"
            f"</table><br><br></div>"
        )
        self._src_hint = QLabel(self._src_list)
        self._src_hint.setText(hint_html)
        self._src_hint.setTextFormat(Qt.TextFormat.RichText)
        self._src_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._src_hint.setStyleSheet("background:transparent;")
        self._src_hint.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._src_hint.setWordWrap(True)

        orig_resize = self._src_list.resizeEvent

        def _src_resize(ev, _lw=self._src_list, _hl=self._src_hint):
            orig_resize(ev)
            _hl.setGeometry(_lw.rect())

        self._src_list.resizeEvent = _src_resize
        self._populate_lists()

        self._src_list.currentRowChanged.connect(lambda r: self._on_selection(self._src_list, self._dst_list, r))
        self._dst_list.currentRowChanged.connect(lambda r: self._on_selection(self._dst_list, self._src_list, r))
        self._src_list.itemDoubleClicked.connect(lambda item: self._edit_pair(self._src_list.row(item)))
        self._dst_list.itemDoubleClicked.connect(lambda item: self._edit_pair(self._dst_list.row(item)))

        def _panel(_label: str, _lw: QListWidget) -> QWidget:
            w  = QWidget()
            vl = QVBoxLayout(w)
            vl.setContentsMargins(0, 0, 0, 0)
            vl.setSpacing(4)
            lbl = QLabel(_label)
            lbl.setStyleSheet(f"font-weight:bold;font-size:{fs['lg']}px;color:{t['accent']};padding:1px 0;")
            vl.addWidget(lbl)
            vl.addWidget(_lw, 1)
            return w

        orientation    = Qt.Orientation.Vertical if self.stacked else Qt.Orientation.Horizontal
        self._splitter = QSplitter(orientation)
        self._splitter.setChildrenCollapsible(False)
        self._splitter.addWidget(_panel("Source",      self._src_list))
        self._splitter.addWidget(_panel("Destination", self._dst_list))
        self._splitter.setSizes([10000, 10000])
        root.addWidget(self._splitter, 1)

        root.addWidget(_sep())
        tb = QHBoxLayout()
        tb.setSpacing(6)
        for label, tip, fn in [
            ("➕ Add Pair",  "Add a new source/destination pair",   self._add_pair),
            ("✏️ Edit",      "Edit selected pair (or double-click)", self._edit_selected),
            ("🗑 Remove",    "Remove selected pair",                 self._remove_selected),
            ("▲ Move Up",   "Move selected pair up",                self._move_up),
            ("▼ Move Down", "Move selected pair down",              self._move_down),
        ]:
            b = QPushButton(label)
            b.setToolTip(tip)
            b.setFixedHeight(32)
            b.setMinimumWidth(110)
            b.clicked.connect(fn)
            tb.addWidget(b)
        tb.addStretch(1)

        self._expand_paths_cb = QCheckBox("Expand paths")
        self._expand_paths_cb.setChecked(self._show_full_paths)
        self._expand_paths_cb.toggled.connect(self._on_full_paths_toggled)
        tb.addWidget(self._expand_paths_cb)
        root.addLayout(tb)

        root.addWidget(_sep())
        flags = QHBoxLayout()
        self.no_backup  = QCheckBox("Exclude from backup")
        self.no_restore = QCheckBox("Exclude from restore")
        self.no_backup.setChecked( e.get("details", {}).get("no_backup",  False))
        self.no_restore.setChecked(e.get("details", {}).get("no_restore", False))
        flags.addWidget(self.no_backup)
        flags.addSpacing(16)
        flags.addWidget(self.no_restore)
        flags.addStretch()
        root.addLayout(flags)

        root.addWidget(_sep())
        root.addWidget(_ok_cancel_buttons(self, self._accept))

    def _populate_lists(self) -> None:
        self._src_list.clear()
        self._dst_list.clear()
        expand = os.path.expanduser
        fmt    = expand if self._show_full_paths else (lambda p: apply_replacements(expand(p)))
        for src, dst in self.pairs:
            self._src_list.addItem(fmt(src))
            self._dst_list.addItem(fmt(dst))
        self._src_hint.setVisible(self._src_list.count() == 0)
        self._src_hint.setGeometry(self._src_list.rect())

    def _on_full_paths_toggled(self, checked: bool) -> None:
        self._show_full_paths = checked
        self._populate_lists()

    @staticmethod
    def _set_row_colours(lw: QListWidget, row: int, bg: QColor, fg: QColor) -> None:
        if 0 <= row < lw.count():
            item = lw.item(row)
            item.setData(Qt.ItemDataRole.BackgroundRole, bg)
            item.setData(Qt.ItemDataRole.ForegroundRole, fg)

    def _clear_all_colours(self) -> None:
        for lw in (self._src_list, self._dst_list):
            fg = lw.palette().color(lw.foregroundRole())
            for i in range(lw.count()):
                lw.item(i).setData(Qt.ItemDataRole.BackgroundRole, self._COL_CLEAR)
                lw.item(i).setData(Qt.ItemDataRole.ForegroundRole, fg)

    def _on_selection(self, active: QListWidget, partner: QListWidget, row: int) -> None:
        if self._suppress_sync or row < 0:
            return
        self._suppress_sync = True
        try:
            self._clear_all_colours()
            self._set_row_colours(active,  row, self._COL_ACTIVE_BG,  self._COL_ACTIVE_FG)
            self._set_row_colours(partner, row, self._COL_PARTNER_BG, self._COL_PARTNER_FG)
            partner.setCurrentRow(row)
        finally:
            self._suppress_sync = False

    def _pair_dialog(self, src: str = "", dst: str = "", title: str = "Add Entry") -> Optional[tuple[str, str]]:
        from PyQt6.QtGui import QFont, QFontMetrics
        from PyQt6.QtWidgets import QApplication

        scr       = QApplication.primaryScreen()
        screen    = scr.availableGeometry() if scr else None
        dlg_w     = max(700, min((screen.width() - 80) if screen else 900, 1200))
        dlg_max_h = (screen.height() - 80) if screen else 800

        dlg = QDialog(self)
        dlg.setWindowTitle(title)
        dlg.setMinimumWidth(dlg_w)
        if screen:
            dlg.setMaximumSize(screen.width() - 40, dlg_max_h)

        mono   = QFont("monospace", 13)
        fm     = QFontMetrics(mono)
        line_h = fm.height() + 6
        t      = current_theme()

        def _make_editor(prefill: str, placeholder: str) -> QPlainTextEdit:
            ed = QPlainTextEdit(prefill)
            ed.setFont(mono)
            ed.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
            ed.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            ed.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
            ed.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
            ed.setPlaceholderText(placeholder)
            ed.setStyleSheet(
                f"QPlainTextEdit{{background:{t['bg2']};color:{t['text']};"
                f"border:1px solid {t['header_sep']};border-radius:4px;padding:6px}}"
                f"QPlainTextEdit:focus{{border:1px solid {t['accent']};}}"
            )
            ph_lines = placeholder.count("\n") + 1

            def _adjust():
                doc_h = int(ed.document().size().height())
                eff   = max(doc_h, ph_lines if not ed.toPlainText() else 1)
                new_h = max(min(eff * line_h + 12, dlg_max_h // 3), line_h + 12)
                ed.setFixedHeight(new_h)
                dlg.adjustSize()

            ed.document().contentsChanged.connect(_adjust)
            _adjust()
            return ed

        vl = QVBoxLayout(dlg)
        vl.setSpacing(10)
        vl.setContentsMargins(16, 16, 16, 16)

        def _path_row(label: str, prefill: str, placeholder: str) -> QPlainTextEdit:
            vl.addWidget(QLabel(label))
            ed = _make_editor(prefill, placeholder)
            vl.addWidget(ed)
            vl.addLayout(_browse_buttons(dlg, ed))
            return ed

        src_ed = _path_row("Source path:",      src, "Enter path or use '📄 File' or '📁 Directory'")
        dst_ed = _path_row("Destination path:", dst, "Enter path or use '📄 File' or '📁 Directory'")

        vl.addWidget(_sep())
        bb     = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)  # type: ignore
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        vl.addWidget(bb)

        dlg.adjustSize()
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return None
        s, d = src_ed.toPlainText().strip(), dst_ed.toPlainText().strip()
        return (s, d) if (s or d) else None

    def _add_pair(self) -> None:
        result = self._pair_dialog()
        if result is None:
            return
        self.pairs.append([result[0], result[1]])
        self._populate_lists()
        self._src_list.setCurrentRow(len(self.pairs) - 1)

    def _edit_pair(self, row: int) -> None:
        if not (0 <= row < len(self.pairs)):
            return
        result = self._pair_dialog(*self.pairs[row], title=f"Edit pair #{row + 1}")
        if result is None:
            return
        self.pairs[row] = list(result)
        self._populate_lists()
        self._src_list.setCurrentRow(row)

    def _edit_selected(self) -> None:
        row = self._src_list.currentRow()
        if row < 0:
            row = self._dst_list.currentRow()
        self._edit_pair(row)

    def _remove_selected(self) -> None:
        row = self._src_list.currentRow()
        if row < 0:
            row = self._dst_list.currentRow()
        if 0 <= row < len(self.pairs):
            self.pairs.pop(row)
            self._populate_lists()
            new_row = min(row, len(self.pairs) - 1)
            if new_row >= 0:
                self._src_list.setCurrentRow(new_row)

    def _move_up(self) -> None:
        row = self._src_list.currentRow()
        if row > 0:
            self.pairs[row - 1], self.pairs[row] = self.pairs[row], self.pairs[row - 1]
            self._populate_lists()
            self._src_list.setCurrentRow(row - 1)

    def _move_down(self) -> None:
        row = self._src_list.currentRow()
        if 0 <= row < len(self.pairs) - 1:
            self.pairs[row], self.pairs[row + 1] = self.pairs[row + 1], self.pairs[row]
            self._populate_lists()
            self._src_list.setCurrentRow(row + 1)

    def _toggle_layout(self) -> None:
        self._entry_snapshot = {
            **self._entry_snapshot,
            "header":  self.hdr.currentText().strip(),
            "title":   self.title_edit.text().strip(),
            "details": {"no_backup":  self.no_backup.isChecked(),
                        "no_restore": self.no_restore.isChecked()},
        }
        self.stacked = not self.stacked
        self.done(2)

    def _accept(self) -> None:
        hdr   = self.hdr.currentText().strip()
        title = self.title_edit.text().strip()
        if not hdr or not title:
            QMessageBox.warning(self, "Error", "Header and title are required fields.")
            return

        valid_pairs = [(s.strip(), d.strip()) for s, d in self.pairs if s.strip() and d.strip()]
        if not valid_pairs:
            QMessageBox.warning(self, "Error", "At least one source and one destination path are required.")
            return

        if hdr not in S.headers:
            S.headers[hdr] = {"inactive": False, "color": "#ffffff"}

        self.result = {"header": hdr, "title": title,
                       "source": [s for s, _ in valid_pairs], "destination": [d for _, d in valid_pairs],
                       "details": {"no_backup":  self.no_backup.isChecked(), "no_restore": self.no_restore.isChecked()}}
        self.accept()


class MountDialog(QDialog):

    def __init__(self, parent, opt: Optional[dict]):
        super().__init__(parent)
        self.result: dict = {}
        self.setWindowTitle("Edit Drive" if opt else "New Drive")
        self.setMinimumSize(900, 500)
        opt = opt or {}
        t   = current_theme()
        layout = QVBoxLayout(self)
        layout.setSpacing(15)
        layout.addWidget(_hdr_label("Configure Drive"))
        layout.addWidget(_sep())
        form = QFormLayout()
        form.setSpacing(15)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapAllRows)

        def _field(key: str, placeholder: str) -> QLineEdit:
            f = QLineEdit(opt.get(key, ""))
            f.setPlaceholderText(placeholder)
            return f

        def _info_label(text: str, tooltip: str) -> QLabel:
            lbl = QLabel(text)
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lbl.setToolTip(tooltip)
            lbl.setToolTipDuration(600_000)
            lbl.setCursor(Qt.CursorShape.WhatsThisCursor)
            lbl.setStyleSheet(f"color:{t['accent2']};")
            return lbl
        self.name = _field("drive_name", "e.g. Backup 1")
        form.addRow(QLabel("Drive name:"))
        form.addRow(self.name)
        self.mount_path = _field("mount_path", "e.g. smb://192.168.0.38/Backup Drive/")
        form.addRow(_info_label("󰔨 Mount path (optional)",
                                "<u>Mount Path (optional)</u><br><br>"
                                "Only needed if this drive cannot be detected automatically.<br><br>"
                                "<i>Leave empty</i> for standard USB/SATA drives — Backup Helper finds them "
                                "automatically under <code>/run/media/&lt;user&gt;/&lt;name&gt;</code>, "
                                "<code>/media/&lt;user&gt;/&lt;name&gt;</code> or <code>/mnt/&lt;name&gt;</code>"
                                " using the name from above.<br><br>"
                                "<i>Fill in</i> when the drive is mounted elsewhere (sshfs, KDE Connect, etc.)."))
        form.addRow(self.mount_path)
        self.mount = _field("mount_command", "udisksctl mount --block-device /dev/sdX1")
        form.addRow(_info_label("󰔨 Mount command:",
                                "<u>Mount Command</u><br><br>"
                                "The command is executed non-interactively — <b>no password prompt will appear</b>."
                                "<br><br><b>sshfs:</b> SSH connections must use key-based authentication.<br>"
                                "Set up a key pair first:<br>"
                                "<code>ssh-keygen -t ed25519 &amp;&amp; ssh-copy-id user@host</code><br><br>"
                                "Example: <code>sshfs user@host:/remote/path ~/local/mountpoint</code><br><br>"
                                "<b>udisksctl / mount:</b> Work as usual for local drives.<br>"
                                "<b>kdeconnect-cli:</b> The device must already be paired and reachable.<br><br>"
                                "<small>Allowed commands: mount, umount, mount.cifs, udisksctl, kdeconnect-cli, "
                                "sshfs, fusermount3, fusermount</small>"))
        form.addRow(self.mount)
        self.unmnt    = _field("unmount_command", "udisksctl unmount --block-device /dev/sdX1")
        lbl_unmnt     = QLabel("Unmount command:")
        lbl_unmnt.setAlignment(Qt.AlignmentFlag.AlignCenter)
        form.addRow(lbl_unmnt)
        form.addRow(self.unmnt)
        layout.addLayout(form)
        layout.addStretch()
        layout.addWidget(_sep())
        layout.addWidget(_ok_cancel_buttons(self, self._accept))

    def _accept(self) -> None:
        if not self.name.text().strip():
            QMessageBox.warning(self, "Error", "Name is a required field.")
            return
        self.result = {"drive_name": self.name.text().strip(), "mount_path": self.mount_path.text().strip(),
                       "mount_command": self.mount.text().strip(), "unmount_command": self.unmnt.text().strip()}
        self.accept()


class MountsDialog(_ListDialog):

    def __init__(self, parent):
        self.was_changed: bool = False
        super().__init__(parent, "Mount Options", (700, 460), "Mounted Drives",
                         [("🆕 New", "_new"), ("✎ Edit", "_edit"), ("✕ Remove", "_del")])

    def _refresh(self) -> None:
        from drive_utils import get_mounts, is_mounted
        self.item_list.clear()
        t   = current_theme()
        out = get_mounts()
        for opt in S.mount_options:
            mounted = is_mounted(opt, out)
            status  = "●" if mounted else "○"
            item    = QListWidgetItem(f"  {status}  {opt.get('drive_name', '?')}")
            item.setForeground(QColor(t["green"] if mounted else t["text_dim"]))
            item.setData(Qt.ItemDataRole.UserRole, opt)
            self.item_list.addItem(item)

    def _new(self) -> None:
        dlg = MountDialog(self, None)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            S.mount_options.append(dlg.result)
            save_profile()
            self.was_changed = True
            self._refresh()

    def _edit(self) -> None:
        opt = self._selected_data()
        if not opt: return
        dlg = MountDialog(self, opt)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            idx = next((i for i, o in enumerate(S.mount_options) if o is opt), None)
            if idx is not None:
                S.mount_options[idx] = dlg.result
            save_profile()
            self.was_changed = True
            self._refresh()

    def _del(self) -> None:
        opt = self._selected_data()
        if opt:
            S.mount_options = [o for o in S.mount_options if o is not opt]
            save_profile()
            self.was_changed = True
            self._refresh()


class HeaderSettingsDialog(QDialog):

    def __init__(self, parent):
        super().__init__(parent)
        self._headers_backup = copy.deepcopy(S.headers)
        self._entries_backup = copy.deepcopy(S.entries)
        self.was_changed: bool = False
        self.setWindowTitle("Header Settings")
        self.setMinimumSize(750, 500)
        layout = QVBoxLayout(self)
        layout.addWidget(_hdr_label("Headers"))
        layout.addWidget(_sep())
        self.item_list = QListWidget()
        layout.addWidget(self.item_list, 1)
        layout.addLayout(_btn_row([("🆕 New", self._new), ("🎨 Color", self._color), ("⏸ Toggle active", self._toggle),
                                   ("✕ Delete", self._delete), ("↑ Up", self._move_up), ("↓ Down", self._move_down)]))
        layout.addWidget(_sep())
        layout.addWidget(_ok_cancel_buttons(self, self.accept, "Save && Close"))
        self._refresh()

    def reject(self) -> None:
        S.headers = self._headers_backup
        S.entries = self._entries_backup
        super().reject()

    def _refresh(self) -> None:
        t   = current_theme()
        row = self.item_list.currentRow()
        self.item_list.clear()
        for name, d in S.headers.items():
            status = "  [inactive]" if d["inactive"] else ""
            item   = QListWidgetItem(f"  {name}{status}")
            item.setForeground(QColor(t["text_dim"] if d["inactive"] else d["color"]))
            item.setData(Qt.ItemDataRole.UserRole, name)
            self.item_list.addItem(item)
        if 0 <= row < self.item_list.count():
            self.item_list.setCurrentRow(row)

    def _selected_name(self) -> Optional[str]:
        item = self.item_list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def _new(self) -> None:
        name, ok = _ask_text(self, "New Header", "Header name:")
        if not ok or not name.strip():
            return
        name = name.strip()
        if name in S.headers:
            QMessageBox.warning(self, "Duplicate", f"Header '{name}' already exists.")
            return
        col = QColorDialog.getColor(QColor(current_theme()["accent"]), self, "Choose header colour")
        S.headers[name] = {"inactive": False, "color": col.name() if col.isValid() else "#ffffff"}
        self.was_changed = True
        self._refresh()
        for i in range(self.item_list.count()):
            if self.item_list.item(i).data(Qt.ItemDataRole.UserRole) == name:
                self.item_list.setCurrentRow(i)
                break

    def _color(self) -> None:
        name = self._selected_name()
        if not name: return
        col = QColorDialog.getColor(QColor(S.headers[name]["color"]), self)
        if col.isValid():
            S.headers[name]["color"] = col.name()
            self.was_changed = True
            self._refresh()

    def _toggle(self) -> None:
        name = self._selected_name()
        if name:
            S.headers[name]["inactive"] = not S.headers[name]["inactive"]
            self.was_changed = True
            self._refresh()

    def _delete(self) -> None:
        name = self._selected_name()
        if not name: return
        if QMessageBox.question(self, "Delete", f"Delete header '{name}' and all its entries?",
                                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes:
            del S.headers[name]
            S.entries    = [e for e in S.entries if e["header"] != name]
            self.was_changed = True
            self._refresh()

    def _move_header(self, direction: int) -> bool:
        name = self._selected_name()
        if not name: return False
        keys    = list(S.headers.keys())
        idx     = keys.index(name)
        new_idx = idx + direction
        if not (0 <= new_idx < len(keys)): return False
        keys[idx], keys[new_idx] = keys[new_idx], keys[idx]
        S.headers = {k: S.headers[k] for k in keys}
        self._refresh()
        self.item_list.setCurrentRow(new_idx)
        return True

    def _move_up(self)   -> None:
        if self._move_header(-1): self.was_changed = True

    def _move_down(self) -> None:
        if self._move_header(+1): self.was_changed = True


class ProfilesDialog(QDialog):

    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle("Profile Manager")
        self.setMinimumSize(700, 520)
        self.was_changed: bool = False
        t      = current_theme()
        layout = QVBoxLayout(self)
        layout.addWidget(_hdr_label("Profile Manager"))
        layout.addWidget(_sep())
        self._active_lbl = QLabel()
        self._active_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._active_lbl.setStyleSheet(f"color:{t['accent']};font-weight:bold;padding:4px;")
        layout.addWidget(self._active_lbl)
        self.item_list = QListWidget()
        self.item_list.itemDoubleClicked.connect(self._load)
        layout.addWidget(self.item_list, 1)
        layout.addLayout(_btn_row([("▶ Load", self._load), ("🆕 New",      self._new),
                                   ("⎘ Duplicate", self._copy), ("✕ Delete", self._del)]))
        layout.addLayout(_btn_row([("⬆ Import", self._import), ("⬇ Export", self._export)]))
        layout.addWidget(_sep())
        close_btn = QPushButton("✕ Close")
        close_btn.clicked.connect(self.accept)
        layout.addWidget(close_btn)
        self._refresh()

    def _activate_profile(self, name: str) -> bool:
        if load_profile(_PROFILES_DIR / f"{name}.json"):
            save_profile()
            self.was_changed = True
            self._refresh()
            return True
        return False

    def _refresh(self) -> None:
        t   = current_theme()
        row = self.item_list.currentRow()
        self._active_lbl.setText(f"Active profile:  {S.profile_name or '—'}")
        self.item_list.clear()
        for name in list_profiles():
            active = name == S.profile_name
            item   = QListWidgetItem(f"  {'✓ ' if active else '  '}{name}")
            item.setForeground(QColor(t["accent"] if active else t["text"]))
            item.setData(Qt.ItemDataRole.UserRole, name)
            self.item_list.addItem(item)
        if 0 <= row < self.item_list.count(): self.item_list.setCurrentRow(row)

    def _selected_name(self) -> Optional[str]:
        item = self.item_list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def _load(self) -> None:
        name = self._selected_name()
        if not name:
            QMessageBox.information(self, "Load Profile", "Please select a profile first.")
            return
        if name == S.profile_name:
            QMessageBox.information(self, "Load Profile", f"'{name}' is already the active profile.")
            return
        if S.profile_name:
            old_path = _PROFILES_DIR / f"{S.profile_name}.json"
            try:
                data = json.loads(old_path.read_text(encoding="utf-8"))
                if data.pop("is_default", None): _atomic_write(old_path, data)
            except (FileNotFoundError, json.JSONDecodeError, OSError) as exc:
                logger.warning("_load: could not clear is_default from '%s': %s", old_path.name, exc)

        if self._activate_profile(name):
            QMessageBox.information(self, "Profile Loaded", f"Profile '{name}' is now active.")
        else: QMessageBox.critical(self, "Error", f"Could not load profile '{name}'.")

    def _new(self) -> None:
        name = _ask_profile_name("New Profile", "", self)
        if not name: return
        dest = _PROFILES_DIR / f"{name}.json"
        if dest.exists() and QMessageBox.question(
                self, "Overwrite?", f"Profile '{name}' already exists. Overwrite it?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
            return
        fresh = State()
        S.profile_name        = name
        S.entries             = fresh.entries
        S.headers             = fresh.headers
        S.mount_options       = fresh.mount_options
        S.system_manager_ops  = fresh.system_manager_ops
        S.system_files        = fresh.system_files
        S.basic_packages      = fresh.basic_packages
        S.aur_packages        = fresh.aur_packages
        S.specific_packages   = fresh.specific_packages
        S.user_shell          = fresh.user_shell
        S.ui                  = dict(fresh.ui)
        save_profile()
        self.was_changed = True
        self._refresh()
        QMessageBox.information(self, "Profile Created", f"Blank profile '{name}' created and is now active.\n\n"
            "Go to Settings → Header Settings to add headers before creating entries.")

    def _copy(self) -> None:
        src_name = self._selected_name() or S.profile_name
        if not src_name:
            QMessageBox.information(self, "Duplicate", "No profile selected.")
            return
        name = _ask_profile_name("Duplicate Profile", f"{src_name} copy", self)
        if not name: return
        src_path = _PROFILES_DIR / f"{src_name}.json"
        if not src_path.exists() and src_name == S.profile_name:
            save_profile()
            self.was_changed = True
        if not src_path.exists():
            QMessageBox.warning(self, "Duplicate", f"Source file for '{src_name}' not found.")
            return
        dest = _PROFILES_DIR / f"{name}.json"
        if dest.exists() and QMessageBox.question(
                self, "Overwrite?", f"Profile '{name}' already exists. Overwrite it?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
            return
        shutil.copy2(src_path, dest)
        self.was_changed = True
        self._refresh()
        QMessageBox.information(self, "Duplicated", f"'{src_name}' duplicated as '{name}'.")

    def _del(self) -> None:
        name = self._selected_name()
        if not name: return
        if name == S.profile_name:
            QMessageBox.warning(self, "Delete Profile", "Cannot delete the currently active profile.")
            return
        if QMessageBox.question(self, "Delete Profile", f"Permanently delete profile '{name}'?",
                                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No) \
                == QMessageBox.StandardButton.Yes:
            try:
                (_PROFILES_DIR / f"{name}.json").unlink(missing_ok=True)
                self.was_changed = True
                self._refresh()
            except Exception as exc: QMessageBox.critical(self, "Error", f"Could not delete profile: {exc}")

    def _import(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Import profile(s)", str(_HOME),
            "Profile files (*.json *.tar.gz *.tgz);;JSON (*.json);;Archive (*.tar.gz *.tgz)")
        if not path: return
        _PROFILES_DIR.mkdir(parents=True, exist_ok=True)
        if path.endswith((".tar.gz", ".tgz")):
            self._import_archive(path)
            return
        name = _ask_profile_name("Import Profile", Path(path).stem, self)
        if not name: return
        dest = _PROFILES_DIR / f"{name}.json"
        if dest.exists() and QMessageBox.question(
                self, "Overwrite?", f"Profile '{name}' already exists. Overwrite it?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
            return
        shutil.copy2(path, dest)
        self._refresh()
        if QMessageBox.question(self, "Import Complete", f"'{name}' imported successfully.\nLoad it now?",
                                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes:

            if not self._activate_profile(name):
                QMessageBox.critical(self, "Error", f"Could not load profile '{name}'.")

    def _import_archive(self, path: str) -> None:
        try:
            with tarfile.open(path, "r:gz") as tar:
                members = [m for m in tar.getmembers() if m.isfile() and m.name.endswith(".json")]
                if not members:
                    QMessageBox.warning(self, "Import", "The archive contains no .json profile files.")
                    return
                imported, skipped = [], []
                for member in members:
                    stem = Path(member.name).stem
                    if not _PROFILE_RE.match(stem):
                        skipped.append(f"{stem}  (invalid name)")
                        continue
                    dest      = _PROFILES_DIR / f"{stem}.json"
                    overwrite = True
                    if dest.exists():
                        ans = QMessageBox.question(self, "Overwrite?", f"Profile '{stem}' already exists. Overwrite?",
                            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No | QMessageBox.StandardButton.Cancel)
                        if ans == QMessageBox.StandardButton.Cancel: break
                        overwrite = ans == QMessageBox.StandardButton.Yes
                    if overwrite:
                        f = tar.extractfile(member)
                        if f:
                            dest.write_bytes(f.read())
                        imported.append(stem)
                    else: skipped.append(f"{stem}  (skipped)")
        except Exception as exc:
            QMessageBox.critical(self, "Import Failed", str(exc))
            return
        self._refresh()
        parts = []
        if imported: parts.append("Imported:\n  " + "\n  ".join(imported))
        if skipped:  parts.append("Skipped:\n  "  + "\n  ".join(skipped))
        QMessageBox.information(self, "Import Complete", "\n\n".join(parts) or "Nothing imported.")
        if len(imported) == 1 and QMessageBox.question(self, "Load Profile", f"Load '{imported[0]}' now?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes:
            if not self._activate_profile(imported[0]):
                QMessageBox.critical(self, "Error", f"Could not load profile '{imported[0]}'.")

    def _export(self) -> None:
        profiles = list_profiles()
        if not profiles:
            QMessageBox.information(self, "Export", "No profiles to export.")
            return
        selected_name = self._selected_name() or S.profile_name
        export_all    = False

        if len(profiles) > 1:
            from PyQt6.QtWidgets import QButtonGroup, QRadioButton
            choice_dlg = QDialog(self)
            choice_dlg.setWindowTitle("Export — What to export?")
            vl     = QVBoxLayout(choice_dlg)
            vl.addWidget(QLabel("What would you like to export?"))
            rb_sel = QRadioButton(f"Selected profile only  ({selected_name})  →  .json")
            rb_all = QRadioButton(f"All {len(profiles)} profiles  →  .tar.gz archive")
            rb_sel.setChecked(True)
            bg = QButtonGroup(choice_dlg)
            bg.addButton(rb_sel)
            bg.addButton(rb_all)
            vl.addWidget(rb_sel)
            vl.addWidget(rb_all)
            bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)  # type: ignore
            bb.accepted.connect(choice_dlg.accept)
            bb.rejected.connect(choice_dlg.reject)
            vl.addWidget(bb)
            if choice_dlg.exec() != QDialog.DialogCode.Accepted: return
            export_all = rb_all.isChecked()

        if export_all:
            ts      = datetime.now().strftime("%Y%m%d_%H%M%S")
            path, _ = QFileDialog.getSaveFileName(self, "Export all profiles",
                                          str(_HOME / f"backup_helper_profiles_{ts}.tar.gz"), "Archive (*.tar.gz)")
            if not path: return
            if not path.endswith(".tar.gz"): path += ".tar.gz"
            try:
                if S.profile_name:
                    save_profile()
                    self.was_changed = True
                with tarfile.open(path, "w:gz") as tar:
                    for name in profiles:
                        src = _PROFILES_DIR / f"{name}.json"
                        if src.exists(): tar.add(src, arcname=f"{name}.json")
                QMessageBox.information(self, "Exported", f"All {len(profiles)} profiles exported to:\n{path}")
            except Exception as exc: QMessageBox.critical(self, "Export Failed", str(exc))
            return

        name = selected_name
        if not name:
            QMessageBox.information(self, "Export", "No profile to export.")
            return
        src = _PROFILES_DIR / f"{name}.json"
        if not src.exists():
            if name == S.profile_name:
                save_profile()
                self.was_changed = True
            else:
                QMessageBox.warning(self, "Export", f"Profile file for '{name}' not found.")
                return
        path, _ = QFileDialog.getSaveFileName(self, "Export profile",
                                              str(_HOME / f"{name}.json"), "JSON (*.json)")
        if path:
            shutil.copy2(src, path)
            QMessageBox.information(self, "Exported", f"Profile '{name}' exported to:\n{path}")


class LogViewer(_TextViewDialog):

    def __init__(self, parent):
        t = current_theme()
        super().__init__(parent, "Log Viewer", (1350, 950),
                         extra_buttons=[("🔄 Refresh", self._load), ("🗑 Clear", self._clear)])
        top = QWidget()
        top.setStyleSheet(f"background:{t['bg2']};border-bottom:1px solid {t['header_sep']};")
        tl  = QHBoxLayout(top)
        tl.setContentsMargins(14, 8, 14, 8)
        tl.addStretch()
        tl.addWidget(_hdr_label("📋 Log File"))
        tl.addStretch(1)
        tl.addWidget(QLabel(apply_replacements(str(_LOG_FILE))))
        layout = self.layout()
        if isinstance(layout, QVBoxLayout): layout.insertWidget(0, top)
        self._load()

    def _load(self) -> None:
        if not _LOG_FILE.exists():
            self.view.setPlainText("No log file found.")
            return
        try:
            lines  = _LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
            prefix = f"[… last 2000 of {len(lines)} lines …]\n" if len(lines) > 2000 else ""
            self.view.setPlainText(prefix + "\n".join(lines[-2000:]))
            cursor = self.view.textCursor()
            cursor.movePosition(QTextCursor.MoveOperation.End)
            self.view.setTextCursor(cursor)
        except Exception as e: self.view.setPlainText(f"Error reading log file: {e}")

    def _clear(self) -> None:
        if QMessageBox.question(self, "Clear log", "Permanently delete all log entries?",
                                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No) \
                == QMessageBox.StandardButton.Yes:
            try:
                _LOG_FILE.write_text("", encoding="utf-8")
            except OSError as e:
                QMessageBox.warning(self, "Error", f"Could not clear log: {e}")
                return
            self._load()


class SysInfoDialog(_TextViewDialog):
    done_sig = pyqtSignal(str)

    def __init__(self, parent):
        super().__init__(parent, "System Information", (1350, 825), font_size=font_sz(2))
        self.view.setPlainText("⏳ Loading system information…")
        self.done_sig.connect(self.view.setPlainText)
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self) -> None:
        try:
            r = subprocess.run(["inxi", "-SMCGAz", "--no-host", "--color", "0"],
                               capture_output=True, text=True, timeout=15,
                               env={**os.environ, "LANG": "C"}, check=False)
            self.done_sig.emit(r.stdout.strip() or r.stderr.strip() or "No output received from inxi.")
        except FileNotFoundError:
            self.done_sig.emit("'inxi' is not installed.\n\n"
                               "Installation:\n"
                               "  Arch:     sudo pacman -S inxi\n"
                               "  Debian:   sudo apt install inxi\n"
                               "  Fedora:   sudo dnf install inxi\n"
                               "  openSUSE: sudo zypper install inxi\n")
        except subprocess.TimeoutExpired: self.done_sig.emit("System information request timed out.")
        except Exception as exc: self.done_sig.emit(f"An unexpected error occurred: {exc}")

    def closeEvent(self, event) -> None:
        try:
            self.done_sig.disconnect()
        except RuntimeError: pass
        super().closeEvent(event)

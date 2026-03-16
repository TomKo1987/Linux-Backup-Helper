from pathlib import Path
from typing import Optional
from datetime import datetime
import os, shutil, subprocess, threading, re, tarfile, json

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QColor, QTextCursor
from PyQt6.QtWidgets import (
    QCheckBox, QColorDialog, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
    QLabel, QMessageBox, QPushButton, QSizePolicy, QTextEdit, QWidget, QListWidget,
    QFormLayout, QHBoxLayout, QInputDialog, QListWidgetItem, QLineEdit, QVBoxLayout
)

from themes import current_theme
from state import (
    list_profiles, load_profile, save_profile, logger, _atomic_write,
    S, _HOME, _LOG_FILE, _PROFILES_DIR, _PROFILE_RE, apply_replacements, _norm_paths
)

def _sep() -> QWidget:
    line = QWidget()
    line.setFixedHeight(1)
    line.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
    line.setStyleSheet(f"background:{current_theme()['header_sep']};")
    return line


def _hdr_label(text: str, color: str = "", size: int = 15) -> QLabel:
    lbl = QLabel(text)
    lbl.setStyleSheet(f"font-size:{size}px;font-weight:bold; color:{color or current_theme()['accent']};padding:4px 0;")
    lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
    return lbl


def _ok_cancel_buttons(dialog: QDialog, ok_fn, ok_label: str = "Save", cancel_label: str = "Cancel") -> QDialogButtonBox:
    bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel) # type: ignore
    bb.button(QDialogButtonBox.StandardButton.Ok).setText(ok_label)
    bb.button(QDialogButtonBox.StandardButton.Cancel).setText(cancel_label)
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


def _browse_path(parent: QWidget, target: QTextEdit, mode: str = "file") -> None:
    if mode == "dir":
        path = QFileDialog.getExistingDirectory(parent, "Select directory", str(_HOME))
    else:
        path, _ = QFileDialog.getOpenFileName(parent, "Select file", str(_HOME))
    if not path:
        return
    lines = [l.strip() for l in target.toPlainText().splitlines() if l.strip()]
    if path not in lines:
        lines.append(path)
    fill = getattr(target, "_fill_lines", None)
    if fill is not None:
        fill(lines)
    else:
        target.setPlainText("\n".join(lines))


class _ListDialog(QDialog):

    def __init__(self, parent, title: str, size: tuple[int, int], hdr_text: str, btn_specs: list[tuple[str, str]], close_label: str = "✕  Close"):
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

    def _refresh(self):
        raise NotImplementedError

    def _selected_data(self):
        item = self.item_list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None


# noinspection PyUnresolvedReferences
class EntryDialog(QDialog):
    _COL_ACTIVE_BG   = QColor("#1a5fb4")
    _COL_ACTIVE_FG   = QColor("#ffffff")
    _COL_PARTNER_BG  = QColor("#c17d11")
    _COL_PARTNER_FG  = QColor("#ffffff")
    _COL_CLEAR       = QColor(0, 0, 0, 0)

    def __init__(self, parent, entry: Optional[dict], *, stacked: bool = False, _pairs: Optional[list[list[str]]] = None):
        super().__init__(parent)
        self.result:          dict            = {}
        self.pairs:          list[list[str]] = list(_pairs) if _pairs else []
        self.stacked:        bool            = stacked
        self._suppress_sync:  bool            = False
        self._entry_snapshot: dict            = entry or {}
        self.setWindowTitle("Edit Entry" if entry else "New Entry")
        self._build(self._entry_snapshot)

    @staticmethod
    def _parse_text_input(raw) -> list[str]:
        if isinstance(raw, str):
            result = []
            for line in raw.splitlines():
                line = re.sub(r"^\d+:\s*", "", line.strip())
                if line:
                    result.append(line)
            return result
        return _norm_paths(raw)

    def _compute_size(self) -> tuple[int, int]:
        from PyQt6.QtGui import QFontMetrics, QFont
        from PyQt6.QtWidgets import QApplication

        mono = QFont("monospace")
        mono.setPointSize(15)
        fm   = QFontMetrics(mono)

        all_paths = [p for pair in self.pairs for p in pair]
        if not all_paths:
            max_px = 400
        else:
            max_px = max(fm.horizontalAdvance(p) for p in all_paths)

        screen  = QApplication.primaryScreen().availableGeometry()
        padding = 80

        if self.stacked:
            w = max(1110,  min(max_px + 120,      screen.width()  - padding)) + 20
            h = max(900,  min(screen.height() - padding, 1100)) + 10
        else:
            w = max(1200, min(max_px * 2 + 150,  screen.width()  - padding))
            h = max(800,  min(screen.height() - padding, 950)) + 10

        return w, h

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if not event.spontaneous():
            w, h = self._compute_size()
            self.setMinimumSize(w, h)
            self.resize(w, h)

    def _build(self, e: dict) -> None:
        from PyQt6.QtWidgets import QSplitter, QAbstractItemView

        t = current_theme()

        src_paths = _norm_paths(e.get("source", []))
        dst_paths = _norm_paths(e.get("destination", []))

        if not self.pairs:
            n = max(len(src_paths), len(dst_paths))
            self.pairs = [[src_paths[i] if i < len(src_paths) else "", dst_paths[i] if i < len(dst_paths) else ""] for i in range(n)]

        root = QVBoxLayout(self)
        root.setSpacing(8)
        root.setContentsMargins(12, 12, 12, 12)

        title_row = QHBoxLayout()
        title_row.addWidget(_hdr_label("Edit Entry" if e else "New Entry"))
        title_row.addStretch()
        lbl_mode = "Side-by-Side View" if self.stacked else "Stacked View"
        layout_btn = QPushButton(lbl_mode)
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

        lw_style = ("QListWidget { font-family: monospace; font-size: 15px; }"
                    "QListWidget::item { padding: 6px 6px; }")

        self._src_list = QListWidget()
        self._dst_list = QListWidget()
        for lw in (self._src_list, self._dst_list):
            lw.setStyleSheet(lw_style)
            lw.setAlternatingRowColors(True)
            lw.setMinimumHeight(180)
            lw.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
            lw.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)

        self._populate_lists()

        self._src_list.currentRowChanged.connect(lambda r: self._on_selection(self._src_list, self._dst_list, r))
        self._dst_list.currentRowChanged.connect(lambda r: self._on_selection(self._dst_list, self._src_list, r))

        self._src_list.itemDoubleClicked.connect(lambda item: self._edit_pair(self._src_list.row(item)))
        self._dst_list.itemDoubleClicked.connect(lambda item: self._edit_pair(self._dst_list.row(item)))

        def _lw_panel(label: str, _lw: QListWidget) -> QWidget:
            w  = QWidget()
            vl = QVBoxLayout(w)
            vl.setContentsMargins(0, 0, 0, 0)
            vl.setSpacing(4)
            lbl = QLabel(label)
            lbl.setStyleSheet(f"font-weight:bold; font-size:16px; color:{t['accent']}; padding:1px 0;")
            vl.addWidget(lbl)
            vl.addWidget(_lw, 1)
            return w

        src_panel = _lw_panel("Source", self._src_list)
        dst_panel = _lw_panel("Destination", self._dst_list)

        orientation = Qt.Orientation.Vertical if self.stacked else Qt.Orientation.Horizontal
        self._splitter = QSplitter(orientation)
        self._splitter.setChildrenCollapsible(False)
        self._splitter.addWidget(src_panel)
        self._splitter.addWidget(dst_panel)
        self._splitter.setSizes([10000, 10000])
        root.addWidget(self._splitter, 1)

        root.addWidget(_sep())
        tb = QHBoxLayout()
        tb.setSpacing(6)

        def _mk_btn(label: str, tip: str, fn) -> QPushButton:
            b = QPushButton(label)
            b.setToolTip(tip)
            b.setFixedHeight(32)
            b.setMinimumWidth(110)
            b.clicked.connect(fn)
            return b

        tb.addWidget(_mk_btn("➕  Add Pair",  "Add a new source/destination pair",     self._add_pair))
        tb.addWidget(_mk_btn("✏️  Edit",       "Edit selected pair (or double-click)", self._edit_selected))
        tb.addWidget(_mk_btn("🗑  Remove",     "Remove selected pair",                  self._remove_selected))
        tb.addWidget(_mk_btn("▲  Move Up",    "Move selected pair up",                  self._move_up))
        tb.addWidget(_mk_btn("▼  Move Down",  "Move selected pair down",                self._move_down))
        tb.addStretch()
        root.addLayout(tb)

        root.addWidget(_sep())
        flags = QHBoxLayout()
        self.no_backup  = QCheckBox("Exclude from backup")
        self.no_restore = QCheckBox("Exclude from restore")
        self.no_backup.setChecked(e.get("details", {}).get("no_backup", False))
        self.no_restore.setChecked(e.get("details", {}).get("no_restore", False))
        flags.addWidget(self.no_backup)
        flags.addSpacing(16)
        flags.addWidget(self.no_restore)
        flags.addStretch()
        root.addLayout(flags)

        root.addWidget(_sep())
        root.addWidget(_ok_cancel_buttons(self, self._accept, ok_label="Save", cancel_label="Cancel"))

    def _populate_lists(self) -> None:
        self._src_list.clear()
        self._dst_list.clear()
        for src, dst in self.pairs:
            self._src_list.addItem(src)
            self._dst_list.addItem(dst)

    @staticmethod
    def _set_row_colours(lw: QListWidget, row: int, bg: QColor, fg: QColor) -> None:
        if 0 <= row < lw.count():
            item = lw.item(row)
            item.setData(Qt.ItemDataRole.BackgroundRole, bg)
            item.setData(Qt.ItemDataRole.ForegroundRole, fg)

    def _clear_all_colours(self) -> None:
        transparent = QColor(0, 0, 0, 0)
        for lw in (self._src_list, self._dst_list):
            default_fg = lw.palette().color(lw.foregroundRole())
            for i in range(lw.count()):
                lw.item(i).setData(Qt.ItemDataRole.BackgroundRole, transparent)
                lw.item(i).setData(Qt.ItemDataRole.ForegroundRole, default_fg)

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
        from PyQt6.QtWidgets import QApplication, QPlainTextEdit, QSizePolicy

        screen = QApplication.primaryScreen().availableGeometry()
        dlg_w = max(700, min(screen.width() - 80, 1200))
        dlg_max_h = screen.height() - 80

        dlg = QDialog(self)
        dlg.setWindowTitle(title)
        dlg.setMinimumWidth(dlg_w)
        dlg.setMaximumSize(screen.width() - 40, dlg_max_h)

        vl = QVBoxLayout(dlg)
        vl.setSpacing(10)
        vl.setContentsMargins(16, 16, 16, 16)

        mono = QFont("monospace")
        mono.setPointSize(13)
        fm = QFontMetrics(mono)
        line_h = fm.height() + 6

        def _make_editor(prefill: str) -> QPlainTextEdit:
            editor = QPlainTextEdit(prefill)
            editor.setFont(mono)
            editor.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
            editor.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            editor.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
            editor.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
            editor.setPlaceholderText("Enter path or use Browse buttons →")
            t = current_theme()
            editor.setStyleSheet(f"QPlainTextEdit {{"
                                 f"background: {t['bg2']};"
                                 f"color: {t['text']};"
                                 f"border: 1px solid {t['header_sep']};"
                                 f"border-radius: 4px;"
                                 f"padding: 6px}}"
                                 f"QPlainTextEdit:focus {{"
                                 f"border: 1px solid {t['accent']};}}")

            def _adjust_height():
                doc_h = int(editor.document().size().height())
                lines = max(1, doc_h)
                new_h = min(lines * line_h + 12, dlg_max_h // 3)
                new_h = max(new_h, line_h + 12)
                editor.setFixedHeight(new_h)
                dlg.adjustSize()

            editor.document().contentsChanged.connect(_adjust_height)
            _adjust_height()
            return editor

        def _path_row(label: str, prefill: str) -> QPlainTextEdit:
            vl.addWidget(QLabel(label))
            editor = _make_editor(prefill)
            vl.addWidget(editor)
            btn_row = QHBoxLayout()
            btn_row.setContentsMargins(0, 0, 0, 0)
            btn_row.addStretch()
            for btn_lbl, mode in [("📄 File", "file"), ("📁 Directory", "dir")]:
                b = QPushButton(btn_lbl)
                b.setFixedHeight(28)
                b.clicked.connect(lambda _c=False, _e=editor, _m=mode: _browse_inline(_e, _m))
                btn_row.addWidget(b)
            vl.addLayout(btn_row)
            return editor

        def _browse_inline(editor: QPlainTextEdit, mode: str) -> None:
            if mode == "dir":
                p = QFileDialog.getExistingDirectory(dlg, "Select directory", str(_HOME))
            else:
                p, _ = QFileDialog.getOpenFileName(dlg, "Select file", str(_HOME))
            if p:
                editor.setPlainText(p)
                cursor = editor.textCursor()
                cursor.movePosition(cursor.MoveOperation.End)
                editor.setTextCursor(cursor)

        src_ed = _path_row("Source path:", src)
        dst_ed = _path_row("Destination path:", dst)

        vl.addWidget(_sep())
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)  # type: ignore
        bb.button(QDialogButtonBox.StandardButton.Ok).setText("OK")
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        vl.addWidget(bb)

        dlg.adjustSize()
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return None

        s = src_ed.toPlainText().strip()
        d = dst_ed.toPlainText().strip()
        if not s and not d:
            return None
        return s, d

    def _add_pair(self) -> None:
        result = self._pair_dialog()
        if result is None:
            return
        self.pairs.append([result[0], result[1]])
        self._populate_lists()
        self._src_list.setCurrentRow(len(self.pairs) - 1)

    def _edit_pair(self, row: int) -> None:
        if row < 0 or row >= len(self.pairs):
            return
        result = self._pair_dialog(src=self.pairs[row][0], dst=self.pairs[row][1], title=f"Edit pair #{row + 1}")
        if result is None:
            return
        self.pairs[row] = [result[0], result[1]]
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
        if row <= 0:
            return
        self.pairs[row - 1], self.pairs[row] = self.pairs[row], self.pairs[row - 1]
        self._populate_lists()
        self._src_list.setCurrentRow(row - 1)

    def _move_down(self) -> None:
        row = self._src_list.currentRow()
        if row < 0 or row >= len(self.pairs) - 1:
            return
        self.pairs[row + 1], self.pairs[row] = self.pairs[row], self.pairs[row + 1]
        self._populate_lists()
        self._src_list.setCurrentRow(row + 1)

    def _toggle_layout(self) -> None:
        self._entry_snapshot = {
            **self._entry_snapshot,
            "header": self.hdr.currentText().strip(),
            "title":  self.title_edit.text().strip(),
            "details": {"no_backup":  self.no_backup.isChecked(), "no_restore": self.no_restore.isChecked()}}
        self.stacked = not self.stacked
        self.done(2)

    def _accept(self) -> None:
        hdr = self.hdr.currentText().strip()
        title = self.title_edit.text().strip()

        if not hdr or not title:
            QMessageBox.warning(self, "Error", "Header and title are required fields.")
            return

        valid_pairs = [(p[0].strip(), p[1].strip()) for p in self.pairs if p[0].strip() and p[1].strip()]

        if not valid_pairs:
            QMessageBox.warning(self, "Error", "At least one source and one destination path are required.")
            return

        src = [p[0] for p in valid_pairs]
        dst = [p[1] for p in valid_pairs]

        if hdr not in S.headers:
            S.headers[hdr] = {"inactive": False, "color": "#ffffff"}

        self.result = {"header": hdr, "title": title, "source": src, "destination": dst,
                       "details": {"no_backup": self.no_backup.isChecked(), "no_restore": self.no_restore.isChecked()}}
        self.accept()


class MountDialog(QDialog):
    def __init__(self, parent, opt: Optional[dict]):
        super().__init__(parent)
        self.result: dict = {}
        self.setWindowTitle("Edit Drive" if opt else "New Drive")
        self.setMinimumSize(900, 500)
        opt = opt or {}

        t      = current_theme()
        layout = QVBoxLayout(self)
        layout.setSpacing(15)
        layout.addWidget(_hdr_label("Configure Drive"))
        layout.addWidget(_sep())

        form = QFormLayout()
        form.setSpacing(15)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapAllRows)

        self.name = QLineEdit(opt.get("drive_name", ""))
        self.name.setPlaceholderText("e.g. Backup 1")
        lbl_name = QLabel("Drive name:")
        lbl_name.setAlignment(Qt.AlignmentFlag.AlignCenter)
        form.addRow(lbl_name)
        form.addRow(self.name)

        self.mount_path = QLineEdit(opt.get("mount_path", ""))
        self.mount_path.setPlaceholderText("e.g. smb://192.168.0.38/Backup Drive/")
        lbl_mount_path = QLabel("󰔨  Mount path (optional)")
        lbl_mount_path.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lbl_mount_path.setToolTip("<u>Mount Path (optional)</u><br><br>"
                                  "Only needed if this drive cannot be detected automatically.<br><br>"
                                  "<i>Leave empty</i> for standard USB/SATA drives — Backup Helper finds them "
                                  "automatically under <code>/run/media/&lt;user&gt;/&lt;name&gt;</code>, "
                                  "<code>/media/&lt;user&gt;/&lt;name&gt;</code> or <code>/mnt/&lt;name&gt;</code>.<br><br>"
                                  "<i>Fill in</i> when the drive is mounted elsewhere (sshfs, KDE Connect, etc.).<br>"
                                  "The path must match the beginning of the paths in your backup entries.<br><br>"
                                  "<small>Allowed commands: mount, umount, udisksctl, kdeconnect-cli, sshfs, fusermount3</small>")
        lbl_mount_path.setToolTipDuration(600_000)
        lbl_mount_path.setCursor(Qt.CursorShape.WhatsThisCursor)
        lbl_mount_path.setStyleSheet(f"color:{t['accent2']}; text-decoration:underline dotted;")
        form.addRow(lbl_mount_path)
        form.addRow(self.mount_path)

        self.mount = QLineEdit(opt.get("mount_command", ""))
        self.mount.setPlaceholderText("udisksctl mount --block-device /dev/sdX1")
        lbl_mnt = QLabel("󰔨  Mount command:")
        lbl_mnt.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lbl_mnt.setToolTip("<u>Mount Command</u><br><br>"
                           "The command is executed non-interactively — <b>no password prompt will appear</b>.<br><br>"
                           "<b>sshfs:</b> SSH connections must use key-based authentication.<br>"
                           "Set up a key pair first:<br>"
                           "<code>ssh-keygen -t ed25519 &amp;&amp; ssh-copy-id user@host</code><br><br>"
                           "Example: <code>sshfs user@host:/remote/path ~/local/mountpoint</code><br><br>"
                           "<b>udisksctl / mount:</b> Work as usual for local drives.<br>"
                           "<b>kdeconnect-cli:</b> The device must already be paired and reachable.")
        lbl_mnt.setToolTipDuration(600_000)
        lbl_mnt.setCursor(Qt.CursorShape.WhatsThisCursor)
        lbl_mnt.setStyleSheet(f"color:{t['accent2']}; text-decoration:underline dotted;")
        form.addRow(lbl_mnt)
        form.addRow(self.mount)

        self.unmnt = QLineEdit(opt.get("unmount_command", ""))
        self.unmnt.setPlaceholderText("udisksctl unmount --block-device /dev/sdX1")
        lbl_unmnt = QLabel("Unmount command:")
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


class HeaderSettingsDialog(QDialog):

    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle("Header Settings")
        self.setMinimumSize(750, 500)

        layout = QVBoxLayout(self)
        layout.addWidget(_hdr_label("Headers"))
        layout.addWidget(_sep())

        self.item_list = QListWidget()
        layout.addWidget(self.item_list, 1)

        layout.addLayout(_btn_row([("🆕  New", self._new), ("🎨  Color", self._color), ("⏸  Toggle active", self._toggle),
                                   ("✕  Delete", self._delete), ("↑  Up", self._move_up), ("↓  Down", self._move_down)]))
        layout.addWidget(_sep())
        layout.addWidget(_ok_cancel_buttons(self, self.accept, "Save && Close"))
        self._refresh()

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

    def _move_header(self, direction: int) -> None:
        name = self._selected_name()
        if not name:
            return
        keys = list(S.headers.keys())
        idx  = keys.index(name)
        new  = idx + direction
        if not (0 <= new < len(keys)):
            return
        keys[idx], keys[new] = keys[new], keys[idx]
        S.headers = {k: S.headers[k] for k in keys}
        self._refresh()
        self.item_list.setCurrentRow(new)

    def _new(self) -> None:
        name, ok = QInputDialog.getText(self, "New Header", "               Name:               ")
        if not ok or not name.strip():
            return
        if name.strip() in S.headers:
            QMessageBox.warning(self, "Duplicate", f"Header '{name.strip()}' already exists.")
            return
        col   = QColorDialog.getColor(QColor("#7dcfff"), self, "Choose header colour")
        color = col.name() if col.isValid() else "#ffffff"
        S.headers[name.strip()] = {"inactive": False, "color": color}
        self._refresh()
        for i in range(self.item_list.count()):
            if self.item_list.item(i).data(Qt.ItemDataRole.UserRole) == name.strip():
                self.item_list.setCurrentRow(i)
                break

    def _color(self) -> None:
        name = self._selected_name()
        if not name:
            return
        col = QColorDialog.getColor(QColor(S.headers[name]["color"]), self)
        if col.isValid():
            S.headers[name]["color"] = col.name()
            self._refresh()

    def _toggle(self) -> None:
        name = self._selected_name()
        if name:
            S.headers[name]["inactive"] = not S.headers[name]["inactive"]
            self._refresh()

    def _delete(self) -> None:
        name = self._selected_name()
        if not name:
            return
        if QMessageBox.question(self, "Delete", f"Delete header '{name}' and all its entries?",
                                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes:
            del S.headers[name]
            S.entries = [e for e in S.entries if e["header"] != name]
            self._refresh()

    def _move_up(self)   -> None: self._move_header(-1)
    def _move_down(self) -> None: self._move_header(+1)


class MountsDialog(_ListDialog):
    def __init__(self, parent):
        super().__init__(parent, "Mount Options", (700, 460), "Mounted Drives",
                         [("🆕  New", "_new"), ("✎  Edit", "_edit"), ("✕  Remove", "_del")])

    def _refresh(self) -> None:
        from drive_utils import get_mount_output, is_mounted
        self.item_list.clear()
        t   = current_theme()
        out = get_mount_output()
        for opt in S.mount_options:
            mounted   = is_mounted(opt, out)
            indicator = "●" if mounted else "○"
            item      = QListWidgetItem(f"  {indicator}  {opt.get('drive_name', '?')}")
            item.setForeground(QColor(t["green"] if mounted else t["text_dim"]))
            item.setData(Qt.ItemDataRole.UserRole, opt)
            self.item_list.addItem(item)

    def _new(self) -> None:
        dlg = MountDialog(self, None)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            S.mount_options.append(dlg.result)
            save_profile()
            self._refresh()

    def _edit(self) -> None:
        opt = self._selected_data()
        if not opt:
            return
        dlg = MountDialog(self, opt)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            idx = next((i for i, o in enumerate(S.mount_options) if o is opt), None)
            if idx is not None:
                S.mount_options[idx] = dlg.result
            else:
                S.mount_options.append(dlg.result)
            save_profile()
            self._refresh()

    def _del(self) -> None:
        opt = self._selected_data()
        if opt:
            name = opt.get("drive_name", "")
            S.mount_options = [o for o in S.mount_options if o.get("drive_name") != name]
            save_profile()
            self._refresh()


class ProfilesDialog(QDialog):

    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle("Profile Manager")
        self.setMinimumSize(700, 520)
        self._build()

    def _build(self) -> None:
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

        row1 = QHBoxLayout()
        for label, fn in [("▶  Load", self._load), ("🆕  New", self._new), ("⎘  Duplicate", self._copy), ("✕  Delete", self._del)]:
            b = QPushButton(label)
            b.clicked.connect(fn)
            row1.addWidget(b)
        layout.addLayout(row1)

        row2 = QHBoxLayout()
        for label, fn in [("⬆  Import", self._import), ("⬇  Export", self._export)]:
            b = QPushButton(label)
            b.clicked.connect(fn)
            row2.addWidget(b)
        layout.addLayout(row2)

        layout.addWidget(_sep())
        close_btn = QPushButton("✕  Close")
        close_btn.clicked.connect(self.accept)
        layout.addWidget(close_btn)

        self._refresh()

    def _refresh(self) -> None:
        t = current_theme()
        self._active_lbl.setText(f"Active profile:  {S.profile_name or '—'}")
        row = self.item_list.currentRow()
        self.item_list.clear()
        for name in list_profiles():
            active = name == S.profile_name
            item   = QListWidgetItem(f"  {'✓  ' if active else '   '}{name}")
            item.setForeground(QColor(t["accent"] if active else t["text"]))
            item.setData(Qt.ItemDataRole.UserRole, name)
            self.item_list.addItem(item)
        if 0 <= row < self.item_list.count():
            self.item_list.setCurrentRow(row)

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
                if data.pop("is_default", None):
                    _atomic_write(old_path, data)
            except (FileNotFoundError, json.JSONDecodeError, PermissionError, OSError) as exc:
                logger.warning("_load: could not clear is_default from '%s': %s", old_path.name, exc)
        if load_profile(_PROFILES_DIR / f"{name}.json"):
            save_profile()
            self._refresh()
            QMessageBox.information(self, "Profile Loaded", f"Profile '{name}' is now active.")
        else:
            QMessageBox.critical(self, "Error", f"Could not load profile '{name}'.")

    def _new(self) -> None:
        name = self._ask_profile_name("New Profile", "", self)
        if not name:
            return
        dest = _PROFILES_DIR / f"{name}.json"
        if dest.exists():
            if QMessageBox.question(self, "Overwrite?", f"Profile '{name}' already exists. Overwrite it?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
                return
        S.profile_name       = name
        S.entries            = []
        S.headers            = {}
        S.mount_options      = []
        S.system_manager_ops = []
        S.system_files       = []
        S.basic_packages     = []
        S.aur_packages       = []
        S.specific_packages  = []
        S.user_shell         = "bash"
        save_profile()
        self._refresh()
        QMessageBox.information(self, "Profile Created", f"Blank profile '{name}' created and is now active.\n\n"
                                                         "Go to Settings → Header Settings to add headers before creating entries.")

    def _copy(self) -> None:
        src_name = self._selected_name() or S.profile_name
        if not src_name:
            QMessageBox.information(self, "Duplicate", "No profile selected.")
            return
        name = self._ask_profile_name("Duplicate Profile", f"{src_name} copy", self)
        if not name:
            return
        dest     = _PROFILES_DIR / f"{name}.json"
        src_path = _PROFILES_DIR / f"{src_name}.json"
        if not src_path.exists() and src_name == S.profile_name:
            save_profile()
        if not src_path.exists():
            QMessageBox.warning(self, "Duplicate", f"Source profile file for '{src_name}' not found.")
            return
        if dest.exists():
            if QMessageBox.question(self, "Overwrite?", f"Profile '{name}' already exists. Overwrite it?",
                                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
                return
        shutil.copy2(src_path, dest)
        self._refresh()
        QMessageBox.information(self, "Duplicated", f"'{src_name}' duplicated as '{name}'.")

    def _del(self) -> None:
        name = self._selected_name()
        if not name:
            return
        if name == S.profile_name:
            QMessageBox.warning(self, "Delete Profile", "Cannot delete the currently active profile.")
            return
        if QMessageBox.question(self, "Delete Profile", f"Permanently delete profile '{name}'?",
                                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes:
            try:
                (_PROFILES_DIR / f"{name}.json").unlink(missing_ok=True)
                self._refresh()
            except Exception as exc:
                QMessageBox.critical(self, "Error", f"Could not delete profile: {exc}")

    def _import(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Import profile(s)", str(_HOME),
                                              "Profile files (*.json *.tar.gz *.tgz);;JSON (*.json);;Archive (*.tar.gz *.tgz)")
        if not path:
            return

        _PROFILES_DIR.mkdir(parents=True, exist_ok=True)

        if path.endswith((".tar.gz", ".tgz")):
            self._import_archive(path)
            return

        name = self._ask_profile_name("Import Profile", Path(path).stem, self)
        if not name:
            return
        dest = _PROFILES_DIR / f"{name}.json"
        if dest.exists():
            if QMessageBox.question(self, "Overwrite?", f"Profile '{name}' already exists. Overwrite it?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
                return
        shutil.copy2(path, dest)
        self._refresh()
        if QMessageBox.question(self, "Import Complete", f"'{name}' imported successfully.\nLoad it now?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes:
            if load_profile(dest):
                save_profile()
                self._refresh()

    def _import_archive(self, path: str) -> None:

        try:
            with tarfile.open(path, "r:gz") as tar:
                json_members = [m for m in tar.getmembers() if m.isfile() and m.name.endswith(".json")]
                if not json_members:
                    QMessageBox.warning(self, "Import", "The archive contains no .json profile files.")
                    return

                imported: list[str] = []
                skipped:  list[str] = []

                for member in json_members:
                    stem = Path(member.name).stem
                    if not _PROFILE_RE.match(stem):
                        skipped.append(f"{stem}  (invalid name)")
                        continue

                    dest      = _PROFILES_DIR / f"{stem}.json"
                    overwrite = True
                    if dest.exists():
                        ans = QMessageBox.question(self, "Overwrite?", f"Profile '{stem}' already exists. Overwrite it?",
                                                   QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
                                                   | QMessageBox.StandardButton.Cancel)
                        if ans == QMessageBox.StandardButton.Cancel:
                            break
                        overwrite = ans == QMessageBox.StandardButton.Yes

                    if overwrite:
                        f = tar.extractfile(member)
                        if f:
                            dest.write_bytes(f.read())
                        imported.append(stem)
                    else:
                        skipped.append(f"{stem}  (skipped)")

        except Exception as exc:
            QMessageBox.critical(self, "Import Failed", str(exc))
            return

        self._refresh()
        msg_parts = []
        if imported:
            msg_parts.append("Imported:\n  " + "\n  ".join(imported))
        if skipped:
            msg_parts.append("Skipped:\n  " + "\n  ".join(skipped))
        QMessageBox.information(self, "Import Complete", "\n\n".join(msg_parts) or "Nothing imported.")

        if len(imported) == 1:
            if QMessageBox.question(self, "Load Profile", f"Load '{imported[0]}' now?",
                                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes:
                if load_profile(_PROFILES_DIR / f"{imported[0]}.json"):
                    save_profile()
                    self._refresh()

    def _export(self) -> None:
        profiles = list_profiles()
        if not profiles:
            QMessageBox.information(self, "Export", "No profiles to export.")
            return

        selected_name = self._selected_name() or S.profile_name

        if len(profiles) > 1:
            from PyQt6.QtWidgets import QRadioButton, QButtonGroup
            choice_dlg = QDialog(self)
            choice_dlg.setWindowTitle("Export — What to export?")
            vl = QVBoxLayout(choice_dlg)
            vl.addWidget(QLabel("What would you like to export?"))

            rb_selected = QRadioButton(f"Selected profile only  ({selected_name})  →  .json")
            rb_all      = QRadioButton(f"All {len(profiles)} profiles  →  .tar.gz archive")
            rb_selected.setChecked(True)
            bg = QButtonGroup(choice_dlg)
            bg.addButton(rb_selected)
            bg.addButton(rb_all)
            vl.addWidget(rb_selected)
            vl.addWidget(rb_all)

            bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel) # type: ignore
            bb.accepted.connect(choice_dlg.accept)
            bb.rejected.connect(choice_dlg.reject)
            vl.addWidget(bb)

            if choice_dlg.exec() != QDialog.DialogCode.Accepted:
                return
            export_all = rb_all.isChecked()
        else:
            export_all = False

        if export_all:
            timestamp    = datetime.now().strftime("%Y%m%d_%H%M%S")
            default_name = f"backup_helper_profiles_{timestamp}.tar.gz"
            path, _ = QFileDialog.getSaveFileName(self, "Export all profiles", str(_HOME / default_name),
                                                  "Archive (*.tar.gz)")
            if not path:
                return
            if not path.endswith(".tar.gz"):
                path += ".tar.gz"
            try:
                if S.profile_name:
                    save_profile()
                with tarfile.open(path, "w:gz") as tar:
                    for name in profiles:
                        src = _PROFILES_DIR / f"{name}.json"
                        if src.exists():
                            tar.add(src, arcname=f"{name}.json")
                QMessageBox.information(self, "Exported", f"All {len(profiles)} profiles exported to:\n{path}")
            except Exception as exc:
                QMessageBox.critical(self, "Export Failed", str(exc))
            return

        name = selected_name
        if not name:
            QMessageBox.information(self, "Export", "No profile to export.")
            return
        src = _PROFILES_DIR / f"{name}.json"
        if not src.exists():
            if name == S.profile_name:
                save_profile()
            else:
                QMessageBox.warning(self, "Export", f"Profile file for '{name}' not found.")
                return
        path, _ = QFileDialog.getSaveFileName(self, "Export profile", str(_HOME / f"{name}.json"), "JSON (*.json)")
        if path:
            shutil.copy2(src, path)
            QMessageBox.information(self, "Exported", f"Profile '{name}' exported to:\n{path}")

    @staticmethod
    def _ask_profile_name(title: str, default: str, parent: Optional[QWidget] = None) -> Optional[str]:

        while True:
            name, ok = QInputDialog.getText(parent, title, "Profile name:", text=default)
            if not ok:
                return None
            clean = name.strip()
            if not clean:
                QMessageBox.warning(parent, "Invalid Name", "Name must not be empty.")
                continue
            if not _PROFILE_RE.match(clean):
                QMessageBox.warning(parent, "Invalid Name", "Only letters, digits, spaces, hyphens and dots are allowed.")
                continue
            return clean


class LogViewer(QDialog):
    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle("Log Viewer")
        self.setMinimumSize(1350, 950)

        t      = current_theme()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(5, 5, 5, 5)
        layout.setSpacing(5)

        top = QWidget()
        top.setStyleSheet(f"background:{t['bg2']};border-bottom:1px solid {t['header_sep']};")
        tl  = QHBoxLayout(top)
        tl.setContentsMargins(14, 8, 14, 8)
        tl.addStretch()
        tl.addWidget(_hdr_label("📋  Log File"))
        tl.addStretch(1)
        tl.addWidget(QLabel(apply_replacements(str(_LOG_FILE))))
        layout.addWidget(top)

        self.view = QTextEdit()
        self.view.setReadOnly(True)
        self.view.setStyleSheet("font-family:monospace;font-size:14px;border:none;border-radius:0;")
        layout.addWidget(self.view, 1)

        bot = QWidget()
        bot.setStyleSheet(f"background:{t['bg2']};border-top:1px solid {t['header_sep']};")
        bl  = QHBoxLayout(bot)
        bl.setContentsMargins(12, 8, 12, 8)
        bl.setSpacing(8)
        for label, fn in [
            ("🔄  Refresh", self._load),
            ("🗑  Clear",   self._clear),
            ("✕  Close",   self.accept),
        ]:
            b = QPushButton(label)
            b.setFixedHeight(36)
            b.clicked.connect(fn)
            bl.addWidget(b)
        layout.addWidget(bot)
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
        except Exception as e:
            self.view.setPlainText(f"Error reading log file: {e}")

    def _clear(self) -> None:
        if QMessageBox.question(self, "Clear log", "Permanently delete all log entries?",
                                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes:
            try:
                _LOG_FILE.write_text("", encoding="utf-8")
            except OSError as e:
                QMessageBox.warning(self, "Error", f"Could not clear log: {e}")
                return
            self._load()


class SysInfoDialog(QDialog):
    done_sig = pyqtSignal(str)

    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle("System Information")
        self.setMinimumSize(1350, 800)

        t      = current_theme()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(5, 5, 5, 5)
        layout.setSpacing(0)

        self.view = QTextEdit()
        self.view.setReadOnly(True)
        self.view.setStyleSheet("font-family:monospace;font-size:16px;border:none;border-radius:0;")
        layout.addWidget(self.view, 1)

        bot = QWidget()
        bot.setStyleSheet(f"background:{t['bg2']};border-top:1px solid {t['header_sep']};")
        bl  = QHBoxLayout(bot)
        bl.setContentsMargins(12, 8, 12, 8)
        close = QPushButton("✕  Close")
        close.setFixedHeight(36)
        close.clicked.connect(self.accept)
        bl.addWidget(close)
        layout.addWidget(bot)

        self.view.setPlainText("⏳  Loading system information…")
        self.done_sig.connect(self.view.setPlainText)
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self) -> None:
        try:
            r = subprocess.run(["inxi", "-SMCGAz", "--no-host", "--color", "0"],
                               capture_output=True, text=True, timeout=15, env={**os.environ, "LANG": "C"}, check=False)
            self.done_sig.emit(r.stdout.strip() or r.stderr.strip() or "No output received from inxi.")
        except FileNotFoundError:
            self.done_sig.emit(
                "'inxi' is not installed.\n\n"
                "Installation:\n"
                "  Arch:   sudo pacman -S inxi\n"
                "  Debian: sudo apt install inxi"
            )
        except subprocess.TimeoutExpired:
            self.done_sig.emit("System information request timed out.")
        except Exception as exc:
            self.done_sig.emit(f"An unexpected error occurred: {exc}")
import atexit
from typing import Protocol, Type, runtime_checkable

from PyQt6.QtGui import QFontDatabase
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QMessageBox,
    QGridLayout, QHBoxLayout, QLabel, QVBoxLayout, QWidget, QPushButton, QScrollArea,
)

from samba_credentials import SambaPasswordDialog
from themes import THEMES, current_theme, apply_style
from dialogs import EntryDialog, HeaderSettingsDialog, MountsDialog, ProfilesDialog
from state import (
    S, _PROFILES_DIR, _COLS_NARROW, _COLS_WIDE,
    apply_replacements, block_set, save_profile, generate_tooltip,
)


def _atexit_cleanup() -> None:
    from system_manager import _emergency_cleanup
    _emergency_cleanup()

atexit.register(_atexit_cleanup)


_COPY_LOGIC_TOOLTIP = (
    "<b>How files are copied and when they are skipped</b><br><br>"
    "<b>Local File Logic:</b><br>"
    "- A file is <b>copied</b> if the destination is missing, the size differs, "
    "or the source is newer than the backup.<br>"
    "- A file is <b>skipped</b> only if the size matches <b>and</b> the backup "
    "is already as new as the source.<br><br>"
    "<b>Samba (SMB) Difference:</b><br>"
    "- For network shares, the system only checks <b>existence and file size</b> to save bandwidth.<br>"
    "- Remote paths <b>must</b> follow the pattern: <code>'smb://ip/rest-of-path'</code>.<br>"
    "- <b>Example:</b> <code>'smb://192.168.0.53/share/data'</code>.<br>"
    "- <b>Local requirement:</b> <code>smbclient</code> must be installed on <b>this machine</b> "
    "to communicate with SMB shares.<br>"
    "- <b>Remote requirement:</b> Samba must be correctly installed and configured on the <b>target system</b>. "
    "The share must be accessible and the firewall must allow SMB traffic (standard port 445).<br><br>"
    "<b>Samba Credentials &amp; Security:</b><br>"
    "- Passwords are <b>never stored in plain text</b>. They are managed securely via "
    "<b>KWallet</b> (KDE) or your <b>System Keyring</b>.<br>"
    "- <b>User Specific:</b> The system specifically looks for credentials belonging to the "
    "<b>currently logged-in OS user</b>.<br>"
    "- If KWallet is available and already contains an entry starting with <code>'smb-'</code>, "
    "that entry is used — and can be updated in KWallet but not deleted.<br>"
    "- Otherwise, credentials can be created and managed directly in Backup Helper "
    "and are saved in the system keyring under the service name <code>'backup-helper-samba'</code>.<br><br>"
    "The SMB password is passed to <code>smbclient</code> via the <code>PASSWD</code> "
    "environment variable — <b>never as a command-line argument</b>, so it does not "
    "appear in <code>/proc/&lt;pid&gt;/cmdline</code> or <code>ps aux</code>.<br><br>"
    "- <b>Connection order:</b> First, a login with stored credentials is attempted. "
    "If that fails or no credentials are stored, a <b>guest/anonymous</b> connection is tried. "
    "If that also fails, all SMB tasks will be marked as errors with a corresponding note.<br><br>"
    "<b>Always Skipped:</b><br>"
    "- System lock files are automatically ignored to prevent errors.<br>"
    "- Patterns: files named <code>lock</code> or <code>.lock</code>, "
    "files ending in <code>.lock</code>, <code>.lck</code> or named exactly <code>lockfile</code>, "
    "as well as <code>Singleton</code> (browser) and <code>cookies.sqlite-wal</code> (SQLite WAL).<br><br>"
    "<b>Status Colors:</b><br>"
    "- <span style='color:#31ff1a;'>Green</span> = Successfully copied, "
    "<span style='color:#f7c948;'>Yellow</span> = Skipped, "
    "<span style='color:#ff5370;'>Red</span> = Error."
)


class _ThemeDialog(QDialog):
    def keyPressEvent(self, event) -> None:
        if event.key() != Qt.Key.Key_Escape:
            super().keyPressEvent(event)


# noinspection PyUnresolvedReferences
class _BaseCheckboxWindow(QDialog):
    changed = pyqtSignal()
    _window_title: str = ""
    _cols_key:     str = "backup_window_columns"

    def __init__(self, parent) -> None:
        super().__init__(parent)
        self.setWindowTitle(self._window_title)

        self.checkbox_dirs: list[tuple[QCheckBox, list, list, str]] = []
        self.cols = S.ui.get(self._cols_key, _COLS_NARROW)
        self._selectall:     QCheckBox   | None = None
        self._col_btn:       QPushButton | None = None
        self._entry_stacked: bool = False

        self.main_layout = QVBoxLayout(self)
        self.main_layout.setContentsMargins(5, 5, 5, 5)
        self.main_layout.setSpacing(5)

        t = current_theme()
        self._top_wrap = QWidget()
        self._top_wrap.setStyleSheet(f"background:{t['bg2']};border-bottom:1px solid {t['header_sep']};")
        self._top_hbox = QHBoxLayout(self._top_wrap)
        self._top_hbox.setContentsMargins(5, 5, 5, 5)
        self.main_layout.addWidget(self._top_wrap)

        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.scroll_area.setFrameShape(QScrollArea.Shape.NoFrame)
        self.main_layout.addWidget(self.scroll_area, 1)

        self._setup_ui()

    def _entry_filter(self, entry: dict) -> bool:
        return True

    def _show_inactive_headers(self) -> bool:
        return False

    def _tips(self) -> dict:
        backup_tips, _, _ = generate_tooltip()
        return backup_tips

    def _src_dst(self, entry: dict) -> tuple[list, list]:
        return entry.get("source", []), entry.get("destination", [])

    def _add_action_buttons(self, grid: QGridLayout, row: int) -> None:
        pass

    def _extra_top_widgets(self) -> list:
        return []

    def _setup_ui(self) -> None:
        self.checkbox_dirs.clear()
        self._rebuild_top_controls()

        content = QWidget()
        grid    = QGridLayout(content)
        grid.setSpacing(5)
        grid.setContentsMargins(5, 5, 5, 5)

        row = self._populate_checkboxes(grid)
        self._add_action_buttons(grid, row)
        self.scroll_area.setWidget(content)

        scr = QApplication.primaryScreen()
        if scr:
            sg    = scr.availableGeometry()
            hint  = content.sizeHint()
            top_h = self._top_wrap.sizeHint().height() + 20
            self.resize(min(max(hint.width() + 20, 0), sg.width()), min(hint.height() + top_h, sg.height()))

    def _rebuild_top_controls(self) -> None:
        while self._top_hbox.count():
            item = self._top_hbox.takeAt(0)
            if w := item.widget():
                w.deleteLater()

        next_cols_val = _COLS_NARROW if self.cols == _COLS_WIDE else _COLS_WIDE

        self._selectall = QCheckBox("Select All")
        self._selectall.clicked.connect(self._toggle_all)

        self._col_btn = QPushButton(f"{next_cols_val} Columns")
        self._col_btn.clicked.connect(self._toggle_cols)

        self._top_hbox.addWidget(self._selectall)

        for w in self._extra_top_widgets():
            self._top_hbox.addStretch(1)
            self._top_hbox.addWidget(w)

        self._top_hbox.addStretch(1)
        self._top_hbox.addWidget(self._col_btn)

    def _populate_checkboxes(self, grid: QGridLayout) -> int:
        self.checkbox_dirs.clear()
        tips          = self._tips()
        show_inactive = self._show_inactive_headers()

        grouped: dict[str, list] = {}
        for e in S.entries:
            if not self._entry_filter(e):
                continue
            h = e.get("header", "Unknown")
            if not show_inactive and S.headers.get(h, {}).get("inactive"):
                continue
            grouped.setdefault(h, []).append(e)

        t   = current_theme()
        row = 0

        for h in S.headers:
            entries = grouped.get(h)
            if not entries:
                continue

            hdr_data = S.headers[h]
            inactive = show_inactive and hdr_data.get("inactive", False)
            color    = t["text_dim"] if inactive else hdr_data.get("color", "#ffffff")
            label    = f"{h} (Inactive)" if inactive else h

            hdr_lbl = QLabel(label)
            hdr_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            hdr_lbl.setStyleSheet(f"font-size:17px;font-weight:bold;color:{color};padding:4px 0;")
            grid.addWidget(hdr_lbl, row, 0, 1, self.cols)
            row += 1

            col = 0
            for e in sorted(entries, key=lambda x: x.get("title", "").lower()):
                title = e["title"]
                cb    = QCheckBox(title.replace("<br>", "\n").replace("&", "&&"))
                cb.setToolTipDuration(600_000)
                if title in tips:
                    cb.setToolTip(tips[title])
                cb.setStyleSheet(f"QCheckBox{{color:{color};}} QToolTip{{color:{t['success']};}}")
                cb.stateChanged.connect(self._sync_select_all)
                cb.entry_data = e

                src, dst = self._src_dst(e)
                self.checkbox_dirs.append((cb, src, dst, title))
                grid.addWidget(cb, row, col)
                col += 1
                if col >= self.cols:
                    col = 0
                    row += 1

            if col != 0:
                row += 1

            spacer = QWidget()
            spacer.setFixedHeight(6)
            grid.addWidget(spacer, row, 0, 1, self.cols)
            row += 1

        return row

    def _toggle_all(self) -> None:
        state = self._selectall.isChecked()
        for cb, *_ in self.checkbox_dirs:
            cb.setChecked(state)

    def _sync_select_all(self) -> None:
        if self._selectall and (cbs := [cb for cb, *_ in self.checkbox_dirs]):
            block_set(self._selectall, all(cb.isChecked() for cb in cbs))

    def _toggle_cols(self) -> None:
        self.cols = _COLS_WIDE if self.cols == _COLS_NARROW else _COLS_NARROW
        S.ui[self._cols_key] = self.cols
        save_profile()
        self.changed.emit()
        self.done(2)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if not event.spontaneous():
            for cb, *_ in self.checkbox_dirs:
                cb.blockSignals(True)
                cb.setChecked(False)
                cb.blockSignals(False)
            if self._selectall:
                self._selectall.blockSignals(True)
                self._selectall.setChecked(False)
                self._selectall.blockSignals(False)
                self._selectall.setFocus()

    def closeEvent(self, event) -> None:
        for cb, *_ in self.checkbox_dirs:
            try:
                cb.stateChanged.disconnect()
            except (TypeError, RuntimeError):
                pass
        super().closeEvent(event)

    def keyPressEvent(self, event) -> None:
        k = event.key()
        if k in (Qt.Key.Key_Enter, Qt.Key.Key_Return):
            focused = self.focusWidget()
            if isinstance(focused, (QCheckBox, QPushButton)):
                focused.toggle()
                if focused == self._selectall:
                    self._toggle_all()
        elif k == Qt.Key.Key_Escape:
            self.close()
        else:
            super().keyPressEvent(event)


@runtime_checkable
class _CopyWindowProtocol(Protocol):
    checkbox_dirs: list
    cols: int
    _op_label: str

    def close(self) -> None: ...
    def information(self, *a, **k) -> None: ...


class _CopyMixin:
    _op_label: str = ""

    def _start_copy(self: "_BaseCheckboxWindow") -> None:
        from copy_worker import CopyDialog
        from drive_utils import check_drives_to_mount, mount_required_drives, unmount_drive

        selected = [(src, dst, title) for cb, src, dst, title in self.checkbox_dirs if cb.isChecked()]
        if not selected:
            QMessageBox.information(self, "Note", "Nothing selected.")
            return

        paths = [p for src, dst, _title in selected for p in src + dst]
        drives_to_mount = check_drives_to_mount(paths)
        proceed, drives_to_unmount = mount_required_drives(drives_to_mount, self)
        if not proceed:
            return

        CopyDialog(self, selected, self._op_label).exec()  # type: ignore[arg-type]

        failed = []
        for opt in drives_to_unmount:
            ok, err = unmount_drive(opt)
            if not ok:
                failed.append(f"• {opt.get('drive_name', '?')}: {err}")
        if failed:
            QMessageBox.warning(self, "Unmount Failed", "Could not unmount the following drives:\n\n" + "\n".join(failed))

    def _add_action_buttons(self: "_BaseCheckboxWindow", grid: QGridLayout, row: int) -> None:
        action_btn = QPushButton(self._op_label)  # type: ignore[attr-defined]
        action_btn.setFixedHeight(30)
        action_btn.setStyleSheet("font-size:15px;font-weight:bold;")
        action_btn.clicked.connect(self._start_copy)  # type: ignore[attr-defined]

        close_btn = QPushButton("Close")
        close_btn.setFixedHeight(30)
        close_btn.setStyleSheet("font-size:15px;")
        close_btn.clicked.connect(self.close)

        grid.addWidget(action_btn, row,     0, 1, self.cols)
        grid.addWidget(close_btn,  row + 1, 0, 1, self.cols)


class BackupWindow(_CopyMixin, _BaseCheckboxWindow):
    _window_title = "Create Backup"
    _cols_key     = "backup_window_columns"
    _op_label     = "Create Backup"

    def _entry_filter(self, entry: dict) -> bool:
        return not entry.get("details", {}).get("no_backup", False)

    def _tips(self) -> dict:
        backup_tips, _, _ = generate_tooltip()
        return backup_tips

    def _src_dst(self, entry: dict) -> tuple[list, list]:
        return entry.get("source", []), entry.get("destination", [])


class RestoreWindow(_CopyMixin, _BaseCheckboxWindow):
    _window_title = "Restore Backup"
    _cols_key     = "restore_window_columns"
    _op_label     = "Restore Backup"

    def _entry_filter(self, entry: dict) -> bool:
        return not entry.get("details", {}).get("no_restore", False)

    def _tips(self) -> dict:
        _, restore_tips, _ = generate_tooltip()
        return restore_tips

    def _src_dst(self, entry: dict) -> tuple[list, list]:
        return entry.get("destination", []), entry.get("source", [])


class SettingsWindow(_BaseCheckboxWindow):
    _window_title = "Settings"
    _cols_key     = "settings_window_columns"

    def _show_inactive_headers(self) -> bool:
        return True

    def _entry_filter(self, entry: dict) -> bool:
        return True

    def _tips(self) -> dict:
        backup_tips, _, _ = generate_tooltip()
        return backup_tips

    def _src_dst(self, entry: dict) -> tuple[list, list]:
        return entry.get("source", []), entry.get("destination", [])

    def _extra_top_widgets(self) -> list:
        return [self._make_config_path_label()]

    @staticmethod
    def _make_config_path_label() -> QLabel:
        t    = current_theme()
        path = (str(_PROFILES_DIR / f"{S.profile_name}.json") if S.profile_name else str(_PROFILES_DIR))
        lbl = QLabel(" 󰔨  "
                     f"<span style='font-size:16px;color:{t['accent2']};"
                     f"text-decoration:underline dotted;'>{apply_replacements(path)}</span>")
        lbl.setTextFormat(Qt.TextFormat.RichText)
        lbl.setToolTip(_COPY_LOGIC_TOOLTIP)
        lbl.setToolTipDuration(600_000)
        lbl.setCursor(Qt.CursorShape.WhatsThisCursor)
        return lbl

    def _add_action_buttons(self, grid: QGridLayout, row: int) -> None:
        def _btn(lbl: str, fn) -> QPushButton:
            b = QPushButton(lbl)
            b.clicked.connect(fn)
            return b

        def _hrow(*btns) -> QHBoxLayout:
            hb = QHBoxLayout()
            for b in btns:
                hb.addWidget(b)
            return hb

        grid.addLayout(_hrow(_btn("System Manager Options", self._open_sm_options)), row, 0, 1, self.cols)
        row += 1
        grid.addLayout(_hrow(_btn("New Entry", self._new_entry), _btn("Edit Entry", self._edit_entry),
                             _btn("Delete Entry", self._del_entry), _btn("Header Settings", self._header_settings)),
                       row, 0, 1, self.cols)
        row += 1
        grid.addLayout(_hrow(_btn("Mount Options", self._manage_mounts), _btn("Samba Credentials", self._samba_credentials),
                             _btn("Profile Manager", self._manage_profiles)), row, 0, 1, self.cols)
        row += 1
        grid.addWidget(_btn("Change Theme", self._change_theme), row, 0, 1, self.cols)
        row += 1
        grid.addWidget(_btn("Close", self.close), row, 0, 1, self.cols)


    def _new_entry(self) -> None:
        if not S.headers:
            QMessageBox.information(self, "No Headers Found",
                                    "Before creating an entry you need at least one header.\n\n"
                                    "Headers group your entries and can each have their own colour.\n"
                                    "The Header Settings dialog will open now — click '🆕 New' to add one.")
            dlg = HeaderSettingsDialog(self)
            if dlg.exec() != QDialog.DialogCode.Accepted or not S.headers:
                return
            save_profile()
            self.changed.emit()

        pairs: list[list[str]] = []
        while True:
            dlg  = EntryDialog(self, None, stacked=self._entry_stacked, _pairs=pairs or None)
            code = dlg.exec()
            if code == QDialog.DialogCode.Accepted:
                S.entries.append(dlg.result)
                save_profile()
                self.changed.emit()
                self.done(2)
                break
            elif code == 2:
                self._entry_stacked = dlg.stacked
                pairs = dlg.pairs
            else:
                break

    def _edit_entry(self) -> None:
        checked = [cb for cb, *_ in self.checkbox_dirs if cb.isChecked()]
        if not checked:
            QMessageBox.information(self, "Edit Entry", "Please check one or more entries to edit.")
            return

        changed_any = False
        total = len(checked)

        for i, cb in enumerate(checked):
            entry = getattr(cb, "entry_data", None)
            if not entry:
                continue
            pairs: list[list[str]] = []
            while True:
                dlg  = EntryDialog(self, entry, stacked=self._entry_stacked, _pairs=pairs or None)
                if total > 1:
                    dlg.setWindowTitle(f"Edit Entry ({i + 1}/{total}) — {entry['title']}")
                code = dlg.exec()
                if code == QDialog.DialogCode.Accepted:
                    idx = next((j for j, e in enumerate(S.entries) if e is entry), None)
                    if idx is not None:
                        S.entries[idx] = dlg.result
                        changed_any = True
                    break
                elif code == 2:
                    self._entry_stacked = dlg.stacked
                    pairs = dlg.pairs
                else:
                    break

        if changed_any:
            save_profile()
            self.changed.emit()
            self.done(2)

    def _del_entry(self) -> None:
        to_delete_ids = {id(cb.entry_data) for cb, *_ in self.checkbox_dirs
                         if cb.isChecked() and getattr(cb, "entry_data", None) is not None}
        if not to_delete_ids:
            QMessageBox.information(self, "Delete Entry", "Please check one or more entries to delete.")
            return

        to_delete = [e for e in S.entries if id(e) in to_delete_ids]
        names = ", ".join(e["title"].replace("<br>", " ") for e in to_delete)
        if QMessageBox.question(self, "Delete", f"Really delete: {names}?",
                                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes:
            S.entries = [e for e in S.entries if id(e) not in to_delete_ids]
            save_profile()
            self.changed.emit()
            self.done(2)

    def _header_settings(self) -> None:
        dlg = HeaderSettingsDialog(self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            save_profile()
            self.changed.emit()
            self.done(2)

    def _manage_mounts(self) -> None:
        MountsDialog(self).exec()

    def _samba_credentials(self) -> None:
        SambaPasswordDialog(self).exec()

    def _manage_profiles(self) -> None:
        dlg = ProfilesDialog(self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self.changed.emit()
            self.done(2)

    def _open_sm_options(self) -> None:
        from system_manager_options import SystemManagerOptions
        SystemManagerOptions(self).exec()

    def _change_theme(self) -> None:
        dlg = _ThemeDialog(self)
        dlg.setWindowTitle("Theme and Font Settings")
        dlg.setMinimumSize(480, 380)
        dlg.setWindowModality(Qt.WindowModality.NonModal)

        layout = QVBoxLayout(dlg)

        def _combo(label: str, items: list[str], current: str) -> QComboBox:
            layout.addWidget(QLabel(label))
            cb = QComboBox()
            cb.addItems(items)
            cb.setCurrentText(current)
            layout.addWidget(cb)
            return cb

        theme_cb = _combo("Select Theme:", list(THEMES.keys()), S.ui.get("theme", "Tokyo Night"))

        font_cb = _combo("Select Font:", ["(System Default)"] + sorted(QFontDatabase.families()),
                         S.ui.get("font_family", "") or "(System Default)")

        size_cb = _combo("Select Font Size:", ["10", "11", "12", "13", "14", "15", "16", "17", "18", "20", "22", "24"],
                         str(S.ui.get("font_size", 14)))

        orig = (S.ui.get("theme", "Tokyo Night"), S.ui.get("font_family", ""), S.ui.get("font_size", 14))

        def _apply(save: bool = False) -> None:
            chosen_font = font_cb.currentText()
            if chosen_font == "(System Default)":
                chosen_font = ""
            S.ui.update(theme=theme_cb.currentText(), font_family=chosen_font, font_size=int(size_cb.currentText()))
            apply_style()
            if save:
                save_profile()
                self.done(2)

        prev_btn = QPushButton("Preview")
        prev_btn.clicked.connect(lambda: _apply(False))
        layout.addWidget(prev_btn)

        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel) # type: ignore
        ok_btn     = bb.button(QDialogButtonBox.StandardButton.Ok)
        cancel_btn = bb.button(QDialogButtonBox.StandardButton.Cancel)

        if ok_btn:
            def _on_ok() -> None:
                _apply(True)
                dlg.accept()
                QMessageBox.information(self, "Theme Saved", f"Theme: {theme_cb.currentText()}, "
                                                             f"Font: {font_cb.currentText()} {size_cb.currentText()}px")
            ok_btn.clicked.connect(_on_ok)

        if cancel_btn:
            cancel_btn.clicked.connect(lambda: (S.ui.update(theme=orig[0], font_family=orig[1], font_size=orig[2]),
                                                apply_style(), dlg.reject()))

        layout.addWidget(bb)
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()


_WINDOW_MAP: "dict[str, Type[_BaseCheckboxWindow]]" = {}


def base_window(parent, mode: str = "Settings") -> "_BaseCheckboxWindow":
    cls = _WINDOW_MAP.get(mode, SettingsWindow)
    return cls(parent)


_WINDOW_MAP.update({"Backup": BackupWindow, "Restore":  RestoreWindow, "Settings": SettingsWindow})
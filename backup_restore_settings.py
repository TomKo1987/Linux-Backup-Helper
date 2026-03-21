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
from state import S, _PROFILES_DIR, _COLS_NARROW, _COLS_WIDE, apply_replacements, block_set, save_profile, generate_tooltip

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

    def _exclusion_note(self, entry: dict) -> str:
        return ""

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
                title      = e["title"]
                excl_note  = self._exclusion_note(e)
                cb_color   = t["muted"] if excl_note else color

                cb = QCheckBox(title.replace("<br>", "\n").replace("&", "&&"))
                cb.setToolTipDuration(600_000)

                base_tip = tips.get(title, "")
                if excl_note:
                    warn = (f"<br><br><span style='color:{t['warning']};font-weight:bold;'>"
                            f"⚠ {excl_note}</span>")
                    cb.setToolTip((base_tip + warn) if base_tip else
                                  f"<span style='color:{t['warning']};font-weight:bold;'>⚠ {excl_note}</span>")
                elif base_tip:
                    cb.setToolTip(base_tip)

                cb.setStyleSheet(f"QCheckBox{{color:{cb_color};}} QToolTip{{color:{t['success']};}}")
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
            if isinstance(focused, QCheckBox):
                focused.toggle()
                if focused == self._selectall:
                    self._toggle_all()
            elif isinstance(focused, QPushButton):
                focused.click()
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
        from drive_utils import check_drives_to_mount, mount_required_drives

        selected = [(src, dst, title) for cb, src, dst, title in self.checkbox_dirs if cb.isChecked()]
        if not selected:
            QMessageBox.information(self, "Note", "Nothing selected.")
            return

        paths = [p for src, dst, _title in selected for p in src + dst]
        drives_to_mount = check_drives_to_mount(paths)
        if not mount_required_drives(drives_to_mount, self):
            return

        CopyDialog(self, selected, self._op_label).exec()  # type: ignore[arg-type]

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

    def _extra_top_widgets(self) -> list:
        return [self._make_config_path_label()]

    @staticmethod
    def _make_config_path_label() -> QLabel:
        t    = current_theme()
        path = (str(_PROFILES_DIR / f"{S.profile_name}.json") if S.profile_name else str(_PROFILES_DIR))
        lbl  = QLabel(" 󰔨  "
                      f"<span style='font-size:16px;color:{t['accent2']};"
                      f"text-decoration:underline dotted;'>{apply_replacements(path)}</span>")
        lbl.setTextFormat(Qt.TextFormat.RichText)
        lbl.setToolTip(_COPY_LOGIC_TOOLTIP)
        lbl.setToolTipDuration(600_000)
        lbl.setCursor(Qt.CursorShape.WhatsThisCursor)
        return lbl

    @staticmethod
    def _exclusion_note(entry: dict, **kwargs) -> str:
        details = entry.get("details", {})
        no_b = details.get("no_backup",  False)
        no_r = details.get("no_restore", False)
        if no_b and no_r:
            return "Excluded from backup and restore"
        if no_b:
            return "Excluded from backup"
        if no_r:
            return "Excluded from restore"
        return ""

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
        grid.addLayout(_hrow(_btn("New Entry",         self._new_entry),
                             _btn("Edit Entry",        self._edit_entry),
                             _btn("Delete Entry",      self._del_entry),
                             _btn("Header Settings",   self._header_settings)),
                       row, 0, 1, self.cols)
        row += 1
        grid.addLayout(_hrow(_btn("Mount Options",       self._manage_mounts),
                             _btn("Samba Credentials",   self._samba_credentials),
                             _btn("Profile Manager",     self._manage_profiles)),
                       row, 0, 1, self.cols)
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
        if QMessageBox.question(
            self, "Delete", f"Really delete: {names}?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        ) == QMessageBox.StandardButton.Yes:
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
        SambaPasswordDialog.open(self)

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
        dlg.changed.connect(self.done)
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()


class _ThemeDialog(QDialog):

    changed = pyqtSignal(int)

    def __init__(self, parent) -> None:
        super().__init__(parent)
        self.setWindowTitle("Theme and Font Settings")
        self.setMinimumSize(480, 380)
        self.setWindowModality(Qt.WindowModality.NonModal)
        self._orig = (S.ui.get("theme", "Tokyo Night"), S.ui.get("font_family", ""), S.ui.get("font_size", 14))
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        def _combo(label: str, items: list[str], current: str) -> QComboBox:
            layout.addWidget(QLabel(label))
            cb = QComboBox()
            cb.addItems(items)
            cb.setCurrentText(current)
            layout.addWidget(cb)
            return cb

        self._theme_cb = _combo(
            "Select Theme:", list(THEMES.keys()), S.ui.get("theme", "Tokyo Night")
        )
        self._font_cb  = _combo(
            "Select Font:",
            ["(System Default)"] + sorted(QFontDatabase.families()),
            S.ui.get("font_family", "") or "(System Default)",
        )
        self._size_cb  = _combo(
            "Select Font Size:",
            ["10", "11", "12", "13", "14", "15", "16", "17", "18", "20", "22", "24"],
            str(S.ui.get("font_size", 14)),
        )

        prev_btn = QPushButton("Preview")
        prev_btn.clicked.connect(lambda: self._apply(save=False))
        layout.addWidget(prev_btn)

        bb         = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)  # type: ignore
        ok_btn     = bb.button(QDialogButtonBox.StandardButton.Ok)
        cancel_btn = bb.button(QDialogButtonBox.StandardButton.Cancel)

        if ok_btn:
            ok_btn.clicked.connect(self._on_ok)
        if cancel_btn:
            cancel_btn.clicked.connect(self._on_cancel)

        layout.addWidget(bb)

    def _apply(self, save: bool = False) -> None:
        chosen_font = self._font_cb.currentText()
        if chosen_font == "(System Default)":
            chosen_font = ""
        S.ui.update(
            theme=self._theme_cb.currentText(),
            font_family=chosen_font,
            font_size=int(self._size_cb.currentText()),
        )
        apply_style()
        if save:
            save_profile()

    def _on_ok(self) -> None:
        self._apply(save=True)
        self.accept()
        QMessageBox.information(
            self.parent(), "Theme Saved",
            f"Theme: {self._theme_cb.currentText()}, "
            f"Font: {self._font_cb.currentText()} {self._size_cb.currentText()}px",
        )
        self.changed.emit(2)

    def _on_cancel(self) -> None:
        orig_theme, orig_font, orig_size = self._orig
        S.ui.update(theme=orig_theme, font_family=orig_font, font_size=orig_size)
        apply_style()
        self.reject()

    def keyPressEvent(self, event) -> None:
        if event.key() != Qt.Key.Key_Escape:
            super().keyPressEvent(event)


_WINDOW_MAP: "dict[str, Type[_BaseCheckboxWindow]]" = {}


def base_window(parent, mode: str = "Settings") -> "_BaseCheckboxWindow":
    cls = _WINDOW_MAP.get(mode, SettingsWindow)
    return cls(parent)


_WINDOW_MAP.update({"Backup": BackupWindow, "Restore": RestoreWindow, "Settings": SettingsWindow})

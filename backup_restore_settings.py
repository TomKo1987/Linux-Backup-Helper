from typing import Type, Optional

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QFontDatabase
from PyQt6.QtWidgets import (
    QGridLayout, QHBoxLayout, QLabel, QVBoxLayout, QWidget, QScrollArea,
    QApplication, QCheckBox, QComboBox, QDialog, QMessageBox, QPushButton
)

from dialogs import EntryDialog, HeaderSettingsDialog, MountsDialog, ProfilesDialog, _ok_cancel_buttons
from samba_credentials import SambaPasswordDialog
from state import S, _PROFILES_DIR, RESTART_DIALOG, apply_replacements, block_set, save_profile, generate_tooltip
from themes import (
    THEMES, current_theme, apply_style, font_scale, register_style_listener, unregister_style_listener, apply_tooltip
)


def _copy_logic_tooltip() -> str:
    t = current_theme()
    return (
        "<b>Copy &amp; Skip Logic</b><br><br>"
        "<b>Local File Logic:</b><br>"
        "- A file is <b>copied</b> if the destination is missing, the <b>size differs</b>, "
        "or the source is newer than the backup.<br>"
        "- A file is <b>skipped</b> only if the size matches <b>and</b> the backup "
        "is already as new as the source (2s tolerance).<br><br>"
        "<b>Samba (SMB) Logic:</b><br>"
        "- To save bandwidth and avoid latency, the system only checks <b>existence and file size</b>.<br>"
        "- Remote paths <b>must</b> follow the pattern: <code>'smb://ip/path'</code>.<br>"
        "- <b>Local requirement:</b> <code>smbclient</code> must be installed on <b>this machine</b>.<br>"
        "- <b>Remote requirement:</b> Samba must be configured on the <b>target system</b>; "
        "Port must be open (default Port = 445).<br><br>"
        "<b>Always Skipped (Filter):</b><br>"
        "- <b>Locks:</b> <code>.lock</code>, <code>.lck</code>, <code>.parentlock</code>, <code>Singleton</code><br>"
        "- <b>Browser Data:</b> <code>Cache</code>, <code>Session Storage</code>, <code>.sqlite-wal</code>, "
        "<code>recovery.jsonlz4</code>, <code>leveldb/</code>, <code>.ldb</code>, <code>.log</code><br>"
        "- <b>System &amp; Temp:</b> <code>Thumbs.db</code>, <code>.DS_Store</code>, <code>.tmp</code>, "
        "<code>.bak</code>, <code>.baklz4</code><br><br>"
        "<b>Status Colors:</b><br>"
        f"- <span style='color:{t['success']};'>Green</span> = Success, "
        f"<span style='color:{t['warning']};'>Yellow</span> = Skipped, "
        f"<span style='color:{t['error']};'>Red</span> = Error.<br><br><br>"
        "<b>Samba Credentials &amp; Keyring</b><br><br>"
        "- Passwords are <b>never stored in plain text</b>. The system uses a priority chain:<br>"
        "  1. <b>KDE KWallet:</b> Looks for <code>'smb-[username]'</code> in the <code>'kdewallet'</code> folder.<br>"
        "  2. <b>System Keyring:</b> Fallback via <code>libsecret</code> (service: <code>'backup-helper-samba'</code>).<br>"
        "  3. <b>Guest:</b> If no credentials exist, an anonymous connection is attempted.<br><br><br>"
        "<b>Execution Security (Hardened)</b><br><br>"
        "- <b>Zero Visibility:</b> Passwords are <b>never</b> passed via command-line arguments to prevent exposure in process lists.<br>"
        "- <b>RAM-Only Storage:</b> Credentials are stored in <code>/dev/shm</code> (RAM disk). If <code>/dev/shm</code> is unavailable, "
        "the operation aborts to prevent disk-leaks.<br>"
        "- <b>Race-Condition Protection:</b> The credential file remains active for the <b>exact duration</b> of the transfer "
        "and is deleted immediately after the process ends.<br>"
        "- <b>Secure Erasure:</b> Before deletion, the credential file is <b>overwritten with zeros</b> (Wipe) and synced.<br>"
        "- <b>Guest Fallback:</b> In case of access errors to the secure storage, the system safely falls back to a guest connection.<br>"
        "- <b>Memory Safety:</b> Internal password buffers (<code>SecureString</code>) are <b>manually zeroed out</b> in RAM after use."
    )


_COLS_NARROW, _COLS_WIDE = 2, 4


# noinspection PyUnresolvedReferences
class _BaseCheckboxWindow(QDialog):
    _window_title: str = ""
    _cols_key:     str = "backup_window_columns"
    changed = pyqtSignal()

    def __init__(self, parent) -> None:
        super().__init__(parent)
        self.setWindowTitle(self._window_title)

        self.checkbox_dirs: list[tuple[QCheckBox, list, list, str, dict]] = []
        self.cols = S.ui.get(self._cols_key, _COLS_NARROW)
        self._selectall:     QCheckBox   | None = None
        self._col_btn:       QPushButton | None = None

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
        register_style_listener(self._refresh_styles)

    @staticmethod
    def _entry_filter(entry: dict) -> bool: return True

    def _refresh_styles(self) -> None:
        saved = {id(e): cb.isChecked() for cb, _s, _d, _t, e in self.checkbox_dirs if e is not None}
        self._setup_ui()
        for cb, _s, _d, _t, e in self.checkbox_dirs:
            if e is not None and id(e) in saved:
                block_set(cb, saved[id(e)])
        self._sync_select_all()

    def _tips(self) -> dict:
        backup_tips, _, _ = generate_tooltip()
        return backup_tips

    def _src_dst(self, entry: dict) -> tuple[list, list]:
        return entry.get("source", []), entry.get("destination", [])

    def _exclusion_note(self, entry: dict) -> str: return ""

    def _add_action_buttons(self, grid: QGridLayout, row: int) -> None: pass

    def _extra_top_widgets(self) -> list: return []

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
            sg   = scr.availableGeometry()
            hint = content.sizeHint()
            top_h = self._top_wrap.sizeHint().height() + 20
            self.resize(min(hint.width() + 20, sg.width()), min(hint.height() + top_h, sg.height()))

    def _rebuild_top_controls(self) -> None:
        while self._top_hbox.count():
            item = self._top_hbox.takeAt(0)
            if w := item.widget():
                w.deleteLater()

        next_cols = _COLS_NARROW if self.cols == _COLS_WIDE else _COLS_WIDE

        self._selectall = QCheckBox("Select All")
        self._selectall.clicked.connect(self._toggle_all)

        self._col_btn = QPushButton(f"{next_cols} Columns")
        self._col_btn.clicked.connect(self._toggle_cols)

        self._top_hbox.addWidget(self._selectall)
        for w in self._extra_top_widgets():
            self._top_hbox.addStretch(1)
            self._top_hbox.addWidget(w)
        self._top_hbox.addStretch(1)
        self._top_hbox.addWidget(self._col_btn)

    def _populate_checkboxes(self, grid: QGridLayout) -> int:
        tips = self._tips()
        fs = font_scale()

        grouped: dict[str, list] = {}
        for e in S.entries:
            if not self._entry_filter(e):
                continue
            h = e.get("header", "Unknown")
            grouped.setdefault(h, []).append(e)

        t = current_theme()
        row = 0

        for h in S.headers:
            entries = grouped.get(h)
            if not entries:
                continue

            hdr_data = S.headers[h]
            inactive = hdr_data.get("inactive", False)
            color = t["text_dim"] if inactive else hdr_data.get("color", "#ffffff")
            label = f"{h} (Inactive)" if inactive else h

            hdr_lbl = QLabel(label)
            hdr_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            hdr_lbl.setStyleSheet(f"font-size:{fs['xl']}px;font-weight:bold;color:{color};padding:4px 0;")
            grid.addWidget(hdr_lbl, row, 0, 1, self.cols)
            row += 1

            col = 0
            for e in sorted(entries, key=lambda x: x.get("title", "").lower()):
                title = e["title"]
                excl_note = self._exclusion_note(e)
                cb_color = t["muted"] if excl_note else color

                cb = QCheckBox(title.replace("<br>", "\n").replace("&", "&&"))

                base_tip = tips.get(title, "")
                tip_text = ""
                if excl_note:
                    warn = f"<br><br><span style='color:{t['warning']};font-size:{fs['sm']}px; font-weight:bold;'> ⚠ {excl_note}</span>"
                    tip_text = (
                        base_tip + warn) if base_tip else f"<span style='color:{t['warning']};font-weight:bold;'> ⚠ {excl_note}</span>"
                elif base_tip:
                    tip_text = base_tip

                apply_tooltip(cb, tip_text)

                cb.setStyleSheet(f"QCheckBox{{color:{cb_color};}}")
                cb.stateChanged.connect(self._sync_select_all)
                cb.setProperty("entry_data", e)

                src, dst = self._src_dst(e)
                self.checkbox_dirs.append((cb, src, dst, title, e))
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
        checked = self._selectall.isChecked()
        for cb, *_ in self.checkbox_dirs:
            cb.setChecked(checked)

    def _sync_select_all(self) -> None:
        if self._selectall and self.checkbox_dirs:
            block_set(self._selectall, all(cb.isChecked() for cb, *_ in self.checkbox_dirs))

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
                block_set(cb, False)
            if self._selectall:
                block_set(self._selectall, False)
                self._selectall.setFocus()

    def closeEvent(self, event) -> None:
        unregister_style_listener(self._refresh_styles)
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


class _CopyMixin:
    _op_label: str = ""

    def _start_copy(self: "_BaseCheckboxWindow") -> None:
        from copy_worker import CopyDialog
        from drive_utils import check_drives_to_mount, mount_required_drives

        selected = [(src, dst, title) for cb, src, dst, title, *_ in self.checkbox_dirs if cb.isChecked()]
        if not selected:
            QMessageBox.information(self, "Note", "Nothing selected.")
            return

        paths = [p for src, dst, _title in selected for p in src + dst]
        drives_to_mount = check_drives_to_mount(paths)
        if not mount_required_drives(drives_to_mount, self):
            return

        CopyDialog(self, selected, self._op_label).exec()  # type: ignore[arg-type]

    def _add_action_buttons(self: "_BaseCheckboxWindow", grid: QGridLayout, row: int) -> None:
        fs          = font_scale()
        btn_style   = f"font-size:{fs['xl']}px;font-weight:bold;"
        close_style = f"font-size:{fs['xl']}px;"

        action_btn = QPushButton(self._op_label)  # type: ignore[attr-defined]
        action_btn.setMinimumHeight(30)
        action_btn.setStyleSheet(btn_style)
        action_btn.clicked.connect(self._start_copy)  # type: ignore[attr-defined]

        close_btn = QPushButton("Close")
        close_btn.setMinimumHeight(30)
        close_btn.setStyleSheet(close_style)
        close_btn.clicked.connect(self.close)

        grid.addWidget(action_btn, row,     0, 1, self.cols)
        grid.addWidget(close_btn,  row + 1, 0, 1, self.cols)


class BackupWindow(_CopyMixin, _BaseCheckboxWindow):
    _window_title = "Create Backup"
    _cols_key     = "backup_window_columns"
    _op_label     = "Create Backup"

    def _entry_filter(self, entry: dict) -> bool: return not entry.get("details", {}).get("no_backup", False)


class RestoreWindow(_CopyMixin, _BaseCheckboxWindow):
    _window_title = "Restore Backup"
    _cols_key     = "restore_window_columns"
    _op_label     = "Restore Backup"

    def _entry_filter(self, entry: dict) -> bool: return not entry.get("details", {}).get("no_restore", False)

    def _tips(self) -> dict:
        _, restore_tips, _ = generate_tooltip()
        return restore_tips

    def _src_dst(self, entry: dict) -> tuple[list, list]: return entry.get("destination", []), entry.get("source", [])


class SettingsWindow(_BaseCheckboxWindow):
    _window_title = "Settings"
    _cols_key     = "settings_window_columns"

    def __init__(self, parent) -> None:
        super().__init__(parent)
        self._entry_stacked: bool = False

    def _extra_top_widgets(self) -> list: return [self._make_config_path_label()]

    @staticmethod
    def _make_config_path_label() -> QLabel:
        t = current_theme()
        fs = font_scale()
        path = (str(_PROFILES_DIR / f"{S.profile_name}.json") if S.profile_name else str(_PROFILES_DIR))
        lbl = QLabel(f" 󰔨  <span style='font-size:{fs['lg']}px;color:{t['accent2']};"
                     f"text-decoration:underline dotted;'>{apply_replacements(path)}</span>")
        lbl.setTextFormat(Qt.TextFormat.RichText)
        apply_tooltip(lbl, _copy_logic_tooltip())
        return lbl

    def _exclusion_note(self, entry: dict) -> str:
        details = entry.get("details", {})
        no_b    = details.get("no_backup",  False)
        no_r    = details.get("no_restore", False)
        if no_b and no_r:
            return "Excluded from backup and restore"
        if no_b:
            return "Excluded from backup"
        if no_r:
            return "Excluded from restore"
        return ""

    def _add_action_buttons(self, grid: QGridLayout, row: int) -> None:
        def _btn(label: str, fn) -> QPushButton:
            b = QPushButton(label)
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
        grid.addLayout(_hrow(
            _btn("Mount Options", self._manage_mounts), _btn("Samba Credentials", self._samba_credentials),
            _btn("Profile Manager", self._manage_profiles)), row, 0, 1, self.cols)
        row += 1
        grid.addWidget(_btn("Change Theme", self._change_theme), row, 0, 1, self.cols)
        row += 1
        grid.addWidget(_btn("Close", self.close), row, 0, 1, self.cols)

    def _run_entry_dialog(self, initial_entry: Optional[dict], window_title: Optional[str] = None) -> Optional[dict]:
        current_entry     = initial_entry
        pairs: list[list[str]] = []
        pairs_initialised = False

        while True:
            dlg  = EntryDialog(self, current_entry, stacked=self._entry_stacked, _pairs=pairs if pairs_initialised else None)
            if window_title:
                dlg.setWindowTitle(window_title)
            code = dlg.exec()

            if code == QDialog.DialogCode.Accepted:
                return dlg.result
            if code == RESTART_DIALOG:
                self._entry_stacked = dlg.stacked
                pairs               = dlg.pairs
                pairs_initialised   = True
                current_entry       = dlg.snapshot
            else:
                return None

    def _new_entry(self) -> None:
        if not S.headers:
            QMessageBox.information(self, "No Headers Found", "Before creating an entry you need at least one header.\n\n"
                                                              "Headers group your entries and can each have their own colour.\n"
                                                              "The Header Settings dialog will open now — click '🆕 New' to add one.")
            dlg = HeaderSettingsDialog(self)
            if dlg.exec() != QDialog.DialogCode.Accepted or not S.headers:
                return
            save_profile()
            self.changed.emit()

        result = self._run_entry_dialog(None)
        if result is not None:
            S.entries.append(result)
            save_profile()
            self.changed.emit()
            self.done(2)

    def _edit_entry(self) -> None:
        checked = [(cb, entry) for cb, src, dst, title, entry in self.checkbox_dirs if cb.isChecked()]
        if not checked:
            QMessageBox.information(self, "Edit Entry", "Please check one or more entries to edit.")
            return

        changed_any = False
        total       = len(checked)

        for i, (cb, original_entry) in enumerate(checked):
            if not original_entry:
                continue
            title  = f"Edit Entry ({i + 1}/{total}) — {original_entry['title']}" if total > 1 else None
            result = self._run_entry_dialog(original_entry, window_title=title)
            if result is not None:
                idx = next((j for j, e in enumerate(S.entries) if e is original_entry), None)
                if idx is not None:
                    S.entries[idx] = result
                    changed_any    = True

        if changed_any:
            save_profile()
            self.changed.emit()
            self.done(2)

    def _del_entry(self) -> None:
        to_delete = [entry for cb, src, dst, title, entry in self.checkbox_dirs if cb.isChecked() and entry is not None]
        if not to_delete:
            QMessageBox.information(self, "Delete Entry", "Please check one or more entries to delete.")
            return

        names = ", ".join(e["title"].replace("<br>", " ") for e in to_delete)
        if QMessageBox.question(self, "Delete", f"Really delete: {names}?",
                                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes:
            S.entries = [e for e in S.entries if e not in to_delete]
            save_profile()
            self.changed.emit()
            self.done(2)

    def _header_settings(self) -> None:
        dlg = HeaderSettingsDialog(self)
        if dlg.exec() == QDialog.DialogCode.Accepted and dlg.was_changed:
            save_profile()
            self.changed.emit()
            self.done(2)

    def _manage_mounts(self)     -> None: MountsDialog(self).exec()
    def _samba_credentials(self) -> None: SambaPasswordDialog.open(self)

    def _manage_profiles(self) -> None:
        dlg = ProfilesDialog(self)
        dlg.exec()
        if dlg.was_changed:
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

        self._theme_cb = _combo("Select Theme:", list(THEMES.keys()), S.ui.get("theme", "Tokyo Night"))
        self._font_cb = _combo("Select Font:", ["(System Default)"] + sorted(QFontDatabase.families()),
                               S.ui.get("font_family", "") or "(System Default)")
        self._size_cb = _combo("Select Font Size:", ["10", "11", "12", "13", "14", "15", "16", "17", "18", "20", "22", "24"],
                               str(S.ui.get("font_size", 14)))

        prev_btn = QPushButton("Preview")
        prev_btn.clicked.connect(lambda: self._apply(save=False))
        layout.addWidget(prev_btn)
        layout.addWidget(_ok_cancel_buttons(self, self._on_ok, cancel_fn=self._on_cancel))

    def _apply(self, save: bool = False) -> None:
        chosen_font = self._font_cb.currentText()
        if chosen_font == "(System Default)":
            chosen_font = ""
        S.ui.update(theme=self._theme_cb.currentText(), font_family=chosen_font, font_size=int(self._size_cb.currentText()))
        apply_style()
        if save:
            save_profile()

    def _on_ok(self) -> None:
        self._apply(save=True)
        msg = f"Theme: {self._theme_cb.currentText()}, Font: {self._font_cb.currentText()} {self._size_cb.currentText()}px"
        QMessageBox.information(self, "Theme Saved", msg)
        self.changed.emit(2)
        self.accept()

    def _on_cancel(self) -> None:
        orig_theme, orig_font, orig_size = self._orig
        S.ui.update(theme=orig_theme, font_family=orig_font, font_size=orig_size)
        apply_style()
        self.reject()

    def keyPressEvent(self, event) -> None:
        if event.key() != Qt.Key.Key_Escape:
            super().keyPressEvent(event)


_WINDOW_MAP: "dict[str, Type[_BaseCheckboxWindow]]" = {"Backup": BackupWindow, "Restore": RestoreWindow, "Settings": SettingsWindow}


def base_window(parent, mode: str = "Settings") -> "_BaseCheckboxWindow":
    cls = _WINDOW_MAP.get(mode, SettingsWindow)
    return cls(parent)

from __future__ import annotations
from options import Options
from global_style import THEMES
from PyQt6.QtGui import QFontDatabase
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QGridLayout, QLabel, QWidget,
                             QHBoxLayout, QMessageBox, QPushButton, QScrollArea, QSizePolicy, QSpacerItem, QVBoxLayout)

from logging_config import setup_logger
logger = setup_logger(__name__)

_WINDOW_TITLES = {
    "backup":   "Create Backup",
    "restore":  "Restore Backup",
    "settings": "Settings",
}

_COLS_WIDE   = 4
_COLS_NARROW = 2

_COPY_LOGIC_TOOLTIP = (
    "<b>How files are copied and when they are skipped</b><br><br>"
    "<b>Copied Files:</b><br>"
    "- Files are copied when the source is newer than the destination "
    "or the destination does not yet exist.<br>"
    "- For directories, all contained files are evaluated individually.<br>"
    "- File attributes (modification time, permissions) are preserved.<br>"
    "- Samba shares are supported if set up correctly. "
    "Use <code>smb://ip/rest-of-path</code> for source or destination.<br>"
    "Example: <code>smb://192.168.0.53/share/data</code><br><br>"
    "<b>Skipped Files:</b><br>"
    "- Files are skipped when the destination exists <b>and</b> has the same size "
    "<b>and</b> is at least as new as the source.<br>"
    "- Certain lock patterns (e.g. <code>Singleton</code>, <code>lockfile</code>, "
    "<code>cookies.sqlite-wal</code>) are always skipped.<br>"
    "- Missing or inaccessible source files are also skipped.<br>"
    "- Skipped files appear in the 'Skipped' tab with a reason.<br><br>"
    "<b>Errors:</b><br>"
    "- Copy errors (permission denied, network issues, etc.) are listed in the 'Errors' tab.<br><br>"
    "<b>Summary:</b><br>"
    "- Totals for copied, skipped, and error files are shown in the summary.<br>"
    "- Green = copied, yellow = skipped, red = errors."
)


class BaseWindow(QDialog):

    settings_changed = pyqtSignal()

    def __init__(self, parent=None, window_type: str = "base") -> None:
        super().__init__(parent)
        self.window_type  = window_type
        self.columns      = _COLS_NARROW
        self.checkbox_dirs: list[tuple] = []

        self._last_entries_hash: int | None = None
        self._last_ui_state:     tuple | None = None
        self._tooltip_cache:     dict | None = None
        self._shown_once:        bool = False
        self.content_widget:     QWidget | None = None

        self.setWindowTitle(_WINDOW_TITLES.get(window_type, "Window"))

        self.main_layout  = QVBoxLayout(self)
        self.top_controls = QHBoxLayout()
        self.scroll_area  = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.main_layout.addLayout(self.top_controls)
        self.main_layout.addWidget(self.scroll_area, stretch=1)

        self.selectall     = QCheckBox("Select All")
        self.column_toggle = QPushButton()

        self.settings_changed.connect(self.setup_ui)
        self.setup_ui()

    def setup_ui(self) -> None:
        entries_sorted = getattr(Options, "entries_sorted", [])
        entries_hash = hash(tuple(
            (e.get("unique_id", ""), e.get("title", "")) for e in entries_sorted
        ))
        ui_state = (
            self.window_type,
            Options.ui_settings.get(f"{self.window_type}_window_columns", 2),
            len(getattr(Options, "header_order", [])),
            len(getattr(Options, "header_inactive", [])),
        )

        if (self._last_entries_hash == entries_hash and
                self._last_ui_state == ui_state and
                self.content_widget is not None):
            return

        self._last_entries_hash = None
        self._last_ui_state     = None
        self._tooltip_cache     = None

        Options.sort_entries()
        self._clear_content()

        if not Options.headers:
            Options.headers = getattr(Options, "header_order", []).copy()

        col_setting = Options.ui_settings.get(f"{self.window_type}_window_columns", 2)
        self.columns = _COLS_WIDE if col_setting == 4 else _COLS_NARROW

        next_col_count = _COLS_NARROW if self.columns == _COLS_WIDE else _COLS_WIDE
        self._rebuild_top_controls(f"{next_col_count} Columns")

        self.content_widget = QWidget()
        grid = QGridLayout(self.content_widget)

        if self.window_type in ("restore", "settings"):
            sublayout_entries = self._collect_sublayout_entries()
            self._create_sublayout_widgets(sublayout_entries)
            row = self._populate_header_checkboxes(grid, sublayout_entries)
        else:
            row = self._populate_header_checkboxes(grid)

        self._add_action_buttons(grid, row)
        self.scroll_area.setWidget(self.content_widget)
        self._fit_window()

        self._last_entries_hash = entries_hash
        self._last_ui_state     = ui_state

    def _rebuild_top_controls(self, toggle_label: str) -> None:
        self._clear_layout(self.top_controls)

        self.selectall = QCheckBox("Select All")
        self.selectall.clicked.connect(self.toggle_checkboxes_manually)

        self.column_toggle = QPushButton(toggle_label)
        self.column_toggle.clicked.connect(self.toggle_columns)

        self.top_controls.addWidget(self.selectall)

        if self.window_type == "settings":
            self.top_controls.addStretch(1)
            self.top_controls.addWidget(self._make_config_path_label())

        self.top_controls.addStretch(1)
        self.top_controls.addWidget(self.column_toggle)

    @staticmethod
    def _make_config_path_label() -> QLabel:
        raw_path = str(Options.config_file_path)
        for old, new in getattr(Options, "text_replacements", []):
            if old:
                raw_path = raw_path.replace(old, new)

        html = (
            "󰔨  "
            f"<span style='font-size:16px;padding:4px;"
            f"color:#9891c2;text-decoration:underline dotted;'>{raw_path}</span>"
        )
        label = QLabel(html)
        label.setTextFormat(Qt.TextFormat.RichText)
        label.setToolTip(_COPY_LOGIC_TOOLTIP)
        label.setToolTipDuration(30_000)
        label.setCursor(Qt.CursorShape.WhatsThisCursor)
        return label

    def _get_visible_header_entries(self) -> tuple[dict, list]:
        if self.window_type == "settings":
            active_headers = Options.headers
        else:
            active_headers = [h for h in Options.headers if h not in Options.header_inactive]

        filter_key = "no_backup" if self.window_type == "backup" else "no_restore"
        all_entries = [
            e for e in getattr(Options, "entries_sorted", [])
            if self.window_type == "settings" or not e.get(filter_key, False)
        ]

        grouped: dict = {}
        for entry in all_entries:
            h = entry["header"]
            if h in active_headers:
                grouped.setdefault(h, []).append(entry)

        return grouped, active_headers

    def _populate_header_checkboxes(
        self, grid: QGridLayout, sublayout_entries: dict | None = None
    ) -> int:
        self.checkbox_dirs.clear()
        grouped, _ = self._get_visible_header_entries()
        row = 0

        for header, entries in grouped.items():
            inactive = self.window_type == "settings" and header in Options.header_inactive
            color    = "#7f7f7f" if inactive else Options.header_colors.get(header, "#ffffff")
            label_text = f"{header} (Inactive)" if inactive else header

            header_label = QLabel(label_text)
            header_label.setStyleSheet(f"font-size:17px;font-weight:bold;color:{color};")
            hbox = QHBoxLayout()
            hbox.addWidget(header_label)
            grid.addLayout(hbox, row, 0, 1, self.columns)
            row += 1

            col = 0
            for entry in entries:
                checkbox = QCheckBox(entry["title"])
                checkbox.setStyleSheet(f"QCheckBox{{color:{color};}} QToolTip{{color:#07e392;}}")
                checkbox.stateChanged.connect(self.update_select_all_state)

                checkbox.entry_data  = entry
                checkbox.window_type = self.window_type
                checkbox.enterEvent  = lambda evt, cb=checkbox: self._load_tooltip_on_hover(cb, evt)

                if self.window_type != "restore":
                    src, dst = entry.get("source", ""), entry.get("destination", "")
                else:
                    src, dst = entry.get("destination", ""), entry.get("source", "")

                self.checkbox_dirs.append((checkbox, src, dst, entry["unique_id"]))

                placed = False
                if (header == "Games" and
                        self.window_type in ("restore", "settings") and
                        sublayout_entries):
                    for idx in range(1, 5):
                        key = f"sublayout_games_{idx}"
                        if entry["title"] in sublayout_entries.get(key, []):
                            sub = getattr(self, key, None)
                            if sub:
                                sub.addWidget(checkbox)
                                placed = True
                            break

                if not placed:
                    grid.addWidget(checkbox, row, col)
                    col += 1
                    if col >= self.columns:
                        col = 0
                        row += 1

            if col != 0:
                row += 1

            if header == "Games" and self.window_type in ("restore", "settings"):
                row = self._insert_game_sublayouts(grid, row)

        return row

    @staticmethod
    def _collect_sublayout_entries() -> dict:
        result = {f"sublayout_games_{i}": [] for i in range(1, 5)}

        for entry in getattr(Options, "all_entries", []):
            details = getattr(entry, "details", None)
            if not isinstance(details, dict):
                continue
            title = getattr(entry, "title", None)
            if not title:
                continue
            for i in range(1, 5):
                key = f"sublayout_games_{i}"
                if details.get(key, False):
                    result[key].append(title)

        return result

    def _create_sublayout_widgets(self, sublayout_entries: dict) -> None:
        games_inactive = (
            self.window_type == "settings" and "Games" in Options.header_inactive
        )
        color = (
            "#7f7f7f" if games_inactive
            else Options.header_colors.get("Games", "#ffffff")
        )

        for i in range(1, 5):
            key = f"sublayout_games_{i}"
            if not sublayout_entries.get(key):
                continue

            layout = QVBoxLayout()
            setattr(self, key, layout)

            widget = QWidget()
            widget.setStyleSheet("background-color:#2c2f41;")
            setattr(self, f"sublayout_widget_games_{i}", widget)

            name = Options.sublayout_names.get(key, f"Sublayout Games {i}")
            select_all = QCheckBox(name)
            select_all.setStyleSheet(f"QCheckBox{{color:{color};font-size:14px;}}")
            select_all.clicked.connect(
                lambda _, idx=i: self._toggle_sublayout(
                    getattr(self, f"sublayout_games_{idx}", None),
                    getattr(self, f"select_all_games_{idx}", None),
                )
            )
            setattr(self, f"select_all_games_{i}", select_all)

            header_row = QHBoxLayout()
            header_row.addStretch(1)
            header_row.addWidget(select_all)
            header_row.addStretch(1)
            layout.addLayout(header_row)

            widget.setLayout(layout)

    def _insert_game_sublayouts(self, grid: QGridLayout, row: int) -> int:
        sublayouts = [
            (
                getattr(self, f"sublayout_widget_games_{i}", None),
                getattr(self, f"sublayout_games_{i}", None),
            )
            for i in range(1, 5)
        ]
        sublayouts = [(w, l) for w, l in sublayouts if l is not None]

        if not sublayouts:
            return row

        def add_expander(_layout):
            if _layout:
                _layout.addItem(
                    QSpacerItem(0, 0, QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Expanding)
                )

        if self.columns == _COLS_WIDE:
            for i in range(0, len(sublayouts), 2):
                for j, (w, l) in enumerate(sublayouts[i:i + 2]):
                    grid.addWidget(w, row, j * 2, 1, 2)
                    add_expander(l)
                row += 1
        else:
            for i in range(0, len(sublayouts), 2):
                widget, layout = sublayouts[i]
                grid.addWidget(widget, row, 0)
                add_expander(layout)
                if i + 1 < len(sublayouts):
                    w2, l2 = sublayouts[i + 1]
                    grid.addWidget(w2, row, 1)
                    add_expander(l2)
                row += 1

        return row

    def _add_action_buttons(self, grid: QGridLayout, row: int) -> int:
        if self.window_type in ("backup", "restore"):
            label = "Create Backup" if self.window_type == "backup" else "Restore Backup"
            action_btn = QPushButton(label)
            action_btn.clicked.connect(self.start_process)
            close_btn = QPushButton("Close")
            close_btn.clicked.connect(self.go_back)
            grid.addWidget(action_btn, row,     0, 1, self.columns)
            grid.addWidget(close_btn,  row + 1, 0, 1, self.columns)
            return row + 2

        if self.window_type == "settings":
            self.system_manager_settings_button = QPushButton("System Manager Options")
            self.system_manager_settings_button.clicked.connect(self.system_manager_options)
            grid.addWidget(self.system_manager_settings_button, row, 0, 1, self.columns)
            row += 1

            self.add_entry_button    = QPushButton("New Entry")
            self.entry_editor_button = QPushButton("Edit Entry")
            self.delete_button       = QPushButton("Delete Entry")
            self.header_settings_button = QPushButton("Header Settings")
            self.add_entry_button.clicked.connect(lambda: self.entry_dialog(edit_mode=False))
            self.entry_editor_button.clicked.connect(lambda: self.entry_dialog(edit_mode=True))
            self.delete_button.clicked.connect(self.delete_entry)
            self.header_settings_button.clicked.connect(self.header_settings)

            row1 = QHBoxLayout()
            for btn in (self.add_entry_button, self.entry_editor_button,
                        self.delete_button, self.header_settings_button):
                row1.addWidget(btn)
            grid.addLayout(row1, row, 0, 1, self.columns)
            row += 1

            self.smb_password_button = QPushButton("Samba Password")
            self.mount_button        = QPushButton("Mount Options")
            self.profile_button      = QPushButton("Profile Manager")
            self.smb_password_button.clicked.connect(self.open_samba_password_dialog)
            self.mount_button.clicked.connect(self.manage_mount_options)
            self.profile_button.clicked.connect(self.open_profile_manager)

            row2 = QHBoxLayout()
            for btn in (self.smb_password_button, self.mount_button, self.profile_button):
                row2.addWidget(btn)
            grid.addLayout(row2, row, 0, 1, self.columns)
            row += 1

            self.theme_button = QPushButton("Change Theme")
            self.theme_button.clicked.connect(self.change_theme)
            grid.addWidget(self.theme_button, row, 0, 1, self.columns)
            row += 1

            self.close_button = QPushButton("Close")
            self.close_button.clicked.connect(self.go_back)
            grid.addWidget(self.close_button, row, 0, 1, self.columns)
            row += 1

        return row

    def _load_tooltip_on_hover(self, checkbox: QCheckBox, event) -> None:
        if not getattr(checkbox, "_tooltip_set", False):
            if self._tooltip_cache is None:
                backup_tips, restore_tips, sm_tips = Options.generate_tooltip()
                self._tooltip_cache = {
                    "backup":        backup_tips,
                    "restore":       restore_tips,
                    "settings":      backup_tips,
                    "system_manager": sm_tips,
                }

            tips = self._tooltip_cache.get(getattr(checkbox, "window_type", ""), {})
            tip  = tips.get(f"{checkbox.text()}_tooltip", "No detailed information available.")
            checkbox.setToolTip(tip)
            checkbox.setToolTipDuration(600_000)
            try:
                checkbox._tooltip_set = True  # type: ignore[attr-defined]
            except AttributeError:
                pass

        QCheckBox.enterEvent(checkbox, event)

    def update_select_all_state(self) -> None:
        visible_cbs = [cb for cb, *_ in self.checkbox_dirs if cb.isVisible()]
        if not visible_cbs:
            return
        all_checked = all(cb.isChecked() for cb in visible_cbs)
        self.selectall.blockSignals(True)
        self.selectall.setChecked(all_checked)
        self.selectall.blockSignals(False)

        if self.window_type in ("restore", "settings"):
            self._sync_sublayout_select_all()

    def toggle_checkboxes_manually(self) -> None:
        state = self.selectall.isChecked()
        for cb, *_ in self.checkbox_dirs:
            cb.setChecked(state)

    def _sync_sublayout_select_all(self) -> None:
        for i in range(1, 5):
            widget    = getattr(self, f"sublayout_widget_games_{i}", None)
            select_cb = getattr(self, f"select_all_games_{i}", None)
            if widget and select_cb:
                children = [
                    cb for cb in widget.findChildren(QCheckBox) if cb != select_cb
                ]
                all_checked = all(cb.isChecked() for cb in children) if children else False
                _block_set(select_cb, all_checked)

    def _toggle_sublayout(self, layout, select_all_cb: QCheckBox) -> None:
        if layout and select_all_cb:
            state = select_all_cb.isChecked()
            for i in range(layout.count()):
                item = layout.itemAt(i)
                if item:
                    widget = item.widget()
                    if isinstance(widget, QCheckBox) and widget != select_all_cb:
                        _block_set(widget, state)
            self.update_select_all_state()

    def toggle_columns(self) -> None:
        new = _COLS_WIDE if self.columns == _COLS_NARROW else _COLS_NARROW
        if new == self.columns:
            return
        self.setVisible(False)
        self.columns = new
        Options.ui_settings[f"{self.window_type}_window_columns"] = new
        Options.save_config()
        self._last_entries_hash = None
        self._last_ui_state     = None
        self.setup_ui()
        self.setVisible(True)

    def _fit_window(self) -> None:
        if self.content_widget is None:
            return
        self.content_widget.adjustSize()
        primary = QApplication.primaryScreen()
        if primary is None:
            return
        screen  = primary.availableGeometry()
        hint    = self.content_widget.sizeHint()
        margin  = (
            self.main_layout.contentsMargins().top() +
            self.main_layout.contentsMargins().bottom() +
            self.main_layout.spacing() +
            self.top_controls.sizeHint().height() + 20
        )
        self.resize(
            min(hint.width() + 165, screen.width()),
            min(hint.height() + margin, screen.height()),
        )
        self.scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)

    adjust_window_size = _fit_window

    def change_theme(self) -> None:

        class _ThemeDialog(QDialog):
            def keyPressEvent(self, event):
                if event.key() == Qt.Key.Key_Escape:
                    event.ignore()
                else:
                    super().keyPressEvent(event)

        dialog = _ThemeDialog(self)
        dialog.setWindowTitle("Theme and Font Settings")
        dialog.setMinimumSize(500, 400)
        dialog.setWindowModality(Qt.WindowModality.NonModal)
        dialog.closeEvent = lambda e: e.ignore()

        layout = QVBoxLayout(dialog)

        layout.addWidget(QLabel("Select Theme:"))
        theme_combo = QComboBox()
        theme_combo.addItems(list(THEMES.keys()))
        theme_combo.setCurrentText(Options.ui_settings.get("theme", "Tokyo Night"))
        layout.addWidget(theme_combo)

        layout.addWidget(QLabel("Select Font:"))
        font_combo = QComboBox()
        font_combo.addItems(sorted(QFontDatabase.families()))
        font_combo.setCurrentText(Options.ui_settings.get("font_family", "DejaVu Sans"))
        layout.addWidget(font_combo)

        layout.addWidget(QLabel("Select Font Size:"))
        size_combo = QComboBox()
        size_combo.addItems(["10","11","12","13","14","15","16","17","18","20","22","24"])
        size_combo.setCurrentText(str(Options.ui_settings.get("font_size", 14)))
        layout.addWidget(size_combo)

        preview_btn = QPushButton("Preview")
        preview_btn.clicked.connect(lambda: self._apply_theme(
            theme_combo.currentText(),
            font_combo.currentText(),
            int(size_combo.currentText()),
            save=False,
        ))
        layout.addWidget(preview_btn)

        orig_theme = Options.ui_settings.get("theme", "Tokyo Night")
        orig_font  = Options.ui_settings.get("font_family", "DejaVu Sans")
        orig_size  = Options.ui_settings.get("font_size", 14)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel # type: ignore[attr-defined]
        )
        ok_btn     = buttons.button(QDialogButtonBox.StandardButton.Ok)
        cancel_btn = buttons.button(QDialogButtonBox.StandardButton.Cancel)
        if ok_btn is not None:
            ok_btn.clicked.connect(
                lambda: self._save_theme(
                    theme_combo.currentText(),
                    font_combo.currentText(),
                    int(size_combo.currentText()),
                    dialog,
                )
            )
        if cancel_btn is not None:
            cancel_btn.clicked.connect(
                lambda: self._restore_theme(orig_theme, orig_font, int(orig_size), dialog)
            )
        layout.addWidget(buttons)

        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def _apply_theme(self, theme: str, font: str, size: int, save: bool = False) -> None:
        import global_style
        Options.ui_settings.update(theme=theme, font_family=font, font_size=size)
        global_style.current_theme = theme
        style = global_style.get_current_style()
        app = QApplication.instance()
        if isinstance(app, QApplication):
            app.setStyleSheet(style)
        if save:
            Options.save_config()
        if app is not None:
            app.processEvents()
        self._fit_window()

    def _save_theme(self, theme: str, font: str, size: int, dialog: QDialog) -> None:
        self._apply_theme(theme, font, size, save=True)
        dialog.accept()
        try:
            self.setUpdatesEnabled(False)
            self._last_entries_hash = None
            self._last_ui_state     = None
            self._tooltip_cache     = None
            self._clear_content()
            self.setup_ui()
            self._fit_window()
        finally:
            self.setUpdatesEnabled(True)
        app = QApplication.instance()
        if app is not None:
            app.processEvents()
        self.show_message("Success", f"Theme set to {theme}, font {font} {size}px.")
        if self.parent():
            try:
                self.parent().update()
            except RuntimeError:
                pass

    def _restore_theme(self, theme: str, font: str, size: int, dialog: QDialog) -> None:
        self.hide()
        self._apply_theme(theme, font, size, save=False)
        dialog.reject()
        self._fit_window()
        self.show()

    def preview_theme_and_font(self, theme, font, size):
        self._apply_theme(theme, font, size, save=False)

    def restore_theme_and_font(self, theme, font, size, dialog):
        self._restore_theme(theme, font, size, dialog)

    def save_theme_and_font(self, theme, font, size, dialog):
        self._save_theme(theme, font, size, dialog)

    def start_process(self) -> None: pass
    def show_message(self, title: str, message: str) -> None:
        QMessageBox.information(self, title, message)
    def system_manager_options(self) -> None: pass
    def entry_dialog(self, edit_mode: bool = False) -> None: pass
    def delete_entry(self) -> None: pass
    def header_settings(self) -> None: pass
    def open_samba_password_dialog(self) -> None: pass
    def manage_mount_options(self) -> None: pass
    def open_profile_manager(self) -> None: pass

    def keyPressEvent(self, event) -> None:
        key = event.key()
        try:
            focused = self.focusWidget()
            if key in (Qt.Key.Key_Enter, Qt.Key.Key_Return) and isinstance(focused, QCheckBox):
                focused.toggle()
                if focused == self.selectall:
                    self.toggle_checkboxes_manually()
                elif self.window_type in ("restore", "settings"):
                    for i in range(1, 5):
                        sa = getattr(self, f"select_all_games_{i}", None)
                        if focused == sa:
                            self._toggle_sublayout(
                                getattr(self, f"sublayout_games_{i}", None), sa
                            )
                            break
            elif key == Qt.Key.Key_Escape:
                self.go_back()
            else:
                super().keyPressEvent(event)
        except Exception as exc:
            logger.warning("keyPressEvent: %s", exc)
            super().keyPressEvent(event)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if not self._shown_once:
            for cb, *_ in self.checkbox_dirs:
                cb.setChecked(False)
            self._shown_once = True
        if self.selectall is not None:
            self.selectall.setChecked(False)
            self.selectall.setFocus()

    def go_back(self) -> None:
        self.close()

    def closeEvent(self, event) -> None:
        self._shown_once = False
        self._tooltip_cache = None
        for cb, *_ in self.checkbox_dirs:
            cb.blockSignals(True)
            for attr in ("_tooltip_set", "enterEvent", "entry_data", "window_type"):
                try:
                    delattr(cb, attr)
                except AttributeError:
                    pass
        self._clear_content()
        parent = self.parent()
        try:
            if parent:
                parent.show()
        except RuntimeError:
            pass
        super().closeEvent(event)

    def _clear_layout(self, layout) -> None:
        if not layout:
            return
        while layout.count():
            item = layout.takeAt(0)
            if not item:
                continue
            widget = item.widget()
            if widget:
                widget.blockSignals(True)
                widget.setParent(None)
                widget.deleteLater()
            elif item.layout():
                self._clear_layout(item.layout())

    def _clear_content(self) -> None:
        self._tooltip_cache = None
        for cb, *_ in self.checkbox_dirs:
            for attr in ("_tooltip_set", "enterEvent"):
                try:
                    delattr(cb, attr)
                except AttributeError:
                    pass
            cb.blockSignals(True)

        self._clear_layout(self.top_controls)

        old = self.scroll_area.takeWidget()
        if old:
            old.deleteLater()

        self.content_widget = None
        self.checkbox_dirs.clear()

    clear_layout_contents = _clear_content


def _block_set(checkbox: QCheckBox, checked: bool) -> None:
    checkbox.blockSignals(True)
    checkbox.setChecked(checked)
    checkbox.blockSignals(False)

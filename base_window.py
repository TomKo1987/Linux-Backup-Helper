import logging.handlers
from options import Options
from PyQt6.QtGui import QFontDatabase
from PyQt6.QtCore import Qt, pyqtSignal
from global_style import THEMES, get_current_style
from PyQt6.QtWidgets import (QApplication, QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QDialog, QLabel, QGridLayout,
                             QScrollArea, QCheckBox, QSpacerItem, QSizePolicy, QComboBox, QMessageBox, QDialogButtonBox)

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

if not logger.hasHandlers():
    handler = logging.StreamHandler()
    formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
    handler.setFormatter(formatter)
    logger.addHandler(handler)


# noinspection PyUnresolvedReferences
class BaseWindow(QDialog):
    settings_changed = pyqtSignal()

    def __init__(self, parent=None, window_type="base"):
        super().__init__(parent)
        self.window_type = window_type
        self.setWindowTitle(
            {"backup": "Create Backup", "restore": "Restore Backup", "settings": "Settings"}.get(window_type, "Window")
        )
        self._last_entries_hash = None
        self._last_ui_state = None
        self.content_widget = None
        self._tooltip_cache = None
        self.main_layout = QVBoxLayout(self)
        self.top_controls = QHBoxLayout()
        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.main_layout.addLayout(self.top_controls)
        self.main_layout.addWidget(self.scroll_area, stretch=1)
        self.selectall = QCheckBox("Select All")
        self.column_toggle = QPushButton()
        self.columns = 4
        self.checkbox_dirs = []
        self.settings_changed.connect(self.setup_ui)
        self.setup_ui()

    def setup_ui(self):
        current_entries_hash = hash(str(getattr(Options, "entries_sorted", [])))
        current_ui_state = (
            self.window_type,
            Options.ui_settings.get(f"{self.window_type}_window_columns", 2),
            len(getattr(Options, "header_order", [])),
            len(getattr(Options, "header_inactive", [])),
        )

        if (
                self._last_entries_hash == current_entries_hash
                and self._last_ui_state == current_ui_state
                and self.content_widget is not None
        ):
            return

        self._last_entries_hash = None
        self._last_ui_state = None
        self._tooltip_cache = None

        Options.sort_entries()
        self.clear_layout_contents()

        key = f"{self.window_type}_window_columns"
        self.columns = 4 if Options.ui_settings.get(key, 2) == 4 else 2
        self.create_top_controls(f"{2 if self.columns == 4 else 4} Columns")

        self.content_widget = QWidget()
        layout = QGridLayout(self.content_widget)

        if self.window_type in ("restore", "settings"):
            sublayout_entries = self.get_sublayout_entries()
            self.setup_sublayouts(sublayout_entries)
            row_counter = self.add_header_checkboxes(layout, sublayout_entries)
        else:
            row_counter = self.add_header_checkboxes(layout)

        self.add_control_buttons(layout, row_counter)
        self.scroll_area.setWidget(self.content_widget)
        self.adjust_window_size()

        self._last_entries_hash = hash(str(getattr(Options, "entries_sorted", [])))
        self._last_ui_state = (
            self.window_type,
            Options.ui_settings.get(f"{self.window_type}_window_columns", 2),
            len(Options.header_order),
            len(Options.header_inactive),
        )

    def create_top_controls(self, column_text):
        self._clear_layout(self.top_controls)
        self.selectall = QCheckBox("Select All")
        self.selectall.setStyleSheet(f"{get_current_style()}")
        self.selectall.clicked.connect(self.toggle_checkboxes_manually)
        config_path_text = str(Options.config_file_path)
        if hasattr(Options, "text_replacements"):
            for old, new in Options.text_replacements:
                if old:
                    config_path_text = config_path_text.replace(old, new)

        tooltip_text = (
            "<b>How files are copied and when they are skipped</b><br><br>"
            "<b>Copied Files:</b><br>"
            "- Files are copied if the source file is newer than the destination, or if the destination does not exist.<br>"
            "- For directories, all contained files are evaluated individually.<br>"
            "- File attributes (modification time, permissions) are preserved.<br>"
            "- Network (SMB) paths are supported; files can be copied to and from SMB shares.<br><br>"
            "<b>Skipped Files:</b><br>"
            "- Files are skipped and <b>NOT</b> copied if:<br>"
            "&emsp;- The destination file already exists <b>and</b> has the same size <b>and</b> is at least as new as the source file (i.e., up to date).<br>"
            "&emsp;- The file matches certain protection/lock patterns (e.g., <code>Singleton</code>, <code>lockfile</code>, <code>cookies.sqlite-wal</code>, etc.).<br>"
            "&emsp;- The source file does not exist or cannot be accessed.<br>"
            "- Skipped files are shown in the \"Skipped\" tab with a reason, such as \"Up to date\" or \"Protected/locked file\".<br><br>"
            "<b>Errors:</b><br>"
            "- If an error occurs during copying (e.g., permission denied, network problems, or unexpected issues), the file is not copied, and the error is shown in the \"Errors\" tab.<br><br>"
            "<b>Summary:</b><br>"
            "- You can view the total number of processed, copied, skipped, and error files in the summary.<br>"
            "- The tooltip color-codes the results: green for copied, yellow for skipped, red for errors.<br><br>"
            "This logic ensures that only necessary files are copied, avoids overwriting up-to-date or protected files, and provides clear feedback for each file processed."
        )

        tooltip_icon = "󰔨  "
        label_html = (
            f"{tooltip_icon}<span style='font-size: 16px; padding: 4px; "
            f"color: #9891c2; text-decoration: underline dotted;'>{config_path_text}</span>"
        )
        config_save_path_label = QLabel(label_html)
        config_save_path_label.setTextFormat(Qt.TextFormat.RichText)
        config_save_path_label.setToolTip(tooltip_text)
        config_save_path_label.setCursor(Qt.CursorShape.WhatsThisCursor)
        config_save_path_label.setToolTipDuration(30000)

        self.column_toggle = QPushButton(column_text)
        self.column_toggle.clicked.connect(self.toggle_columns)
        self.top_controls.addWidget(self.selectall)
        if self.window_type == "settings":
            self.top_controls.addStretch(1)
            self.top_controls.addWidget(config_save_path_label)
        self.top_controls.addStretch(1)
        self.top_controls.addWidget(self.column_toggle)

    def add_header_checkboxes(self, layout, sublayout_entries=None):
        row = 0
        self.checkbox_dirs.clear()

        active_headers = (Options.headers if self.window_type == "settings" else [h for h in Options.headers if
                                                                                  h not in Options.header_inactive])

        filter_key = "no_backup" if self.window_type == "backup" else "no_restore"

        all_filtered_entries = [e for e in getattr(Options, 'entries_sorted', []) if
                                self.window_type == "settings" or not e.get(filter_key, False)]

        header_entries = {}
        for entry in all_filtered_entries:
            header = entry["header"]
            if header in active_headers:
                if header not in header_entries:
                    header_entries[header] = []
                header_entries[header].append(entry)

        for header, ents in header_entries.items():
            inactive = self.window_type == "settings" and header in Options.header_inactive
            col = 0
            header_color = "#7f7f7f" if inactive else Options.header_colors.get(header, '#ffffff')
            label = QLabel(f"{header} (Inactive)" if inactive else header)
            label.setStyleSheet(f"font-size: 17px; font-weight: bold; color: {header_color};")
            hbox = QHBoxLayout()
            hbox.addWidget(label)
            layout.addLayout(hbox, row, 0, 1, self.columns)
            row += 1

            for entry in ents:
                checkbox = QCheckBox(entry["title"])
                ch_style = f"{get_current_style()} QCheckBox {{color: {header_color}}} QToolTip {{color: '#07e392';}}"
                checkbox.setStyleSheet(ch_style)

                if header == "Games" and self.window_type in ("restore", "settings") and sublayout_entries:
                    added = False
                    for i in range(1, 5):
                        key = f'sublayout_games_{i}'
                        if entry["title"] in sublayout_entries[key]:
                            checkbox.setStyleSheet(
                                f"{get_current_style()} QCheckBox {{color: {header_color};}} QToolTip {{color: '#07e392';}}")
                            sublayout = getattr(self, key, None)
                            if sublayout:
                                sublayout.addWidget(checkbox)
                                added = True
                            break
                    if not added:
                        checkbox.setStyleSheet(ch_style)
                        layout.addWidget(checkbox, row, col)
                        col += 1
                else:
                    checkbox.setStyleSheet(ch_style)
                    layout.addWidget(checkbox, row, col)
                    col += 1

                checkbox.stateChanged.connect(self.update_select_all_state)

                if col >= self.columns:
                    col = 0
                    row += 1

                if self.window_type != "restore":
                    src, dst = entry.get("source", ""), entry.get("destination", "")
                else:
                    src, dst = entry.get("destination", ""), entry.get("source", "")

                self.checkbox_dirs.append((checkbox, src, dst, entry["unique_id"]))

                checkbox.entry_data = entry
                checkbox.window_type = self.window_type
                checkbox.enterEvent = lambda event, cb=checkbox: self._setup_tooltip_on_hover(cb, event)

            if col != 0:
                row += 1
            if header == "Games" and self.window_type in ("restore", "settings"):
                row = self.add_game_sublayouts(layout, row)
        return row

    def _setup_tooltip_on_hover(self, checkbox, event):
        if hasattr(checkbox, '_tooltip_set') and checkbox._tooltip_set:
            try:
                super(QCheckBox, checkbox).enterEvent(event)
            except AttributeError:
                pass
            return

        try:
            if not self._tooltip_cache:
                tooltip_text, tooltip_text_entry_restore, system_manager_tooltips = Options.generate_tooltip()
                self._tooltip_cache = {
                    'backup': tooltip_text,
                    'restore': tooltip_text_entry_restore,
                    'settings': tooltip_text,
                    'system_manager': system_manager_tooltips
                }

            tooltip_dict = self._tooltip_cache.get(checkbox.window_type, {})
            tip_key = f"{checkbox.text()}_tooltip"

            tooltip_content = tooltip_dict.get(tip_key, "No detailed information available")
            checkbox.setToolTip(tooltip_content)
            checkbox.setToolTipDuration(600000)
            checkbox._tooltip_set = True

        except Exception as e:
            logger.warning(f"Error setting tooltip: {e}")
            checkbox.setToolTip("No detailed information available")
            checkbox._tooltip_set = True

        try:
            super(QCheckBox, checkbox).enterEvent(event)
        except AttributeError:
            pass

    @staticmethod
    def get_sublayout_entries():
        sublayout_entries = {f'sublayout_games_{i}': [] for i in range(1, 5)}

        try:
            if not hasattr(Options, 'all_entries') or not Options.all_entries:
                return sublayout_entries

            for entry in Options.all_entries:
                if not hasattr(entry, 'details') or not isinstance(entry.details, dict):
                    continue

                title = entry.title if hasattr(entry, 'title') else entry.details.get('title', '')
                if not title:
                    continue

                for i in range(1, 5):
                    key = f'sublayout_games_{i}'
                    if entry.details.get(key, False):
                        sublayout_entries[key].append(title)

        except Exception as e:
            logger.warning(f"Error getting sublayout entries: {e}")

        return sublayout_entries

    def setup_sublayouts(self, sublayout_entries):
        for i in range(1, 5):
            key = f'sublayout_games_{i}'
            if not sublayout_entries[key]:
                continue
            layout = QVBoxLayout()
            setattr(self, key, layout)
            widget = QWidget()
            setattr(self, f'sublayout_widget_games_{i}', widget)
            ch_layout = QHBoxLayout()
            name = Options.sublayout_names.get(key, f'Sublayout Games {i}')
            select_all = QCheckBox(name)
            color = "#7f7f7f" if self.window_type == "settings" and "Games" in Options.header_inactive else Options.header_colors.get(
                "Games", "#ffffff")
            select_all.setStyleSheet(f"QCheckBox {{color: {color}; font-size: 14px;}}")
            select_all.clicked.connect(lambda checked, idx=i: self._toggle_sublayout_checkboxes(
                getattr(self, f'sublayout_games_{idx}'),
                getattr(self, f'select_all_games_{idx}')
            ))
            setattr(self, f'select_all_games_{i}', select_all)
            ch_layout.addStretch(1)
            ch_layout.addWidget(select_all)
            ch_layout.addStretch(1)
            layout.addLayout(ch_layout)
            widget.setLayout(layout)
            widget.setStyleSheet("background-color: #2c2f41;")

    def add_game_sublayouts(self, layout, row):
        sublayouts = [(getattr(self, f'sublayout_widget_games_{i}', None), getattr(self, f'sublayout_games_{i}', None))
                      for i in range(1, 5)]
        sublayouts = [(w, l) for w, l in sublayouts if l]

        def add_spacer(layout_obj):
            if layout_obj:
                layout_obj.addItem(QSpacerItem(0, 0, QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Expanding))

        if not sublayouts:
            return row

        if self.columns == 4:
            pairs = [(0, 2), (2, 4)]
            for j, (start, end) in enumerate(pairs):
                if len(sublayouts) > start:
                    for idx in range(start, min(end, len(sublayouts))):
                        layout.addWidget(sublayouts[idx][0], row, (idx - start) * 2, 1, 2)
                        add_spacer(sublayouts[idx][1])
                    row += 1
        else:
            for i in range(0, len(sublayouts), 2):
                layout.addWidget(sublayouts[i][0], row, 0)
                add_spacer(sublayouts[i][1])
                if i + 1 < len(sublayouts):
                    layout.addWidget(sublayouts[i + 1][0], row, 1)
                    add_spacer(sublayouts[i + 1][1])
                row += 1
        return row

    def add_control_buttons(self, layout, row):
        if self.window_type in ("backup", "restore"):
            btn = QPushButton("Create Backup" if self.window_type == "backup" else "Restore Backup", self)
            btn.clicked.connect(self.start_process)
            close_btn = QPushButton("Close", self)
            close_btn.clicked.connect(self.go_back)
            layout.addWidget(btn, row, 0, 1, self.columns)
            layout.addWidget(close_btn, row + 1, 0, 1, self.columns)
        elif self.window_type == "settings":
            buttons = [('system_manager_settings_button', "System Manager Options", self.system_manager_options),
                       ('add_entry_button', "New Entry", lambda: self.entry_dialog(edit_mode=False)),
                       ('entry_editor_button', "Edit Entry", lambda: self.entry_dialog(edit_mode=True)),
                       ('delete_button', "Delete Entry", self.delete_entry),
                       ('header_settings_button', "Header Settings", self.header_settings),
                       ('smb_password_button', "Samba Password", self.open_samba_password_dialog),
                       ('mount_button', "Mount Options", self.manage_mount_options),
                       ('theme_button', "Change Theme", self.change_theme),
                       ('close_button', "Close", self.go_back)]
            for name, text, cb in buttons:
                btn = QPushButton(text, self)
                btn.clicked.connect(cb)
                setattr(self, name, btn)
            layout.addWidget(self.system_manager_settings_button, row, 0, 1, self.columns)
            row += 1
            hbox = QHBoxLayout()
            for btn in [self.add_entry_button, self.entry_editor_button, self.delete_button,
                        self.header_settings_button]:
                hbox.addWidget(btn)
            layout.addLayout(hbox, row, 0, 1, self.columns)
            row += 1
            hbox2 = QHBoxLayout()
            for btn in [self.smb_password_button, self.mount_button]:
                hbox2.addWidget(btn)
            layout.addLayout(hbox2, row, 0, 1, self.columns)
            row += 1
            layout.addWidget(self.theme_button, row, 0, 1, self.columns)
            row += 1
            layout.addWidget(self.close_button, row, 0, 1, self.columns)
        return row + 1

    def toggle_columns(self):
        new_columns = 4 if self.columns == 2 else 2
        if new_columns != self.columns:
            self.setVisible(False)
            self.columns = new_columns
            Options.ui_settings[f"{self.window_type}_window_columns"] = self.columns
            Options.save_config()
            self.setup_ui()
            self.setVisible(True)

    def adjust_window_size(self):
        self.content_widget.adjustSize()
        screen = QApplication.primaryScreen().availableGeometry()
        size = self.content_widget.sizeHint()
        margin = (self.main_layout.contentsMargins().top() +
                  self.main_layout.contentsMargins().bottom() +
                  self.main_layout.spacing() +
                  self.top_controls.sizeHint().height() + 20)
        self.resize(min(size.width() + 165, screen.width()),
                    min(size.height() + margin, screen.height()))
        self.scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)

    def _clear_layout(self, layout):
        if layout is None:
            return

        items_to_clean = []
        while layout.count():
            item = layout.takeAt(0)
            if item is not None:
                items_to_clean.append(item)

        for item in items_to_clean:
            try:
                widget = item.widget()
                if widget:
                    widget.blockSignals(True)
                    widget.clearFocus()
                    if hasattr(widget, 'enterEvent'):
                        widget.enterEvent = None
                    widget.setParent(None)
                    widget.deleteLater()
                elif item.layout():
                    self._clear_layout(item.layout())
                    item.layout().deleteLater()
                elif item.spacerItem():
                    pass
            except Exception as e:
                logger.warning(f"Error cleaning up item: {e}")

    def clear_layout_contents(self):
        self._tooltip_cache = None

        for cb, *_ in self.checkbox_dirs:
            if hasattr(cb, '_tooltip_set'):
                delattr(cb, '_tooltip_set')
            if hasattr(cb, 'enterEvent'):
                cb.enterEvent = None
            cb.blockSignals(True)

        self._clear_layout(self.top_controls)

        if self.scroll_area.widget():
            old_widget = self.scroll_area.takeWidget()
            if old_widget:
                old_widget.deleteLater()

        self.content_widget = None
        self.checkbox_dirs.clear()

    def change_theme(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("Theme and Font Settings")
        dialog.setMinimumSize(500, 400)
        dialog.setWindowModality(Qt.WindowModality.NonModal)
        layout = QVBoxLayout(dialog)

        # Theme selection
        layout.addWidget(QLabel("Select Theme:"))
        theme_combo = QComboBox()
        theme_combo.addItems(list(THEMES.keys()))
        current_theme = Options.ui_settings.get("theme", "Tokyo Night")
        theme_combo.setCurrentText(current_theme)
        layout.addWidget(theme_combo)

        # Font family selection
        layout.addWidget(QLabel("Select Font:"))
        font_combo = QComboBox()
        available_fonts = sorted(QFontDatabase.families())
        font_combo.addItems(available_fonts)
        current_font = Options.ui_settings.get("font_family", "DejaVu Sans")
        font_combo.setCurrentText(current_font)
        layout.addWidget(font_combo)

        # Font size selection
        layout.addWidget(QLabel("Select Font Size:"))
        size_combo = QComboBox()
        sizes = ["10", "11", "12", "13", "14", "15", "16", "17", "18", "20", "22", "24"]
        size_combo.addItems(sizes)
        current_size = str(Options.ui_settings.get("font_size", 14))
        size_combo.setCurrentText(current_size)
        layout.addWidget(size_combo)

        original_theme = current_theme
        original_font = current_font
        original_size = current_size

        # Preview button
        preview_btn = QPushButton("Preview")
        preview_btn.clicked.connect(lambda: self.preview_theme_and_font(
            theme_combo.currentText(),
            font_combo.currentText(),
            int(size_combo.currentText())
        ))
        layout.addWidget(preview_btn)

        # Buttons
        button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel  # type: ignore
        )
        button_box.accepted.connect(lambda: self.save_theme_and_font(
            theme_combo.currentText(),
            font_combo.currentText(),
            int(size_combo.currentText()),
            dialog
        ))
        button_box.rejected.connect(lambda: self.restore_theme_and_font(
            original_theme, original_font, int(original_size), dialog
        ))
        layout.addWidget(button_box)

        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def preview_theme_and_font(self, theme_name, font_family, font_size):
        Options.ui_settings["theme"] = theme_name
        Options.ui_settings["font_family"] = font_family
        Options.ui_settings["font_size"] = font_size

        import global_style
        global_style.current_theme = theme_name
        style = global_style.get_current_style()
        QApplication.instance().setStyleSheet(style)
        self.adjust_window_size()

    @staticmethod
    def restore_theme_and_font(original_theme, original_font, original_size, dialog):
        Options.ui_settings["theme"] = original_theme
        Options.ui_settings["font_family"] = original_font
        Options.ui_settings["font_size"] = original_size

        import global_style
        global_style.current_theme = original_theme
        if original_theme in THEMES:
            style = global_style.get_current_style()
            QApplication.instance().setStyleSheet(style)
        dialog.reject()

    def save_theme_and_font(self, theme_name, font_family, font_size, dialog):
        Options.ui_settings["theme"] = theme_name
        Options.ui_settings["font_family"] = font_family
        Options.ui_settings["font_size"] = font_size

        import global_style
        global_style.current_theme = theme_name
        style = global_style.get_current_style()
        QApplication.instance().setStyleSheet(style)

        Options.save_config()
        dialog.accept()

        self.hide()

        self._last_entries_hash = None
        self._last_ui_state = None
        self._tooltip_cache = None

        self.clear_layout_contents()
        self.setup_ui()

        self.show()

        self.show_message("Success", f"Theme changed to {theme_name}, font set to {font_family} {font_size}px!")

        if hasattr(self, 'parent') and self.parent():
            self.parent().update()

    def _delayed_size_adjustment(self):
        if self.content_widget:
            self.content_widget.adjustSize()
            self.content_widget.updateGeometry()
        self.scroll_area.updateGeometry()
        self.adjust_window_size()
        self.update()

    def start_process(self):
        pass

    def show_message(self, title, message):
        QMessageBox.information(self, title, message)

    def system_manager_options(self):
        pass

    def entry_dialog(self, edit_mode=False):
        pass

    def delete_entry(self):
        pass

    def header_settings(self):
        pass

    def open_samba_password_dialog(self):
        pass

    def manage_mount_options(self):
        pass

    @staticmethod
    def _set_checkbox_checked(checkbox, checked):
        checkbox.blockSignals(True)
        checkbox.setChecked(checked)
        checkbox.blockSignals(False)

    def update_select_all_state(self):
        if not self.checkbox_dirs:
            return

        try:
            regular_checkboxes = []
            for cb, *_ in self.checkbox_dirs:
                if cb != self.selectall and cb.isVisible() and not cb.isHidden():
                    try:
                        cb.isChecked()
                        regular_checkboxes.append(cb)
                    except (RuntimeError, AttributeError):
                        continue

            if not regular_checkboxes:
                return

            all_checked = all(cb.isChecked() for cb in regular_checkboxes)

            self.selectall.blockSignals(True)
            self.selectall.setChecked(all_checked)
            self.selectall.blockSignals(False)

            if self.window_type in ("restore", "settings"):
                self.update_game_sublayout_states()

        except Exception as e:
            logger.warning(f"Error updating select all state: {e}")

    def toggle_checkboxes_manually(self):
        is_checked = self.selectall.isChecked()
        for cb, *_ in self.checkbox_dirs:
            if cb != self.selectall:
                cb.setChecked(is_checked)

    def update_game_sublayout_states(self):
        for i in range(1, 5):
            layout = getattr(self, f'sublayout_games_{i}', None)
            widget = getattr(self, f'sublayout_widget_games_{i}', None)
            select_all_cb = getattr(self, f'select_all_games_{i}', None)
            if layout and widget and select_all_cb:
                checkboxes = [cb for cb in widget.findChildren(QCheckBox) if cb != select_all_cb]
                all_checked = all(cb.isChecked() for cb in checkboxes) if checkboxes else False
                self._set_checkbox_checked(select_all_cb, all_checked)

    def _toggle_sublayout_checkboxes(self, layout, select_all_checkbox):
        if layout and select_all_checkbox:
            checked = select_all_checkbox.isChecked()
            for i in range(layout.count()):
                item = layout.itemAt(i)
                if item:
                    cb = item.widget()
                    if cb and isinstance(cb, QCheckBox) and cb != select_all_checkbox:
                        self._set_checkbox_checked(cb, checked)
            self.update_select_all_state()

    def keyPressEvent(self, event):
        try:
            key = event.key()
            fw = self.focusWidget()

            if key in (Qt.Key.Key_Enter, Qt.Key.Key_Return) and isinstance(fw, QCheckBox):
                fw.toggle()

                if fw == self.selectall:
                    self.toggle_checkboxes_manually()
                elif self.window_type in ("restore", "settings"):
                    for i in range(1, 5):
                        select_all_games = getattr(self, f'select_all_games_{i}', None)
                        if fw == select_all_games:
                            sublayout = getattr(self, f'sublayout_games_{i}', None)
                            if sublayout:
                                self._toggle_sublayout_checkboxes(sublayout, fw)
                            break

            elif key == Qt.Key.Key_Escape:
                self.go_back()
            else:
                super().keyPressEvent(event)

        except Exception as e:
            logger.warning(f"Error in keyPressEvent: {e}")
            super().keyPressEvent(event)

    def showEvent(self, event):
        super().showEvent(event)
        try:
            for cb, *_ in self.checkbox_dirs:
                try:
                    if cb.isChecked():
                        cb.setChecked(False)
                except (RuntimeError, AttributeError):
                    continue
            if hasattr(self, 'selectall') and self.selectall and self.selectall.isVisible():
                self.selectall.setFocus()
        except (RuntimeError, AttributeError) as e:
            logger.warning(f"Error in showEvent: {e}")

    def go_back(self):
        self.close()

    def closeEvent(self, event):
        try:
            self._tooltip_cache = None

            for cb, *_ in self.checkbox_dirs:
                try:
                    if hasattr(cb, '_tooltip_set'):
                        delattr(cb, '_tooltip_set')
                    if hasattr(cb, 'enterEvent'):
                        cb.enterEvent = None
                    if hasattr(cb, 'entry_data'):
                        delattr(cb, 'entry_data')
                    if hasattr(cb, 'window_type'):
                        delattr(cb, 'window_type')
                    cb.blockSignals(True)
                except (RuntimeError, AttributeError):
                    continue

            self.clear_layout_contents()

            if self.parent():
                self.parent().show()

        except Exception as e:
            logger.warning(f"Error in closeEvent: {e}")
        finally:
            super().closeEvent(event)

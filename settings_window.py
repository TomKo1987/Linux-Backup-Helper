from options import Options
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor
from base_window import BaseWindow
from global_style import global_style
from package_installer_options import PackageInstallerOptions
from PyQt6.QtWidgets import (QMessageBox, QDialog, QVBoxLayout, QLabel, QFormLayout, QComboBox, QLineEdit, QPushButton,
                             QSizePolicy, QHBoxLayout, QWidget, QCheckBox, QGridLayout, QDialogButtonBox, QColorDialog,
                             QFileDialog, QTextEdit, QListWidget, QListWidgetItem, QInputDialog)


# noinspection PyUnresolvedReferences
class SettingsWindow(BaseWindow):
    def __init__(self, parent=None):
        super().__init__(parent, "settings")
        self.mount_options_dialog = None

    def get_checked_entries(self):
        return [(checkbox, sources, destinations, unique_id) for checkbox, sources, destinations, unique_id in self.checkbox_dirs if checkbox.isChecked()]

    def show_message(self, title, message, icon=QMessageBox.Icon.Information):
        QMessageBox(icon, title, message, QMessageBox.StandardButton.Ok, self).exec()

    @staticmethod
    def format_list_message(items, suffix):
        if not items:
            return f"{suffix}"
        if len(items) == 1:
            return f"{items[0]}{suffix}"
        if len(items) == 2:
            return f"{items[0]} and {items[1]}{suffix}"
        return f"{', '.join(items[:-1])}, and {items[-1]}{suffix}"

    def installer_options(self):
        self.hide()
        PackageInstallerOptions(self).exec()
        self.show()

    def entry_dialog(self, edit_mode=False):
        checked_entries = self.get_checked_entries()
        if edit_mode and not checked_entries:
            return self.show_message("Entry Editor Error", "Nothing selected or selected items cannot be edited.")

        self.hide()
        entries_to_process = checked_entries if edit_mode else [None]

        for entry_data in entries_to_process:
            dialog = QDialog(self)
            dialog.setFixedSize(1000, 550)
            dialog.setWindowTitle("Edit Entry" if edit_mode else "Add New Entry")

            main_layout = QVBoxLayout(dialog)
            main_layout.setContentsMargins(5, 5, 5, 5)

            title_checkbox = entry_data[0].text() if edit_mode else ""
            unique_id = entry_data[3] if edit_mode else None

            header_label_text = (
                f"\n'{title_checkbox}'\n\nType '\\n' for line break in title.\n\nFor samba shares use:\n'smb://ip/rest of samba path'"
                if edit_mode else
                "\nCreate a new entry.\n\nType '\\n' for line break in title.\n\nFor samba shares use:\n'smb://ip/rest of samba path'"
            )
            header_label = QLabel(header_label_text)
            header_label.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
            main_layout.addWidget(header_label)
            main_layout.addStretch(1)

            form_layout = QFormLayout()
            form_layout.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            form_layout.setFormAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
            field_height = 60

            header_combo = QComboBox()
            header_combo.setStyleSheet("color: #ffffff; background-color: #555582; padding: 5px 5px;")
            if not hasattr(Options, 'headers') or not Options.headers:
                Options.headers = ['Default']
            header_combo.addItems(Options.headers)
            header_combo.setMaximumHeight(field_height)

            entry_obj = None
            if edit_mode and hasattr(Options, 'all_entries'):
                entry_obj = next(
                    (e for e in Options.all_entries
                     if hasattr(e, 'details') and e.details.get('unique_id') == unique_id),
                    None
                )
                if entry_obj:
                    idx = header_combo.findText(entry_obj.header)
                    if idx >= 0:
                        header_combo.setCurrentIndex(idx)

            form_layout.addRow(QLabel("Header:"), header_combo)

            title_edit = QLineEdit(title_checkbox)
            title_edit.setMaximumHeight(field_height)
            form_layout.addRow(QLabel("Title:"), title_edit)

            if edit_mode:
                sources, destinations = entry_data[1], entry_data[2]
                for field_type, data in [("Source", sources), ("Destination", destinations)]:
                    btn = QPushButton(f"Edit {field_type} Entries")
                    btn.setMaximumHeight(field_height)
                    btn.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
                    btn.clicked.connect(
                        lambda _, d=data, t=title_checkbox, f=field_type.lower(): self.open_text_editor(d, t, f)
                    )
                    form_layout.addRow(QLabel(f"{field_type}:"), btn)
            else:
                for field_type in ["Source", "Destination"]:
                    field_edit = QLineEdit()
                    field_edit.setMaximumHeight(field_height)
                    field_edit.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)

                    btn = QPushButton(f"Select {field_type} Directory")
                    btn.setMaximumHeight(field_height)
                    btn.setFixedWidth(300)
                    btn.clicked.connect(lambda _, le=field_edit: self.select_directory(le))

                    hbox = QHBoxLayout()
                    hbox.setSpacing(5)
                    hbox.setContentsMargins(2, 2, 2, 2)
                    hbox.addWidget(field_edit)
                    hbox.addWidget(btn)

                    container = QWidget()
                    container.setLayout(hbox)
                    container.setMaximumHeight(field_height)
                    form_layout.addRow(QLabel(f"{field_type}:"), container)

                    setattr(self, f"{field_type.lower()}_edit", field_edit)

            checkboxes = {}
            checkbox_texts = {
                'no_backup': 'No Backup',
                'no_restore': 'No Restoring'
            }

            for i in range(1, 5):
                key = f'sublayout_games_{i}'
                name = Options.sublayout_names.get(key, f'Sublayout Games {i}')
                checkbox_texts[key] = f"Add to Sublayout-Games {i}:\n'{name}'"

            for key, text in checkbox_texts.items():
                cb = QCheckBox(text)
                cb.setChecked(entry_obj.details.get(key, False) if entry_obj else False)
                cb.setStyleSheet(f"{global_style} QCheckBox {{color: '#6ffff5'}}")
                cb.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
                cb.setMaximumHeight(field_height)
                checkboxes[key] = cb

            checkbox_grid = QGridLayout()
            positions = [
                ('no_backup', 0, 0), ('no_restore', 1, 0),
                ('sublayout_games_1', 0, 1), ('sublayout_games_2', 1, 1),
                ('sublayout_games_3', 0, 2), ('sublayout_games_4', 1, 2)
            ]
            for key, row, col in positions:
                checkbox_grid.addWidget(checkboxes[key], row, col)

            form_layout.addRow(QLabel(""), QLabel(""))  # Spacer
            form_layout.addRow(checkbox_grid)

            main_layout.addLayout(form_layout)

            def update_restore(state):
                disabled = state == Qt.CheckState.Checked
                for entry_i in range(1, 5):
                    entry_key = f'sublayout_games_{entry_i}'
                    checkboxes[entry_key].setChecked(False)
                    checkboxes[entry_key].setEnabled(not disabled)

            def make_sublayout_handler(num):
                def handler(state):
                    if state == Qt.CheckState.Checked:
                        checkboxes['no_restore'].blockSignals(True)
                        checkboxes['no_restore'].setChecked(False)
                        checkboxes['no_restore'].blockSignals(False)
                        checkboxes['no_restore'].setEnabled(False)
                        for entry_i in range(1, 5):
                            if entry_i != num:
                                entry_cb = checkboxes[f'sublayout_games_{entry_i}']
                                entry_cb.blockSignals(True)
                                entry_cb.setChecked(False)
                                entry_cb.setEnabled(False)
                                entry_cb.blockSignals(False)
                    else:
                        checkboxes['no_restore'].setEnabled(True)
                        for entry_i in range(1, 5):
                            if entry_i != num:
                                checkboxes[f'sublayout_games_{entry_i}'].setEnabled(True)

                return handler

            checkboxes['no_restore'].stateChanged.connect(update_restore)
            for i in range(1, 5):
                checkboxes[f'sublayout_games_{i}'].stateChanged.connect(make_sublayout_handler(i))

            button_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
            button_box.button(QDialogButtonBox.StandardButton.Save).setText("Save")
            button_box.button(QDialogButtonBox.StandardButton.Cancel).setText("Close")
            button_box.setMaximumHeight(field_height)
            main_layout.addWidget(button_box, alignment=Qt.AlignmentFlag.AlignRight)

            def save_entry():
                header = header_combo.currentText()
                title = title_edit.text().strip()

                if edit_mode:
                    source, destination = sources, destinations
                else:
                    source = self.source_edit.text().strip()
                    destination = self.destination_edit.text().strip()

                if not all([title, source, destination]):
                    return self.show_message("Error", "All fields must be filled in to add a new entry.")

                existing_titles = {
                    entry.title.lower()
                    for entry in getattr(Options, 'all_entries', [])
                    if not edit_mode or entry.title.lower() != title_checkbox.lower()
                }
                if title.lower() in existing_titles:
                    return self.show_message("Duplicate Title", "An entry with this title already exists. Please choose a different title.")

                if edit_mode and entry_obj:
                    entry_obj.header = header
                    entry_obj.title = title
                    new_entry = entry_obj
                else:
                    new_entry = Options(header, title, source, destination)
                    Options.all_entries.append(new_entry)

                for entry_key, entry_cb in checkboxes.items():
                    new_entry.details[entry_key] = entry_cb.isChecked()

                self.show_message("Success", f"Entry '{new_entry.title}' successfully {'updated' if edit_mode else 'added'}!")
                dialog.accept()
                return None

            button_box.accepted.connect(save_entry)
            button_box.rejected.connect(dialog.reject)
            dialog.exec()

        Options.save_config()
        self.show()
        return None

    def delete_entry(self):
        checked_entries = self.get_checked_entries()
        if not checked_entries:
            return self.show_message("Delete Entry Error", "Nothing selected or selected items cannot be deleted.")
        titles = [entry_data[0].text() for entry_data in checked_entries]
        checked_titles_quoted = [f"'{title}'" for title in titles]
        confirm_message = "Are you sure you want to delete " + self.format_list_message(checked_titles_quoted, "?")
        confirm_box = QMessageBox(QMessageBox.Icon.Question, "Confirm Deletion", confirm_message, QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, self)
        confirm_box.setDefaultButton(QMessageBox.StandardButton.No)
        if confirm_box.exec() == QMessageBox.StandardButton.Yes:
            entries_to_delete = []
            for checked_entry in checked_entries:
                if hasattr(Options, 'all_entries'):
                    entry_obj = next((e for e in Options.all_entries if hasattr(e, 'details') and e.details.get('unique_id') == checked_entry[3]), None)
                    if entry_obj:
                        entries_to_delete.append(entry_obj)
            for entry_obj in entries_to_delete:
                if hasattr(Options, 'delete_entry'):
                    Options.delete_entry(entry_obj)
            self.hide()
            Options.save_config()
            info_message = self.format_list_message(checked_titles_quoted, " successfully deleted!")
            self.show_message("Success", info_message)
            self.show()
            return None
        return None

    def select_directory(self, line_edit):
        directory = QFileDialog.getExistingDirectory(self, "Select Directory")
        if directory:
            line_edit.setText(directory)

    def open_text_editor(self, entries_list, title, field):
        dialog = QDialog(self)
        dialog.setMinimumSize(1200, 1000)
        dialog.setWindowTitle(f"Edit {field.capitalize()} Entries for '{title}'")
        layout = QVBoxLayout(dialog)
        text_edit = QTextEdit()
        text_edit.setPlainText("\n".join(map(str, entries_list)) if isinstance(entries_list, list) else entries_list)
        button_layout = QHBoxLayout()
        buttons = [("Back", dialog.reject), ("File Browser", lambda: self.browse_files(text_edit)), ("Save", lambda: self.save_config_from_editor(text_edit, entries_list, field))]
        for btn_text, callback in buttons:
            button = QPushButton(btn_text)
            button.clicked.connect(callback)
            button_layout.addWidget(button)
        layout.addWidget(text_edit)
        layout.addLayout(button_layout)
        dialog.exec()

    def browse_files(self, text_edit):
        files, _ = QFileDialog.getOpenFileNames(self, "Select Files")
        if files:
            current_text = text_edit.toPlainText()
            text_edit.setPlainText(current_text + "\n" + "\n".join(files))

    def save_config_from_editor(self, text_edit, entries_list, field):
        new_entries = text_edit.toPlainText().splitlines()
        if not isinstance(entries_list, list):
            new_value = new_entries[0] if new_entries else ""
            text_edit.setPlainText(new_value)
            return new_value
        entries_list.clear()
        entries_list.extend(new_entries)
        self.show_message("Success", f"{field.capitalize()} successfully edited!")
        text_edit.setPlainText("\n".join(entries_list))
        return entries_list

    def header_settings(self):
        if not hasattr(Options, 'sublayout_names'):
            Options.sublayout_names = {}
        self.hide()
        dialog = QDialog(self)
        dialog.setMinimumSize(850, 950)
        dialog.setWindowTitle("Header Settings")
        layout = QVBoxLayout(dialog)
        list_widget = QListWidget(dialog)
        list_widget.setSelectionMode(QListWidget.SelectionMode.SingleSelection)
        list_widget.setDragDropMode(QListWidget.DragDropMode.InternalMove)
        list_widget.setDragEnabled(True)
        list_widget.setAcceptDrops(True)
        list_widget.setDefaultDropAction(Qt.DropAction.MoveAction)
        header_order = getattr(Options, 'header_order', [])
        for header in list(header_order):
            item_widget = self.create_header_list_item(header, list_widget)
            item = QListWidgetItem()
            item.setSizeHint(item_widget.sizeHint())
            list_widget.addItem(item)
            list_widget.setItemWidget(item, item_widget)
        layout.addWidget(list_widget)
        layout.addWidget(QLabel("Click and hold headers to move them.\nCreating header 'Games' provides access to sublayouts for this header."))
        new_header_button = QPushButton("New Header")
        new_header_button.clicked.connect(lambda: self.add_new_header(list_widget))
        layout.addWidget(new_header_button)
        sublayout_buttons = [QPushButton(f"Name Sublayout-Games {i}:\n{Options.sublayout_names.get(f'sublayout_games_{i}', f'Sublayout Games {i}')}") for i in range(1, 5)]
        for i, btn in enumerate(sublayout_buttons, 1):
            btn.clicked.connect(lambda _, num=i: self.prompt_for_name(dialog, num))
        for i in range(0, 4, 2):
            hbox = QHBoxLayout()
            hbox.addWidget(sublayout_buttons[i])
            hbox.addWidget(sublayout_buttons[i + 1])
            layout.addLayout(hbox)
        button_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        button_box.button(QDialogButtonBox.StandardButton.Save).setText("Save")
        button_box.button(QDialogButtonBox.StandardButton.Cancel).setText("Close")
        layout.addWidget(button_box)
        button_box.accepted.connect(lambda: (self.save_header_options(list_widget), dialog.accept()))
        button_box.rejected.connect(dialog.reject)
        dialog.exec()
        self.show()

    def create_header_list_item(self, header, list_widget):
        item_widget = QWidget()
        item_layout = QHBoxLayout(item_widget)
        if not hasattr(Options, 'header_colors'):
            Options.header_colors = {}
        if not hasattr(Options, 'header_inactive'):
            Options.header_inactive = []
        header_color = Options.header_colors.get(header, '#ffffff')
        darker = self.darken_color(header_color)
        btn_style = "color: black; font-weight: bold; font-size: 17px;"
        color_btn = QPushButton(header)
        color_btn.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        color_btn.setFixedHeight(26)
        color_btn.setStyleSheet(f"QPushButton {{{btn_style} background-color: {darker};}}")
        color_btn.clicked.connect(lambda _, h=header: self.choose_color(h))
        inactive_cb = QCheckBox("Inactive")
        inactive_cb.setObjectName("inactive_checkbox")
        inactive_cb.setStyleSheet("margin-left: 10px;")
        inactive_cb.setChecked(header in Options.header_inactive)
        def update_inactive(checked):
            color_btn.setEnabled(not checked)
            bg = "gray" if checked else darker
            color_btn.setStyleSheet(f"QPushButton {{{btn_style} background-color: {bg}; padding: 0 10;}}")
        inactive_cb.stateChanged.connect(update_inactive)
        update_inactive(inactive_cb.isChecked())
        del_btn = QPushButton("Delete Header")
        del_btn.setStyleSheet("margin-left: 10px;")
        del_btn.clicked.connect(lambda _, h=header: self.delete_header(h, list_widget))
        item_layout.addWidget(color_btn)
        item_layout.addWidget(inactive_cb)
        item_layout.addWidget(del_btn)
        return item_widget

    def add_new_header(self, list_widget):
        dialog = QDialog(self)
        dialog.setWindowTitle("New Header")
        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel("Enter new Header:"))
        input_field = QLineEdit(dialog)
        input_field.setMinimumWidth(450)
        layout.addWidget(input_field)
        button_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        button_box.button(QDialogButtonBox.StandardButton.Save).setText("Save")
        button_box.button(QDialogButtonBox.StandardButton.Cancel).setText("Close")
        layout.addWidget(button_box)
        button_box.accepted.connect(lambda: self.handle_new_header(input_field, dialog, list_widget))
        button_box.rejected.connect(dialog.reject)
        dialog.exec()

    def handle_new_header(self, input_field, dialog, list_widget):
        new_header = input_field.text().strip()
        if not new_header:
            return None
        if new_header in Options.header_colors:
            return self.show_message("Duplicate Header", "Header already exists. Please choose a different name.")
        dialog.accept()
        color_dialog = QColorDialog(self)
        if color_dialog.exec() != QColorDialog.DialogCode.Accepted:
            return None
        Options.header_colors[new_header] = color_dialog.currentColor().name()
        item_widget = self.create_header_list_item(new_header, list_widget)
        item = QListWidgetItem()
        item.setSizeHint(item_widget.sizeHint())
        list_widget.addItem(item)
        list_widget.setItemWidget(item, item_widget)
        Options.header_order.append(new_header)
        Options.save_config()
        self.show_message("Success", "Header successfully created!")
        return None

    def prompt_for_name(self, parent_dialog, sublayout_num):
        name, ok = QInputDialog.getText(parent_dialog, "Enter Name", "   Enter name for sublayout:   ")
        if ok and name:
            key = f'sublayout_games_{sublayout_num}'
            Options.sublayout_names[key] = name
            for button in parent_dialog.findChildren(QPushButton):
                if button.text().startswith(f"Name Sublayout-Games {sublayout_num}:"):
                    button.setText(f"Name Sublayout-Games {sublayout_num}:\n{name}")
                    break
            self.show_message('Success', 'Sublayout name successfully saved!')
            Options.save_config()

    @staticmethod
    def darken_color(color_str):
        color = QColor(color_str)
        h, s, v, a = color.getHsv()
        v = max(0, v - 120)
        return QColor.fromHsv(h, s, v, a).name()

    def choose_color(self, header):
        current_color = Options.header_colors.get(header, '#ffffff')
        color_dialog = QColorDialog(self)
        color_dialog.setCurrentColor(QColor(current_color))
        if color_dialog.exec() == QColorDialog.DialogCode.Accepted:
            Options.header_colors[header] = color_dialog.currentColor().name()
            self.update_button_color(header)
            Options.save_config()
            self.show_message("Success", "Header color successfully saved!")

    def update_button_color(self, header):
        for child in self.findChildren(QPushButton):
            if child.text() == header:
                color = Options.header_colors.get(header, '#ffffff')
                child.setStyleSheet(f"color: black; font-weight: bold; font-size: 20px; background-color: {self.darken_color(color)};")

    def delete_header(self, header, list_widget):
        has_associated_entries = False
        if hasattr(Options, 'all_entries'):
            has_associated_entries = any(hasattr(entry, 'header') and entry.header == header for entry in Options.all_entries)
        if has_associated_entries:
            return self.show_message("Cannot Delete Header", "Header has associated entries and cannot be deleted. Remove them first.", QMessageBox.Icon.Warning)
        confirm_box = QMessageBox(QMessageBox.Icon.Question, "Confirm Deletion", f"Are you sure you want to delete header '{header}'?", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, self)
        confirm_box.setDefaultButton(QMessageBox.StandardButton.No)
        if confirm_box.exec() == QMessageBox.StandardButton.Yes:
            Options.header_colors.pop(header, None)
            if header in Options.header_order:
                Options.header_order.remove(header)
            if header in Options.header_inactive:
                Options.header_inactive.remove(header)
            for i in range(list_widget.count()):
                item_widget = list_widget.itemWidget(list_widget.item(i))
                if item_widget.findChild(QPushButton).text() == header:
                    list_widget.takeItem(i)
                    break
            Options.save_config()
            self.show_message("Success", f"Header '{header}' has been successfully deleted!")
            return None
        return None

    def save_header_options(self, list_widget):
        if not hasattr(Options, 'all_entries'):
            Options.all_entries = []
        new_header_order, new_header_inactive = [], []
        for i in range(list_widget.count()):
            item_widget = list_widget.itemWidget(list_widget.item(i))
            header_btn = item_widget.findChild(QPushButton)
            if not header_btn:
                continue
            header = header_btn.text()
            new_header_order.append(header)
            cb = item_widget.findChild(QCheckBox, "inactive_checkbox")
            if cb and cb.isChecked():
                new_header_inactive.append(header)
        Options.header_order = new_header_order
        Options.header_inactive = new_header_inactive
        for entry in Options.all_entries:
            entry.details['inactive'] = entry.header in new_header_inactive
        Options.save_config()
        self.show_message("Success", "Settings successfully saved!")

    def open_samba_password_dialog(self):
        self.hide()
        from samba_password import SambaPasswordDialog, SambaPasswordManager
        SambaPasswordDialog(self).exec()
        self.show()

    def manage_mount_options(self):
        if not hasattr(Options, 'mount_options'):
            Options.mount_options = []
        if not hasattr(Options, 'run_mount_command_on_launch'):
            Options.run_mount_command_on_launch = False
        self.hide()
        self.mount_options_dialog = QDialog(self)
        dialog = self.mount_options_dialog
        dialog.setMinimumSize(500, 300)
        dialog.setWindowTitle("Mount Options")
        layout = QVBoxLayout(dialog)
        for option in Options.mount_options:
            btn_layout = QHBoxLayout()
            drive_btn = QPushButton(option['drive_name'])
            drive_btn.clicked.connect(lambda _, opt=option: self._edit_mount_option(opt, dialog))
            delete_btn = QPushButton("Delete")
            delete_btn.clicked.connect(lambda _, opt=option: self._delete_mount_option(opt, dialog))
            btn_layout.addWidget(drive_btn, 3)
            btn_layout.addWidget(delete_btn, 1)
            layout.addLayout(btn_layout)
        layout.addStretch(1)
        if Options.mount_options:
            mount_cb = QCheckBox("Mount drives at startup and unmount at shutdown")
            mount_cb.setStyleSheet("color: #6ffff5;")
            mount_cb.setChecked(Options.run_mount_command_on_launch)
            mount_cb.toggled.connect(self._toggle_auto_mount)
            layout.addWidget(mount_cb)
        if len(Options.mount_options) < 3:
            add_btn = QPushButton("New Mount Option")
            add_btn.clicked.connect(lambda: self._edit_mount_option({}, dialog))
            layout.addWidget(add_btn)
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(dialog.accept)
        layout.addWidget(close_btn)
        dialog.exec()
        self.show()

    def _toggle_auto_mount(self, checked):
        Options.run_mount_command_on_launch = checked
        Options.save_config()
        self.show_message('Success', 'Mount Options successfully saved!')

    def _edit_mount_option(self, option=None, parent_dialog=None):
        if parent_dialog:
            parent_dialog.close()
        dialog = QDialog(self)
        dialog.setMinimumSize(500, 300)
        dialog.setWindowTitle(f"Edit Mount Option: {option.get('drive_name','')}" if option and 'drive_name' in option else "New Mount Option")
        layout = QVBoxLayout(dialog)
        fields = {}
        field_labels = [('drive_name', "Drive Name:"), ('mount_command', "Mount Command:"), ('unmount_command', "Unmount Command:")]
        for field, label in field_labels:
            layout.addWidget(QLabel(label))
            value = option.get(field, "") if option else ""
            fields[field] = QLineEdit(value)
            layout.addWidget(fields[field])
        btn_layout = QHBoxLayout()
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(dialog.close)
        save_btn = QPushButton("Save")
        save_btn.clicked.connect(lambda: self._save_mount_option(fields, option, dialog))
        btn_layout.addWidget(close_btn)
        btn_layout.addWidget(save_btn)
        layout.addLayout(btn_layout)
        dialog.exec()
        self.manage_mount_options()

    def _delete_mount_option(self, option, parent_dialog=None):
        confirm = QMessageBox.question(self, "Confirm Deletion", f"Are you sure you want to delete '{option['drive_name']}'?", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if confirm == QMessageBox.StandardButton.Yes:
            Options.mount_options.remove(option)
            Options.save_config()
            self.show_message("Deleted", f"'{option['drive_name']}' successfully deleted!")
            if parent_dialog:
                parent_dialog.close()
                self.manage_mount_options()

    def _save_mount_option(self, fields, option, dialog):
        new_option = {field: fields[field].text().strip() for field in ['drive_name', 'mount_command', 'unmount_command']}
        field_labels = {'drive_name': 'Drive Name', 'mount_command': 'Mount Command', 'unmount_command': 'Unmount Command'}
        for field, label in field_labels.items():
            if not new_option[field]:
                self.show_message("Incomplete Fields", f"{label} is required.")
                return
        for field, label in field_labels.items():
            if any(existing[field].lower() == new_option[field].lower() and existing != option for existing in Options.mount_options):
                self.show_message(f'Duplicate {label}', f'{label} already exists. Please change your input.')
                return
        if option:
            index = Options.mount_options.index(option)
            Options.mount_options[index] = new_option
        else:
            Options.mount_options.append(new_option)
        Options.save_config()
        dialog.close()
        self.show_message('Success', 'Mount Options successfully saved!')

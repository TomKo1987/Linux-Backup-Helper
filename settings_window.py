from __future__ import annotations
from pathlib import Path
import json, os, re, tempfile
from functools import partial
from PyQt6.QtGui import QColor
from base_window import BaseWindow
from PyQt6.QtCore import Qt, QMutexLocker
from global_style import get_current_style
from options import Options, MAX_MOUNT_OPTIONS
from system_manager_options import SystemManagerOptions
from PyQt6.QtWidgets import (QCheckBox, QColorDialog, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QGridLayout,
                             QHBoxLayout, QInputDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMessageBox,
                             QPushButton, QSizePolicy, QTextEdit, QVBoxLayout, QWidget)

from logging_config import setup_logger
logger = setup_logger(__name__)


class SettingsWindow(BaseWindow):

    def __init__(self, parent=None) -> None:
        self._color_cache:       dict = {}
        self.mount_options_dialog = None
        super().__init__(parent, "settings")
        self._ensure_defaults()

    @staticmethod
    def _ensure_defaults() -> None:
        for attr, default in (
            ("headers",        lambda: getattr(Options, "header_order", []).copy() or ["Default"]),
            ("header_order",   list),
            ("header_colors",  dict),
            ("header_inactive",list),
            ("sublayout_names",dict),
            ("all_entries",    list),
            ("mount_options",  list),
            ("run_mount_command_on_launch", lambda: False),
        ):
            if not hasattr(Options, attr):
                setattr(Options, attr, default() if callable(default) else default)

    def show_message(self, title: str, message: str,
                     icon: QMessageBox.Icon = QMessageBox.Icon.Information) -> None:
        QMessageBox(icon, title, message, QMessageBox.StandardButton.Ok, self).exec()

    def _make_dialog(self, title: str, size: tuple = (800, 600)) -> tuple[QDialog, QVBoxLayout]:
        dlg = QDialog(self)
        dlg.setMinimumSize(*size)
        dlg.setWindowTitle(title)
        return dlg, QVBoxLayout(dlg)

    @staticmethod
    def _format_list(items: list, suffix: str) -> str:
        if not items:
            return suffix
        if len(items) == 1:
            return f"{items[0]}{suffix}"
        if len(items) == 2:
            return f"{items[0]} and {items[1]}{suffix}"
        return f"{', '.join(items[:-1])}, and {items[-1]}{suffix}"

    def get_checked_entries(self) -> list:
        return [
            (cb, src, dst, uid)
            for cb, src, dst, uid in getattr(self, "checkbox_dirs", [])
            if cb.isChecked()
        ]

    def system_manager_options(self) -> None:
        self.hide()
        SystemManagerOptions(self).exec()
        self.show()

    def entry_dialog(self, edit_mode: bool = False) -> None:
        checked = self.get_checked_entries()
        if edit_mode and not checked:
            self.show_message("Entry Editor Error",
                              "Nothing selected, or the selected items cannot be edited.")
            return

        self.hide()
        entries_to_process = checked if edit_mode else [None]

        for entry_data in entries_to_process:
            self._run_entry_dialog(entry_data, edit_mode)

        Options.save_config()
        try:
            self.settings_changed.emit()
        except Exception as exc:
            logger.error("entry_dialog: error emitting settings_changed: %s", exc)

        self.show()

    def _run_entry_dialog(self, entry_data, edit_mode: bool) -> None:
        if edit_mode and entry_data is None:
            return
        title_text  = entry_data[0].text() if edit_mode else ""
        unique_id   = entry_data[3]        if edit_mode else None
        entry_obj   = self._find_entry_by_id(unique_id) if edit_mode else None

        dialog = QDialog(self)
        dialog.setFixedSize(1300, 625)
        dialog.setWindowTitle("Edit Entry" if edit_mode else "Add New Entry")
        root = QVBoxLayout(dialog)
        root.setContentsMargins(2, 2, 2, 2)

        if edit_mode:
            desc = (
                f"\n'{title_text}'\n\nType '\\n' for a line-break in the title.\n\n"
                "For Samba shares use:\n'smb://ip/rest-of-path'"
            )
        else:
            desc = (
                "\nCreate a new entry.\n\nType '\\n' for a line-break in the title.\n\n"
                "For Samba shares use:\n'smb://ip/rest-of-path'"
            )
        desc_label = QLabel(desc)
        desc_label.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
        root.addWidget(desc_label)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        fh = 60

        header_combo = _styled_combo(Options.headers, fh)
        if entry_obj:
            idx = header_combo.findText(getattr(entry_obj, "header", ""))
            if idx >= 0:
                header_combo.setCurrentIndex(idx)
        form.addRow(QLabel("Header:"), header_combo)

        title_edit = QLineEdit(title_text)
        title_edit.setMaximumHeight(fh)
        form.addRow(QLabel("Title:"), title_edit)

        if edit_mode:
            sources, destinations = entry_data[1], entry_data[2]
            for field_label, data in (("Source", sources), ("Destination", destinations)):
                btn = QPushButton(f"Edit {field_label} Entries")
                btn.setMaximumHeight(fh)
                btn.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
                btn.clicked.connect(partial(self.open_text_editor, data, title_text,
                                            field_label.lower()))
                form.addRow(QLabel(f"{field_label}:"), btn)
        else:
            for field_label in ("Source", "Destination"):
                field_edit = QLineEdit()
                field_edit.setMaximumHeight(fh)
                field_edit.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
                browse_btn = QPushButton(f"Select {field_label}")
                browse_btn.setMaximumHeight(fh)
                browse_btn.setFixedWidth(300)
                browse_btn.clicked.connect(partial(self._browse_dir, field_edit))
                row_widget = QWidget()
                row_widget.setMaximumHeight(fh)
                hb = QHBoxLayout(row_widget)
                hb.setContentsMargins(2, 2, 2, 2)
                hb.setSpacing(2)
                hb.addWidget(field_edit)
                hb.addWidget(browse_btn)
                form.addRow(QLabel(f"{field_label}:"), row_widget)
                setattr(self, f"{field_label.lower()}_edit", field_edit)

        checkbox_texts = {
            "no_backup":  "No Backup",
            "no_restore": "No Restoring",
        }
        for i in range(1, 5):
            key  = f"sublayout_games_{i}"
            name = Options.sublayout_names.get(key, f"Sublayout Games {i}")
            checkbox_texts[key] = f"Add to Sublayout-Games {i}:\n'{name}'"

        detail_cbs: dict[str, QCheckBox] = {}
        for key, text in checkbox_texts.items():
            cb = QCheckBox(text)
            cb.setChecked(entry_obj.details.get(key, False) if entry_obj else False)
            cb.setStyleSheet(f"{get_current_style()} QCheckBox{{color:#6ffff5}}")
            cb.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
            cb.setMaximumHeight(fh)
            detail_cbs[key] = cb

        cb_grid = QGridLayout()
        positions = [
            ("no_backup",  0, 0), ("no_restore", 1, 0),
            ("sublayout_games_1", 0, 1), ("sublayout_games_2", 1, 1),
            ("sublayout_games_3", 0, 2), ("sublayout_games_4", 1, 2),
        ]
        for key, r, c in positions:
            cb_grid.addWidget(detail_cbs[key], r, c)

        form.addRow(QLabel(""), QLabel(""))
        form.addRow(cb_grid)
        root.addLayout(form)

        def _on_no_restore(state):
            disabled = state == Qt.CheckState.Checked.value
            for item in range(1, 5):
                k = f"sublayout_games_{item}"
                detail_cbs[k].setChecked(False)
                detail_cbs[k].setEnabled(not disabled)

        def _make_sublayout_handler(num):
            def _handler(state):
                if state == Qt.CheckState.Checked.value:
                    _block_set_cb(detail_cbs["no_restore"], False)
                    detail_cbs["no_restore"].setEnabled(False)
                    for j in range(1, 5):
                        if j != num:
                            _block_set_cb(detail_cbs[f"sublayout_games_{j}"], False)
                            detail_cbs[f"sublayout_games_{j}"].setEnabled(False)
                else:
                    detail_cbs["no_restore"].setEnabled(True)
                    for j in range(1, 5):
                        if j != num:
                            detail_cbs[f"sublayout_games_{j}"].setEnabled(True)
            return _handler

        detail_cbs["no_restore"].stateChanged.connect(_on_no_restore)
        for i in range(1, 5):
            detail_cbs[f"sublayout_games_{i}"].stateChanged.connect(_make_sublayout_handler(i))

        btn_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel # type: ignore[attr-defined]
        )
        _set_button_text(btn_box, QDialogButtonBox.StandardButton.Ok, "Save")
        _set_button_text(btn_box, QDialogButtonBox.StandardButton.Cancel, "Close")
        btn_box.setMaximumHeight(fh)
        root.addWidget(btn_box, alignment=Qt.AlignmentFlag.AlignRight)

        def _save():
            header = header_combo.currentText()
            title  = title_edit.text().strip()

            if edit_mode:
                source, destination = sources, destinations
            else:
                source      = self.source_edit.text().strip()      # type: ignore[attr-defined]
                destination = self.destination_edit.text().strip()  # type: ignore[attr-defined]

            src_ok  = bool(source)      if not isinstance(source, list)      else any(source)
            dst_ok  = bool(destination) if not isinstance(destination, list)  else any(destination)

            if not all((title, src_ok, dst_ok)):
                self.show_message("Error", "All fields must be filled in.")
                return

            existing = {
                e.title.lower() for e in getattr(Options, "all_entries", [])
                if hasattr(e, "title") and (not edit_mode or e.title.lower() != title_text.lower())
            }
            if title.lower() in existing:
                self.show_message("Duplicate Title",
                                  "An entry with this title already exists.")
                return

            if edit_mode and entry_obj:
                entry_obj.header = header
                entry_obj.title  = title
                new_entry = entry_obj
            else:
                if not hasattr(Options, "all_entries"):
                    Options.all_entries = []
                new_entry = Options(header, title, source, destination)
                Options.all_entries.append(new_entry)

            for key_, cb_ in detail_cbs.items():
                new_entry.details[key_] = cb_.isChecked()

            self.show_message(
                "Success",
                f"Entry '{new_entry.title}' successfully {'updated' if edit_mode else 'added'}!"
            )
            dialog.accept()

        btn_box.accepted.connect(_save)
        btn_box.rejected.connect(dialog.reject)
        dialog.exec()

    @staticmethod
    def _find_entry_by_id(unique_id):
        return next(
            (e for e in getattr(Options, "all_entries", [])
             if hasattr(e, "details") and e.details.get("unique_id") == unique_id),
            None,
        )

    def delete_entry(self) -> None:
        checked = self.get_checked_entries()
        if not checked:
            self.show_message("Delete Error", "Nothing selected.")
            return

        titles_quoted = [f"'{cb.text()}'" for cb, *_ in checked]
        confirm = QMessageBox(
            QMessageBox.Icon.Warning,
            "Confirm Deletion",
            f"Delete {self._format_list(titles_quoted, '?')}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            self,
        )
        confirm.setDefaultButton(QMessageBox.StandardButton.No)
        if confirm.exec() != QMessageBox.StandardButton.Yes:
            return

        self.hide()
        ids_to_delete = {entry_data[3] for entry_data in checked}

        with QMutexLocker(Options.entries_mutex):
            Options.all_entries = [
                e for e in Options.all_entries
                if not (hasattr(e, "details") and
                        isinstance(e.details, dict) and
                        e.details.get("unique_id") in ids_to_delete)
            ]

        Options.save_config()
        try:
            self.settings_changed.emit()
        except Exception as exc:
            logger.error("delete_entry: error emitting settings_changed: %s", exc)

        self.show_message("Success", self._format_list(titles_quoted, " successfully deleted!"))
        self.show()

    def _browse_dir(self, line_edit: QLineEdit) -> None:
        path = QFileDialog.getExistingDirectory(self, "Select Directory")
        if path:
            line_edit.setText(path)

    def open_text_editor(self, entries_list, title: str, field: str) -> None:
        dlg = QDialog(self)
        dlg.setMinimumSize(1200, 1000)
        dlg.setWindowTitle(f"Edit {field.capitalize()} Entries for '{title}'")
        layout = QVBoxLayout(dlg)

        text_edit = QTextEdit()
        text_edit.setPlainText(
            "\n".join(map(str, entries_list)) if isinstance(entries_list, list)
            else str(entries_list or "")
        )
        layout.addWidget(text_edit)

        btn_row = QHBoxLayout()
        for label, fn in (
            ("Back",         dlg.reject),
            ("File Browser", lambda: self._browse_files(text_edit)),
            ("Save",         lambda: self._save_from_editor(text_edit, entries_list, field)),
        ):
            btn = QPushButton(label)
            btn.clicked.connect(fn)
            btn_row.addWidget(btn)

        layout.addLayout(btn_row)
        dlg.exec()

    def _browse_files(self, text_edit: QTextEdit) -> None:
        files, _ = QFileDialog.getOpenFileNames(self, "Select Files")
        if files:
            current = text_edit.toPlainText()
            text_edit.setPlainText(
                current + ("\n" if current else "") + "\n".join(files)
            )

    def _save_from_editor(self, text_edit: QTextEdit, entries_list, field: str) -> None:
        new_entries = text_edit.toPlainText().splitlines()
        if not isinstance(entries_list, list):
            return
        entries_list.clear()
        entries_list.extend(new_entries)
        Options.save_config()
        try:
            self.settings_changed.emit()
        except Exception as exc:
            logger.error("_save_from_editor: error emitting settings_changed: %s", exc)
        self.show_message("Success", f"{field.capitalize()} entries saved!")
        text_edit.setPlainText("\n".join(entries_list))

    select_directory = _browse_dir

    def header_settings(self) -> None:
        self._ensure_defaults()
        self.hide()

        dlg, layout = self._make_dialog("Header Settings", size=(1100, 1000))

        list_widget = QListWidget(dlg)
        list_widget.setSelectionMode(QListWidget.SelectionMode.SingleSelection)
        list_widget.setDragDropMode(QListWidget.DragDropMode.InternalMove)
        list_widget.setDragEnabled(True)
        list_widget.setAcceptDrops(True)
        list_widget.setDefaultDropAction(Qt.DropAction.MoveAction)

        for header in list(Options.header_order):
            item_widget = self._make_header_list_item(header, list_widget)
            item = QListWidgetItem()
            item.setSizeHint(item_widget.sizeHint())
            list_widget.addItem(item)
            list_widget.setItemWidget(item, item_widget)

        layout.addWidget(list_widget)
        layout.addWidget(QLabel(
            "Click and hold a header to drag it into a new position.\n"
            "Creating a header named 'Games' enables game sublayouts."
        ))

        new_header_btn = QPushButton("New Header")
        new_header_btn.clicked.connect(partial(self._add_new_header, list_widget))
        layout.addWidget(new_header_btn)

        for row_start in (0, 2):
            hb = QHBoxLayout()
            for i in (row_start + 1, row_start + 2):
                key  = f"sublayout_games_{i}"
                name = Options.sublayout_names.get(key, f"Sublayout Games {i}")
                btn  = QPushButton(f"Sublayout-Games {i}:\n{name}")
                btn.setFixedHeight(60)
                btn.clicked.connect(partial(self._rename_sublayout, dlg, i))
                hb.addWidget(btn)
            layout.addLayout(hb)

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel # type: ignore[attr-defined]
        )
        _set_button_text(btns, QDialogButtonBox.StandardButton.Ok, "Save")
        _set_button_text(btns, QDialogButtonBox.StandardButton.Cancel, "Close")
        btns.accepted.connect(lambda: (self._save_header_options(list_widget), dlg.accept()))
        btns.rejected.connect(dlg.reject)
        layout.addWidget(btns)

        dlg.exec()
        self.show()

    def _make_header_list_item(self, header: str, list_widget: QListWidget) -> QWidget:
        widget = QWidget()
        layout = QHBoxLayout(widget)

        color  = Options.header_colors.get(header, "#ffffff")
        darker = self._darkened(color)
        btn_style = "color:black;font-weight:bold;font-size:17px;"

        color_btn = QPushButton(header)
        color_btn.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        color_btn.setFixedHeight(26)
        color_btn.setStyleSheet(f"QPushButton{{{btn_style}background-color:{darker};}}")
        color_btn.clicked.connect(partial(self._pick_header_color, header))

        inactive_cb = QCheckBox("Inactive")
        inactive_cb.setObjectName("inactive_checkbox")
        inactive_cb.setStyleSheet("margin-left:10px;")
        inactive_cb.setChecked(header in Options.header_inactive)

        def _on_inactive_toggled(checked):
            bg = "gray" if checked else darker
            color_btn.setEnabled(not checked)
            color_btn.setStyleSheet(f"QPushButton{{{btn_style}background-color:{bg};padding:0 10px;}}")

        inactive_cb.stateChanged.connect(_on_inactive_toggled)
        _on_inactive_toggled(inactive_cb.isChecked())

        del_btn = QPushButton("Delete Header")
        del_btn.setStyleSheet("margin-left:10px;")
        del_btn.clicked.connect(partial(self._delete_header, header, list_widget))

        layout.addWidget(color_btn)
        layout.addWidget(inactive_cb)
        layout.addWidget(del_btn)
        return widget

    def _add_new_header(self, list_widget: QListWidget) -> None:
        dlg, layout = self._make_dialog("New Header", size=(600, 200))
        layout.addWidget(QLabel("Enter new header name:"))
        field = QLineEdit()
        field.setMinimumWidth(450)
        layout.addWidget(field)
        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel # type: ignore[attr-defined]
        )
        _set_button_text(btns, QDialogButtonBox.StandardButton.Ok, "Save")
        _set_button_text(btns, QDialogButtonBox.StandardButton.Cancel, "Close")
        btns.accepted.connect(lambda: self._confirm_new_header(field, dlg, list_widget))
        btns.rejected.connect(dlg.reject)
        layout.addWidget(btns)
        dlg.exec()

    def _confirm_new_header(self, field: QLineEdit, dlg: QDialog, list_widget: QListWidget) -> None:
        name = field.text().strip()
        if not name:
            return
        if name in Options.header_colors:
            self.show_message("Duplicate", "A header with that name already exists.")
            return
        dlg.accept()

        color_dlg = QColorDialog(self)
        if color_dlg.exec() != QColorDialog.DialogCode.Accepted:
            return

        Options.header_colors[name] = color_dlg.currentColor().name()
        Options.header_order.append(name)

        item_widget = self._make_header_list_item(name, list_widget)
        item = QListWidgetItem()
        item.setSizeHint(item_widget.sizeHint())
        list_widget.addItem(item)
        list_widget.setItemWidget(item, item_widget)

        Options.save_config()
        try:
            self.settings_changed.emit()
        except Exception as exc:
            logger.error("_confirm_new_header: %s", exc)

        self.show_message("Success", "Header created!")

    def _rename_sublayout(self, parent: QDialog, idx: int) -> None:
        name, ok = QInputDialog.getText(parent, "Sublayout Name", "Enter name:")
        if ok and name:
            key = f"sublayout_games_{idx}"
            Options.sublayout_names[key] = name
            for btn in parent.findChildren(QPushButton):
                if isinstance(btn, QPushButton):
                    if btn.text().startswith(f"Sublayout-Games {idx}:"):
                        btn.setText(f"Sublayout-Games {idx}:\n{name}")
                    break
            Options.save_config()
            try:
                self.settings_changed.emit()
            except Exception as exc:
                logger.error("_rename_sublayout: %s", exc)
            self.show_message("Success", "Sublayout name saved!")

    def _pick_header_color(self, header: str) -> None:
        dlg = QColorDialog(self)
        dlg.setCurrentColor(QColor(Options.header_colors.get(header, "#ffffff")))
        if dlg.exec() == QColorDialog.DialogCode.Accepted:
            Options.header_colors[header] = dlg.currentColor().name()
            self._refresh_header_button_color(header)
            Options.save_config()
            try:
                self.settings_changed.emit()
            except Exception as exc:
                logger.error("_pick_header_color: %s", exc)
            self.show_message("Success", "Header color saved!")

    def choose_color(self, header: str) -> None:
        self._pick_header_color(header)

    def _refresh_header_button_color(self, header: str) -> None:
        for btn in self.findChildren(QPushButton):
            if isinstance(btn, QPushButton) and btn.text() == header:
                color = Options.header_colors.get(header, "#ffffff")
                btn.setStyleSheet(
                    f"color:black;font-weight:bold;font-size:20px;"
                    f"background-color:{self._darkened(color)};"
                )

    def _delete_header(self, header: str, list_widget: QListWidget) -> None:
        if any(getattr(e, "header", None) == header for e in getattr(Options, "all_entries", [])):
            self.show_message(
                "Cannot Delete",
                "This header has associated entries. Remove them first.",
                QMessageBox.Icon.Information,
            )
            return

        confirm = QMessageBox(
            QMessageBox.Icon.Question, "Confirm Deletion",
            f"Delete header '{header}'?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, self,
        )
        confirm.setDefaultButton(QMessageBox.StandardButton.No)
        if confirm.exec() != QMessageBox.StandardButton.Yes:
            return

        Options.header_colors.pop(header, None)
        if header in Options.header_order:   Options.header_order.remove(header)
        if header in Options.header_inactive: Options.header_inactive.remove(header)

        for i in range(list_widget.count()):
            item_widget = list_widget.itemWidget(list_widget.item(i))
            if item_widget:
                btn = item_widget.findChild(QPushButton)
                if btn and isinstance(btn, QPushButton) and btn.text() == header:
                    list_widget.takeItem(i)
                    break

        Options.save_config()
        try:
            self.settings_changed.emit()
        except Exception as exc:
            logger.error("_delete_header: %s", exc)

        self.show_message("Success", f"Header '{header}' deleted!")

    def _save_header_options(self, list_widget: QListWidget) -> None:
        new_order = []
        new_inactive = []
        for i in range(list_widget.count()):
            item_widget = list_widget.itemWidget(list_widget.item(i))
            if not item_widget:
                continue
            name = ""
            btn = item_widget.findChild(QPushButton)

            if btn and isinstance(btn, QPushButton):
                name = btn.text().strip()
            if not name:
                continue

            new_order.append(name)

            cb = item_widget.findChild(QCheckBox, "inactive_checkbox")
            if cb and isinstance(cb, QCheckBox) and cb.isChecked():
                new_inactive.append(name)

        Options.header_order   = new_order
        Options.header_inactive = new_inactive

        for entry in getattr(Options, "all_entries", []):
            try:
                if hasattr(entry, "details") and isinstance(entry.details, dict):
                    h = getattr(entry, "header", entry.details.get("header", ""))
                    entry.details["inactive"] = h in new_inactive
            except (AttributeError, TypeError) as exc:
                logger.warning("_save_header_options: %s", exc)

        Options.save_config()
        try:
            self.settings_changed.emit()
        except Exception as exc:
            logger.error("_save_header_options: %s", exc)

        self.show_message("Success", "Header settings saved!")

    def open_samba_password_dialog(self) -> None:
        self.hide()
        from samba_password import SambaPasswordDialog
        SambaPasswordDialog(self).exec()
        self.show()

    def manage_mount_options(self) -> None:
        self._ensure_defaults()

        if self.mount_options_dialog:
            try:
                if not self.mount_options_dialog.isHidden():
                    self.mount_options_dialog.close()
            except (RuntimeError, AttributeError):
                pass
            self.mount_options_dialog = None

        self.hide()

        dlg = QDialog(self)
        self.mount_options_dialog = dlg
        dlg.finished.connect(lambda: setattr(self, "mount_options_dialog", None))
        dlg.setMinimumSize(825, 450)
        dlg.setWindowTitle("Mount Options")

        layout = QVBoxLayout(dlg)

        for opt in Options.mount_options:
            row = QHBoxLayout()
            drive_btn = QPushButton(opt.get("drive_name", "Unknown"))
            drive_btn.clicked.connect(partial(self._edit_mount_option, opt, dlg))
            del_btn = QPushButton("Delete")
            del_btn.clicked.connect(partial(self._delete_mount_option, opt, dlg))
            row.addWidget(drive_btn, 3)
            row.addWidget(del_btn,   1)
            layout.addLayout(row)

        layout.addStretch(1)

        if Options.mount_options:
            auto_cb = QCheckBox("Mount drives at startup and unmount at shutdown")
            auto_cb.setStyleSheet("color:#6ffff5;")
            auto_cb.setChecked(Options.run_mount_command_on_launch)
            auto_cb.toggled.connect(self._toggle_auto_mount)
            layout.addWidget(auto_cb)

        if len(Options.mount_options) < MAX_MOUNT_OPTIONS:
            add_btn = QPushButton("New Mount Option")
            add_btn.clicked.connect(partial(self._edit_mount_option, {}, dlg))
            layout.addWidget(add_btn)

        close_btn = QPushButton("Close")
        close_btn.clicked.connect(dlg.accept)
        layout.addWidget(close_btn)

        try:
            dlg.exec()
        except Exception as exc:
            logger.error("manage_mount_options: %s", exc)
        finally:
            self.mount_options_dialog = None
            self.show()

    def _toggle_auto_mount(self, checked: bool) -> None:
        Options.run_mount_command_on_launch = checked
        Options.save_config()
        self.show_message("Success", "Auto-mount setting saved!")

    def _edit_mount_option(self, option: dict, parent_dlg: QDialog | None) -> None:
        option = option or {}
        is_new = not option.get("drive_name")

        title = (
            "New Mount Option" if is_new
            else f"Edit Mount Option: {option.get('drive_name', '')}"
        )
        dlg = QDialog(self)
        dlg.setMinimumSize(825, 450)
        dlg.setWindowTitle(title)
        layout = QVBoxLayout(dlg)

        fields: dict[str, QLineEdit] = {}
        for key, label in (
            ("drive_name",       "Drive Name:"),
            ("mount_command",    "Mount Command:"),
            ("unmount_command",  "Unmount Command:"),
        ):
            layout.addWidget(QLabel(label))
            edit = QLineEdit(option.get(key, ""))
            fields[key] = edit
            layout.addWidget(edit)

        btn_row = QHBoxLayout()
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(dlg.reject)
        save_btn = QPushButton("Save")
        save_btn.clicked.connect(partial(self._save_mount_option, fields, option, dlg))
        btn_row.addWidget(close_btn)
        btn_row.addWidget(save_btn)
        layout.addLayout(btn_row)

        if parent_dlg:
            try:
                parent_dlg.accept()
            except (RuntimeError, AttributeError) as exc:
                logger.debug("_edit_mount_option: closing parent: %s", exc)

        result = dlg.exec()
        if result == QDialog.DialogCode.Accepted:
            self.manage_mount_options()

    def _delete_mount_option(self, option: dict, parent_dlg: QDialog | None) -> None:
        name = option.get("drive_name", "Unknown")
        confirm = QMessageBox.question(
            self, "Confirm Deletion", f"Delete '{name}'?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if confirm == QMessageBox.StandardButton.Yes:
            if option in Options.mount_options:
                Options.mount_options.remove(option)
                Options.save_config()
                self.show_message("Deleted", f"'{name}' deleted!")
            if parent_dlg:
                try:
                    parent_dlg.accept()
                except (RuntimeError, AttributeError):
                    pass
            self.manage_mount_options()

    def _save_mount_option(self, fields: dict, original: dict, dlg: QDialog) -> None:
        new_opt = {k: fields[k].text().strip() for k in ("drive_name", "mount_command", "unmount_command")}
        required = {"drive_name": "Drive Name", "mount_command": "Mount Command"}
        for key, label in required.items():
            if not new_opt[key]:
                self.show_message("Incomplete Fields", f"{label} is required.")
                return
        for key, label in required.items():
            if any(
                existing[key].lower() == new_opt[key].lower() and existing is not original
                for existing in Options.mount_options
            ):
                self.show_message(f"Duplicate {label}", f"{label} already exists.")
                return

        if original and original in Options.mount_options:
            idx = Options.mount_options.index(original)
            Options.mount_options[idx] = new_opt
        else:
            Options.mount_options.append(new_opt)

        Options.save_config()
        self.show_message("Success", "Mount option saved!")
        dlg.accept()

    def open_profile_manager(self) -> None:
        self.hide()
        dlg = QDialog(self)
        dlg.setWindowTitle("Profile Manager")
        dlg.setMinimumSize(650, 520)
        layout = QVBoxLayout(dlg)

        info = QLabel()
        info.setTextFormat(Qt.TextFormat.RichText)
        info.setWordWrap(True)
        layout.addWidget(info)

        profile_list = QListWidget()
        layout.addWidget(profile_list)

        row1 = QHBoxLayout()
        save_btn   = QPushButton("💾  Save as Profile")
        load_btn   = QPushButton("✅  Load Selected")
        delete_btn = QPushButton("🗑  Delete Selected")
        for btn in (save_btn, load_btn, delete_btn):
            row1.addWidget(btn)
        layout.addLayout(row1)

        row2 = QHBoxLayout()
        new_btn     = QPushButton("✨  New Empty Profile")
        default_btn = QPushButton("⭐  Set as Default")
        new_btn.setToolTip(
            "Create a blank profile with default settings.\n"
            "Use 'Load Selected' to activate it."
        )
        default_btn.setToolTip(
            "Mark the selected profile as the default.\n"
            "It will be loaded automatically on next launch."
        )
        row2.addWidget(new_btn)
        row2.addWidget(default_btn)
        layout.addLayout(row2)

        row3 = QHBoxLayout()
        export_btn = QPushButton("📤  Export All (ZIP)")
        import_btn = QPushButton("📥  Import (ZIP or JSON)")
        row3.addWidget(export_btn)
        row3.addWidget(import_btn)
        layout.addLayout(row3)

        close_btn = QPushButton("Close")
        layout.addWidget(close_btn)

        def _refresh():
            active  = Options.get_active_profile()
            default = Options.get_default_profile()
            info.setText(
                f"Active profile: <b>{active}</b><br>"
                f"Default on startup: <b>{default}</b><br>"
                "Profiles are stored in <i>~/.config/Backup Helper/profiles/</i>.<br>"
                "'Export' creates a ZIP of <b>all</b> profiles; "
                "'Import' restores from a ZIP or single .json."
            )
            _refresh_list(profile_list, active, default)

        def _selected_name() -> str | None:
            item = profile_list.currentItem()
            if not item:
                return None
            text = item.text()
            for marker in (" ✔ ⭐", " ⭐ ✔", " ✔", " ⭐"):
                text = text.replace(marker, "")
            return text.strip() or None

        _refresh()

        def _save():
            name, ok = QInputDialog.getText(dlg, "          Save Profile          ", "          Profile name:          ")
            name = name.strip()
            if not (ok and name):
                return
            if name in Options.list_profiles():
                if not _confirm_overwrite(dlg, name):
                    return
            if Options.save_profile(name):
                self.show_message("Saved", f"Profile '{name}' saved.")
                _refresh()
            else:
                _error(dlg, "Save Failed",
                       "Name may only contain letters, digits, spaces, hyphens, dots or underscores.")

        def _load():
            name = _selected_name()
            if not name:
                self.show_message("No Selection", "Select a profile to load.")
                return
            c = QMessageBox(QMessageBox.Icon.Question, "Load Profile",
                            f"Load '{name}'? This replaces the current configuration.",
                            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, dlg)
            c.setDefaultButton(QMessageBox.StandardButton.No)
            if c.exec() != QMessageBox.StandardButton.Yes:
                return
            if Options.load_profile(name):
                self.show_message("Loaded", f"Now using profile '{name}'.")
                _refresh()
                try:
                    self.settings_changed.emit()
                except Exception as exc:
                    logger.error("open_profile_manager/_load: %s", exc)
            else:
                _error(dlg, "Load Failed", f"Could not load '{name}'.")

        def _delete():
            name = _selected_name()
            if not name:
                self.show_message("No Selection", "Select a profile to delete.")
                return
            if name == Options.get_active_profile():
                self.show_message("Cannot Delete",
                                  "The active profile cannot be deleted.\nLoad a different profile first.")
                return
            c = QMessageBox(QMessageBox.Icon.Warning, "Delete Profile",
                            f"Delete '{name}'? This cannot be undone.",
                            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, dlg)
            c.setDefaultButton(QMessageBox.StandardButton.No)
            if c.exec() != QMessageBox.StandardButton.Yes:
                return
            if Options.delete_profile(name):
                self.show_message("Deleted", f"Profile '{name}' deleted.")
                _refresh()
            else:
                _error(dlg, "Delete Failed", f"Could not delete '{name}'.")

        def _new_empty():
            name, ok = QInputDialog.getText(dlg, "          New Empty Profile          ", "          Profile name:          ")
            name = name.strip()
            if not (ok and name):
                return
            if not re.match(r"^[\w\-. ]+$", name):
                _error(dlg, "Invalid Name",
                       "Name may only contain letters, digits, spaces, hyphens, dots or underscores.")
                return
            if name in Options.list_profiles() and not _confirm_overwrite(dlg, name):
                return
            _create_empty_profile(name, dlg)
            _refresh()

        def _set_default():
            name = _selected_name()
            if not name:
                self.show_message("No Selection", "Select a profile to set as default.")
                return
            if Options.set_default_profile(name):
                self.show_message("Default Set", f"'{name}' will be loaded on startup.")
                _refresh()
            else:
                _error(dlg, "Failed", f"Could not set '{name}' as default.")

        def _export():
            dest, _ = QFileDialog.getSaveFileName(
                dlg, "Export All Profiles",
                str(Options.profiles_dir.parent / "profiles_backup.zip"),
                "ZIP archives (*.zip);;All files (*)",
            )
            if not dest:
                return
            if Options.export_all_profiles(dest):
                self.show_message("Exported", f"Profiles exported to:\n{dest}")
            else:
                _error(dlg, "Export Failed", "No profiles found or could not write ZIP.")

        def _import():
            src, _ = QFileDialog.getOpenFileName(
                dlg, "Import Profiles",
                str(Options.profiles_dir.parent),
                "ZIP / JSON (*.zip *.json);;All files (*)",
            )
            if not src:
                return
            if src.lower().endswith(".zip"):
                overwrite = (
                    QMessageBox(QMessageBox.Icon.Question, "Overwrite Existing?",
                                "Overwrite profiles with the same name?",
                                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, dlg
                                ).exec() == QMessageBox.StandardButton.Yes
                )
                imported, skipped = Options.import_profiles_from_zip(src, overwrite=overwrite)
                msg = f"Imported: {', '.join(imported) or 'none'}"
                if skipped:
                    msg += f"\nSkipped: {', '.join(skipped)}"
                self.show_message("Import Complete", msg)
            else:
                name, ok = QInputDialog.getText(
                    dlg, "Profile Name", "Name for the imported profile:", text=Path(src).stem
                )
                name = name.strip()
                if not (ok and name):
                    return
                if name in Options.list_profiles() and not _confirm_overwrite(dlg, name):
                    return
                if Options.import_single_profile(src, name):
                    self.show_message("Imported", f"Profile '{name}' imported.")
                else:
                    _error(dlg, "Import Failed", "The file is not a valid profile JSON.")
            _refresh()

        save_btn.clicked.connect(_save)
        load_btn.clicked.connect(_load)
        delete_btn.clicked.connect(_delete)
        new_btn.clicked.connect(_new_empty)
        default_btn.clicked.connect(_set_default)
        export_btn.clicked.connect(_export)
        import_btn.clicked.connect(_import)
        close_btn.clicked.connect(dlg.accept)

        dlg.exec()
        self.show()

    def _darkened(self, color_str: str) -> str:
        if color_str not in self._color_cache:
            self._color_cache[color_str] = SettingsWindow.darken_header_color(color_str)
        return self._color_cache[color_str]

    @staticmethod
    def darken_header_color(color_str: str) -> str:
        color = QColor(color_str)
        h, s, v, a = color.getHsv()
        v = max(0, v - 120)
        return QColor.fromHsv(h, s, v, a).name()


def _styled_combo(items: list, max_height: int = 60):
    from PyQt6.QtWidgets import QComboBox
    cb = QComboBox()
    cb.setStyleSheet("color:#ffffff;background-color:#555582;padding:5px;")
    cb.addItems(items)
    cb.setMaximumHeight(max_height)
    return cb


def _confirm_overwrite(parent, name: str) -> bool:
    c = QMessageBox(QMessageBox.Icon.Question, "Overwrite?",
                    f"'{name}' exists. Overwrite?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, parent)
    c.setDefaultButton(QMessageBox.StandardButton.No)
    return c.exec() == QMessageBox.StandardButton.Yes


def _error(parent, title: str, message: str) -> None:
    QMessageBox(QMessageBox.Icon.Critical, title, message,
                QMessageBox.StandardButton.Ok, parent).exec()


def _refresh_list(list_widget: QListWidget, active: str, default: str) -> None:
    list_widget.clear()
    for name in Options.list_profiles():
        markers = ""
        if name == active:  markers += " ✔"
        if name == default: markers += " ⭐"
        list_widget.addItem(f"{name}{markers}")


def _create_empty_profile(name: str, parent) -> None:
    default = {
        "is_default": False,
        "mount_options": [],
        "run_mount_command_on_launch": False,
        "header": {},
        "sublayout_names": {f"sublayout_games_{i}": "" for i in range(1, 5)},
        "system_manager_operations": [],
        "system_files": [],
        "basic_packages": [],
        "aur_packages": [],
        "specific_packages": [],
        "ui_settings": {
            "backup_window_columns":   2,
            "restore_window_columns":  2,
            "settings_window_columns": 2,
            "theme":       Options.ui_settings.get("theme", "Tokyo Night"),
            "font_family": Options.ui_settings.get("font_family", "DejaVu Sans"),
            "font_size":   Options.ui_settings.get("font_size", 14),
        },
        "user_shell": "Bash",
        "entries": [],
    }
    try:
        Options.profiles_dir.mkdir(parents=True, exist_ok=True)
        target = Options.profiles_dir / f"{name}.json"
        with tempfile.NamedTemporaryFile(
            dir=Options.profiles_dir, delete=False, mode="w", encoding="utf-8"
        ) as tmp:
            tmp_path = tmp.name
            json.dump(default, tmp, indent=4, ensure_ascii=False)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.replace(tmp_path, target)
    except Exception as exc:
        logger.error("_create_empty_profile: %s", exc)
        _error(parent, "Error", f"Could not create profile:\n{exc}")


def _block_set_cb(cb: QCheckBox, checked: bool) -> None:
    cb.blockSignals(True)
    cb.setChecked(checked)
    cb.blockSignals(False)


def _set_button_text(
    btn_box: "QDialogButtonBox",
    role: "QDialogButtonBox.StandardButton",
    text: str,
) -> None:
    btn = btn_box.button(role)
    if btn is not None:
        btn.setText(text)

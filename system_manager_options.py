from __future__ import annotations
from pathlib import Path
from PyQt6.QtCore import Qt, QTimer
from global_style import get_current_style
from linux_distro_helper import LinuxDistroHelper
from options import Options, SESSIONS, USER_SHELL
from PyQt6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
                             QGridLayout, QHBoxLayout, QInputDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem,
                             QMessageBox, QPushButton, QScrollArea, QSizePolicy, QTextEdit, QVBoxLayout, QWidget)

from logging_config import setup_logger
logger = setup_logger(__name__)


# noinspection PyUnresolvedReferences
class SystemManagerOptions(QDialog):
    DIALOG_WIDTH  = 1500
    DIALOG_HEIGHT = 750

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("System Manager Options")

        self.distro_helper          = LinuxDistroHelper()
        self.distro_name            = self.distro_helper.distro_pretty_name
        self.session                = self.distro_helper.detect_session()
        self.install_package_command = self.distro_helper.get_pkg_install_cmd("")

        self._active_dialogs: list[QDialog] = []
        self._last_shell:     str | None    = None

        self.top_label:    QLabel    | None = None
        self.distro_label: QLabel    | None = None
        self.shell_combo:  QComboBox | None = None
        self.close_button: QPushButton | None = None

        self.system_manager_operations_widgets: list[tuple[QCheckBox, str]] = []
        self.system_files_widgets:  list[QListWidget] = []
        self.original_system_files: list              = []
        self.basic_packages_widgets:    list[QListWidget] = []
        self.aur_packages_widgets:      list[QListWidget] = []
        self.specific_packages_widgets: list[QListWidget] = []
        self.current_option_type: str | None = None

        self._setup_ui()

    def track_dialog(self, dialog: QDialog):
        self._active_dialogs.append(dialog)
        dialog.finished.connect(lambda: self._remove_dialog(dialog))

    def _remove_dialog(self, dialog: QDialog):
        if dialog in self._active_dialogs:
            self._active_dialogs.remove(dialog)

    def close_all_dialogs(self):
        for d in self._active_dialogs[:]:
            if d and d.isVisible():
                d.close()
        self._active_dialogs.clear()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        self._add_distro_info(layout)

        self.top_label = QLabel(self._build_top_label_text())
        self.top_label.setWordWrap(True)
        self.top_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.top_label)
        layout.addWidget(scroll)

        self._add_button_rows(layout)
        self._add_shell_selection(layout)

        self.close_button = QPushButton("Close")
        layout.addWidget(self.close_button)

        self._connect_signals()
        self.setStyleSheet(get_current_style())
        self.setMinimumSize(self.DIALOG_WIDTH, self.DIALOG_HEIGHT)

    def _build_top_label_text(self) -> str:
        cmd = self.install_package_command
        return (
            f"First you can select 'System Files' in System Manager. These files will be copied using 'sudo', "
            f"for root privilege. If you have 'System Files' selected, System Manager will copy these first. "
            f"This allows you to copy files such as 'pacman.conf' or 'smb.conf' to '/etc/'.\n\n"
            f"Under 'System Manager Operations' you can specify how you would like to proceed. "
            f"Each action is executed one after the other.\nUncheck actions to disable them.\n\n"
            f"Tips:\n\n"
            f"'Basic Packages' will be installed using '{cmd}PACKAGE'.\n\n"
            f"'AUR Packages' provides access to the Arch User Repository. "
            f"Therefore 'yay' must and will be installed. This feature is available only on "
            f"Arch Linux based distributions.\n\n"
            f"You can also define 'Specific Packages'. These packages will be installed only "
            f"(using '{cmd}PACKAGE') if the corresponding session has been recognized.\n"
            f"Both full desktop environments and window managers such as 'Hyprland' and others are supported."
        )

    def _add_distro_info(self, layout: QVBoxLayout):
        yay_info = ""
        if self.distro_helper.has_aur:
            yay_info = (
                " | AUR Helper: 'yay' detected"
                if self.distro_helper.package_is_installed("yay")
                else " | AUR Helper: 'yay' not detected"
            )
        self.distro_label = QLabel(
            f"Recognized Linux distribution: {self.distro_name} | "
            f"Session: {self.session}{yay_info}"
        )
        self.distro_label.setStyleSheet("color:lightgreen")
        layout.addWidget(self.distro_label)

    def _add_button_rows(self, layout: QVBoxLayout):
        self.ops_button        = QPushButton("System Manager Operations")
        self.sys_files_button  = QPushButton("System Files")
        self.pkg_buttons = {
            "basic_packages":    QPushButton("Basic Packages"),
            "aur_packages":      QPushButton("AUR Packages"),
            "specific_packages": QPushButton("Specific Packages"),
        }
        row1 = QHBoxLayout()
        row1.addWidget(self.ops_button)
        row1.addWidget(self.sys_files_button)
        layout.addLayout(row1)

        row2 = QHBoxLayout()
        for btn in self.pkg_buttons.values():
            row2.addWidget(btn)
        layout.addLayout(row2)

    def _add_shell_selection(self, layout: QVBoxLayout):
        self.shell_combo = QComboBox()
        self.shell_combo.addItems(USER_SHELL)
        idx = USER_SHELL.index(Options.user_shell) if Options.user_shell in USER_SHELL else 0
        self.shell_combo.setCurrentIndex(idx)
        self._last_shell = USER_SHELL[idx]

        style = (
            "color:#a9b1d6;font-size:16px;font-weight:500;padding:4px 8px;"
            "background-color:#1a1b26;border-radius:6px;border:1px solid #7aa2f7;"
        )
        lbl = QLabel("Select User Shell:")
        lbl.setStyleSheet(style)
        self.shell_combo.setStyleSheet(style)
        row = QHBoxLayout()
        row.addWidget(lbl)
        row.addWidget(self.shell_combo)
        layout.addLayout(row)

    def _connect_signals(self):
        self.ops_button.clicked.connect(self._edit_operations)
        self.sys_files_button.clicked.connect(self._edit_system_files)
        self.close_button.clicked.connect(self._go_back)
        self.shell_combo.currentIndexChanged.connect(self._on_shell_changed)
        for opt_type, btn in self.pkg_buttons.items():
            btn.clicked.connect(lambda _, ot=opt_type: self._edit_packages(ot))

    def _on_shell_changed(self):
        sel = self.shell_combo.currentText()
        if sel != self._last_shell and sel in USER_SHELL:
            Options.user_shell = sel
            Options.save_config()
            self._last_shell = sel
            QMessageBox.information(self, "User Shell Changed",
                                    f"User Shell has been set to: {sel}")

    def _edit_operations(self):
        self.current_option_type = "system_manager_operations"
        Options.load_config(Options.config_file_path)

        content = QWidget()
        grid    = QGridLayout(content)

        select_all = self._make_select_all_checkbox("Check/Uncheck All")
        grid.addWidget(select_all, 0, 1)
        self._build_operation_checkboxes(grid)
        self._wire_operation_checkbox_logic(select_all)
        self._show_operations_dialog(content)

    def _build_operation_checkboxes(self, grid: QGridLayout):
        arch_only = {"update_mirrors", "install_yay", "install_aur_packages"}
        op_text   = Options.get_system_manager_operation_text(self.distro_helper)
        ops       = [(text.replace("<br>", "\n"), key) for key, text in op_text.items()]

        self.system_manager_operations_widgets = []
        for i, (label, key) in enumerate(ops):
            cb = QCheckBox(label)
            if key in arch_only and not self.distro_helper.supports_aur():
                cb.setChecked(False)
                cb.setEnabled(False)
                cb.setStyleSheet(f"{get_current_style()} QCheckBox{{color:#666;}}")
                tooltips = {
                    "update_mirrors":       "Mirror update is available on Arch Linux only",
                    "install_yay":          "Available only on Arch Linux based distributions",
                    "install_aur_packages": "Available only on Arch Linux based distributions",
                }
                if key in tooltips:
                    cb.setToolTip(tooltips[key])
            else:
                cb.setChecked(key in Options.system_manager_operations)
                cb.setStyleSheet(f"{get_current_style()} QCheckBox{{color:#c8beff;}}")
            grid.addWidget(cb, i + 1, 0)
            self.system_manager_operations_widgets.append((cb, key))

    def _wire_operation_checkbox_logic(self, select_all: QCheckBox):
        yay_cb  = self._checkbox_by_key("install_yay")
        aur_cb  = self._checkbox_by_key("install_aur_packages")

        def update_select_all():
            enabled = [_cb for _cb, _key in self.system_manager_operations_widgets
                       if _cb.isEnabled()
                       and not (_key == "install_yay" and aur_cb and aur_cb.isChecked())]
            if not enabled:
                return
            n = sum(cb_.isChecked() for cb_ in enabled)
            blocked = select_all.blockSignals(True)
            if   n == 0:          select_all.setCheckState(Qt.CheckState.Unchecked)
            elif n == len(enabled): select_all.setCheckState(Qt.CheckState.Checked)
            else:                 select_all.setCheckState(Qt.CheckState.PartiallyChecked)
            select_all.blockSignals(blocked)

        def handle_aur_dependency():
            if aur_cb and yay_cb:
                if aur_cb.isChecked():
                    yay_cb.setEnabled(False)
                    yay_cb.setChecked(True)
                elif self.distro_helper.supports_aur():
                    yay_cb.setEnabled(True)
                    if select_all.checkState() == Qt.CheckState.Unchecked:
                        yay_cb.setChecked(False)
            update_select_all()

        def toggle_all():
            checked = select_all.checkState() == Qt.CheckState.Checked
            for cb_, key_ in self.system_manager_operations_widgets:
                if key_ not in ("install_yay", "install_aur_packages") and cb_.isEnabled():
                    cb_.setChecked(checked)
            if aur_cb and aur_cb.isEnabled():
                aur_cb.setChecked(checked)

        select_all.stateChanged.connect(toggle_all)
        for cb, key in self.system_manager_operations_widgets:
            cb.stateChanged.connect(update_select_all)
            if key == "install_aur_packages":
                cb.stateChanged.connect(handle_aur_dependency)
        handle_aur_dependency()
        update_select_all()

    def _checkbox_by_key(self, key: str) -> QCheckBox | None:
        return next((cb for cb, k in self.system_manager_operations_widgets if k == key), None)

    def _show_operations_dialog(self, content: QWidget):
        dialog = QDialog(self)
        dialog.setWindowTitle("System Manager Operations")
        layout = QVBoxLayout(dialog)
        layout.addWidget(content)

        btn_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel # type: ignore
        )
        btn_box.button(QDialogButtonBox.StandardButton.Ok).setText("Save")
        btn_box.button(QDialogButtonBox.StandardButton.Cancel).setText("Close")
        btn_box.accepted.connect(lambda: self._save_options(dialog=dialog))
        btn_box.rejected.connect(dialog.reject)
        layout.addWidget(btn_box)

        btn_box.button(QDialogButtonBox.StandardButton.Cancel).setFocus()
        content.adjustSize()
        dialog.adjustSize()
        dialog.setMinimumSize(dialog.sizeHint())
        dialog.exec()

    def _edit_system_files(self):
        Options.load_config(Options.config_file_path)
        self.system_files_widgets  = []
        self.original_system_files = []

        content = QWidget()
        grid    = QGridLayout(content)
        self._build_system_file_widgets(Options.system_files or [], grid)

        dialog, layout = self._make_scroll_dialog(
            "Edit 'System Files' [Uncheck to remove]",
            content,
            lambda dlg: self._save_options(dlg, "system_files"),
        )
        add_btn = QPushButton("Add 'System File'")
        add_btn.clicked.connect(self._add_system_files)
        layout.insertWidget(1, add_btn)
        dialog.exec()

    def _build_system_file_widgets(self, files: list, grid: QGridLayout):
        max_w = 0
        for i, info in enumerate(files):
            src, dst = self._parse_file_info(info)
            if not src or not dst:
                continue
            self.original_system_files.append(info)
            lw   = self._make_file_list_widget(src, dst, info)
            txt  = self._apply_replacements(f"{src}  --->  {dst}")
            text_w = lw.fontMetrics().horizontalAdvance(txt)
            max_w  = max(max_w, text_w)
            grid.addWidget(lw, i, 0)
            self.system_files_widgets.append(lw)

        for lw in self.system_files_widgets:
            lw.setMinimumWidth(max_w + 50)

        if self.system_files_widgets:
            sa = self._make_select_all_checkbox("Check/Uncheck All System Files")
            sa.setStyleSheet(get_current_style())
            grid.addWidget(sa, len(files), 0)
            self._wire_list_select_all(sa, self.system_files_widgets)

    def _add_system_files(self):
        self._close_top_modal()
        box = QMessageBox(self)
        box.setWindowTitle("Add 'System Files'")
        box.setText("Would you like to add a single file or an entire directory?")
        file_btn   = box.addButton("File",      QMessageBox.ButtonRole.YesRole)
        dir_btn    = box.addButton("Directory", QMessageBox.ButtonRole.NoRole)
        cancel_btn = box.addButton(QMessageBox.StandardButton.Cancel)
        box.exec()

        clicked = box.clickedButton()
        if clicked == cancel_btn:
            QTimer.singleShot(0, self._edit_system_files)
            return

        sources = None
        if clicked == file_btn:
            files, _ = QFileDialog.getOpenFileNames(self, "Select 'System File'")
            sources  = files or None
        elif clicked == dir_btn:
            d       = QFileDialog.getExistingDirectory(self, "Select 'System Directory'")
            sources = [d] if d else None

        dst_dir = QFileDialog.getExistingDirectory(self, "Select Destination Directory") if sources else None

        if sources and dst_dir:
            self._process_new_system_files(sources, dst_dir)
        else:
            QTimer.singleShot(0, self._edit_system_files)

    def _process_new_system_files(self, sources: list, dst_dir: str):
        Options.system_files = Options.system_files or []
        added = []
        for s in sources:
            try:
                sp = Path(s)
                if not sp.exists():
                    continue
                src_str = str(sp.resolve())
                dst_str = str(Path(dst_dir) / sp.name)
                if not any(isinstance(f, dict) and str(f.get("source", "")) == src_str
                           for f in Options.system_files):
                    Options.system_files.append({"source": src_str, "destination": dst_str})
                    added.append(sp.name)
            except Exception as exc:
                logger.error("Error processing %s: %s", s, exc)

        if Options.system_files:
            Options.system_files.sort(
                key=lambda x: x.get("source", "").lower() if isinstance(x, dict) else str(x).lower()
            )
        if added:
            Options.save_config()
            msg = (f"'{added[0]}' was successfully added!"
                   if len(added) == 1
                   else f"Added successfully:\n{chr(10).join(added)}")
            QMessageBox.information(self, "'System Files'", msg)
        else:
            QMessageBox.information(self, "No changes", "No new items have been added.")
        QTimer.singleShot(0, self._edit_system_files)

    def _edit_packages(self, option_type: str):
        is_specific = option_type == "specific_packages"
        title       = f"Edit '{option_type.replace('_', ' ').title()}' [Uncheck to remove]"
        add_label   = f"Add '{option_type.replace('_', ' ').title().replace(' Packages', ' Package')}'"
        self._manage_packages(title, option_type, add_label, is_specific)

    def _manage_packages(self, title: str, option_type: str, add_btn_text: str, is_specific: bool):
        self.current_option_type = option_type
        Options.load_config(Options.config_file_path)
        packages = getattr(Options, option_type, [])
        if not isinstance(packages, list):
            packages = []

        widgets = self._build_package_list_widgets(packages, is_specific)
        content = QWidget()
        grid    = QGridLayout(content)
        for i, w in enumerate(widgets):
            grid.addWidget(w, i // 5, i % 5)
        setattr(self, f"{option_type}_widgets", widgets)

        if widgets:
            row = (len(widgets) - 1) // 5 + 1
            sa  = self._make_select_all_checkbox(
                f"Check/Uncheck All {option_type.replace('_', ' ').title()}"
            )
            sa.setStyleSheet(get_current_style())
            grid.addWidget(sa, row, 0, 1, 5)
            self._wire_list_select_all(sa, widgets)

        dialog, layout = self._make_scroll_dialog(
            title, content,
            lambda dlg: self._save_options(dlg, option_type),
        )
        self._add_search_bar(layout, widgets)
        self._add_package_management_buttons(layout, add_btn_text, option_type, is_specific)
        dialog.exec()

    def _build_package_list_widgets(self, packages: list, is_specific: bool) -> list[QListWidget]:
        widgets = []
        for p in packages:
            if is_specific and isinstance(p, dict):
                text       = f"{p['package']}\n({p['session']})"
                is_disabled = p.get("disabled", False)
            elif isinstance(p, dict):
                text       = p.get("name", str(p))
                is_disabled = p.get("disabled", False)
            else:
                text       = str(p)
                is_disabled = False

            item = QListWidgetItem(text)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(
                Qt.CheckState.PartiallyChecked if is_disabled else Qt.CheckState.Checked
            )
            lw = QListWidget()
            lw.addItem(item)
            lw.setMaximumHeight(60 if is_specific else 40)
            lw.setProperty("tristate_enabled", True)
            lw.itemClicked.connect(self._cycle_tristate)
            lw.setToolTip(
                "☑ = Active (will be installed)\n"
                "▣ = Disabled (will be skipped)\n"
                "☐ = Delete (will be removed)"
            )
            widgets.append(lw)
        return widgets

    @staticmethod
    def _cycle_tristate(item: QListWidgetItem):
        state = item.checkState()
        if   state == Qt.CheckState.Checked:        item.setCheckState(Qt.CheckState.PartiallyChecked)
        elif state == Qt.CheckState.PartiallyChecked: item.setCheckState(Qt.CheckState.Unchecked)
        else:                                        item.setCheckState(Qt.CheckState.Checked)

    @staticmethod
    def _add_search_bar(layout: QVBoxLayout, widgets: list):
        row   = QHBoxLayout()
        field = QLineEdit()
        field.setPlaceholderText("Type to filter packages…")
        field.textChanged.connect(
            lambda t: [w.setVisible(t.lower() in w.item(0).text().lower())
                       for w in widgets if w.count()]
        )
        row.addWidget(field)
        layout.insertLayout(1, row)

    def _add_package_management_buttons(self, layout, add_text, option_type, is_specific):
        add_btn = QPushButton(add_text)
        add_btn.clicked.connect(lambda: self._add_package(option_type))
        layout.insertWidget(2, add_btn)

        if not is_specific:
            batch_btn = QPushButton(f"Add '{option_type.replace('_', ' ').title()}' in Batches")
            batch_btn.clicked.connect(lambda: self._batch_add_packages(option_type))
            layout.insertWidget(3, batch_btn)

            io_row   = QHBoxLayout()
            imp_btn  = QPushButton("📥 Import from File (TXT/CSV)")
            exp_btn  = QPushButton("📤 Export to File (TXT)")
            imp_btn.clicked.connect(lambda: self._import_packages(option_type))
            exp_btn.clicked.connect(lambda: self._export_packages(option_type))
            io_row.addWidget(imp_btn)
            io_row.addWidget(exp_btn)
            layout.insertLayout(4, io_row)

    def _add_package(self, option_type: str):
        self._close_top_modal()
        if option_type == "specific_packages":
            self._add_specific_package()
        else:
            self._add_regular_package(option_type)

    def _add_regular_package(self, option_type: str):
        label = option_type.replace("_", " ").title().replace(" Packages", " Package")
        name, ok = QInputDialog.getText(
            self, f"Add '{label}'",
            "                              Enter Package Name:                              ",
        )
        if ok and name.strip():
            current = getattr(Options, option_type, [])
            existing = {(p.get("name") if isinstance(p, dict) else p) for p in current}
            if name.strip() not in existing:
                current.append(name.strip())
                setattr(Options, option_type, current)
                Options.save_config()
                QMessageBox.information(self, "Package Added",
                                        f"Package '{name}' has been successfully added!")
            else:
                QMessageBox.warning(self, "Duplicate Package",
                                    f"Package '{name}' already exists.")
        QTimer.singleShot(0, lambda: self._edit_packages(option_type))

    def _batch_add_packages(self, option_type: str):
        self._close_top_modal()
        dialog = QDialog(self)
        dialog.setWindowTitle(f"Add '{option_type.replace('_', ' ').title()}' in Batches")
        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel("Enter package names (one per line):"))
        text_edit = QTextEdit()
        text_edit.setMinimumHeight(300)
        text_edit.setPlaceholderText("Package 1…")
        layout.addWidget(text_edit)
        btn_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel # type: ignore
        )
        btn_box.accepted.connect(dialog.accept)
        btn_box.rejected.connect(dialog.reject)
        layout.addWidget(btn_box)
        dialog.setMinimumSize(750, 550)
        dialog.resize(750, 600)

        if dialog.exec() == QDialog.DialogCode.Accepted:
            text = text_edit.toPlainText().strip()
            if text:
                pkgs    = [p.strip() for p in text.splitlines() if p.strip()]
                current = getattr(Options, option_type, [])
                existing = {(p.get("name") if isinstance(p, dict) else p) for p in current}
                added, dupes = [], []
                for p in pkgs:
                    if p in existing:
                        dupes.append(p)
                    else:
                        added.append(p)
                        existing.add(p)
                        current.append(p)
                setattr(Options, option_type, list(current))
                Options.save_config()
                if dupes:
                    plural = "s already exist" if len(dupes) > 1 else " already exists"
                    QMessageBox.warning(self, "Duplicates",
                                        f"The following package{plural}:\n\n" + "\n".join(dupes))
                if added:
                    verb = "have been" if len(added) > 1 else "has been"
                    QMessageBox.information(self, "Packages Added",
                                            f"The following packages {verb} added:\n\n" + "\n".join(added))
        QTimer.singleShot(0, lambda: self._edit_packages(option_type))

    def _add_specific_package(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("Add 'Specific Package'")
        layout = QVBoxLayout(dialog)
        form   = QFormLayout()

        pkg_input = QLineEdit()
        pkg_input.setFixedHeight(38)
        form.addRow("Package Name:", pkg_input)

        sess_combo = QComboBox()
        sess_combo.setStyleSheet("color:#fff;background-color:#555582;padding:5px;")
        if SESSIONS:
            sess_combo.addItems(SESSIONS)
        else:
            sess_combo.addItem("No sessions available")
            sess_combo.setEnabled(False)
        sess_combo.setFixedHeight(38)
        form.addRow("Session:", sess_combo)
        layout.addLayout(form)

        btn_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel # type: ignore
        )
        btn_box.accepted.connect(dialog.accept)
        btn_box.rejected.connect(dialog.reject)
        layout.addWidget(btn_box)
        dialog.setFixedWidth(650)

        if dialog.exec() == QDialog.DialogCode.Accepted:
            name    = pkg_input.text().strip()
            session = sess_combo.currentText()
            if not name:
                QMessageBox.warning(self, "Missing Information", "Package name is required.")
                return
            if not hasattr(Options, "specific_packages") or Options.specific_packages is None:
                Options.specific_packages = []
            new_pkg = {"package": name, "session": session}
            exists  = any(
                isinstance(p, dict) and p.get("package") == name and p.get("session") == session
                for p in Options.specific_packages
            )
            if not exists:
                Options.specific_packages.append(new_pkg)
                Options.save_config()
                QMessageBox.information(self, "Package Added",
                                        f"Package '{name}' for '{session}' successfully added!")
            else:
                QMessageBox.warning(self, "Duplicate Package",
                                    f"Package '{name}' for '{session}' already exists.")
            self._edit_packages("specific_packages")

    def _save_options(self, dialog: QDialog | None = None, option_type: str | None = None):
        current_type = option_type or self.current_option_type
        changed      = False

        if current_type == "system_manager_operations":
            Options.system_manager_operations = [
                key for cb, key in self.system_manager_operations_widgets if cb.isChecked()
            ]

        elif current_type == "system_files":
            new_files = []
            for lw in self.system_files_widgets:
                if lw.count():
                    item  = lw.item(0)
                    state = item.checkState() if item else None
                    if state in (Qt.CheckState.Checked, Qt.CheckState.PartiallyChecked):
                        data = item.data(Qt.ItemDataRole.UserRole)
                        if isinstance(data, dict):
                            new_files.append({
                                "source":      data.get("source", ""),
                                "destination": data.get("destination", ""),
                                "disabled":    state == Qt.CheckState.PartiallyChecked,
                            })
            if len(new_files) != len(Options.system_files or []):
                changed = True
            Options.system_files = new_files

        elif current_type in ("basic_packages", "aur_packages", "specific_packages"):
            is_specific = current_type == "specific_packages"
            widgets     = getattr(self, f"{current_type}_widgets", [])
            new_pkgs    = []
            for lw in widgets:
                if lw.count():
                    item  = lw.item(0)
                    state = item.checkState() if item else None
                    if state in (Qt.CheckState.Checked, Qt.CheckState.PartiallyChecked):
                        if is_specific:
                            text = item.text()
                            if "\n" in text:
                                parts = text.split("\n", 1)
                                new_pkgs.append({
                                    "package":  parts[0].strip(),
                                    "session":  parts[1].strip("()"),
                                    "disabled": state == Qt.CheckState.PartiallyChecked,
                                })
                        else:
                            new_pkgs.append({
                                "name":     item.text(),
                                "disabled": state == Qt.CheckState.PartiallyChecked,
                            })
            if len(new_pkgs) != len(getattr(Options, current_type, [])):
                changed = True
            setattr(Options, current_type, new_pkgs)

        Options.save_config()
        QMessageBox.information(self, "Options Saved", "Your changes have been saved.")
        if dialog:
            dialog.accept()
        if changed:
            if current_type == "system_files":
                QTimer.singleShot(0, self._edit_system_files)
            elif current_type in ("basic_packages", "aur_packages", "specific_packages"):
                QTimer.singleShot(0, lambda: self._edit_packages(current_type))

    def _import_packages(self, option_type: str):
        path, _ = QFileDialog.getOpenFileName(
            self, "Import Packages", str(Path.home()),
            "Text / CSV files (*.txt *.csv);;All files (*)",
        )
        if not path:
            return
        try:
            with open(path, encoding="utf-8") as f:
                raw_lines = f.readlines()
        except OSError as exc:
            QMessageBox.critical(self, "Import Error", f"Cannot read file:\n{exc}")
            return

        current  = getattr(Options, option_type, [])
        existing = {(p.get("name") if isinstance(p, dict) else p) for p in current}
        added, dupes, invalid = [], [], []
        for line in raw_lines:
            name = line.strip().strip("\"'").split(",")[0].strip()
            if not name or name.startswith("#"):
                continue
            if name in existing:
                dupes.append(name)
            elif not all(c.isalnum() or c in "-_.+:" for c in name):
                invalid.append(name)
            else:
                added.append(name)
                existing.add(name)
                current.append({"name": name, "disabled": False})

        setattr(Options, option_type, current)
        Options.save_config()
        summary = f"Import complete.\n\nAdded: {len(added)}"
        if dupes:
            summary += f"\nSkipped (duplicates): {len(dupes)}"
        if invalid:
            summary += f"\nSkipped (invalid names): {len(invalid)}"
            summary += "\n  " + ", ".join(invalid[:10])
            if len(invalid) > 10:
                summary += f" … (+{len(invalid) - 10} more)"
        QMessageBox.information(self, "Import Result", summary)
        QTimer.singleShot(0, lambda: self._edit_packages(option_type))

    def _export_packages(self, option_type: str):
        packages = getattr(Options, option_type, [])
        if not packages:
            QMessageBox.information(self, "Export", "No packages to export.")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Export Packages", str(Path.home() / f"{option_type}.txt"),
            "Text files (*.txt);;All files (*)",
        )
        if not path:
            return
        lines = [p.get("name", "") if isinstance(p, dict) else str(p) for p in packages]
        lines = [l for l in lines if l]
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
            QMessageBox.information(self, "Export Complete",
                                    f"Exported {len(lines)} package(s) to:\n{path}")
        except OSError as exc:
            QMessageBox.critical(self, "Export Error", f"Cannot write file:\n{exc}")

    def _go_back(self):
        self.close()
        if self.parent():
            self.parent().show()

    def _close_top_modal(self):
        for w in QApplication.topLevelWidgets():
            if isinstance(w, QDialog) and w.isModal() and w is not self:
                w.close()
                return

    @staticmethod
    def _make_select_all_checkbox(label: str = "Check/Uncheck All") -> QCheckBox:
        cb = QCheckBox(label)
        cb.setStyleSheet(f"{get_current_style()} QCheckBox{{color:#6ffff5;}}")
        return cb

    @staticmethod
    def _wire_list_select_all(select_all: QCheckBox, widgets: list[QListWidget]):
        def toggle_all():
            checked = select_all.checkState() == Qt.CheckState.Checked
            for _lw in widgets:
                item = _lw.item(0) if _lw.count() else None
                if item:
                    item.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)

        def update_state():
            total = checked = partially = 0
            for _lw in widgets:
                item = _lw.item(0) if _lw.count() else None
                if item:
                    total += 1
                    s = item.checkState()
                    if s == Qt.CheckState.Checked:         checked    += 1
                    elif s == Qt.CheckState.PartiallyChecked: partially += 1
            if not total:
                return
            blocked = select_all.blockSignals(True)
            if   checked == total:               select_all.setCheckState(Qt.CheckState.Checked)
            elif checked + partially == 0:       select_all.setCheckState(Qt.CheckState.Unchecked)
            else:                                select_all.setCheckState(Qt.CheckState.PartiallyChecked)
            select_all.blockSignals(blocked)

        select_all.stateChanged.connect(toggle_all)
        for lw in widgets:
            if lw.count():
                lw.itemChanged.connect(update_state)
        update_state()

    def _make_file_list_widget(self, src: str, dst: str, data=None) -> QListWidget:
        display = self._apply_replacements(f"{src}  --->  {dst}")
        item    = QListWidgetItem(display)
        item.setData(Qt.ItemDataRole.UserRole, {"source": src, "destination": dst})
        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
        if isinstance(data, dict) and data.get("disabled", False):
            item.setCheckState(Qt.CheckState.PartiallyChecked)
        else:
            item.setCheckState(Qt.CheckState.Checked)

        lw = QListWidget()
        lw.addItem(item)
        lw.setMaximumHeight(40)
        lw.itemClicked.connect(self._cycle_tristate)
        lw.setToolTip(
            "☑ = Active (will be copied)\n"
            "▣ = Disabled (will be skipped)\n"
            "☐ = Delete (will be removed)"
        )
        return lw

    @staticmethod
    def _parse_file_info(info) -> tuple[str, str]:
        if isinstance(info, dict):
            return info.get("source", ""), info.get("destination", "")
        if isinstance(info, str) and " -> " in info:
            parts = info.split(" -> ", 1)
            if len(parts) == 2:
                return parts[0].strip(), parts[1].strip()
        return "", ""

    @staticmethod
    def _apply_replacements(text: str) -> str:
        for old, new in Options.text_replacements:
            text = text.replace(old, new)
        return text

    def _make_scroll_dialog(self, title: str, content: QWidget, save_cb=None
                             ) -> tuple[QDialog, QVBoxLayout]:
        dialog = QDialog(self)
        dialog.setWindowTitle(title)
        layout = QVBoxLayout(dialog)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        scroll.setWidget(content)
        layout.addWidget(scroll)

        btn_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel # type: ignore
        )
        btn_box.button(QDialogButtonBox.StandardButton.Ok).setText("Save")
        btn_box.button(QDialogButtonBox.StandardButton.Cancel).setText("Close")
        if save_cb:
            btn_box.accepted.connect(lambda: save_cb(dialog))
        btn_box.rejected.connect(dialog.reject)
        layout.addWidget(btn_box)

        self._fit_dialog(dialog, content, scroll)
        btn_box.button(QDialogButtonBox.StandardButton.Cancel).setFocus()
        return dialog, layout

    @staticmethod
    def _fit_dialog(dialog: QDialog, content: QWidget, scroll: QScrollArea):
        content.adjustSize()
        sz  = content.sizeHint()
        scr = QApplication.primaryScreen()
        if not scr:
            return
        sg    = scr.availableGeometry()
        mw    = max(dialog.geometry().width()  - dialog.contentsRect().width(),  400)
        mh    = max(dialog.geometry().height() - dialog.contentsRect().height(), 250)
        opt_w = min(sz.width()  + mw, sg.width())
        opt_h = min(sz.height() + mh, sg.height())
        dialog.resize(opt_w, opt_h)
        dialog.setMinimumSize(min(opt_w, sg.width()  // 3), min(opt_h, sg.height() // 4))
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setMaximumHeight(min(sz.height() + mh, sg.height() - mh))
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setMaximumWidth(min(sz.width() + mw, sg.width() - mw))

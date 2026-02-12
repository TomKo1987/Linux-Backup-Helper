from pathlib import Path
from PyQt6.QtCore import Qt, QTimer
from linux_distro_helper import LinuxDistroHelper
from options import Options, SESSIONS, USER_SHELL
from global_style import global_style, get_current_style
from PyQt6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QGridLayout, QFormLayout, QLabel, QPushButton, QWidget,
                             QComboBox, QCheckBox, QListWidget, QListWidgetItem, QScrollArea, QDialogButtonBox, QLineEdit,
                             QMessageBox, QFileDialog, QInputDialog, QTextEdit, QApplication, QSizePolicy)

from logging_config import setup_logger
logger = setup_logger(__name__)


# noinspection PyUnresolvedReferences
class SystemManagerOptions(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("System Manager Options")
        self.distro_helper = LinuxDistroHelper()
        self.distro_name = self.distro_helper.distro_pretty_name
        self.session = self.distro_helper.detect_session()
        self.install_package_command = self.distro_helper.get_pkg_install_cmd("")
        self._active_dialogs = []
        self._last_shell = None

        self.top_label = None
        self.distro_label = None
        self.shell_combo = None
        self.close_button = None
        self.system_manager_operations_button = None
        self.system_files_button = None
        self.package_buttons = {}

        self.system_manager_operations_widgets = []
        self.system_files_widgets = []
        self.original_system_files = []
        self.essential_packages_widgets = []
        self.additional_packages_widgets = []
        self.specific_packages_widgets = []
        self.current_option_type = None

        self.setup_ui()

    def track_dialog(self, dialog):
        self._active_dialogs.append(dialog)
        dialog.finished.connect(lambda: self._remove_dialog(dialog))

    def _remove_dialog(self, dialog):
        if dialog in self._active_dialogs:
            self._active_dialogs.remove(dialog)

    def close_all_dialogs(self):
        for dialog in self._active_dialogs[:]:
            if dialog and dialog.isVisible():
                dialog.close()
        self._active_dialogs.clear()

    def _get_top_label_text(self):
        return (
            f"First you can select 'System Files' in System Manager. These files will be copied using 'sudo', "
            f"for root privilege. If you have 'System Files' selected, System Manager will copy these first. "
            f"This allows you to copy files such as 'pacman.conf' or 'smb.conf' to '/etc/'.\n\n"
            f"Under 'System Manager Operations' you can specify how you would like to proceed. "
            f"Each action is executed one after the other. Uncheck actions to disable them.\n\n"
            f"Tips:\n\n"
            f"It is possible to copy to and from samba shares if samba is set up correctly. "
            f"Source and/or destination must be saved as follows:\n\n"
            f"'smb://ip/rest of path'\n\n"
            f"Example: 'smb://192.168.0.53/rest of smb share path'\n\n"
            f"'Essential Packages' will be installed using '{self.install_package_command}PACKAGE'.\n\n"
            f"'Additional Packages' provides access to the Arch User Repository. "
            f"Therefore 'yay' must and will be installed. This feature is available only on "
            f"Arch Linux based distributions.\n\n"
            f"You can also define 'Specific Packages'. These packages will be installed only "
            f"(using '{self.install_package_command}PACKAGE') "
            f"if the corresponding session has been recognized.\n"
            f"Both full desktop environments and window managers such as 'Hyprland' and others are supported."
        )

    def setup_ui(self):
        layout = QVBoxLayout(self)
        self._add_distro_info(layout)

        self.top_label = QLabel(self._get_top_label_text())
        self.top_label.setWordWrap(True)
        self.top_label.setSizePolicy(QSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred))

        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        scroll_area.setWidget(self.top_label)
        layout.addWidget(scroll_area)

        self._add_button_layouts(layout)
        self._add_shell_selection(layout)

        self.close_button = QPushButton("Close")
        layout.addWidget(self.close_button)

        self._connect_signals()
        self.setStyleSheet(get_current_style())
        self.setMinimumSize(1425, 950)

    def _add_distro_info(self, layout):
        yay_info = ""
        if self.distro_helper.has_aur:
            yay_info = " | AUR Helper: 'yay' detected" if self.distro_helper.package_is_installed('yay') \
                else " | AUR Helper: 'yay' not detected"

        self.distro_label = QLabel(
            f"Recognized Linux distribution: {self.distro_name} | Session: {self.session}{yay_info}"
        )
        self.distro_label.setStyleSheet("color: lightgreen")
        layout.addWidget(self.distro_label)

    def _add_button_layouts(self, layout):
        self.system_manager_operations_button = QPushButton("System Manager Operations")
        self.system_files_button = QPushButton("System Files")
        self.package_buttons = {
            "essential_packages": QPushButton("Essential Packages"),
            "additional_packages": QPushButton("Additional Packages"),
            "specific_packages": QPushButton("Specific Packages")
        }

        hbox1_buttons = QHBoxLayout()
        hbox1_buttons.addWidget(self.system_manager_operations_button)
        hbox1_buttons.addWidget(self.system_files_button)
        layout.addLayout(hbox1_buttons)

        hbox2_buttons = QHBoxLayout()
        for button in self.package_buttons.values():
            hbox2_buttons.addWidget(button)
        layout.addLayout(hbox2_buttons)

    def _add_shell_selection(self, layout):
        self.shell_combo = QComboBox()
        self.shell_combo.addItems(USER_SHELL)
        idx = USER_SHELL.index(Options.user_shell) if Options.user_shell in USER_SHELL else 0
        self.shell_combo.setCurrentIndex(idx)
        self._last_shell = USER_SHELL[idx]

        shell_box_layout = QHBoxLayout()
        shell_label = QLabel("Select User Shell:")
        border_style = "border-radius: 6px; border: 1px solid #7aa2f7;"
        style = f"color: #a9b1d6; font-size: 16px; font-weight: 500; padding: 4px 8px; background-color: #1a1b26; {border_style}"
        shell_label.setStyleSheet(style)
        self.shell_combo.setStyleSheet(style)

        shell_box_layout.addWidget(shell_label)
        shell_box_layout.addWidget(self.shell_combo)
        layout.addLayout(shell_box_layout)

    def _connect_signals(self):
        self.system_manager_operations_button.clicked.connect(self.system_manager_operations)
        self.system_files_button.clicked.connect(self.edit_system_files)
        self.close_button.clicked.connect(self.go_back)
        self.shell_combo.currentIndexChanged.connect(self._on_shell_changed)

        for option_type, button in self.package_buttons.items():
            button.clicked.connect(lambda _, ot=option_type: self.edit_packages(ot))

    def _on_shell_changed(self):
        selected_shell = self.shell_combo.currentText()
        if selected_shell != self._last_shell and selected_shell in USER_SHELL:
            Options.user_shell = selected_shell
            Options.save_config()
            self._last_shell = selected_shell
            QMessageBox.information(self, "User Shell Changed", f"User Shell has been set to: {selected_shell}")

    def system_manager_operations(self):
        self.current_option_type = "system_manager_operations"
        Options.load_config(Options.config_file_path)

        content_widget = QWidget()
        grid_layout = QGridLayout(content_widget)

        select_all_checkbox = self._create_select_all_checkbox()
        grid_layout.addWidget(select_all_checkbox, 0, 1)

        self._create_operation_checkboxes(grid_layout)
        self._setup_checkbox_interactions(select_all_checkbox)
        self._show_operations_dialog(content_widget)

    @staticmethod
    def _create_select_all_checkbox():
        select_all_checkbox = QCheckBox("Check/Uncheck All")
        select_all_checkbox.setStyleSheet(f"{global_style} QCheckBox {{color: #6ffff5;}}")
        return select_all_checkbox

    def _create_operation_checkboxes(self, grid_layout):
        arch_only_operations = {'update_mirrors', 'install_yay', 'install_additional_packages'}
        system_manager_operation_text = Options.get_system_manager_operation_text(self.distro_helper)
        operations = [(text.replace("<br>", "\n"), key) for key, text in system_manager_operation_text.items()]

        self.system_manager_operations_widgets = []
        for index, (label, operation_key) in enumerate(operations):
            checkbox = QCheckBox(label)

            if operation_key in arch_only_operations and not self.distro_helper.supports_aur():
                self._configure_disabled_checkbox(checkbox, operation_key)
            else:
                self._configure_enabled_checkbox(checkbox, operation_key)

            grid_layout.addWidget(checkbox, index + 1, 0)
            self.system_manager_operations_widgets.append((checkbox, operation_key))

    @staticmethod
    def _configure_disabled_checkbox(checkbox, operation_key):
        checkbox.setChecked(False)
        checkbox.setEnabled(False)
        checkbox.setStyleSheet(f"{global_style} QCheckBox {{ color: #666666; }}")

        tooltips = {
            'update_mirrors': "Mirror update is available on Arch Linux only",
            'install_yay': "Available only on Arch Linux based distributions",
            'install_additional_packages': "Available only on Arch Linux based distributions"
        }

        if operation_key in tooltips:
            checkbox.setToolTip(tooltips[operation_key])

    @staticmethod
    def _configure_enabled_checkbox(checkbox, operation_key):
        checkbox.setChecked(operation_key in Options.system_manager_operations)
        checkbox.setStyleSheet(f"{global_style} QCheckBox {{ color: #c8beff; }}")

    def _setup_checkbox_interactions(self, select_all_checkbox):
        install_yay_checkbox = self._get_checkbox_by_key('install_yay')
        install_additional_packages_checkbox = self._get_checkbox_by_key('install_additional_packages')

        def handle_dependencies():
            if install_additional_packages_checkbox and install_yay_checkbox:
                if install_additional_packages_checkbox.isChecked():
                    install_yay_checkbox.setEnabled(False)
                    install_yay_checkbox.setChecked(True)
                elif self.distro_helper.supports_aur():
                    install_yay_checkbox.setEnabled(True)
                    if select_all_checkbox.checkState() == Qt.CheckState.Unchecked:
                        install_yay_checkbox.setChecked(False)
            update_select_all_state()

        def toggle_all_checkboxes():
            checked = select_all_checkbox.checkState() == Qt.CheckState.Checked
            for operation_checkbox, key in self.system_manager_operations_widgets:
                if key not in ['install_yay', 'install_additional_packages'] and operation_checkbox.isEnabled():
                    operation_checkbox.setChecked(checked)

            if install_additional_packages_checkbox and install_additional_packages_checkbox.isEnabled():
                install_additional_packages_checkbox.setChecked(checked)

        def update_select_all_state():
            enabled_checkboxes = [
                cb for cb, key in self.system_manager_operations_widgets
                if cb.isEnabled() and not (key == 'install_yay' and
                                           install_additional_packages_checkbox and
                                           install_additional_packages_checkbox.isChecked())
            ]

            if not enabled_checkboxes:
                return

            checked_count = sum(1 for cb in enabled_checkboxes if cb.isChecked())
            block = select_all_checkbox.blockSignals(True)

            if checked_count == 0:
                select_all_checkbox.setCheckState(Qt.CheckState.Unchecked)
            elif checked_count == len(enabled_checkboxes):
                select_all_checkbox.setCheckState(Qt.CheckState.Checked)
            else:
                select_all_checkbox.setCheckState(Qt.CheckState.PartiallyChecked)

            select_all_checkbox.blockSignals(block)

        select_all_checkbox.stateChanged.connect(toggle_all_checkboxes)
        for checkbox, option_key in self.system_manager_operations_widgets:
            checkbox.stateChanged.connect(update_select_all_state)
            if option_key == 'install_additional_packages':
                checkbox.stateChanged.connect(handle_dependencies)

        handle_dependencies()
        update_select_all_state()

    def _get_checkbox_by_key(self, key):
        return next((cb for cb, k in self.system_manager_operations_widgets if k == key), None)

    def _show_operations_dialog(self, content_widget):
        dialog = QDialog(self)
        dialog.setWindowTitle("System Manager Operations")
        layout = QVBoxLayout(dialog)
        layout.addWidget(content_widget)

        button_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel) # type: ignore
        button_box.button(QDialogButtonBox.StandardButton.Ok).setText('Save')
        button_box.button(QDialogButtonBox.StandardButton.Cancel).setText('Close')

        button_box.accepted.connect(lambda: self.save_system_manager_options(dialog=dialog))
        button_box.rejected.connect(dialog.reject)
        layout.addWidget(button_box)

        button_box.button(QDialogButtonBox.StandardButton.Cancel).setFocus()
        content_widget.adjustSize()
        dialog.adjustSize()
        dialog.setMinimumSize(dialog.sizeHint())
        dialog.exec()

    def create_dialog(self, title, content_widget, button_callback=None):
        dialog = QDialog(self)
        dialog.setWindowTitle(title)
        layout = QVBoxLayout(dialog)

        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        scroll_area.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        scroll_area.setWidget(content_widget)
        layout.addWidget(scroll_area)

        button_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel) # type: ignore
        button_box.button(QDialogButtonBox.StandardButton.Ok).setText('Save')
        button_box.button(QDialogButtonBox.StandardButton.Cancel).setText('Close')

        if button_callback:
            button_box.accepted.connect(lambda: button_callback(dialog))
        button_box.rejected.connect(dialog.reject)
        layout.addWidget(button_box)

        self._adjust_dialog_size(dialog, content_widget, scroll_area)
        button_box.button(QDialogButtonBox.StandardButton.Cancel).setFocus()
        return dialog, layout

    def close_current_dialog(self):
        for widget in QApplication.topLevelWidgets():
            if isinstance(widget, QDialog) and widget.isModal() and widget != self:
                widget.close()
                return True
        return False

    @staticmethod
    def _adjust_dialog_size(dialog, content_widget, scroll_area):
        content_widget.adjustSize()
        content_size = content_widget.sizeHint()
        screen = QApplication.primaryScreen().availableGeometry()

        margin_w = dialog.geometry().width() - dialog.contentsRect().width()
        if margin_w <= 0:
            margin_w = 350
        margin_h = dialog.geometry().height() - dialog.contentsRect().height()
        if margin_h <= 0:
            margin_h = 200

        optimal_height = min(content_size.height() + margin_h, screen.height())
        optimal_width = min(content_size.width() + margin_w, screen.width())

        dialog.resize(optimal_width, optimal_height)
        dialog.setMinimumSize(min(optimal_width, screen.width() // 3), min(optimal_height, screen.height() // 4))

        scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll_area.setMaximumHeight(min(content_size.height() + margin_h, screen.height() - margin_h))
        scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll_area.setMaximumWidth(min(content_size.width() + margin_w, screen.width() - margin_w))

    def edit_system_files(self):
        Options.load_config(Options.config_file_path)
        content_widget = QWidget()
        grid_layout = QGridLayout(content_widget)

        self.system_files_widgets = []
        self.original_system_files = []

        system_files = Options.system_files or []
        self._create_system_file_widgets(system_files, grid_layout)

        dialog, layout = self.create_dialog(
            "Edit 'System Files' [Uncheck to remove]",
            content_widget,
            lambda dlg: self.save_system_manager_options(dlg, "system_files")
        )

        add_system_files_button = QPushButton("Add 'System File'")
        add_system_files_button.clicked.connect(self.add_system_files)
        layout.insertWidget(1, add_system_files_button)

        dialog.exec()

    def _create_system_file_widgets(self, system_files, grid_layout):
        max_text_width = 0

        for file_index, file_info in enumerate(system_files):
            file_source, file_destination = self._parse_file_info(file_info)

            if not file_source or not file_destination:
                continue

            self.original_system_files.append(file_info)

            container_widget = self._create_file_list_widget(file_source, file_destination, file_info)
            list_widget = getattr(container_widget, 'list_widget', container_widget)

            display_text = f"{file_source}  --->  {file_destination}"
            display_text = self._apply_text_replacements(display_text)
            text_width = list_widget.fontMetrics().horizontalAdvance(display_text)
            max_text_width = max(max_text_width, text_width)

            grid_layout.addWidget(container_widget, file_index, 0)
            self.system_files_widgets.append(container_widget)

        for container_widget in self.system_files_widgets:
            list_widget = getattr(container_widget, 'list_widget', container_widget)
            list_widget.setMinimumWidth(max_text_width + 50)

        if self.system_files_widgets:
            select_all_checkbox = self._create_select_all_checkbox()
            select_all_checkbox.setText("Check/Uncheck All System Files")
            select_all_checkbox.setStyleSheet(get_current_style())
            grid_layout.addWidget(select_all_checkbox, len(system_files), 0)
            self._setup_system_files_select_all(select_all_checkbox)

    def _setup_system_files_select_all(self, select_all_checkbox):

        def toggle_all_system_files():
            checked = select_all_checkbox.checkState() == Qt.CheckState.Checked
            for list_widget_item in self.system_files_widgets:
                if list_widget_item.count() > 0:
                    item = list_widget_item.item(0)
                    if item:
                        item.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)

        def update_select_all_state():
            if not self.system_files_widgets:
                return

            checked_count = 0
            partially_checked_count = 0
            total_count = 0

            for list_widget_item in self.system_files_widgets:
                if list_widget_item.count() > 0:
                    item = list_widget_item.item(0)
                    if item:
                        total_count += 1
                        state = item.checkState()
                        if state == Qt.CheckState.Checked:
                            checked_count += 1
                        elif state == Qt.CheckState.PartiallyChecked:
                            partially_checked_count += 1

            if total_count == 0:
                return

            select_all_checkbox.blockSignals(True)
            if checked_count == total_count:
                select_all_checkbox.setCheckState(Qt.CheckState.Checked)
            elif checked_count + partially_checked_count == 0:
                select_all_checkbox.setCheckState(Qt.CheckState.Unchecked)
            else:
                select_all_checkbox.setCheckState(Qt.CheckState.PartiallyChecked)
            select_all_checkbox.blockSignals(False)

        select_all_checkbox.stateChanged.connect(toggle_all_system_files)
        for list_widget in self.system_files_widgets:
            if list_widget.count() > 0:
                list_widget.itemChanged.connect(update_select_all_state)
        update_select_all_state()

    @staticmethod
    def _parse_file_info(file_info):
        file_source = file_destination = ""

        if isinstance(file_info, dict):
            file_source = file_info.get('source', '')
            file_destination = file_info.get('destination', '')
        elif isinstance(file_info, str) and ' -> ' in file_info:
            parts = file_info.split(' -> ', 1)
            if len(parts) == 2:
                file_source, file_destination = parts[0].strip(), parts[1].strip()

        return file_source, file_destination

    @staticmethod
    def _apply_text_replacements(text):
        for old_text, new_text in Options.text_replacements:
            text = text.replace(old_text, new_text)
        return text

    def add_system_files(self):
        self.close_current_dialog()

        box = QMessageBox(self)
        box.setWindowTitle("Add 'System Files'")
        box.setText("Would you like to add a single file or an entire directory?")
        file_button = box.addButton("File", QMessageBox.ButtonRole.YesRole)
        dir_button = box.addButton("Directory", QMessageBox.ButtonRole.NoRole)
        cancel_button = box.addButton(QMessageBox.StandardButton.Cancel)
        box.exec()

        clicked_button = box.clickedButton()

        if clicked_button == cancel_button:
            QTimer.singleShot(0, self.edit_system_files)
            return

        sources = None
        if clicked_button == file_button:
            files, _ = QFileDialog.getOpenFileNames(self, "Select 'System File'")
            sources = files if files else None
        elif clicked_button == dir_button:
            directory = QFileDialog.getExistingDirectory(self, "Select 'System Directory'")
            sources = [directory] if directory else None

        destination_dir = None
        if sources:
            destination_dir = QFileDialog.getExistingDirectory(self, "Select Destination Directory")

        if sources and destination_dir:
            self._process_new_system_files(sources, destination_dir)
        else:
            QTimer.singleShot(0, self.edit_system_files)

    def _process_new_system_files(self, sources, destination_dir):
        Options.system_files = Options.system_files or []
        added_items = []

        for source in sources:
            try:
                source_path = Path(source)
                if not source_path.exists():
                    continue

                source_str = str(source_path.resolve())
                destination = str(Path(destination_dir) / source_path.name)

                if not any(isinstance(item, dict) and str(item.get('source', '')) == source_str
                           for item in Options.system_files):
                    Options.system_files.append({'source': source_str, 'destination': destination})
                    added_items.append(source_path.name)
            except Exception as e:
                logger.error(f"Error when processing {source}: {e}")

        if Options.system_files:
            Options.system_files.sort(
                key=lambda x: x.get('source', '').lower() if isinstance(x, dict) else str(x).lower()
            )

        self._handle_added_files_result(added_items)

    def _handle_added_files_result(self, added_items):
        if added_items:
            Options.save_config()
            if len(added_items) == 1:
                msg = f"'{added_items[0]}' was successfully added!"
            else:
                msg = f"The following items have been successfully added:\n{chr(10).join(added_items)}"
            QMessageBox.information(self, "'System Files'", msg)
        else:
            QMessageBox.information(self, "No changes", "No new items have been added.")

        QTimer.singleShot(0, self.edit_system_files)

    def _create_file_list_widget(self, file_source, file_destination, file_data=None):
        display_text = self._apply_text_replacements(f"{file_source}  --->  {file_destination}")

        item = QListWidgetItem(display_text)
        item.setData(Qt.ItemDataRole.UserRole, {'source': file_source, 'destination': file_destination})
        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)

        if isinstance(file_data, dict):
            is_disabled = file_data.get('disabled', False)
            item.setCheckState(Qt.CheckState.PartiallyChecked if is_disabled else Qt.CheckState.Checked)
        else:
            item.setCheckState(Qt.CheckState.Checked)

        list_widget = QListWidget()
        list_widget.addItem(item)
        list_widget.setMaximumHeight(40)
        list_widget.itemClicked.connect(lambda widget_item: self._handle_tristate_click(widget_item))
        list_widget.setToolTip(
            "☑ = Active (will be copied)\n▣ = Disabled (will be skipped)\n☐ = Delete (will be removed)"
        )

        return list_widget

    @staticmethod
    def _create_package_list_widget(packages, is_specific=False):
        widgets = []
        for package in packages:
            if is_specific and isinstance(package, dict):
                item_text = f"{package['package']}\n({package['session']})"
                is_disabled = package.get('disabled', False)
            elif isinstance(package, dict):
                item_text = package.get('name', str(package))
                is_disabled = package.get('disabled', False)
            else:
                item_text = str(package)
                is_disabled = False

            item = QListWidgetItem(item_text)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.PartiallyChecked if is_disabled else Qt.CheckState.Checked)

            list_widget = QListWidget()
            list_widget.addItem(item)
            list_widget.setMaximumHeight(60 if is_specific else 40)
            list_widget.setProperty("tristate_enabled", True)
            list_widget.setToolTip(
                "☑ = Active (will be installed)\n▣ = Disabled (will be skipped)\n☐ = Delete (will be removed)"
            )

            widgets.append(list_widget)
        return widgets

    @staticmethod
    def _handle_tristate_click(item):
        if not item:
            return

        current_state = item.checkState()
        if current_state == Qt.CheckState.Checked:
            item.setCheckState(Qt.CheckState.PartiallyChecked)
        elif current_state == Qt.CheckState.PartiallyChecked:
            item.setCheckState(Qt.CheckState.Unchecked)
        else:
            item.setCheckState(Qt.CheckState.Checked)

    def manage_packages(self, title, option_type, add_button_text, is_specific=False):
        self.current_option_type = option_type
        content_widget = QWidget()
        grid_layout = QGridLayout(content_widget)
        Options.load_config(Options.config_file_path)

        packages = getattr(Options, option_type, [])
        if not isinstance(packages, list):
            packages = []

        package_widgets = self._create_package_list_widget(packages, is_specific)

        for widget in package_widgets:
            widget.itemClicked.connect(lambda item: self._handle_tristate_click(item))

        for index, widget in enumerate(package_widgets):
            grid_layout.addWidget(widget, index // 5, index % 5)
        setattr(self, f"{option_type}_widgets", package_widgets)

        if package_widgets:
            row = (len(package_widgets) - 1) // 5 + 1
            select_all_checkbox = self._create_select_all_checkbox()
            select_all_checkbox.setText(f"Check/Uncheck All {option_type.replace('_', ' ').title()}")
            select_all_checkbox.setStyleSheet(get_current_style())
            grid_layout.addWidget(select_all_checkbox, row, 0, 1, 5)
            self._setup_packages_select_all(select_all_checkbox, package_widgets)

        dialog, layout = self.create_dialog(
            title, content_widget, lambda dlg: self.save_system_manager_options(dlg, option_type)
        )
        self._add_search_functionality(layout, package_widgets)
        self._add_package_management_buttons(layout, add_button_text, option_type, is_specific)
        dialog.exec()

    @staticmethod
    def _setup_packages_select_all(select_all_checkbox, package_widgets):

        def toggle_all_packages():
            checked = select_all_checkbox.checkState() == Qt.CheckState.Checked
            for list_widget_item in package_widgets:
                if list_widget_item.count() > 0:
                    item = list_widget_item.item(0)
                    if item:
                        item.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)

        def update_select_all_state():
            if not package_widgets:
                return

            checked_count = 0
            partially_checked_count = 0
            total_count = 0

            for list_widget_item in package_widgets:
                if list_widget_item.count() > 0:
                    item = list_widget_item.item(0)
                    if item:
                        total_count += 1
                        state = item.checkState()
                        if state == Qt.CheckState.Checked:
                            checked_count += 1
                        elif state == Qt.CheckState.PartiallyChecked:
                            partially_checked_count += 1

            if total_count == 0:
                return

            select_all_checkbox.blockSignals(True)
            if checked_count == total_count:
                select_all_checkbox.setCheckState(Qt.CheckState.Checked)
            elif checked_count + partially_checked_count == 0:
                select_all_checkbox.setCheckState(Qt.CheckState.Unchecked)
            else:
                select_all_checkbox.setCheckState(Qt.CheckState.PartiallyChecked)
            select_all_checkbox.blockSignals(False)

        select_all_checkbox.stateChanged.connect(toggle_all_packages)
        for list_widget in package_widgets:
            if list_widget.count() > 0:
                list_widget.itemChanged.connect(update_select_all_state)
        update_select_all_state()

    def _add_search_functionality(self, layout, package_widgets):
        search_layout = QHBoxLayout()
        search_input = QLineEdit()
        search_input.setPlaceholderText("Type to filter packages...")
        search_input.textChanged.connect(lambda text: self._filter_packages(text, package_widgets))
        search_layout.addWidget(search_input)
        layout.insertLayout(1, search_layout)

    def _add_package_management_buttons(self, layout, add_button_text, option_type, is_specific):
        add_package_button = QPushButton(add_button_text)
        add_package_button.clicked.connect(lambda: self.add_package(option_type))
        layout.insertWidget(2, add_package_button)

        if not is_specific:
            batch_button = QPushButton(f"Add '{option_type.replace('_', ' ').title()}' in Batches")
            batch_button.clicked.connect(lambda: self.batch_add_packages(option_type))
            layout.insertWidget(3, batch_button)

    @staticmethod
    def _filter_packages(search_text, package_widgets):
        for widget in package_widgets:
            if isinstance(widget, QListWidget) and widget.count() > 0:
                widget.setVisible(search_text.lower() in widget.item(0).text().lower())

    def edit_packages(self, option_type):
        is_specific = option_type == "specific_packages"
        title = f"Edit '{option_type.replace('_', ' ').title()}' [Uncheck to remove]"
        add_button_text = f"Add '{option_type.replace('_', ' ').title().replace(' Packages', ' Package')}'"
        self.manage_packages(title, option_type, add_button_text, is_specific)

    def add_package(self, option_type):
        self.close_current_dialog()
        if option_type == "specific_packages":
            self._add_specific_package()
        else:
            self._add_regular_package(option_type)

    def _add_regular_package(self, option_type):
        package_type_name = option_type.replace("_", " ").title().replace(" Packages", " Package")
        package_name, ok = QInputDialog.getText(
            self, f"Add '{package_type_name}'",
            f"                              Enter Package Name:                              "
        )

        if ok and package_name.strip():
            current_packages = getattr(Options, option_type, [])
            existing_names = [p.get('name') if isinstance(p, dict) else p for p in current_packages]

            if package_name.strip() not in existing_names:
                current_packages.append(package_name.strip())
                setattr(Options, option_type, current_packages)
                Options.save_config()
                QMessageBox.information(self, "Package Added", f"Package '{package_name}' has been successfully added!")
            else:
                QMessageBox.warning(self, "Duplicate Package", f"Package '{package_name}' already exists.")

        QTimer.singleShot(0, lambda: self.edit_packages(option_type))

    def batch_add_packages(self, option_type):
        self.close_current_dialog()

        dialog = QDialog(self)
        dialog.setWindowTitle(f"Add '{option_type.replace('_', ' ').title()}' in Batches")
        layout = QVBoxLayout(dialog)

        label = QLabel("Enter package names (one per line):")
        layout.addWidget(label)

        text_edit = QTextEdit()
        text_edit.setMinimumHeight(300)
        text_edit.setPlaceholderText("package1\npackage2\npackage3\n...")
        layout.addWidget(text_edit)

        button_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel) # type: ignore
        button_box.accepted.connect(dialog.accept)
        button_box.rejected.connect(dialog.reject)
        layout.addWidget(button_box)

        dialog.setMinimumSize(750, 550)
        dialog.resize(750, 600)

        if dialog.exec() == QDialog.DialogCode.Accepted:
            text = text_edit.toPlainText().strip()
            if text:
                packages = [pkg.strip() for pkg in text.splitlines() if pkg.strip()]
                current_packages = getattr(Options, option_type, [])
                existing_names = {p.get('name') if isinstance(p, dict) else p for p in current_packages}

                added_packages = []
                duplicates = []

                for package in packages:
                    if package not in existing_names:
                        added_packages.append(package)
                        existing_names.add(package)
                        current_packages.append(package)
                    else:
                        duplicates.append(package)

                setattr(Options, option_type, list(current_packages))
                Options.save_config()

                if duplicates:
                    dup_list = "\n".join(duplicates)
                    title = "Duplicate Package" if len(duplicates) == 1 else "Duplicate Packages"
                    plural = 's already exist' if len(duplicates) > 1 else ' already exists'
                    QMessageBox.warning(self, title, f"The following package{plural}:\n\n{dup_list}")

                if added_packages:
                    added_list = "\n".join(added_packages)
                    title = "Add Package" if len(added_packages) == 1 else "Add Packages"
                    message = (f"The following package{'s have been' if len(added_packages) > 1 else ' has been'} "
                               f"successfully added:\n\n{added_list}")
                    QMessageBox.information(self, title, message)

        QTimer.singleShot(0, lambda: self.edit_packages(option_type))

    def _add_specific_package(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("Add 'Specific Package'")
        layout = QVBoxLayout(dialog)
        field_height = 38

        form_layout = QFormLayout()
        package_input = QLineEdit()
        package_input.setFixedHeight(field_height)
        form_layout.addRow("Package Name:", package_input)

        session_combo = QComboBox()
        session_combo.setStyleSheet("color: #ffffff; background-color: #555582; padding: 5px 5px;")

        if SESSIONS:
            session_combo.addItems(SESSIONS)
        else:
            session_combo.addItem("No sessions available")
            session_combo.setEnabled(False)

        session_combo.setFixedHeight(field_height)
        form_layout.addRow("Session:", session_combo)
        layout.addLayout(form_layout)

        button_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel) # type: ignore
        button_box.accepted.connect(dialog.accept)
        button_box.rejected.connect(dialog.reject)
        layout.addWidget(button_box)
        dialog.setFixedWidth(650)

        if dialog.exec() == QDialog.DialogCode.Accepted:
            package_name = package_input.text().strip()
            session = session_combo.currentText()

            if not package_name:
                QMessageBox.warning(self, "Missing Information", "Package name is required.")
                return

            if not hasattr(Options, 'specific_packages') or Options.specific_packages is None:
                Options.specific_packages = []

            new_package = {'package': package_name, 'session': session}
            exists = any(isinstance(pkg, dict) and pkg.get('package') == package_name and pkg.get('session') == session
                         for pkg in Options.specific_packages)

            if not exists:
                Options.specific_packages.append(new_package)
                Options.save_config()
                QMessageBox.information(self, "Package Added",
                                        f"Package '{package_name}' for '{session}' successfully added!")
            else:
                QMessageBox.warning(self, "Duplicate Package",
                                    f"Package '{package_name}' for '{session}' already exists.")

            self.edit_packages("specific_packages")

    def save_system_manager_options(self, dialog=None, option_type=None):
        current_type = option_type or self.current_option_type
        options_changed = False

        if current_type == "system_manager_operations":
            selected_ops = [key for checkbox, key in self.system_manager_operations_widgets if checkbox.isChecked()]
            Options.system_manager_operations = selected_ops

        elif current_type == "system_files":
            new_files = []
            for list_widget in self.system_files_widgets:
                if list_widget.count() > 0:
                    item = list_widget.item(0)
                    if item:
                        check_state = item.checkState()
                        if check_state in [Qt.CheckState.Checked, Qt.CheckState.PartiallyChecked]:
                            original_data = item.data(Qt.ItemDataRole.UserRole)
                            if original_data and isinstance(original_data, dict):
                                file_entry = {
                                    'source': original_data.get('source', ''),
                                    'destination': original_data.get('destination', ''),
                                    'disabled': check_state == Qt.CheckState.PartiallyChecked
                                }
                                new_files.append(file_entry)

            if len(new_files) != len(Options.system_files or []):
                options_changed = True
            Options.system_files = new_files

        elif current_type in ["essential_packages", "additional_packages", "specific_packages"]:
            widgets = getattr(self, f"{current_type}_widgets", [])
            new_packages = []
            is_specific = (current_type == "specific_packages")

            for list_widget in widgets:
                if list_widget.count() > 0:
                    item = list_widget.item(0)
                    if item:
                        check_state = item.checkState()
                        if check_state in [Qt.CheckState.Checked, Qt.CheckState.PartiallyChecked]:
                            if is_specific:
                                item_text = item.text()
                                if '\n' in item_text:
                                    parts = item_text.split('\n', 1)
                                    if len(parts) >= 2:
                                        package_name = parts[0].strip()
                                        session_part = parts[1].strip('()')
                                        new_packages.append({
                                            'package': package_name,
                                            'session': session_part,
                                            'disabled': check_state == Qt.CheckState.PartiallyChecked
                                        })
                            else:
                                new_packages.append({
                                    'name': item.text(),
                                    'disabled': check_state == Qt.CheckState.PartiallyChecked
                                })

            original_packages = getattr(Options, current_type, [])
            if len(new_packages) != len(original_packages):
                options_changed = True
            setattr(Options, current_type, new_packages)

        Options.save_config()
        QMessageBox.information(self, "Options Saved", "Your changes have been saved.")

        if dialog:
            dialog.accept()

        if options_changed:
            if current_type == "system_files":
                QTimer.singleShot(0, self.edit_system_files)
            elif current_type in ["essential_packages", "additional_packages", "specific_packages"]:
                QTimer.singleShot(0, lambda: self.edit_packages(current_type))

    def go_back(self):
        self.close()
        if self.parent():
            self.parent().show()

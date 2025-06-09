import logging.handlers
from pathlib import Path
from PyQt6.QtCore import Qt
from global_style import global_style
from linux_distro_helper import LinuxDistroHelper
from options import Options, SESSIONS, USER_SHELL
from PyQt6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QGridLayout, QFormLayout, QLabel, QPushButton, QWidget,
                             QComboBox, QCheckBox, QListWidget, QListWidgetItem, QScrollArea, QDialogButtonBox, QMessageBox,
                             QFileDialog, QInputDialog, QLineEdit, QApplication, QSizePolicy)

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

if not logger.handlers:
    handler = logging.StreamHandler()
    formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
    handler.setFormatter(formatter)
    logger.addHandler(handler)


# noinspection PyUnresolvedReferences
class PackageInstallerOptions(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Package Installer Options")
        self.current_option_type = None
        self.distro_helper = LinuxDistroHelper()
        self._initialize_attributes()
        self.system_files_widgets = []
        self.original_system_files = []
        self.setup_ui()

    def _initialize_attributes(self):
        self.distro_name = self.distro_helper.distro_pretty_name
        self.session = self.distro_helper.detect_session()
        self.install_package_command = self.distro_helper.get_pkg_install_cmd("")

        self._create_labels()
        self._create_buttons()
        self._create_combo_boxes()

        self.system_files_widgets = []
        self.installer_operations_widgets = []
        self.essential_packages_widgets = []
        self.additional_packages_widgets = []
        self.specific_packages_widgets = []

        self.current_option_type = None
        self.original_system_files = []
        self._last_shell = None

    def _create_labels(self):
        self.top_label = QLabel(self._get_top_label_text())
        self.top_label.setWordWrap(True)
        self.top_label.setSizePolicy(QSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred))

    def _create_buttons(self):
        self.installer_operations_button = QPushButton("Installer Operations")
        self.system_files_button = QPushButton("System Files")
        self.package_buttons = {
            "essential_packages": QPushButton("Essential Packages"),
            "additional_packages": QPushButton("Additional Packages"),
            "specific_packages": QPushButton("Specific Packages")
        }
        self.close_button = QPushButton("Close")

    def _create_combo_boxes(self):
        self.shell_combo = QComboBox()
        self.shell_combo.addItems(USER_SHELL)
        idx = USER_SHELL.index(Options.user_shell) if Options.user_shell in USER_SHELL else 0
        self.shell_combo.setCurrentIndex(idx)
        self._last_shell = USER_SHELL[idx]

    def _get_top_label_text(self):
        return (f"\nFirst you can select 'System Files' in Package Installer. These files will be copied using 'sudo', "
                f"for root privilege. If you have 'System Files' selected, Package Installer will copy these first. This "
                f"allows you to copy files such as 'pacman.conf' or 'smb.conf' to '/etc/'.\n\nUnder 'Installer Operations' you can specify how you would like to proceed. "
                f"Each action is executed one after the other. Uncheck actions to disable them.\n\n\nTips:\n\n"
                f"It is possible to copy to and from samba shares if samba is set up correctly. Source and/or destination must be saved as follows:\n\n"
                f"'smb://ip/rest of path'\n\nExample: 'smb://192.168.0.53/rest of smb share path'\n\n"
                f"\n'Essential Packages' will be installed using '{self.install_package_command}PACKAGE'.\n\n'Additional Packages' provides access to the Arch User Repository. "
                f"Therefore 'yay' must and will be installed. This feature is available only on Arch Linux based distributions.\n\nYou can also define 'Specific Packages'. These packages will be installed only\n(using '{self.install_package_command}PACKAGE') "
                f"if the corresponding session has been recognized.\nBoth full desktop environments and window managers such as 'Hyprland' and others are supported.\n")

    def setup_ui(self):
        layout = QVBoxLayout(self)
        self._add_distro_info(layout)
        layout.addWidget(self.top_label)
        self._add_button_layouts(layout)
        self._add_shell_selection(layout)
        self._connect_signals()
        layout.addWidget(self.close_button)
        self.setFixedSize(1100, 800)

    def _add_distro_info(self, layout):
        yay_info = ""
        if self.distro_helper.has_aur:
            yay_info = " | AUR Helper: 'yay' detected" if self.distro_helper.package_is_installed('yay') else " | AUR Helper: 'yay' not detected"
        self.distro_label = QLabel(f"Recognized Linux distribution: {self.distro_name} | Session: {self.session}{yay_info}")
        self.distro_label.setStyleSheet("color: lightgreen")
        layout.addWidget(self.distro_label)

    def _add_button_layouts(self, layout):
        hbox1_buttons = QHBoxLayout()
        hbox1_buttons.addWidget(self.installer_operations_button)
        hbox1_buttons.addWidget(self.system_files_button)
        layout.addLayout(hbox1_buttons)
        hbox2_buttons = QHBoxLayout()
        for button in self.package_buttons.values():
            hbox2_buttons.addWidget(button)
        layout.addLayout(hbox2_buttons)

    def _add_shell_selection(self, layout):
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
        self.installer_operations_button.clicked.connect(self.installer_operations)
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

    def installer_operations(self):
        self.current_option_type = "installer_operations"
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
        select_all_checkbox.setStyleSheet(f"{global_style} QCheckBox {{color: '#6ffff5'}}")
        return select_all_checkbox

    def _create_operation_checkboxes(self, grid_layout):
        arch_only_operations = {'update_mirrors', 'install_yay', 'install_additional_packages'}
        package_installer_operation_text = Options.get_package_installer_operation_text(self.distro_helper)
        operations = [(text.replace("<br>", "\n"), key) for key, text in package_installer_operation_text.items()]
        self.installer_operations_widgets = []
        for index, (label, operation_key) in enumerate(operations):
            checkbox = QCheckBox(label)
            if operation_key in arch_only_operations and not self.distro_helper.supports_aur():
                self._configure_disabled_checkbox(checkbox, operation_key)
            else:
                self._configure_enabled_checkbox(checkbox, operation_key)
            grid_layout.addWidget(checkbox, index, 0)
            self.installer_operations_widgets.append((checkbox, operation_key))

    @staticmethod
    def _configure_disabled_checkbox(checkbox, operation_key):
        checkbox.setChecked(False)
        checkbox.setEnabled(False)
        checkbox.setStyleSheet(f"{global_style} QCheckBox {{ color: #666666; }}")

        if operation_key == 'update_mirrors':
            checkbox.setToolTip("Mirror update is available on Arch Linux only")
        elif operation_key in ['install_yay', 'install_additional_packages']:
            checkbox.setToolTip("Available only on Arch Linux based distributions")

    @staticmethod
    def _configure_enabled_checkbox(checkbox, operation_key):
        checkbox.setChecked(operation_key in Options.installer_operations)
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
            for operation_checkbox, key in self.installer_operations_widgets:
                if key not in ['install_yay', 'install_additional_packages'] and operation_checkbox.isEnabled():
                    operation_checkbox.setChecked(checked)

            if install_additional_packages_checkbox and install_additional_packages_checkbox.isEnabled():
                install_additional_packages_checkbox.setChecked(checked)

        def update_select_all_state():
            enabled_checkboxes = [
                cb for cb, key in self.installer_operations_widgets
                if cb.isEnabled() and not (key == 'install_yay' and
                                           install_additional_packages_checkbox and install_additional_packages_checkbox.isChecked())
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
        for checkbox, option_key in self.installer_operations_widgets:
            checkbox.stateChanged.connect(update_select_all_state)
            if option_key == 'install_additional_packages':
                checkbox.stateChanged.connect(handle_dependencies)

        handle_dependencies()
        update_select_all_state()

    def _get_checkbox_by_key(self, key):
        return next((cb for cb, k in self.installer_operations_widgets if k == key), None)

    def _show_operations_dialog(self, content_widget):
        dialog = QDialog(self)
        dialog.setWindowTitle("Package Installer Operations")
        layout = QVBoxLayout(dialog)
        layout.addWidget(content_widget)

        button_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Close)
        button_box.accepted.connect(lambda: self.save_installer_options())
        button_box.rejected.connect(dialog.reject)
        layout.addWidget(button_box)

        button_box.button(QDialogButtonBox.StandardButton.Close).setFocus()
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

        button_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Close)
        if button_callback:
            button_box.accepted.connect(lambda: button_callback(dialog))
        button_box.rejected.connect(dialog.reject)
        layout.addWidget(button_box)

        self._adjust_dialog_size(dialog, content_widget, scroll_area)
        button_box.button(QDialogButtonBox.StandardButton.Close).setFocus()
        return dialog, layout

    def close_current_dialog(self):
        for widget in QApplication.topLevelWidgets():
            if isinstance(widget, QDialog) and widget.isModal() and widget != self:
                widget.accept()
                return

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

    @staticmethod
    def _resize_listwidget_to_contents(listwidget, min_width=120, extra=36):
        fm = listwidget.fontMetrics()
        max_width = min_width
        height = 0

        for i in range(listwidget.count()):
            item = listwidget.item(i)
            if item is None:
                continue
            text = item.text()
            line_widths = [fm.horizontalAdvance(line) for line in text.splitlines()]
            max_width = max(max_width, max(line_widths, default=0) + extra)
            height += fm.lineSpacing() * (len(text.splitlines()) or 1) + 6

        listwidget.setMinimumWidth(max_width + 8)
        listwidget.setMinimumHeight(height + 12)

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
            lambda dlg: self.save_installer_options(dlg, "system_files")
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

            self.original_system_files.append({'source': file_source, 'destination': file_destination})

            list_widget = self._create_file_list_widget(file_source, file_destination)

            display_text = f"{file_source}  --->  {file_destination}"
            display_text = self._apply_text_replacements(display_text)
            text_width = list_widget.fontMetrics().horizontalAdvance(display_text)
            max_text_width = max(max_text_width, text_width)

            grid_layout.addWidget(list_widget, file_index, 0)
            self.system_files_widgets.append(list_widget)

        for list_widget in self.system_files_widgets:
            list_widget.setMinimumWidth(max_text_width)

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
        try:
            box = QMessageBox(self)
            box.setWindowTitle("Add 'System Files'")
            box.setText("Would you like to add a single file or an entire directory?")
            file_button = box.addButton("File", QMessageBox.ButtonRole.YesRole)
            dir_button = box.addButton("Directory", QMessageBox.ButtonRole.NoRole)
            cancel_button = box.addButton(QMessageBox.StandardButton.Cancel)

            box.exec()

            clicked_button = box.clickedButton()

            if clicked_button == cancel_button:
                return

            if clicked_button == file_button:
                files, _ = QFileDialog.getOpenFileNames(self, "Select 'System File'")
                if not files:
                    return
                sources = files
            else:
                directory = QFileDialog.getExistingDirectory(self, "Select 'System Directory'")
                if not directory:
                    return
                sources = [directory]

            destination_dir = QFileDialog.getExistingDirectory(self, "Select Destination Directory")
            if not destination_dir:
                return

            self._process_new_system_files(sources, destination_dir)

        except Exception as e:
            QMessageBox.critical(self, "Error", f"An error occurred when adding the System File: {str(e)}", QMessageBox.StandardButton.Ok)

    def _process_new_system_files(self, sources, destination_dir):
        if not hasattr(Options, 'system_files') or Options.system_files is None:
            Options.system_files = []
        elif not isinstance(Options.system_files, list):
            Options.system_files = []

        added_items = []
        for source in sources:
            try:
                source_path = Path(source)

                if not source_path.exists():
                    logger.warning(f"Source path does not exist: {source}")
                    continue

                source_str = str(source_path.resolve())

                if source_path.is_file():
                    destination = str(Path(destination_dir) / source_path.name)
                    item_name = source_path.name
                else:
                    destination = str(Path(destination_dir) / source_path.name)
                    item_name = f"{source_path.name}/ (directory)"

                if not any(isinstance(item, dict) and str(item.get('source', '')) == source_str for item in Options.system_files):
                    Options.system_files.append({'source': source_str, 'destination': destination})
                    added_items.append(item_name)
            except Exception as file_error:
                logger.error(f"Error when processing {source}: {file_error}")
                continue

        self._handle_added_files_result(added_items)

    def _handle_added_files_result(self, added_items):
        if added_items:
            Options.save_config()
            item_list = '\n'.join(added_items)
            msg = (f"The following items have been successfully added:\n{item_list}"
                   if len(added_items) > 1 else f"'{added_items[0]}' was successfully added!")
            QMessageBox.information(self, "'System Files'", msg, QMessageBox.StandardButton.Ok)
            self.edit_system_files()
        else:
            QMessageBox.information(self, "No changes", "No new items have been added.", QMessageBox.StandardButton.Ok)

    def _create_file_list_widget(self, file_source, file_destination):
        item_text = f"{file_source}  --->  {file_destination}"
        display_text = self._apply_text_replacements(item_text)

        item = QListWidgetItem(display_text)
        item.setData(Qt.ItemDataRole.UserRole, {'source': file_source, 'destination': file_destination})
        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
        item.setCheckState(Qt.CheckState.Checked)

        list_widget = QListWidget()
        list_widget.addItem(item)
        list_widget.setMaximumHeight(40)

        return list_widget

    @staticmethod
    def _create_package_list_widget(packages, is_specific=False):
        widgets = []
        for package in packages:
            if is_specific and isinstance(package, dict):
                package_name = package['package']
                session = package['session']
                item_text = f"{package_name}\n({session})"
            else:
                item_text = package

            item = QListWidgetItem(item_text)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked)

            list_widget = QListWidget()
            list_widget.addItem(item)
            list_widget.setMaximumHeight(40 if not is_specific else 60)
            widgets.append(list_widget)

        return widgets

    def manage_packages(self, title, option_type, add_button_text, is_specific=False):
        self.current_option_type = option_type
        content_widget = QWidget()
        grid_layout = QGridLayout(content_widget)

        Options.load_config(Options.config_file_path)
        packages = getattr(Options, option_type, [])

        if not isinstance(packages, list):
            packages = []
            setattr(Options, option_type, packages)

        package_widgets = self._create_package_list_widget(packages, is_specific)

        for index, widget in enumerate(package_widgets):
            grid_layout.addWidget(widget, index // 5, index % 5)

        setattr(self, f"{option_type}_widgets", package_widgets)

        dialog, layout = self.create_dialog(
            title,
            content_widget,
            lambda dlg: self.save_installer_options(dlg, option_type)
        )

        self._add_search_functionality(layout, package_widgets)

        self._add_package_management_buttons(layout, add_button_text, option_type, is_specific)

        dialog.exec()

    def _add_search_functionality(self, layout, package_widgets):
        search_layout = QHBoxLayout()
        search_label = QLabel("Search:")
        search_input = QLineEdit()
        search_input.setPlaceholderText("Type to filter packages...")
        search_input.textChanged.connect(lambda text: self._filter_packages(text, package_widgets))

        search_layout.addWidget(search_label)
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
        search_text = search_text.lower()
        for widget in package_widgets:
            if isinstance(widget, QListWidget) and widget.count() > 0:
                item = widget.item(0)
                widget.setVisible(search_text in item.text().lower())

    def edit_packages(self, option_type):
        Options.load_config(Options.config_file_path)
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
            if package_name not in current_packages:
                current_packages.append(package_name.strip())
                setattr(Options, option_type, current_packages)
                Options.save_config()
                QMessageBox.information(self, "Package Added", f"Package '{package_name}' successfully added!",
                                        QMessageBox.StandardButton.Ok)
            else:
                QMessageBox.warning(self, "Duplicate Package", f"Package '{package_name}' already exists.",
                                    QMessageBox.StandardButton.Ok)
            self.edit_packages(option_type)

    def batch_add_packages(self, option_type):
        self.close_current_dialog()
        text, ok = QInputDialog.getMultiLineText(self, f"Add '{option_type.replace('_', ' ').title()}' in Batches", "                         Enter package names (one per line):                         ")
        if ok and text.strip():
            packages = [pkg.strip() for pkg in text.splitlines() if pkg.strip()]
            current_packages = set(getattr(Options, option_type, []))
            added_packages = []
            duplicates = []
            for package in packages:
                if package not in current_packages:
                    added_packages.append(package)
                    current_packages.add(package)
                else:
                    duplicates.append(package)
            setattr(Options, option_type, list(current_packages))
            Options.save_config()
            if duplicates:
                dup_list = "\n".join(duplicates)
                title = "Duplicate Package" if len(duplicates) == 1 else "Duplicate Packages"
                plural = 's already exist' if len(duplicates) > 1 else ' already exists'
                QMessageBox.warning(self, title, f"The following package{plural}:\n\n{dup_list}", QMessageBox.StandardButton.Ok)
            if added_packages:
                added_list = "\n".join(added_packages)
                title = "Add Package" if len(added_packages) == 1 else "Add Packages"
                message = f"The following package{'s have' if len(added_packages) > 1 else ' was'} successfully added:\n\n{added_list}"
                QMessageBox.information(self, title, message, QMessageBox.StandardButton.Ok)
            self.edit_packages(option_type)

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
        button_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        button_box.accepted.connect(dialog.accept)
        button_box.rejected.connect(dialog.reject)
        layout.addWidget(button_box)
        dialog.setFixedWidth(650)
        result = dialog.exec()
        if result == QDialog.DialogCode.Accepted:
            package_name = package_input.text().strip()
            session = session_combo.currentText()
            if not package_name:
                QMessageBox.warning(self, "Missing Information", "Package name is required.", QMessageBox.StandardButton.Ok)
                return

            if not hasattr(Options, 'specific_packages') or Options.specific_packages is None:
                Options.specific_packages = []

            new_package = {'package': package_name, 'session': session}
            exists = any(isinstance(pkg, dict) and pkg.get('package') == package_name and pkg.get('session') == session for pkg
                in Options.specific_packages)
            if not exists:
                Options.specific_packages.append(new_package)
                Options.save_config()
                QMessageBox.information(self, "Package Added", f"Package '{package_name}' for '{session}' successfully added!", QMessageBox.StandardButton.Ok)
            else:
                QMessageBox.warning(self, "Duplicate Package", f"Package '{package_name}' for '{session}' already exists.", QMessageBox.StandardButton.Ok)
            self.edit_packages("specific_packages")

    def _get_checked_items(self, widgets, option_type):
        if option_type == "system_files":
            return self._get_system_files_from_widgets()
        elif option_type == "specific_packages":
            return self._get_specific_packages_from_widgets()
        else:
            return self._get_checked_items_from_widgets(widgets)

    @staticmethod
    def _get_checked_items_from_widgets(widget_list):
        return [widget.item(i).text() for widget in widget_list if isinstance(widget, QListWidget)
                for i in range(widget.count())
                if widget.item(i).checkState() == Qt.CheckState.Checked]

    def _get_system_files_from_widgets(self):
        files = []
        for widget in self.system_files_widgets:
            if isinstance(widget, QListWidget):
                for i in range(widget.count()):
                    item = widget.item(i)
                    if item and item.checkState() == Qt.CheckState.Checked:
                        original_data = item.data(Qt.ItemDataRole.UserRole)
                        if original_data and isinstance(original_data, dict):
                            source = original_data.get('source', '')
                            destination = original_data.get('destination', '')
                            if source and destination:
                                files.append({'source': source, 'destination': destination})
        return files

    def _get_specific_packages_from_widgets(self):
        packages = []
        if not hasattr(self, 'specific_packages_widgets'):
            return packages

        for widget in self.specific_packages_widgets:
            if isinstance(widget, QListWidget):
                for i in range(widget.count()):
                    item = widget.item(i)
                    if item and item.checkState() == Qt.CheckState.Checked:
                        item_text = item.text()
                        if '\n' in item_text:
                            parts = item_text.split('\n', 1)
                            if len(parts) >= 2:
                                package_name = parts[0].strip()
                                session_part = parts[1].strip('()')
                                packages.append({'package': package_name, 'session': session_part})
                        else:
                            parts = item_text.partition('(')
                            package_name = parts[0].strip()
                            session = parts[2].partition(')')[0].strip() if parts[1] else ""
                            if package_name:
                                packages.append({'package': package_name, 'session': session})
        return packages

    def save_installer_options(self, dialog=None, option_type=None):
        try:
            current_type = option_type or self.current_option_type
            if current_type == "installer_operations":
                if not hasattr(self, 'installer_operations_widgets'):
                    QMessageBox.warning(self, "Error", "No installer operations widgets found.")
                    return False
                updated_list = [option_key for checkbox, option_key in self.installer_operations_widgets if
                                checkbox.isChecked()]
            else:
                widget_list = getattr(self, f"{current_type}_widgets", [])
                updated_list = self._get_checked_items(widget_list, current_type)
            setattr(Options, current_type, updated_list)
            Options.save_config()
            QMessageBox.information(self, "Saved", "Settings have been successfully saved!", QMessageBox.StandardButton.Ok)
            if dialog and option_type:
                dialog.accept()
                if option_type == "installer_operations":
                    self.installer_operations()
                elif option_type == "system_files":
                    self.edit_system_files()
                elif option_type in ["essential_packages", "additional_packages", "specific_packages"]:
                    self.edit_packages(option_type)
            return True
        except Exception as e:
            QMessageBox.critical(self, "Error", f"An error occurred while saving: {str(e)}", QMessageBox.StandardButton.Ok)
            return False

    def go_back(self):
        self.close()
        self.parent().show()

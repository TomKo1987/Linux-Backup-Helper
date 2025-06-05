from pathlib import Path
from options import Options, SESSIONS
from sudo_password import SecureString
from linux_distro_helper import LinuxDistroHelper
from PyQt6.QtGui import QTextCursor, QColor, QIcon
from PyQt6.QtCore import Qt, pyqtSignal, QThread, QElapsedTimer, QTimer
import ast, getpass, os, pwd, shutil, socket, subprocess, tempfile, threading, time, urllib.error, urllib.request
from PyQt6.QtWidgets import (QVBoxLayout, QHBoxLayout, QPushButton, QListWidgetItem, QApplication, QListWidget, QWidget, QCheckBox, QTextEdit, QGraphicsDropShadowEffect, QDialogButtonBox, QDialog, QLabel, QScrollArea)

user = pwd.getpwuid(os.getuid()).pw_name
home_user = os.getenv("HOME")
home_config = Path(home_user).joinpath(".config")


# noinspection PyUnresolvedReferences
class PackageInstallerLauncher:
    def __init__(self, parent=None):
        self.parent = parent
        self.config = getattr(parent, 'config', {}) if parent else {}
        from drive_manager import DriveManager
        self.drive_manager = DriveManager()
        self.failed_attempts = getattr(parent, 'failed_attempts', 0)
        self.package_installer_thread = None
        self.package_installer_dialog = None
        self.sudo_checkbox = None

    def launch(self):
        if self.parent:
            self.parent.hide()
        try:
            self.confirm_and_start_package_installer()
        finally:
            if self.parent:
                self.parent.show()

    def confirm_and_start_package_installer(self):
        installer_operations = self.config.get('installer_operations', [])
        _, _, installer_tooltips = Options.generate_tooltip()
        distro_helper = LinuxDistroHelper()
        package_installer_operation_text = Options.get_package_installer_operation_text(distro_helper)
        operations_text = {k: v.replace("&&", "&") for k, v in package_installer_operation_text.items()}
        dialog, content_widget, content_layout = self._create_installer_dialog()
        self._display_operations(installer_operations, operations_text, installer_tooltips, content_layout)
        if self._show_dialog_and_get_result(dialog, content_widget):
            self._handle_dialog_accepted(installer_operations)

    @staticmethod
    def _create_installer_dialog():
        dialog = QDialog()
        dialog.setWindowTitle('Package Installer')
        layout = QVBoxLayout()
        content_widget = QWidget()
        content_layout = QVBoxLayout(content_widget)
        header_label = QLabel("<span style='font-size: 18px;'>Package Installer will perform the following operations:<br></span>")
        header_label.setTextFormat(Qt.TextFormat.RichText)
        content_layout.addWidget(header_label)
        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        scroll_area.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        scroll_area.setWidget(content_widget)
        layout.addWidget(scroll_area)
        dialog.setLayout(layout)
        return dialog, content_widget, content_layout

    def _display_operations(self, installer_operations, operations_text, installer_tooltips, content_layout):
        for i, opt in enumerate(installer_operations):
            if opt in operations_text:
                has_tooltip = opt in installer_tooltips and installer_tooltips[opt]
                self._add_operation_row(i, operations_text[opt], has_tooltip, installer_tooltips.get(opt, ""), content_layout)

    @staticmethod
    def _add_operation_row(index, text, has_tooltip, tooltip_text, layout):
        style_color = "#9891c2;" if has_tooltip else "#c8beff;"
        text_style = "text-decoration: underline dotted;" if has_tooltip else ""
        tooltip_icon = "💡" if has_tooltip else ""
        operation_text = f"{tooltip_icon}<span style='font-size: 16px; padding: 5px; color: {style_color}{text_style}'>{text}</span>"
        row_layout = QHBoxLayout()
        number_label = QLabel(f"{index + 1}:")
        number_label.setStyleSheet("font-size: 16px; padding: 5px; qproperty-alignment: 'AlignLeft'")
        operation_label = QLabel(operation_text)
        operation_label.setTextFormat(Qt.TextFormat.RichText)
        operation_label.setStyleSheet("font-size: 16px; padding: 5px; qproperty-alignment: 'AlignLeft'")
        if has_tooltip:
            operation_label.setToolTip(tooltip_text)
            operation_label.setCursor(Qt.CursorShape.WhatsThisCursor)
            operation_label.setToolTipDuration(30000)
        row_layout.addWidget(number_label)
        row_layout.addWidget(operation_label)
        row_layout.addStretch(1)
        layout.addLayout(row_layout)

    def _show_dialog_and_get_result(self, dialog, content_widget):
        confirm_label = QLabel("<span style='font-size: 16px;'>Start Package Installer?<br>(Check 'Enter sudo password' if a sudo password is set.)<br></span>")
        button_layout = QHBoxLayout()
        button_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Yes | QDialogButtonBox.StandardButton.No)
        button_box.button(QDialogButtonBox.StandardButton.Yes).setText('Yes')
        button_box.button(QDialogButtonBox.StandardButton.No).setText('No')
        button_box.accepted.connect(dialog.accept)
        button_box.rejected.connect(dialog.reject)
        self.sudo_checkbox = QCheckBox("Enter sudo password")
        self.sudo_checkbox.setStyleSheet("font-size: 16px; color: #6ffff5")
        if self.failed_attempts != 0:
            self.sudo_checkbox.setText("Sudo password must be entered!")
            self.sudo_checkbox.setChecked(True)
            self.sudo_checkbox.setEnabled(False)
            self.sudo_checkbox.setStyleSheet("color: #787878")
        button_layout.addWidget(self.sudo_checkbox)
        button_layout.addWidget(button_box)
        content_widget.layout().addWidget(confirm_label)
        content_widget.layout().addLayout(button_layout)
        screen_geometry = QApplication.primaryScreen().availableGeometry()
        content_size = content_widget.sizeHint()
        dialog.resize(min(content_size.width() + 40, screen_geometry.width()), min(content_size.height() + 40, screen_geometry.height()))
        button_box.button(QDialogButtonBox.StandardButton.No).setFocus()
        return dialog.exec() == QDialog.DialogCode.Accepted

    def start_package_installer_thread(self, sudo_password):
        self.package_installer_thread = PackageInstallerThread(sudo_password)
        self.package_installer_dialog = PackageInstallerDialog(self.parent)
        self.package_installer_thread.started.connect(self.show_package_installer_dialog)
        self.package_installer_thread.passwordFailed.connect(self.on_password_failed)
        self.package_installer_thread.passwordSuccess.connect(self.on_password_success)
        self.package_installer_thread.outputReceived.connect(self.package_installer_dialog.update_operation_dialog)
        self.package_installer_thread.taskStatusChanged.connect(self.package_installer_dialog.update_task_checklist_status)
        self.package_installer_thread.finished.connect(self.on_package_installer_finished)
        self.package_installer_thread.start()

    def _handle_dialog_accepted(self, installer_operations):
        if "copy_system_files" in installer_operations:
            system_files = self.config.get('system_files', [])
            paths_to_check = []
            for file in system_files:
                if isinstance(file, dict):
                    for key in ('source', 'destination'):
                        if key in file:
                            paths_to_check.append(file[key])
            drives_to_mount = self.drive_manager.check_drives_to_mount(paths_to_check)
            if drives_to_mount and not self.drive_manager.mount_required_drives(drives_to_mount, self.parent):
                return
        if self.sudo_checkbox.isChecked():
            self.show_sudo_password_dialog()
        else:
            self.start_package_installer_thread("")

    def show_package_installer_dialog(self):
        try:
            self.package_installer_dialog.exec()
        finally:
            self.drive_manager.unmount_drives()

    def show_sudo_password_dialog(self):
        from sudo_password import SudoPasswordDialog, SecureString
        dialog = SudoPasswordDialog(self.parent)
        dialog.sudo_password_entered.connect(self.on_sudo_password_entered)
        dialog.update_failed_attempts(self.failed_attempts)
        dialog.exec()

    def on_sudo_password_entered(self, sudo_password):
        self.start_package_installer_thread(sudo_password)

    def on_password_failed(self):
        self.failed_attempts += 1
        if self.parent:
            self.parent.failed_attempts = self.failed_attempts

        if self.package_installer_dialog:
            self.package_installer_dialog.update_failed_attempts(self.failed_attempts)
            self.package_installer_dialog.auth_failed = True
            error_msg = ("<p style='color: #ff4a4d; font-size: 18px; font-weight: bold;'>"
                         "<br>Authentication failed. Canceling process to prevent account lockout."
                         "<br>This could be due to:"
                         "<ul>"
                         "<li>Incorrect or missing password</li>"
                         "<li>Password is unauthorized</li>"
                         "<li>User not in sudoers file</li>"
                         "<li>Sudo configuration issue</li>"
                         "</ul>"
                         "Package Installer has been aborted to protect your system."
                         "</p>")
            self.package_installer_dialog.update_operation_dialog(error_msg)
            self.package_installer_dialog.completed_message_shown = True
            self.package_installer_dialog.update_timer.stop()
            self.package_installer_dialog.has_error = True
            self.package_installer_dialog.ok_button.setEnabled(True)

        if self.package_installer_thread:
            self.package_installer_thread.terminated = True
            self.package_installer_thread.quit()
            try:
                if not self.package_installer_thread.wait(2000):
                    self.package_installer_thread.terminate()
                    self.package_installer_thread.wait(1000)
            except (RuntimeError, AttributeError):
                pass

    def on_password_success(self):
        self.failed_attempts = 0
        if self.parent:
            self.parent.failed_attempts = 0
        if self.package_installer_dialog:
            self.package_installer_dialog.update_failed_attempts(self.failed_attempts)
            self.package_installer_dialog.auth_failed = False

    def on_package_installer_finished(self):
        self.package_installer_thread = None
        self.package_installer_dialog = None


class StyleConfig:
    FONT_MAIN = "DejaVu Sans Mono"
    FONT_SUBPROCESS = "Hack"

    COLORS = {
        'primary': '#7aa2f7',
        'success': '#8fffab',
        'warning': '#e0af68',
        'error': '#ff5555',
        'info': '#7dcfff',
        'text': '#c0caf5',
        'background_primary': '#1a1b26',
        'background_secondary': '#24283b',
        'background_gradient_start': '#11141d',
        'background_gradient_end': '#222a3b',
        'border': '#414868',
        'muted': '#7c7c7c'
    }

    STYLE_MAP = {
        "operation": (FONT_MAIN, 16, "#6ffff5", 1.2),
        "info": (FONT_MAIN, 15, "#ceec9e", 1.0),
        "subprocess": (FONT_SUBPROCESS, 13, "#f9e7ff", 0.6),
        "success": (FONT_MAIN, 15, "#8fffab", 1.0),
        "warning": (FONT_MAIN, 15, "#ffaa00", 1.0),
        "error": (FONT_MAIN, 15, "#ff5555", 1.0)
    }

    @classmethod
    def get_style_string(cls, style_name):
        if style_name not in cls.STYLE_MAP:
            return ""

        font, size, color, line_height = cls.STYLE_MAP[style_name]
        return f"font-family: {font}; font-size: {size}px; color: {color}; padding: 5px; line-height: {line_height};"


class TaskStatus:
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    SUCCESS = "success"
    WARNING = "warning"
    ERROR = "error"


# noinspection PyUnresolvedReferences
class PackageInstallerDialog(QDialog):
    outputReceived = pyqtSignal(str, str)
    DIALOG_SIZE = (1400, 1100)
    CHECKLIST_WIDTH = 370
    BUTTON_SIZE = (145, 40)
    SHADOW_BLUR = 80
    SHADOW_OFFSET = 15

    def __init__(self, parent=None):
        super().__init__(parent)
        self._init_attributes()
        self._setup_ui_components()
        self.installer_thread = None
        self.current_task = None
        self.completed_message_shown = False
        self.has_error = False
        self.auth_failed = False
        self.setup_ui()

    def _init_attributes(self):
        self.task_status = {}
        self.task_descriptions = {}
        self.update_timer = QTimer(self)
        self.timer = QElapsedTimer()

    def _setup_ui_components(self):
        self.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
        self.setFixedSize(*self.DIALOG_SIZE)

        self.shadow = self._create_shadow_effect()

        self.layout = QHBoxLayout(self)
        self.left_panel = QVBoxLayout()
        self.right_panel = QVBoxLayout()

        self.scroll_area = QScrollArea()
        self.text_edit = QTextEdit()
        self.failed_attempts_label = QLabel(self)

        self.checklist_label = QLabel("Pending Operations:")
        self.checklist = QListWidget()
        self.elapsed_time_label = QLabel("\nElapsed time:\n00s\n")
        self.ok_button = QPushButton("Close")

    def _create_shadow_effect(self):
        shadow = QGraphicsDropShadowEffect()
        shadow.setBlurRadius(self.SHADOW_BLUR)
        shadow.setXOffset(self.SHADOW_OFFSET)
        shadow.setYOffset(self.SHADOW_OFFSET)
        shadow.setColor(QColor(0, 0, 0, 160))
        return shadow

    def setup_ui(self):
        self._apply_global_styles()
        self._configure_scroll_area()
        self._configure_text_edit()
        self._configure_checklist()
        self._configure_elapsed_time()
        self._configure_ok_button()
        self._setup_timers()
        self._setup_layout()

    def _apply_global_styles(self):
        colors = StyleConfig.COLORS
        self.setStyleSheet(f"""
            QTextEdit {{
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0, 
                           stop:0 {colors['background_gradient_start']}, 
                           stop:1 {colors['background_gradient_end']});
                color: {colors['text']};
                border: none;
                border-radius: 8px;
            }}
        """)

    def _configure_scroll_area(self):
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll_area.setWidget(self.text_edit)

    def _configure_text_edit(self):
        self.text_edit.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        self.text_edit.setReadOnly(True)
        self.text_edit.setHtml(
            "<p style='color: #55ff55; font-size: 20px; text-align: center; margin-top: 25px;'>"
            "<b>Package Installer</b><br>Initialization completed. Starting Package Installer</p>"
        )

    def _configure_checklist(self):
        colors = StyleConfig.COLORS
        border_style = self._get_border_style()

        self.checklist_label.setStyleSheet(f"""
            color: {colors['info']};
            font-size: 18px;
            font-weight: bold;
            padding: 10px;
            background: qlineargradient(x1:0, y1:0, x2:1, y2:0, 
                       stop:0 {colors['background_secondary']}, 
                       stop:1 {colors['border']});
            {border_style}
        """)

        self.checklist.setStyleSheet(f"""
            QListWidget {{
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0, 
                           stop:0 {colors['background_secondary']}, 
                           stop:1 {colors['border']});
                font-size: 15px;
                padding: 4px;
                {border_style}
            }}
            QListWidget::item {{
                padding: 4px;
                border-radius: 4px;
                border: 1px solid transparent;
            }}
        """)
        self.checklist.setFixedWidth(self.CHECKLIST_WIDTH)

    def _configure_elapsed_time(self):
        colors = StyleConfig.COLORS
        self.elapsed_time_label.setGraphicsEffect(self.shadow)
        self.elapsed_time_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.elapsed_time_label.setStyleSheet(f"""
            color: {colors['info']};
            font-size: 17px;
            {self._get_border_style()}
            background: qlineargradient(x1:0, y1:0, x2:1, y2:0, 
                       stop:0 {colors['background_secondary']}, 
                       stop:1 {colors['border']});
            text-align: center;
            font-weight: bold;
            padding: 3px;
        """)

    def _configure_ok_button(self):
        self.ok_button.setFixedSize(*self.BUTTON_SIZE)
        self.ok_button.clicked.connect(self.accept)
        self.ok_button.setEnabled(False)

    def _setup_timers(self):
        self.update_timer.timeout.connect(self.update_elapsed_time)
        self.timer.start()
        self.update_timer.start(1000)  # Update every second

    def _setup_layout(self):
        self.left_panel.addWidget(self.scroll_area)
        self._setup_failed_attempts_label()
        self.left_panel.addWidget(self.failed_attempts_label)

        self.right_panel.addWidget(self.checklist_label)
        self.right_panel.addWidget(self.checklist)
        self.right_panel.addStretch(1)
        self.right_panel.addWidget(self.elapsed_time_label)
        self.right_panel.addStretch(1)

        button_container = QHBoxLayout()
        button_container.addStretch()
        button_container.addWidget(self.ok_button)
        self.right_panel.addLayout(button_container)

        self.layout.addLayout(self.left_panel, 3)
        self.layout.addSpacing(10)
        self.layout.addLayout(self.right_panel, 1)

    def _setup_failed_attempts_label(self):
        self.failed_attempts_label.setStyleSheet(f"""
            color: {StyleConfig.COLORS['error']};
            font-size: 16px;
            font-weight: bold;
            padding: 10px;
            margin-top: 8px;
            border-radius: 8px;
            background-color: rgba(247, 118, 142, 0.15);
            border-left: 4px solid {StyleConfig.COLORS['error']};
        """)
        self.failed_attempts_label.setVisible(False)

    @staticmethod
    def _get_border_style():
        return f"""
            border-radius: 8px;
            border-right: 1px solid {StyleConfig.COLORS['primary']};
            border-top: 1px solid {StyleConfig.COLORS['primary']};
            border-bottom: 1px solid {StyleConfig.COLORS['primary']};
            border-left: 4px solid {StyleConfig.COLORS['primary']};
        """

    def initialize_checklist(self):
        self.checklist.clear()
        self.task_status.clear()

        cleaned_tasks = [
            (tid, desc.replace("...", "").replace("with 'yay'", ""))
            for tid, desc in self.task_descriptions
        ]

        for task_id, desc in cleaned_tasks:
            item = QListWidgetItem(desc)
            item.setData(Qt.ItemDataRole.UserRole, task_id)
            item.setIcon(QIcon.fromTheme("dialog-question"))
            item.setForeground(QColor(StyleConfig.COLORS['muted']))
            self.checklist.addItem(item)
            self.task_status[task_id] = TaskStatus.PENDING

        self._adjust_checklist_height()

    def _adjust_checklist_height(self):
        total_height = sum(
            self.checklist.sizeHintForRow(i)
            for i in range(self.checklist.count())
        )
        total_height += 2 * self.checklist.frameWidth()
        self.checklist.setFixedHeight(max(total_height, 40))

    def update_task_checklist_status(self, task_id, status):
        if task_id not in self.task_status:
            return

        self.task_status[task_id] = status

        if status in (TaskStatus.ERROR, TaskStatus.WARNING):
            self.has_error = True

        self._update_checklist_item_appearance(task_id, status)

    def _update_checklist_item_appearance(self, task_id, status):
        status_config = {
            TaskStatus.SUCCESS: (StyleConfig.COLORS['success'], "dialog-ok-apply"),
            TaskStatus.ERROR: (StyleConfig.COLORS['error'], "dialog-error"),
            TaskStatus.WARNING: (StyleConfig.COLORS['warning'], "dialog-warning"),
            TaskStatus.IN_PROGRESS: (StyleConfig.COLORS['info'], "media-playback-start")
        }

        if status not in status_config:
            return

        color, icon_name = status_config[status]

        for i in range(self.checklist.count()):
            item = self.checklist.item(i)
            if item.data(Qt.ItemDataRole.UserRole) == task_id:
                item.setIcon(QIcon.fromTheme(icon_name))
                item.setForeground(QColor(color))

                bg_color = QColor(color)
                bg_color.setAlpha(25)
                item.setBackground(bg_color)

                self.checklist.scrollToItem(item)
                break

    def update_operation_dialog(self, output: str, message_type: str = "info"):
        cursor = self.text_edit.textCursor()

        if self._handle_special_outputs(output, cursor):
            return

        if message_type == "finish":
            if not self.auth_failed:
                self._show_completion_message()
            return

        if message_type == "task_list":
            self._handle_task_list(output)
            return

        self._process_regular_output(output, message_type, cursor)

    def _handle_special_outputs(self, output: str, cursor: QTextCursor) -> bool:
        if "/var/lib/pacman/db.lck" in output:
            self._show_database_lock_error(cursor)
            return True
        return False

    def _show_database_lock_error(self, cursor: QTextCursor):
        error_html = f"""
        <hr style='border: none; margin: 10px 20px; border-top: 1px dashed rgba(247, 118, 142, 0.4);'>
        <div style='padding: 15px; margin: 10px; border-radius: 10px; border-left: 4px solid {StyleConfig.COLORS["error"]};'>
            <p style='color: {StyleConfig.COLORS["error"]}; font-size: 18px; text-align: center;'>
                <b>⚠️ Installation Aborted</b><br>
                <span style='font-size: 16px;'>'/var/lib/pacman/db.lck' detected!</span><br>
                <span style='color: {StyleConfig.COLORS["text"]}; font-size: 14px;'>
                    Remove using: <code>sudo rm -r /var/lib/pacman/db.lck</code>
                </span>
            </p>
        </div>
        """

        cursor.insertHtml(error_html)
        self._finalize_text_edit(cursor)
        self._enable_close_button()
        self._stop_installation()

    def _handle_task_list(self, output: str):
        try:
            self.task_descriptions = ast.literal_eval(output)
            self.initialize_checklist()
        except (SyntaxError, ValueError):
            pass

    def _process_regular_output(self, output: str, message_type: str, cursor: QTextCursor):
        if message_type not in StyleConfig.STYLE_MAP and "<span " not in output:
            return

        if "<span " in output:
            html_content = output
        else:
            html_content = self._format_output_as_html(output, message_type)

        for old, new in Options.text_replacements:
            html_content = html_content.replace(old, new)

        self._finalize_text_edit(cursor, html_content)

    @staticmethod
    def _format_output_as_html(output: str, message_type: str) -> str:
        style = StyleConfig.get_style_string(message_type)

        if message_type == "operation":
            return f"""
            <hr style='border: none; margin: 15px 30px; border-top: 1px dashed rgba(111, 255, 245, 0.3);'>
            <div style='padding: 10px; border-radius: 8px; margin: 5px 0;'>
                <p style='{style}'>{output}</p>
            </div><br>
            """
        else:
            lines = [
                f"<p style='{style}'>{line}</p>"
                for line in output.splitlines()
                if line.strip()
            ]
            return "\n".join(lines) + "<br>"

    def _finalize_text_edit(self, cursor: QTextCursor, html_content: str = None):
        try:
            cursor.movePosition(QTextCursor.MoveOperation.End)
            if html_content:
                cursor.insertHtml(html_content)
            self.text_edit.setTextCursor(cursor)

            scrollbar = self.text_edit.verticalScrollBar()
            if scrollbar:
                scrollbar.setValue(scrollbar.maximum())
        except Exception as e:
            print(f"Text edit update failed: {e}")

    def _show_completion_message(self):
        if self.completed_message_shown or self.auth_failed:
            return

        self.completed_message_shown = True
        self.update_timer.stop()

        is_error = self.has_error
        color = StyleConfig.COLORS['warning' if is_error else 'success']
        summary_text = "Completed with issues" if is_error else "Successfully Completed"
        icon = "⚠️" if is_error else "✅"
        message = f"Package Installer {'completed with warnings/errors' if is_error else 'successfully completed all operations<br>'}"

        color_obj = QColor(color)
        r, g, b = color_obj.red(), color_obj.green(), color_obj.blue()

        cursor = self.text_edit.textCursor()
        completion_html = f"""
        <hr style='border: none; margin: 25px 50px; border-top: 2px solid {color};'>
        <div style='text-align: center; padding: 20px; margin: 15px 30px;
                    border-radius: 15px; border: 1px solid rgba({r}, {g}, {b}, 0.3);'>
            <p style='color: {color}; font-size: 20px; font-weight: bold;'>{icon} {summary_text}</p>
            <p style='color: {color}; font-size: 18px;'>{message}</p>
        </div>
        """
        cursor.insertHtml(completion_html)

        self._enable_close_button()
        self._update_checklist_label_completion(icon, summary_text, color, r, g, b)
        self.text_edit.setTextCursor(cursor)

    def _enable_close_button(self):
        self.ok_button.setEnabled(True)
        self.ok_button.setFocus()

    def _stop_installation(self):
        self.update_timer.stop()
        if hasattr(self, 'installer_thread') and self.installer_thread and self.installer_thread.isRunning():
            self.installer_thread.terminate()

    def _update_checklist_label_completion(self, icon: str, summary_text: str, color: str, r: int, g: int, b: int):
        self.checklist_label.setText(f"{icon} {summary_text}")
        completion_style = f"""
            color: {color};
            font-size: 18px;
            font-weight: bold;
            padding: 10px;
            background-color: rgba({r}, {g}, {b}, 0.15);
            {self._get_border_style()}
        """
        self.checklist_label.setStyleSheet(completion_style)

    def update_elapsed_time(self):
        try:
            elapsed = max(0, int(self.timer.elapsed() / 1000))
            time_text = self._format_elapsed_time(elapsed)
            self.elapsed_time_label.setText(time_text)
        except Exception as e:
            print(f"Error in update_elapsed_time: {e}")
            self.elapsed_time_label.setText("\nElapsed time:\n--\n")

    @staticmethod
    def _format_elapsed_time(elapsed: int) -> str:
        """Format elapsed time as a readable string."""
        h, remainder = divmod(elapsed, 3600)
        m, s = divmod(remainder, 60)

        if h:
            return f"\nElapsed time:\n{h:02}h {m:02}m {s:02}s\n"
        elif m:
            return f"\nElapsed time:\n{m:02}m {s:02}s\n"
        else:
            return f"\nElapsed time:\n{s:02}s\n"

    def update_failed_attempts(self, failed_attempts: int):
        """Update failed authentication attempts display."""
        if failed_attempts > 0:
            text = f"⚠️ Failed Authentication Attempts: {failed_attempts}"
            self.failed_attempts_label.setText(text)
            self.failed_attempts_label.setVisible(True)
            self.auth_failed = True
            self._enable_close_button()

    def keyPressEvent(self, event):
        """Handle key press events."""
        key_handlers = {
            Qt.Key.Key_Down: self._handle_down_key,
            Qt.Key.Key_Escape: self._handle_escape_key,
            Qt.Key.Key_Tab: self.focusNextChild
        }

        handler = key_handlers.get(event.key())
        if handler:
            handler()
        else:
            super().keyPressEvent(event)

    def _handle_down_key(self):
        """Handle down arrow key press."""
        scrollbar = self.scroll_area.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())
        self.ok_button.setFocus()

    def _handle_escape_key(self):
        """Handle escape key press."""
        if self.completed_message_shown:
            self.close()
        # If not completed, ignore escape key

    def closeEvent(self, event):
        """Handle dialog close event."""
        # Prevent closing if operation is still in progress
        if not self.completed_message_shown and not self.auth_failed:
            event.ignore()
            return

        # Clean up installer thread
        self._cleanup_installer_thread()
        super().closeEvent(event)

    def _cleanup_installer_thread(self):
        """Clean up the installer thread."""
        if not (hasattr(self, 'installer_thread') and self.installer_thread):
            return

        if self.installer_thread.isRunning():
            self.installer_thread.terminated = True
            self.installer_thread.quit()

            try:
                if not self.installer_thread.wait(2000):  # Wait 2 seconds
                    self.installer_thread.terminate()
                    self.installer_thread.wait(1000)  # Wait 1 more second
            except RuntimeError as e:
                print(f"Thread cleanup warning: {e}")


# noinspection PyUnresolvedReferences
class PackageInstallerThread(QThread):
    started = pyqtSignal()
    outputReceived = pyqtSignal(str, str)
    passwordFailed = pyqtSignal()
    passwordSuccess = pyqtSignal()
    taskStatusChanged = pyqtSignal(str, str)
    finished = pyqtSignal()

    def __init__(self, sudo_password):
        super().__init__()
        self.enabled_tasks = None
        self.task_descriptions = None
        self.sudo_password = SecureString(sudo_password)
        self.auth_failed = self.has_error = self.terminated = False
        self.temp_dir = self.askpass_script_path = self.current_task = None
        self.task_status = {}
        self._installed_packages_cache = {}
        self.distro = LinuxDistroHelper()

    def run(self):
        self.started.emit()
        self.prepare_tasks()
        try:
            if self.terminated:
                return
            if not self.test_sudo_access():
                self.auth_failed = True
                self.passwordFailed.emit()
                return
            self.passwordSuccess.emit()
            if not self.auth_failed and not self.terminated:
                self.start_package_installer()
        except Exception as e:
            self.outputReceived.emit(f"Critical error during execution: {e}", "error")
            self.has_error = True
        finally:
            self.cleanup_temp_files()
            self.sudo_password.clear()

    def prepare_tasks(self):
        Options.load_config(Options.config_file_path)
        installer_operations = Options.installer_operations
        tasks = self._define_base_tasks()
        for service_task_id, (desc, name, pkgs) in self._define_service_tasks().items():
            def make_task(task_name, task_pkgs):
                return lambda: self.setup_service_with_packages(task_name, list(task_pkgs))
            tasks[service_task_id] = (desc, make_task(name, pkgs))
        tasks.update({"remove_orphaned_packages": ("Removing orphaned packages...", self.remove_orphaned_packages), "clean_cache": ("Cleaning cache...", self.clean_cache)})
        self.enabled_tasks = {tid: t for tid, t in tasks.items() if tid in installer_operations}
        self.task_descriptions = [(tid, desc) for tid, (desc, _) in self.enabled_tasks.items()]
        self.outputReceived.emit(str(self.task_descriptions), "task_list")

    def _define_base_tasks(self):
        return {"copy_system_files": ("Copying 'System Files'...", lambda: self.copy_files(self.parse_system_files(Options.system_files))),
                "update_mirrors": ("Updating mirrors...", lambda: self.update_mirrors("update_mirrors")),
                "set_user_shell": ("Setting user shell...", lambda: self.set_user_shell("set_user_shell")),
                "update_system": ("Updating system...", lambda: self.update_system("update_system")),
                "install_kernel_header": ("Installing kernel headers...", lambda: self.install_kernel_header("install_kernel_header")),
                "install_essential_packages": ("Installing 'Essential Packages'...", lambda: self.batch_install(Options.essential_packages, "Essential Package")),
                "install_yay": ("Installing 'yay'...", self.install_yay),
                "install_additional_packages": ("Installing 'Additional Packages' with 'yay'...", lambda: self.batch_install(Options.additional_packages, "Additional Package")),
                "install_specific_packages": ("Installing 'Specific Packages'...", self.install_specific_packages_based_on_session)}

    def _define_service_tasks(self):
        return {
            "enable_printer_support": (
                "Initializing printer support...",
                "cups",
                self.distro.get_printer_packages()
            ),
            "enable_samba_network_filesharing": (
                "Initializing samba...",
                "smb",
                self.distro.get_samba_packages()
            ),
            "enable_bluetooth_service": (
                "Initializing bluetooth...",
                "bluetooth",
                self.distro.get_bluetooth_packages()
            ),
            "enable_atd_service": (
                "Initializing atd...",
                "atd",
                self.distro.get_at_packages()
            ),
            "enable_cronie_service": (
                "Initializing cronie...",
                "cronie",
                self.distro.get_cron_packages()
            ),
            "enable_firewall": (
                "Initializing firewall...",
                "ufw",
                self.distro.get_firewall_packages()
            )
        }

    def reset_sudo_timeout(self):
        try:
            subprocess.run(['sudo', '-K'], stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
            time.sleep(0.5)
        except Exception as e:
            self.outputReceived.emit(f"Warning: Could not reset sudo state: {e}")

    def test_sudo_access(self):
        self.outputReceived.emit("Verifying sudo access...", "operation")
        self.reset_sudo_timeout()
        self.cleanup_temp_files()
        if not self.create_askpass_script():
            self.auth_failed = True
            return False
        try:
            env = os.environ.copy()
            env['SUDO_ASKPASS'] = self.askpass_script_path
            process = subprocess.run(['sudo', '-A', 'echo', 'Sudo access successfully verified...'], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env, timeout=0.2)
            if process.stdout:
                self.outputReceived.emit(process.stdout.strip(), "success")
            if process.stderr:
                self.outputReceived.emit(process.stderr.strip(), "error")
            return process.returncode == 0
        except subprocess.TimeoutExpired:
            self.auth_failed = self.has_error = True
            return False
        except Exception as e:
            self.auth_failed = self.has_error = True
            self.outputReceived.emit(f"Error during sudo authentication test: {e}", "error")
            return False

    def create_askpass_script(self):
        try:
            self.temp_dir = tempfile.mkdtemp(prefix="installer_")
            os.chmod(self.temp_dir, 0o700)
            self.askpass_script_path = Path(self.temp_dir, 'askpass.sh')
            self.askpass_script_path.write_text('#!/bin/sh\ncat "$SUDO_PASSWORD_FILE"', encoding='utf-8')
            os.chmod(self.askpass_script_path, 0o700)
            password_file = Path(self.temp_dir, 'sudo_pass')
            password_file.write_text(self.sudo_password.get_value(), encoding='utf-8')
            os.chmod(password_file, 0o600)
            os.environ['SUDO_PASSWORD_FILE'] = str(password_file)
            return True
        except Exception as e:
            self.outputReceived.emit(f"Error creating askpass script: {e}", "error")
            return False

    def cleanup_temp_files(self):
        if not self.temp_dir or not Path(self.temp_dir).exists():
            return
        try:
            for filename in ('sudo_pass', 'askpass.sh'):
                file_path = Path(self.temp_dir, filename)
                if file_path.exists():
                    try:
                        with open(file_path, 'wb') as f:
                            f.write(os.urandom(3072))
                        file_path.unlink()
                    except Exception as e:
                        self.outputReceived.emit(f"Error securely removing {filename}: {e}", "warning")
            for file_path in Path(self.temp_dir).glob('*'):
                if file_path.is_file():
                    try:
                        file_path.unlink()
                    except Exception as e:
                        self.outputReceived.emit(f"Error removing temporary file {file_path}: {e}", "warning")
            shutil.rmtree(self.temp_dir, ignore_errors=True)
            self.temp_dir = self.askpass_script_path = None
        except Exception as e:
            self.outputReceived.emit(f"Error cleaning up temporary files: {e}", "warning")

    def run_sudo_command(self, command):
        if self.terminated:
            return False
        try:
            env = os.environ.copy()
            env['SUDO_ASKPASS'] = self.askpass_script_path
            if isinstance(command, list):
                if command[0] == 'sudo' and '-A' not in command:
                    command.insert(1, '-A')
                elif command[0] == 'yay' and not any(arg.startswith('--sudoflags=') for arg in command):
                    command.insert(1, '--sudoflags=-A')
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env, bufsize=4096)
            return self._process_command_output(process)
        except Exception as e:
            self.outputReceived.emit(f"<span>Error during command execution: {e}</span>", "error")
            return False

    def _process_command_output(self, process):
        output_buffer, error_buffer = [], []
        def read_stream(stream, buffer, name):
            try:
                for line in iter(stream.readline, ''):
                    if not line or self.terminated:
                        break
                    line = line.strip()
                    if line:
                        buffer.append(line)
                        self.outputReceived.emit(f"<span>{line}</span>", "subprocess")
            except Exception as error:
                self.outputReceived.emit(f"<span>Error reading {name} stream: {error}</span>", "error")
        threads = [threading.Thread(target=read_stream, args=(process.stdout, output_buffer, "stdout"), daemon=True),
                   threading.Thread(target=read_stream, args=(process.stderr, error_buffer, "stderr"), daemon=True)]
        for t in threads: t.start()
        try:
            process.wait(timeout=600)
            for t in threads: t.join()
        except subprocess.TimeoutExpired:
            self.outputReceived.emit("<span>Command Timeout. Process is terminated...</span>", "error")
            process.kill()
            process.wait()
            return False
        if process.returncode != 0:
            self.outputReceived.emit(f"<span>Command error: {process.returncode}</span>", "error")
            if error_buffer:
                self.outputReceived.emit(f"<span>Error details: {' '.join(error_buffer)}</span>", "error")
        return process

    def start_package_installer(self):
        for task_id, (description, function) in self.enabled_tasks.items():
            if self.terminated:
                break
            self.current_task = task_id
            self.taskStatusChanged.emit(task_id, "in_progress")
            self.outputReceived.emit(description, "operation")
            try:
                success = function()
                status = "success" if success is not False else "error"
                self.taskStatusChanged.emit(task_id, status)
                if status == "error":
                    self.has_error = True
            except Exception as e:
                self.has_error = True
                self.outputReceived.emit(f"Task '{task_id}' failed: {e}", "error")
                self.taskStatusChanged.emit(task_id, "error")
        self.outputReceived.emit("", "finish")

    def parse_system_files(self, files):
        parsed_files = []
        for file in files:
            if not isinstance(file, dict):
                self.outputReceived.emit(f"Expected Dictionary but got: {type(file)}", "error")
                continue
            src, dest = file.get('source', '').strip(), file.get('destination', '').strip()
            if src and dest:
                parsed_files.append((src, dest))
            else:
                self.outputReceived.emit(f"Invalid Dictionary Format: {file}", "error")
        return parsed_files

    def copy_files(self, files):
        task_id, success = "copy_system_files", True
        if not files:
            self.outputReceived.emit("No 'System Files' to copy", "warning")
            self.taskStatusChanged.emit(task_id, "warning")
            return True
        for src, dest in files:
            if not Path(src).exists():
                self.outputReceived.emit(f"Source file does not exist: '{src}'", "error")
                success = False
                continue
            dest_dir = Path(dest).parent
            if not dest_dir.exists() and not self._create_directory(dest_dir):
                success = False
                continue
            filename = os.path.basename(src)
            self.outputReceived.emit(f"Copying: '{src}'", "info")
            cmd = ['sudo', 'cp', '-r'] if Path(src).is_dir() else ['sudo', 'cp']
            cmd.extend([str(src), str(dest)])
            result = self.run_sudo_command(cmd)
            if result and result.returncode == 0:
                self.outputReceived.emit(f"Successfully copied: '{filename}' to '{dest}'", "success")
            else:
                self.outputReceived.emit(f"Error copying: '{filename}'", "error")
                success = False
        self.taskStatusChanged.emit(task_id, "success" if success else "error")
        return success

    def _create_directory(self, dest_dir):
        result = self.run_sudo_command(['sudo', 'mkdir', '-p', str(dest_dir)])
        success = result and result.returncode == 0
        self.outputReceived.emit(f"{'Created' if success else 'Error creating'} directory: '{dest_dir}'", "info" if success else "error")
        return success

    def install_package_generic(self, package, package_type=None):
        type_str = package_type or "package"
        self.outputReceived.emit(f"Installing '{type_str}': '{package}'...", "info")
        if self.distro.package_is_installed(package):
            self.outputReceived.emit(f"'{package}' already present...", "success")
            return True
        cmd = self.distro.get_pkg_install_cmd(package)
        result = self.run_sudo_command(cmd.split())
        success = result and result.returncode == 0 and self.distro.package_is_installed(package)
        self.outputReceived.emit(f"'{package}' {'successfully installed' if success else 'failed to install'}...", "success" if success else "error")
        return success

    def batch_install(self, packages, package_type):
        task_id_map = {"Essential Package": "install_essential_packages", "Additional Package": "install_additional_packages"}
        task_id = task_id_map.get(package_type)
        pkgs = [p.strip() for p in (packages or []) if isinstance(p, str) and p.strip()]
        pkgs_to_install = self.distro.filter_not_installed(pkgs)
        if not pkgs_to_install:
            self.outputReceived.emit(f"All '{package_type}s' already present...", "success")
            if task_id: self.taskStatusChanged.emit(task_id, "success")
            return True
        if package_type == "Essential Package":
            cmd = self.distro.get_pkg_install_cmd(" ".join(pkgs_to_install))
        else:
            cmd = f"yay -S --noconfirm {' '.join(pkgs_to_install)}"
        self.run_sudo_command(cmd.split())
        failed = []
        installed = []
        for pkg in pkgs_to_install:
            if self.distro.package_is_installed(pkg):
                installed.append(pkg)
            else:
                failed.append(pkg)
        if failed:
            self.outputReceived.emit(f"Warning: Failed to install the following '{package_type}s': {', '.join(failed)}", "warning")
        if installed:
            self.outputReceived.emit(f"Successfully installed {len(installed)} of {len(pkgs_to_install)} '{package_type}s':\n{', '.join(f'\'{pkg}\'' for pkg in installed)}", "success")
        if task_id:
            status = "success" if not failed else "warning" if installed else "error"
            self.taskStatusChanged.emit(task_id, status)
        return not failed

    def update_mirrors(self, task_id):
        if self.distro.distro_id != "arch":
            self.outputReceived.emit("Mirror update is only supported for Arch Linux.", "info")
            self.taskStatusChanged.emit(task_id, "success")
            return True
        self.install_package_generic("reflector", package_type="Service Package")
        country = self._detect_country()
        command = ['sudo', 'reflector', '--verbose', '--latest', '10', '--protocol', 'https', '--sort', 'rate', '--save', '/etc/pacman.d/mirrorlist']
        if country:
            self.outputReceived.emit(f"<br>Detected country: {country}", "success")
            command.extend(['--country', country])
        else:
            self.outputReceived.emit("Unable to detect country. Searching globally instead.", "info")
        result = self.run_sudo_command(command)
        success = result and result.returncode == 0
        status = "success" if success else "error"
        self.outputReceived.emit(f"Mirrors {'successfully updated' if success else 'failed to update'}...", status)
        self.taskStatusChanged.emit(task_id, status)
        return success

    @staticmethod
    def _detect_country():
        urls = ['https://ipinfo.io/country', 'https://ifconfig.co/country-iso']
        for url in urls:
            try:
                with urllib.request.urlopen(url, timeout=5) as response:
                    if response.status == 200:
                        country = response.read().decode().strip()
                        if country and len(country) <= 3:
                            return country
            except (urllib.error.URLError, socket.timeout):
                continue
        return None

    def set_user_shell(self, task_id):
        config_shell = getattr(Options, "user_shell", "Bash").strip()
        shell_pkg = self.distro.get_shell_package_name(config_shell)
        shell_bin = shutil.which(shell_pkg) or f"/bin/{shell_pkg}"
        try:
            actual_user = os.environ.get('SUDO_USER') or os.environ.get('USER') or os.environ.get('LOGNAME') or getpass.getuser()
            current_shell = pwd.getpwnam(actual_user).pw_shell
            self.outputReceived.emit(f"Target user: '{actual_user}' (current shell: {current_shell})", "info")
        except Exception as e:
            self.outputReceived.emit(f"Error when determining the target user: {e}", "error")
            self.taskStatusChanged.emit(task_id, "error")
            return False
        if current_shell == shell_bin:
            self.outputReceived.emit(f"Current user shell already '{config_shell}'. No changes required...", "success")
            self.taskStatusChanged.emit(task_id, "success")
            return True
        if not self.distro.package_is_installed(shell_pkg):
            if not self.install_package_generic(shell_pkg, package_type="Shell Package"):
                self.outputReceived.emit(f"Error when installing '{shell_pkg}'.", "error")
                self.taskStatusChanged.emit(task_id, "error")
                return False
        if not Path(shell_bin).exists():
            self.outputReceived.emit(f"Shell binary '{shell_bin}' does not exist after installation.", "error")
            self.taskStatusChanged.emit(task_id, "error")
            return False
        try:
            with open("/etc/shells", "r") as f:
                shells = [line.strip() for line in f if line.strip()]
            if shell_bin not in shells:
                self.outputReceived.emit(f"Adding '{shell_bin}' to /etc/shells...", "info")
                append_cmd = ['sudo', 'sh', '-c', f'echo "{shell_bin}" >> /etc/shells']
                append_result = self.run_sudo_command(append_cmd)
                if not append_result or append_result.returncode != 0:
                    self.outputReceived.emit(f"Error when adding the shell to /etc/shells.", "error")
                    self.taskStatusChanged.emit(task_id, "error")
                    return False
            self.outputReceived.emit(f"Changing user shell for '{actual_user}' to '{shell_bin}'...", "info")
            chsh_cmd = ['sudo', 'chsh', '-s', shell_bin, actual_user]
            chsh_result = self.run_sudo_command(chsh_cmd)
            if chsh_result and chsh_result.returncode == 0:
                self.outputReceived.emit(f"Shell for user '{actual_user}' successfully changed to '{config_shell}'...", "success")
                self.taskStatusChanged.emit(task_id, "success")
                return True
            else:
                self.outputReceived.emit(f"Error when changing the shell for user '{actual_user}'.", "error")
                self.taskStatusChanged.emit(task_id, "error")
                return False
        except Exception as e:
            self.outputReceived.emit(f"Error while setting shell: {e}", "error")
            self.taskStatusChanged.emit(task_id, "error")
            return False

    def update_system(self, task_id):
        if self.distro.package_is_installed('yay'):
            cmd = "yay --noconfirm"
        else:
            cmd = self.distro.get_pkg_update_cmd()
        result = self.run_sudo_command(cmd.split())
        success = result and result.returncode == 0
        status = "success" if success else "error"
        self.outputReceived.emit(f"System {'successfully updated' if success else 'update failed'}...", status)
        self.taskStatusChanged.emit(task_id, status)
        return success

    def install_kernel_header(self, task_id):
        kernel_pkg = self.distro.get_kernel_headers_pkg()
        success = self.install_package_generic(kernel_pkg, package_type="Header Package")
        status = "success" if success else "error"
        self.taskStatusChanged.emit(task_id, status)
        return success

    def setup_service_with_packages(self, service, packages):
        task_id = {'cups': "enable_printer_support", 'smb': "enable_samba_network_filesharing", 'bluetooth': "enable_bluetooth_service",
                   'atd': "enable_atd_service", 'cronie': "enable_cronie_service", 'ufw': "enable_firewall"}.get(service)
        success = all(self.install_package_generic(pkg, package_type="Service Package") for pkg in packages)
        service_success = self.enable_service(service)
        if task_id:
            self.taskStatusChanged.emit(task_id, "success" if success and service_success else "error")
        return success and service_success

    def enable_service(self, service):
        self.outputReceived.emit(f"Enabling: '{service}.service'...", "info")
        is_active = subprocess.run(['systemctl', 'is-active', '--quiet', f'{service}.service'], check=False).returncode == 0
        if is_active:
            self.outputReceived.emit(f"'{service}.service' already enabled...", "success")
            return True
        self.outputReceived.emit("\n", "info")
        result = self.run_sudo_command(['sudo', 'systemctl', 'enable', '--now', f'{service}.service'])
        success = result and result.returncode == 0
        if success:
            self.outputReceived.emit(f"'{service}.service' successfully enabled...", "success")
            if service == "ufw":
                for ufw_cmd in (['sudo', 'ufw', 'default', 'deny'], ['sudo', 'ufw', 'enable'], ['sudo', 'ufw', 'reload']):
                    if not (self.run_sudo_command(ufw_cmd) or False):
                        success = False
        else:
            error = getattr(result, 'stderr', 'Unknown error')
            self.outputReceived.emit(f"Error enabling '{service}.service': {error}", "error")
        return success

    def install_yay(self):
        task_id = "install_yay"
        if not self.distro.supports_aur():
            self.outputReceived.emit("'yay' (AUR) is not supported on this distribution.", "warning")
            self.taskStatusChanged.emit(task_id, "warning")
            return True
        if self.distro.package_is_installed("yay"):
            self.outputReceived.emit("'yay' already present...", "success")
            self.taskStatusChanged.emit(task_id, "success")
            return True
        if not (self.run_sudo_command(self.distro.get_pkg_install_cmd("base-devel git go").split()) or False):
            self.taskStatusChanged.emit(task_id, "error")
            return False
        yay_build_path = Path(home_user) / "yay"
        if yay_build_path.exists():
            shutil.rmtree(yay_build_path) if yay_build_path.is_dir() else yay_build_path.unlink()
        def run_and_stream(cmd, cwd):
            try:
                with subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, cwd=cwd) as proc:
                    for line in proc.stdout:
                        self.outputReceived.emit(line.rstrip(), "subprocess")
                    return proc.wait() == 0
            except Exception as e:
                self.outputReceived.emit(f"Exception: {e}", "error")
                return False
        self.outputReceived.emit("Cloning 'yay' from git...", "subprocess")
        if not run_and_stream(['git', 'clone', 'https://aur.archlinux.org/yay.git'], home_user):
            self.taskStatusChanged.emit(task_id, "error")
            return False
        self.outputReceived.emit("Building package 'yay'...", "subprocess")
        if not run_and_stream(['makepkg', '-c', '--noconfirm'], yay_build_path):
            self.taskStatusChanged.emit(task_id, "error")
            return False
        to_remove = [pkg for pkg in ['yay-debug', 'go'] if self.distro.package_is_installed(pkg)]
        if to_remove:
            result = self.run_sudo_command(['sudo', 'pacman', '-R', '--noconfirm'] + to_remove)
            self.outputReceived.emit(f"{'Successfully removed' if result and result.returncode == 0 else 'Error during uninstallation of'}: '{', '.join(to_remove)}'", "subprocess" if result and result.returncode == 0 else "warning")
        pkg_files = sorted(f for f in os.listdir(yay_build_path) if f.endswith('.pkg.tar.zst'))
        if not pkg_files:
            self.outputReceived.emit("No package file found for installation.", "warning")
            self.taskStatusChanged.emit(task_id, "error")
            return False
        pkg_path = os.path.join(str(yay_build_path), pkg_files[0])
        result = self.run_sudo_command(['sudo', 'pacman', '-U', '--noconfirm', str(pkg_path)])
        success = result and result.returncode == 0
        shutil.rmtree(yay_build_path, ignore_errors=True)
        shutil.rmtree(Path(home_config) / "go", ignore_errors=True)
        self.outputReceived.emit(f"'yay' {'successfully installed' if success else 'installation failed'}...", "success" if success else "error")
        self.taskStatusChanged.emit(task_id, "success" if success else "error")
        return success

    def install_specific_packages_based_on_session(self):
        task_id = "install_specific_packages"
        session = None
        for var in ['XDG_CURRENT_DESKTOP', 'XDG_SESSION_DESKTOP', 'DESKTOP_SESSION']:
            val = os.getenv(var)
            if val:
                parts = [p.strip() for p in val.split(':') if p.strip()]
                for part in parts:
                    for env in SESSIONS:
                        if part.lower() == env.lower():
                            session = env
                            break
                    if session:
                        break
            if session:
                break
        if not session:
            self.outputReceived.emit("Unable to determine current desktop environment or window manager.", "warning")
            self.taskStatusChanged.emit(task_id, "error")
            return False
        self.outputReceived.emit(f"Detected session: {session}", "success")
        matching_packages = [spec_pkg.get('package') for spec_pkg in Options.specific_packages if spec_pkg.get('session') == session and 'package' in spec_pkg]
        pkgs_to_install = self.distro.filter_not_installed(matching_packages)
        if not pkgs_to_install:
            self.outputReceived.emit(f"All 'Specific Packages' for {session} already present...", "success")
            self.taskStatusChanged.emit(task_id, "success")
            return True
        cmd = self.distro.get_pkg_install_cmd(" ".join(pkgs_to_install))
        self.run_sudo_command(cmd.split())
        failed = []
        for pkg in pkgs_to_install:
            if not self.distro.package_is_installed(pkg):
                failed.append(pkg)
        success = not failed
        if failed:
            self.outputReceived.emit(f"Failed to install: {', '.join(failed)}", "warning")
        self.taskStatusChanged.emit(task_id, "success" if success else "error")
        return success

    def remove_orphaned_packages(self):
        task_id = "remove_orphaned_packages"
        find_orphans_cmd = self.distro.get_find_orphans_cmd()
        orphaned_packages = subprocess.run(find_orphans_cmd.split(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True).stdout.strip()
        if orphaned_packages:
            packages_list = orphaned_packages.split('\n')
            result = self.run_sudo_command(self.distro.get_pkg_remove_cmd(' '.join(packages_list)).split())
            success = result and result.returncode == 0
            self.outputReceived.emit(f"Orphaned packages {'successfully removed' if success else 'could not be removed'}...", "success" if success else "error")
        else:
            self.outputReceived.emit("No orphaned packages found...", "success")
            success = True
        self.taskStatusChanged.emit(task_id, "success" if success else "error")
        return success

    def clean_cache(self):
        task_id = "clean_cache"
        result = self.run_sudo_command(self.distro.get_clean_cache_cmd().split())
        success = result and result.returncode == 0
        self.outputReceived.emit("Cache of system package manager successfully cleaned..." if success else "Error cleaning cache...", "success" if success else "error")
        if self.distro.package_is_installed('yay'):
            self.outputReceived.emit("<br>Cleaning 'yay' cache...", "info")
            result_yay = self.run_sudo_command(['yay', '-Scc', '--noconfirm'])
            if result_yay and result_yay.returncode == 0:
                self.outputReceived.emit("'yay' cache successfully cleaned...", "success")
            else:
                self.outputReceived.emit("Error cleaning 'yay' cache...", "error")
                success = False
        self.taskStatusChanged.emit(task_id, "success" if success else "error")
        return successfrom pathlib import Path
from options import Options, SESSIONS
from sudo_password import SecureString
from linux_distro_helper import LinuxDistroHelper
from PyQt6.QtGui import QTextCursor, QColor, QIcon
from PyQt6.QtCore import Qt, pyqtSignal, QThread, QElapsedTimer, QTimer
import ast, getpass, os, pwd, shutil, socket, subprocess, tempfile, threading, time, urllib.error, urllib.request
from PyQt6.QtWidgets import (QVBoxLayout, QHBoxLayout, QPushButton, QListWidgetItem, QApplication, QListWidget, QWidget, QCheckBox, QTextEdit, QGraphicsDropShadowEffect, QDialogButtonBox, QDialog, QLabel, QScrollArea)

user = pwd.getpwuid(os.getuid()).pw_name
home_user = os.getenv("HOME")
home_config = Path(home_user).joinpath(".config")


# noinspection PyUnresolvedReferences
class PackageInstallerLauncher:
    def __init__(self, parent=None):
        self.parent = parent
        self.config = getattr(parent, 'config', {}) if parent else {}
        from drive_manager import DriveManager
        self.drive_manager = DriveManager()
        self.failed_attempts = getattr(parent, 'failed_attempts', 0)
        self.package_installer_thread = None
        self.package_installer_dialog = None
        self.sudo_checkbox = None

    def launch(self):
        if self.parent:
            self.parent.hide()
        try:
            self.confirm_and_start_package_installer()
        finally:
            if self.parent:
                self.parent.show()

    def confirm_and_start_package_installer(self):
        installer_operations = self.config.get('installer_operations', [])
        _, _, installer_tooltips = Options.generate_tooltip()
        distro_helper = LinuxDistroHelper()
        package_installer_operation_text = Options.get_package_installer_operation_text(distro_helper)
        operations_text = {k: v.replace("&&", "&") for k, v in package_installer_operation_text.items()}
        dialog, content_widget, content_layout = self._create_installer_dialog()
        self._display_operations(installer_operations, operations_text, installer_tooltips, content_layout)
        if self._show_dialog_and_get_result(dialog, content_widget):
            self._handle_dialog_accepted(installer_operations)

    @staticmethod
    def _create_installer_dialog():
        dialog = QDialog()
        dialog.setWindowTitle('Package Installer')
        layout = QVBoxLayout()
        content_widget = QWidget()
        content_layout = QVBoxLayout(content_widget)
        header_label = QLabel("<span style='font-size: 18px;'>Package Installer will perform the following operations:<br></span>")
        header_label.setTextFormat(Qt.TextFormat.RichText)
        content_layout.addWidget(header_label)
        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        scroll_area.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        scroll_area.setWidget(content_widget)
        layout.addWidget(scroll_area)
        dialog.setLayout(layout)
        return dialog, content_widget, content_layout

    def _display_operations(self, installer_operations, operations_text, installer_tooltips, content_layout):
        for i, opt in enumerate(installer_operations):
            if opt in operations_text:
                has_tooltip = opt in installer_tooltips and installer_tooltips[opt]
                self._add_operation_row(i, operations_text[opt], has_tooltip, installer_tooltips.get(opt, ""), content_layout)

    @staticmethod
    def _add_operation_row(index, text, has_tooltip, tooltip_text, layout):
        style_color = "#9891c2;" if has_tooltip else "#c8beff;"
        text_style = "text-decoration: underline dotted;" if has_tooltip else ""
        tooltip_icon = "💡" if has_tooltip else ""
        operation_text = f"{tooltip_icon}<span style='font-size: 16px; padding: 5px; color: {style_color}{text_style}'>{text}</span>"
        row_layout = QHBoxLayout()
        number_label = QLabel(f"{index + 1}:")
        number_label.setStyleSheet("font-size: 16px; padding: 5px; qproperty-alignment: 'AlignLeft'")
        operation_label = QLabel(operation_text)
        operation_label.setTextFormat(Qt.TextFormat.RichText)
        operation_label.setStyleSheet("font-size: 16px; padding: 5px; qproperty-alignment: 'AlignLeft'")
        if has_tooltip:
            operation_label.setToolTip(tooltip_text)
            operation_label.setCursor(Qt.CursorShape.WhatsThisCursor)
            operation_label.setToolTipDuration(30000)
        row_layout.addWidget(number_label)
        row_layout.addWidget(operation_label)
        row_layout.addStretch(1)
        layout.addLayout(row_layout)

    def _show_dialog_and_get_result(self, dialog, content_widget):
        confirm_label = QLabel("<span style='font-size: 16px;'>Start Package Installer?<br>(Check 'Enter sudo password' if a sudo password is set.)<br></span>")
        button_layout = QHBoxLayout()
        button_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Yes | QDialogButtonBox.StandardButton.No)
        button_box.button(QDialogButtonBox.StandardButton.Yes).setText('Yes')
        button_box.button(QDialogButtonBox.StandardButton.No).setText('No')
        button_box.accepted.connect(dialog.accept)
        button_box.rejected.connect(dialog.reject)
        self.sudo_checkbox = QCheckBox("Enter sudo password")
        self.sudo_checkbox.setStyleSheet("font-size: 16px; color: #6ffff5")
        if self.failed_attempts != 0:
            self.sudo_checkbox.setText("Sudo password must be entered!")
            self.sudo_checkbox.setChecked(True)
            self.sudo_checkbox.setEnabled(False)
            self.sudo_checkbox.setStyleSheet("color: #787878")
        button_layout.addWidget(self.sudo_checkbox)
        button_layout.addWidget(button_box)
        content_widget.layout().addWidget(confirm_label)
        content_widget.layout().addLayout(button_layout)
        screen_geometry = QApplication.primaryScreen().availableGeometry()
        content_size = content_widget.sizeHint()
        dialog.resize(min(content_size.width() + 40, screen_geometry.width()), min(content_size.height() + 40, screen_geometry.height()))
        button_box.button(QDialogButtonBox.StandardButton.No).setFocus()
        return dialog.exec() == QDialog.DialogCode.Accepted

    def start_package_installer_thread(self, sudo_password):
        self.package_installer_thread = PackageInstallerThread(sudo_password)
        self.package_installer_dialog = PackageInstallerDialog(self.parent)
        self.package_installer_thread.started.connect(self.show_package_installer_dialog)
        self.package_installer_thread.passwordFailed.connect(self.on_password_failed)
        self.package_installer_thread.passwordSuccess.connect(self.on_password_success)
        self.package_installer_thread.outputReceived.connect(self.package_installer_dialog.update_operation_dialog)
        self.package_installer_thread.taskStatusChanged.connect(self.package_installer_dialog.update_task_checklist_status)
        self.package_installer_thread.finished.connect(self.on_package_installer_finished)
        self.package_installer_thread.start()

    def _handle_dialog_accepted(self, installer_operations):
        if "copy_system_files" in installer_operations:
            system_files = self.config.get('system_files', [])
            paths_to_check = []
            for file in system_files:
                if isinstance(file, dict):
                    for key in ('source', 'destination'):
                        if key in file:
                            paths_to_check.append(file[key])
            drives_to_mount = self.drive_manager.check_drives_to_mount(paths_to_check)
            if drives_to_mount and not self.drive_manager.mount_required_drives(drives_to_mount, self.parent):
                return
        if self.sudo_checkbox.isChecked():
            self.show_sudo_password_dialog()
        else:
            self.start_package_installer_thread("")

    def show_package_installer_dialog(self):
        try:
            self.package_installer_dialog.exec()
        finally:
            self.drive_manager.unmount_drives()

    def show_sudo_password_dialog(self):
        from sudo_password import SudoPasswordDialog, SecureString
        dialog = SudoPasswordDialog(self.parent)
        dialog.sudo_password_entered.connect(self.on_sudo_password_entered)
        dialog.update_failed_attempts(self.failed_attempts)
        dialog.exec()

    def on_sudo_password_entered(self, sudo_password):
        self.start_package_installer_thread(sudo_password)

    def on_password_failed(self):
        self.failed_attempts += 1
        if self.parent:
            self.parent.failed_attempts = self.failed_attempts

        if self.package_installer_dialog:
            self.package_installer_dialog.update_failed_attempts(self.failed_attempts)
            self.package_installer_dialog.auth_failed = True
            error_msg = ("<p style='color: #ff4a4d; font-size: 18px; font-weight: bold;'>"
                         "<br>Authentication failed. Canceling process to prevent account lockout."
                         "<br>This could be due to:"
                         "<ul>"
                         "<li>Incorrect or missing password</li>"
                         "<li>Password is unauthorized</li>"
                         "<li>User not in sudoers file</li>"
                         "<li>Sudo configuration issue</li>"
                         "</ul>"
                         "Package Installer has been aborted to protect your system."
                         "</p>")
            self.package_installer_dialog.update_operation_dialog(error_msg)
            self.package_installer_dialog.completed_message_shown = True
            self.package_installer_dialog.update_timer.stop()
            self.package_installer_dialog.has_error = True
            self.package_installer_dialog.ok_button.setEnabled(True)

        if self.package_installer_thread:
            self.package_installer_thread.terminated = True
            self.package_installer_thread.quit()
            try:
                if not self.package_installer_thread.wait(2000):
                    self.package_installer_thread.terminate()
                    self.package_installer_thread.wait(1000)
            except (RuntimeError, AttributeError):
                pass

    def on_password_success(self):
        self.failed_attempts = 0
        if self.parent:
            self.parent.failed_attempts = 0
        if self.package_installer_dialog:
            self.package_installer_dialog.update_failed_attempts(self.failed_attempts)
            self.package_installer_dialog.auth_failed = False

    def on_package_installer_finished(self):
        self.package_installer_thread = None
        self.package_installer_dialog = None


class StyleConfig:
    FONT_MAIN = "DejaVu Sans Mono"
    FONT_SUBPROCESS = "Hack"

    COLORS = {
        'primary': '#7aa2f7',
        'success': '#8fffab',
        'warning': '#e0af68',
        'error': '#ff5555',
        'info': '#7dcfff',
        'text': '#c0caf5',
        'background_primary': '#1a1b26',
        'background_secondary': '#24283b',
        'background_gradient_start': '#11141d',
        'background_gradient_end': '#222a3b',
        'border': '#414868',
        'muted': '#7c7c7c'
    }

    STYLE_MAP = {
        "operation": (FONT_MAIN, 16, "#6ffff5", 1.2),
        "info": (FONT_MAIN, 15, "#ceec9e", 1.0),
        "subprocess": (FONT_SUBPROCESS, 13, "#f9e7ff", 0.6),
        "success": (FONT_MAIN, 15, "#8fffab", 1.0),
        "warning": (FONT_MAIN, 15, "#ffaa00", 1.0),
        "error": (FONT_MAIN, 15, "#ff5555", 1.0)
    }

    @classmethod
    def get_style_string(cls, style_name):
        if style_name not in cls.STYLE_MAP:
            return ""

        font, size, color, line_height = cls.STYLE_MAP[style_name]
        return f"font-family: {font}; font-size: {size}px; color: {color}; padding: 5px; line-height: {line_height};"


class TaskStatus:
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    SUCCESS = "success"
    WARNING = "warning"
    ERROR = "error"


# noinspection PyUnresolvedReferences
class PackageInstallerDialog(QDialog):
    outputReceived = pyqtSignal(str, str)
    DIALOG_SIZE = (1400, 1100)
    CHECKLIST_WIDTH = 370
    BUTTON_SIZE = (145, 40)
    SHADOW_BLUR = 80
    SHADOW_OFFSET = 15

    def __init__(self, parent=None):
        super().__init__(parent)
        self._init_attributes()
        self._setup_ui_components()
        self.installer_thread = None
        self.current_task = None
        self.completed_message_shown = False
        self.has_error = False
        self.auth_failed = False
        self.setup_ui()

    def _init_attributes(self):
        self.task_status = {}
        self.task_descriptions = {}
        self.update_timer = QTimer(self)
        self.timer = QElapsedTimer()

    def _setup_ui_components(self):
        self.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
        self.setFixedSize(*self.DIALOG_SIZE)

        self.shadow = self._create_shadow_effect()

        self.layout = QHBoxLayout(self)
        self.left_panel = QVBoxLayout()
        self.right_panel = QVBoxLayout()

        self.scroll_area = QScrollArea()
        self.text_edit = QTextEdit()
        self.failed_attempts_label = QLabel(self)

        self.checklist_label = QLabel("Pending Operations:")
        self.checklist = QListWidget()
        self.elapsed_time_label = QLabel("\nElapsed time:\n00s\n")
        self.ok_button = QPushButton("Close")

    def _create_shadow_effect(self):
        shadow = QGraphicsDropShadowEffect()
        shadow.setBlurRadius(self.SHADOW_BLUR)
        shadow.setXOffset(self.SHADOW_OFFSET)
        shadow.setYOffset(self.SHADOW_OFFSET)
        shadow.setColor(QColor(0, 0, 0, 160))
        return shadow

    def setup_ui(self):
        self._apply_global_styles()
        self._configure_scroll_area()
        self._configure_text_edit()
        self._configure_checklist()
        self._configure_elapsed_time()
        self._configure_ok_button()
        self._setup_timers()
        self._setup_layout()

    def _apply_global_styles(self):
        colors = StyleConfig.COLORS
        self.setStyleSheet(f"""
            QDialog {{
                background-color: {colors['background_primary']};
                border-radius: 15px;
                border: 1px solid #2d2d44;
            }}
            QScrollArea {{
                background-color: #181b28;
                border: 1px solid {colors['border']};
                border-radius: 10px;
            }}
            QTextEdit {{
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0, 
                           stop:0 {colors['background_gradient_start']}, 
                           stop:1 {colors['background_gradient_end']});
                color: {colors['text']};
                border: none;
                border-radius: 8px;
            }}
            QLabel {{
                color: {colors['text']};
                font-size: 16px;
            }}
        """)

    def _configure_scroll_area(self):
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll_area.setWidget(self.text_edit)

    def _configure_text_edit(self):
        self.text_edit.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        self.text_edit.setReadOnly(True)
        self.text_edit.setHtml(
            "<p style='color: #55ff55; font-size: 20px; text-align: center; margin-top: 25px;'>"
            "<b>Package Installer</b><br>Initialization completed. Starting Package Installer</p>"
        )

    def _configure_checklist(self):
        colors = StyleConfig.COLORS
        border_style = self._get_border_style()

        self.checklist_label.setStyleSheet(f"""
            color: {colors['info']};
            font-size: 18px;
            font-weight: bold;
            padding: 10px;
            background: qlineargradient(x1:0, y1:0, x2:1, y2:0, 
                       stop:0 {colors['background_secondary']}, 
                       stop:1 {colors['border']});
            {border_style}
        """)

        self.checklist.setStyleSheet(f"""
            QListWidget {{
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0, 
                           stop:0 {colors['background_secondary']}, 
                           stop:1 {colors['border']});
                font-size: 15px;
                padding: 4px;
                {border_style}
            }}
            QListWidget::item {{
                padding: 4px;
                border-radius: 4px;
                border: 1px solid transparent;
            }}
        """)
        self.checklist.setFixedWidth(self.CHECKLIST_WIDTH)

    def _configure_elapsed_time(self):
        colors = StyleConfig.COLORS
        self.elapsed_time_label.setGraphicsEffect(self.shadow)
        self.elapsed_time_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.elapsed_time_label.setStyleSheet(f"""
            color: {colors['info']};
            font-size: 17px;
            {self._get_border_style()}
            background: qlineargradient(x1:0, y1:0, x2:1, y2:0, 
                       stop:0 {colors['background_secondary']}, 
                       stop:1 {colors['border']});
            text-align: center;
            font-weight: bold;
            padding: 3px;
        """)

    def _configure_ok_button(self):
        self.ok_button.setFixedSize(*self.BUTTON_SIZE)
        self.ok_button.clicked.connect(self.accept)
        self.ok_button.setEnabled(False)

    def _setup_timers(self):
        self.update_timer.timeout.connect(self.update_elapsed_time)
        self.timer.start()
        self.update_timer.start(1000)  # Update every second

    def _setup_layout(self):
        self.left_panel.addWidget(self.scroll_area)
        self._setup_failed_attempts_label()
        self.left_panel.addWidget(self.failed_attempts_label)

        self.right_panel.addWidget(self.checklist_label)
        self.right_panel.addWidget(self.checklist)
        self.right_panel.addStretch(1)
        self.right_panel.addWidget(self.elapsed_time_label)
        self.right_panel.addStretch(1)

        button_container = QHBoxLayout()
        button_container.addStretch()
        button_container.addWidget(self.ok_button)
        self.right_panel.addLayout(button_container)

        self.layout.addLayout(self.left_panel, 3)
        self.layout.addSpacing(10)
        self.layout.addLayout(self.right_panel, 1)

    def _setup_failed_attempts_label(self):
        self.failed_attempts_label.setStyleSheet(f"""
            color: {StyleConfig.COLORS['error']};
            font-size: 16px;
            font-weight: bold;
            padding: 10px;
            margin-top: 8px;
            border-radius: 8px;
            background-color: rgba(247, 118, 142, 0.15);
            border-left: 4px solid {StyleConfig.COLORS['error']};
        """)
        self.failed_attempts_label.setVisible(False)

    @staticmethod
    def _get_border_style():
        return f"""
            border-radius: 8px;
            border-right: 1px solid {StyleConfig.COLORS['primary']};
            border-top: 1px solid {StyleConfig.COLORS['primary']};
            border-bottom: 1px solid {StyleConfig.COLORS['primary']};
            border-left: 4px solid {StyleConfig.COLORS['primary']};
        """

    def initialize_checklist(self):
        self.checklist.clear()
        self.task_status.clear()

        cleaned_tasks = [
            (tid, desc.replace("...", "").replace("with 'yay'", ""))
            for tid, desc in self.task_descriptions
        ]

        for task_id, desc in cleaned_tasks:
            item = QListWidgetItem(desc)
            item.setData(Qt.ItemDataRole.UserRole, task_id)
            item.setIcon(QIcon.fromTheme("dialog-question"))
            item.setForeground(QColor(StyleConfig.COLORS['muted']))
            self.checklist.addItem(item)
            self.task_status[task_id] = TaskStatus.PENDING

        self._adjust_checklist_height()

    def _adjust_checklist_height(self):
        total_height = sum(
            self.checklist.sizeHintForRow(i)
            for i in range(self.checklist.count())
        )
        total_height += 2 * self.checklist.frameWidth()
        self.checklist.setFixedHeight(max(total_height, 40))

    def update_task_checklist_status(self, task_id, status):
        if task_id not in self.task_status:
            return

        self.task_status[task_id] = status

        if status in (TaskStatus.ERROR, TaskStatus.WARNING):
            self.has_error = True

        self._update_checklist_item_appearance(task_id, status)

    def _update_checklist_item_appearance(self, task_id, status):
        status_config = {
            TaskStatus.SUCCESS: (StyleConfig.COLORS['success'], "dialog-ok-apply"),
            TaskStatus.ERROR: (StyleConfig.COLORS['error'], "dialog-error"),
            TaskStatus.WARNING: (StyleConfig.COLORS['warning'], "dialog-warning"),
            TaskStatus.IN_PROGRESS: (StyleConfig.COLORS['info'], "media-playback-start")
        }

        if status not in status_config:
            return

        color, icon_name = status_config[status]

        for i in range(self.checklist.count()):
            item = self.checklist.item(i)
            if item.data(Qt.ItemDataRole.UserRole) == task_id:
                item.setIcon(QIcon.fromTheme(icon_name))
                item.setForeground(QColor(color))

                bg_color = QColor(color)
                bg_color.setAlpha(25)
                item.setBackground(bg_color)

                self.checklist.scrollToItem(item)
                break

    def update_operation_dialog(self, output: str, message_type: str = "info"):
        cursor = self.text_edit.textCursor()

        if self._handle_special_outputs(output, cursor):
            return

        if message_type == "finish":
            if not self.auth_failed:
                self._show_completion_message()
            return

        if message_type == "task_list":
            self._handle_task_list(output)
            return

        self._process_regular_output(output, message_type, cursor)

    def _handle_special_outputs(self, output: str, cursor: QTextCursor) -> bool:
        if "/var/lib/pacman/db.lck" in output:
            self._show_database_lock_error(cursor)
            return True
        return False

    def _show_database_lock_error(self, cursor: QTextCursor):
        error_html = f"""
        <hr style='border: none; margin: 10px 20px; border-top: 1px dashed rgba(247, 118, 142, 0.4);'>
        <div style='padding: 15px; margin: 10px; border-radius: 10px; border-left: 4px solid {StyleConfig.COLORS["error"]};'>
            <p style='color: {StyleConfig.COLORS["error"]}; font-size: 18px; text-align: center;'>
                <b>⚠️ Installation Aborted</b><br>
                <span style='font-size: 16px;'>'/var/lib/pacman/db.lck' detected!</span><br>
                <span style='color: {StyleConfig.COLORS["text"]}; font-size: 14px;'>
                    Remove using: <code>sudo rm -r /var/lib/pacman/db.lck</code>
                </span>
            </p>
        </div>
        """

        cursor.insertHtml(error_html)
        self._finalize_text_edit(cursor)
        self._enable_close_button()
        self._stop_installation()

    def _handle_task_list(self, output: str):
        try:
            self.task_descriptions = ast.literal_eval(output)
            self.initialize_checklist()
        except (SyntaxError, ValueError):
            pass

    def _process_regular_output(self, output: str, message_type: str, cursor: QTextCursor):
        if message_type not in StyleConfig.STYLE_MAP and "<span " not in output:
            return

        if "<span " in output:
            html_content = output
        else:
            html_content = self._format_output_as_html(output, message_type)

        for old, new in Options.text_replacements:
            html_content = html_content.replace(old, new)

        self._finalize_text_edit(cursor, html_content)

    @staticmethod
    def _format_output_as_html(output: str, message_type: str) -> str:
        style = StyleConfig.get_style_string(message_type)

        if message_type == "operation":
            return f"""
            <hr style='border: none; margin: 15px 30px; border-top: 1px dashed rgba(111, 255, 245, 0.3);'>
            <div style='padding: 10px; border-radius: 8px; margin: 5px 0;'>
                <p style='{style}'>{output}</p>
            </div><br>
            """
        else:
            lines = [
                f"<p style='{style}'>{line}</p>"
                for line in output.splitlines()
                if line.strip()
            ]
            return "\n".join(lines) + "<br>"

    def _finalize_text_edit(self, cursor: QTextCursor, html_content: str = None):
        try:
            cursor.movePosition(QTextCursor.MoveOperation.End)
            if html_content:
                cursor.insertHtml(html_content)
            self.text_edit.setTextCursor(cursor)

            scrollbar = self.text_edit.verticalScrollBar()
            if scrollbar:
                scrollbar.setValue(scrollbar.maximum())
        except Exception as e:
            print(f"Text edit update failed: {e}")

    def _show_completion_message(self):
        if self.completed_message_shown or self.auth_failed:
            return

        self.completed_message_shown = True
        self.update_timer.stop()

        is_error = self.has_error
        color = StyleConfig.COLORS['warning' if is_error else 'success']
        summary_text = "Completed with issues" if is_error else "Successfully Completed"
        icon = "⚠️" if is_error else "✅"
        message = f"Package Installer {'completed with warnings/errors' if is_error else 'successfully completed all operations<br>'}"

        color_obj = QColor(color)
        r, g, b = color_obj.red(), color_obj.green(), color_obj.blue()

        cursor = self.text_edit.textCursor()
        completion_html = f"""
        <hr style='border: none; margin: 25px 50px; border-top: 2px solid {color};'>
        <div style='text-align: center; padding: 20px; margin: 15px 30px;
                    border-radius: 15px; border: 1px solid rgba({r}, {g}, {b}, 0.3);'>
            <p style='color: {color}; font-size: 20px; font-weight: bold;'>{icon} {summary_text}</p>
            <p style='color: {color}; font-size: 18px;'>{message}</p>
        </div>
        """
        cursor.insertHtml(completion_html)

        self._enable_close_button()
        self._update_checklist_label_completion(icon, summary_text, color, r, g, b)
        self.text_edit.setTextCursor(cursor)

    def _enable_close_button(self):
        self.ok_button.setEnabled(True)
        self.ok_button.setFocus()

    def _stop_installation(self):
        self.update_timer.stop()
        if hasattr(self, 'installer_thread') and self.installer_thread and self.installer_thread.isRunning():
            self.installer_thread.terminate()

    def _update_checklist_label_completion(self, icon: str, summary_text: str, color: str, r: int, g: int, b: int):
        self.checklist_label.setText(f"{icon} {summary_text}")
        completion_style = f"""
            color: {color};
            font-size: 18px;
            font-weight: bold;
            padding: 10px;
            background-color: rgba({r}, {g}, {b}, 0.15);
            {self._get_border_style()}
        """
        self.checklist_label.setStyleSheet(completion_style)

    def update_elapsed_time(self):
        try:
            elapsed = max(0, int(self.timer.elapsed() / 1000))
            time_text = self._format_elapsed_time(elapsed)
            self.elapsed_time_label.setText(time_text)
        except Exception as e:
            print(f"Error in update_elapsed_time: {e}")
            self.elapsed_time_label.setText("\nElapsed time:\n--\n")

    @staticmethod
    def _format_elapsed_time(elapsed: int) -> str:
        """Format elapsed time as a readable string."""
        h, remainder = divmod(elapsed, 3600)
        m, s = divmod(remainder, 60)

        if h:
            return f"\nElapsed time:\n{h:02}h {m:02}m {s:02}s\n"
        elif m:
            return f"\nElapsed time:\n{m:02}m {s:02}s\n"
        else:
            return f"\nElapsed time:\n{s:02}s\n"

    def update_failed_attempts(self, failed_attempts: int):
        """Update failed authentication attempts display."""
        if failed_attempts > 0:
            text = f"⚠️ Failed Authentication Attempts: {failed_attempts}"
            self.failed_attempts_label.setText(text)
            self.failed_attempts_label.setVisible(True)
            self.auth_failed = True
            self._enable_close_button()

    def keyPressEvent(self, event):
        """Handle key press events."""
        key_handlers = {
            Qt.Key.Key_Down: self._handle_down_key,
            Qt.Key.Key_Escape: self._handle_escape_key,
            Qt.Key.Key_Tab: self.focusNextChild
        }

        handler = key_handlers.get(event.key())
        if handler:
            handler()
        else:
            super().keyPressEvent(event)

    def _handle_down_key(self):
        """Handle down arrow key press."""
        scrollbar = self.scroll_area.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())
        self.ok_button.setFocus()

    def _handle_escape_key(self):
        """Handle escape key press."""
        if self.completed_message_shown:
            self.close()
        # If not completed, ignore escape key

    def closeEvent(self, event):
        """Handle dialog close event."""
        # Prevent closing if operation is still in progress
        if not self.completed_message_shown and not self.auth_failed:
            event.ignore()
            return

        # Clean up installer thread
        self._cleanup_installer_thread()
        super().closeEvent(event)

    def _cleanup_installer_thread(self):
        """Clean up the installer thread."""
        if not (hasattr(self, 'installer_thread') and self.installer_thread):
            return

        if self.installer_thread.isRunning():
            self.installer_thread.terminated = True
            self.installer_thread.quit()

            try:
                if not self.installer_thread.wait(2000):  # Wait 2 seconds
                    self.installer_thread.terminate()
                    self.installer_thread.wait(1000)  # Wait 1 more second
            except RuntimeError as e:
                print(f"Thread cleanup warning: {e}")


# noinspection PyUnresolvedReferences
class PackageInstallerThread(QThread):
    started = pyqtSignal()
    outputReceived = pyqtSignal(str, str)
    passwordFailed = pyqtSignal()
    passwordSuccess = pyqtSignal()
    taskStatusChanged = pyqtSignal(str, str)
    finished = pyqtSignal()

    def __init__(self, sudo_password):
        super().__init__()
        self.enabled_tasks = None
        self.task_descriptions = None
        self.sudo_password = SecureString(sudo_password)
        self.auth_failed = self.has_error = self.terminated = False
        self.temp_dir = self.askpass_script_path = self.current_task = None
        self.task_status = {}
        self._installed_packages_cache = {}
        self.distro = LinuxDistroHelper()

    def run(self):
        self.started.emit()
        self.prepare_tasks()
        try:
            if self.terminated:
                return
            if not self.test_sudo_access():
                self.auth_failed = True
                self.passwordFailed.emit()
                return
            self.passwordSuccess.emit()
            if not self.auth_failed and not self.terminated:
                self.start_package_installer()
        except Exception as e:
            self.outputReceived.emit(f"Critical error during execution: {e}", "error")
            self.has_error = True
        finally:
            self.cleanup_temp_files()
            self.sudo_password.clear()

    def prepare_tasks(self):
        Options.load_config(Options.config_file_path)
        installer_operations = Options.installer_operations
        tasks = self._define_base_tasks()
        for service_task_id, (desc, name, pkgs) in self._define_service_tasks().items():
            def make_task(task_name, task_pkgs):
                return lambda: self.setup_service_with_packages(task_name, list(task_pkgs))
            tasks[service_task_id] = (desc, make_task(name, pkgs))
        tasks.update({"remove_orphaned_packages": ("Removing orphaned packages...", self.remove_orphaned_packages), "clean_cache": ("Cleaning cache...", self.clean_cache)})
        self.enabled_tasks = {tid: t for tid, t in tasks.items() if tid in installer_operations}
        self.task_descriptions = [(tid, desc) for tid, (desc, _) in self.enabled_tasks.items()]
        self.outputReceived.emit(str(self.task_descriptions), "task_list")

    def _define_base_tasks(self):
        return {"copy_system_files": ("Copying 'System Files'...", lambda: self.copy_files(self.parse_system_files(Options.system_files))),
                "update_mirrors": ("Updating mirrors...", lambda: self.update_mirrors("update_mirrors")),
                "set_user_shell": ("Setting user shell...", lambda: self.set_user_shell("set_user_shell")),
                "update_system": ("Updating system...", lambda: self.update_system("update_system")),
                "install_kernel_header": ("Installing kernel headers...", lambda: self.install_kernel_header("install_kernel_header")),
                "install_essential_packages": ("Installing 'Essential Packages'...", lambda: self.batch_install(Options.essential_packages, "Essential Package")),
                "install_yay": ("Installing 'yay'...", self.install_yay),
                "install_additional_packages": ("Installing 'Additional Packages' with 'yay'...", lambda: self.batch_install(Options.additional_packages, "Additional Package")),
                "install_specific_packages": ("Installing 'Specific Packages'...", self.install_specific_packages_based_on_session)}

    def _define_service_tasks(self):
        return {
            "enable_printer_support": (
                "Initializing printer support...",
                "cups",
                self.distro.get_printer_packages()
            ),
            "enable_samba_network_filesharing": (
                "Initializing samba...",
                "smb",
                self.distro.get_samba_packages()
            ),
            "enable_bluetooth_service": (
                "Initializing bluetooth...",
                "bluetooth",
                self.distro.get_bluetooth_packages()
            ),
            "enable_atd_service": (
                "Initializing atd...",
                "atd",
                self.distro.get_at_packages()
            ),
            "enable_cronie_service": (
                "Initializing cronie...",
                "cronie",
                self.distro.get_cron_packages()
            ),
            "enable_firewall": (
                "Initializing firewall...",
                "ufw",
                self.distro.get_firewall_packages()
            )
        }

    def reset_sudo_timeout(self):
        try:
            subprocess.run(['sudo', '-K'], stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
            time.sleep(0.5)
        except Exception as e:
            self.outputReceived.emit(f"Warning: Could not reset sudo state: {e}")

    def test_sudo_access(self):
        self.outputReceived.emit("Verifying sudo access...", "operation")
        self.reset_sudo_timeout()
        self.cleanup_temp_files()
        if not self.create_askpass_script():
            self.auth_failed = True
            return False
        try:
            env = os.environ.copy()
            env['SUDO_ASKPASS'] = self.askpass_script_path
            process = subprocess.run(['sudo', '-A', 'echo', 'Sudo access successfully verified...'], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env, timeout=0.2)
            if process.stdout:
                self.outputReceived.emit(process.stdout.strip(), "success")
            if process.stderr:
                self.outputReceived.emit(process.stderr.strip(), "error")
            return process.returncode == 0
        except subprocess.TimeoutExpired:
            self.auth_failed = self.has_error = True
            return False
        except Exception as e:
            self.auth_failed = self.has_error = True
            self.outputReceived.emit(f"Error during sudo authentication test: {e}", "error")
            return False

    def create_askpass_script(self):
        try:
            self.temp_dir = tempfile.mkdtemp(prefix="installer_")
            os.chmod(self.temp_dir, 0o700)
            self.askpass_script_path = Path(self.temp_dir, 'askpass.sh')
            self.askpass_script_path.write_text('#!/bin/sh\ncat "$SUDO_PASSWORD_FILE"', encoding='utf-8')
            os.chmod(self.askpass_script_path, 0o700)
            password_file = Path(self.temp_dir, 'sudo_pass')
            password_file.write_text(self.sudo_password.get_value(), encoding='utf-8')
            os.chmod(password_file, 0o600)
            os.environ['SUDO_PASSWORD_FILE'] = str(password_file)
            return True
        except Exception as e:
            self.outputReceived.emit(f"Error creating askpass script: {e}", "error")
            return False

    def cleanup_temp_files(self):
        if not self.temp_dir or not Path(self.temp_dir).exists():
            return
        try:
            for filename in ('sudo_pass', 'askpass.sh'):
                file_path = Path(self.temp_dir, filename)
                if file_path.exists():
                    try:
                        with open(file_path, 'wb') as f:
                            f.write(os.urandom(3072))
                        file_path.unlink()
                    except Exception as e:
                        self.outputReceived.emit(f"Error securely removing {filename}: {e}", "warning")
            for file_path in Path(self.temp_dir).glob('*'):
                if file_path.is_file():
                    try:
                        file_path.unlink()
                    except Exception as e:
                        self.outputReceived.emit(f"Error removing temporary file {file_path}: {e}", "warning")
            shutil.rmtree(self.temp_dir, ignore_errors=True)
            self.temp_dir = self.askpass_script_path = None
        except Exception as e:
            self.outputReceived.emit(f"Error cleaning up temporary files: {e}", "warning")

    def run_sudo_command(self, command):
        if self.terminated:
            return False
        try:
            env = os.environ.copy()
            env['SUDO_ASKPASS'] = self.askpass_script_path
            if isinstance(command, list):
                if command[0] == 'sudo' and '-A' not in command:
                    command.insert(1, '-A')
                elif command[0] == 'yay' and not any(arg.startswith('--sudoflags=') for arg in command):
                    command.insert(1, '--sudoflags=-A')
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env, bufsize=4096)
            return self._process_command_output(process)
        except Exception as e:
            self.outputReceived.emit(f"<span>Error during command execution: {e}</span>", "error")
            return False

    def _process_command_output(self, process):
        output_buffer, error_buffer = [], []
        def read_stream(stream, buffer, name):
            try:
                for line in iter(stream.readline, ''):
                    if not line or self.terminated:
                        break
                    line = line.strip()
                    if line:
                        buffer.append(line)
                        self.outputReceived.emit(f"<span>{line}</span>", "subprocess")
            except Exception as error:
                self.outputReceived.emit(f"<span>Error reading {name} stream: {error}</span>", "error")
        threads = [threading.Thread(target=read_stream, args=(process.stdout, output_buffer, "stdout"), daemon=True),
                   threading.Thread(target=read_stream, args=(process.stderr, error_buffer, "stderr"), daemon=True)]
        for t in threads: t.start()
        try:
            process.wait(timeout=600)
            for t in threads: t.join()
        except subprocess.TimeoutExpired:
            self.outputReceived.emit("<span>Command Timeout. Process is terminated...</span>", "error")
            process.kill()
            process.wait()
            return False
        if process.returncode != 0:
            self.outputReceived.emit(f"<span>Command error: {process.returncode}</span>", "error")
            if error_buffer:
                self.outputReceived.emit(f"<span>Error details: {' '.join(error_buffer)}</span>", "error")
        return process

    def start_package_installer(self):
        for task_id, (description, function) in self.enabled_tasks.items():
            if self.terminated:
                break
            self.current_task = task_id
            self.taskStatusChanged.emit(task_id, "in_progress")
            self.outputReceived.emit(description, "operation")
            try:
                success = function()
                status = "success" if success is not False else "error"
                self.taskStatusChanged.emit(task_id, status)
                if status == "error":
                    self.has_error = True
            except Exception as e:
                self.has_error = True
                self.outputReceived.emit(f"Task '{task_id}' failed: {e}", "error")
                self.taskStatusChanged.emit(task_id, "error")
        self.outputReceived.emit("", "finish")

    def parse_system_files(self, files):
        parsed_files = []
        for file in files:
            if not isinstance(file, dict):
                self.outputReceived.emit(f"Expected Dictionary but got: {type(file)}", "error")
                continue
            src, dest = file.get('source', '').strip(), file.get('destination', '').strip()
            if src and dest:
                parsed_files.append((src, dest))
            else:
                self.outputReceived.emit(f"Invalid Dictionary Format: {file}", "error")
        return parsed_files

    def copy_files(self, files):
        task_id, success = "copy_system_files", True
        if not files:
            self.outputReceived.emit("No 'System Files' to copy", "warning")
            self.taskStatusChanged.emit(task_id, "warning")
            return True
        for src, dest in files:
            if not Path(src).exists():
                self.outputReceived.emit(f"Source file does not exist: '{src}'", "error")
                success = False
                continue
            dest_dir = Path(dest).parent
            if not dest_dir.exists() and not self._create_directory(dest_dir):
                success = False
                continue
            filename = os.path.basename(src)
            self.outputReceived.emit(f"Copying: '{src}'", "info")
            cmd = ['sudo', 'cp', '-r'] if Path(src).is_dir() else ['sudo', 'cp']
            cmd.extend([str(src), str(dest)])
            result = self.run_sudo_command(cmd)
            if result and result.returncode == 0:
                self.outputReceived.emit(f"Successfully copied: '{filename}' to '{dest}'", "success")
            else:
                self.outputReceived.emit(f"Error copying: '{filename}'", "error")
                success = False
        self.taskStatusChanged.emit(task_id, "success" if success else "error")
        return success

    def _create_directory(self, dest_dir):
        result = self.run_sudo_command(['sudo', 'mkdir', '-p', str(dest_dir)])
        success = result and result.returncode == 0
        self.outputReceived.emit(f"{'Created' if success else 'Error creating'} directory: '{dest_dir}'", "info" if success else "error")
        return success

    def install_package_generic(self, package, package_type=None):
        type_str = package_type or "package"
        self.outputReceived.emit(f"Installing '{type_str}': '{package}'...", "info")
        if self.distro.package_is_installed(package):
            self.outputReceived.emit(f"'{package}' already present...", "success")
            return True
        cmd = self.distro.get_pkg_install_cmd(package)
        result = self.run_sudo_command(cmd.split())
        success = result and result.returncode == 0 and self.distro.package_is_installed(package)
        self.outputReceived.emit(f"'{package}' {'successfully installed' if success else 'failed to install'}...", "success" if success else "error")
        return success

    def batch_install(self, packages, package_type):
        task_id_map = {"Essential Package": "install_essential_packages", "Additional Package": "install_additional_packages"}
        task_id = task_id_map.get(package_type)
        pkgs = [p.strip() for p in (packages or []) if isinstance(p, str) and p.strip()]
        pkgs_to_install = self.distro.filter_not_installed(pkgs)
        if not pkgs_to_install:
            self.outputReceived.emit(f"All '{package_type}s' already present...", "success")
            if task_id: self.taskStatusChanged.emit(task_id, "success")
            return True
        if package_type == "Essential Package":
            cmd = self.distro.get_pkg_install_cmd(" ".join(pkgs_to_install))
        else:
            cmd = f"yay -S --noconfirm {' '.join(pkgs_to_install)}"
        self.run_sudo_command(cmd.split())
        failed = []
        installed = []
        for pkg in pkgs_to_install:
            if self.distro.package_is_installed(pkg):
                installed.append(pkg)
            else:
                failed.append(pkg)
        if failed:
            self.outputReceived.emit(f"Warning: Failed to install the following '{package_type}s': {', '.join(failed)}", "warning")
        if installed:
            self.outputReceived.emit(f"Successfully installed {len(installed)} of {len(pkgs_to_install)} '{package_type}s':\n{', '.join(f'\'{pkg}\'' for pkg in installed)}", "success")
        if task_id:
            status = "success" if not failed else "warning" if installed else "error"
            self.taskStatusChanged.emit(task_id, status)
        return not failed

    def update_mirrors(self, task_id):
        if self.distro.distro_id != "arch":
            self.outputReceived.emit("Mirror update is only supported for Arch Linux.", "info")
            self.taskStatusChanged.emit(task_id, "success")
            return True
        self.install_package_generic("reflector", package_type="Service Package")
        country = self._detect_country()
        command = ['sudo', 'reflector', '--verbose', '--latest', '10', '--protocol', 'https', '--sort', 'rate', '--save', '/etc/pacman.d/mirrorlist']
        if country:
            self.outputReceived.emit(f"<br>Detected country: {country}", "success")
            command.extend(['--country', country])
        else:
            self.outputReceived.emit("Unable to detect country. Searching globally instead.", "info")
        result = self.run_sudo_command(command)
        success = result and result.returncode == 0
        status = "success" if success else "error"
        self.outputReceived.emit(f"Mirrors {'successfully updated' if success else 'failed to update'}...", status)
        self.taskStatusChanged.emit(task_id, status)
        return success

    @staticmethod
    def _detect_country():
        urls = ['https://ipinfo.io/country', 'https://ifconfig.co/country-iso']
        for url in urls:
            try:
                with urllib.request.urlopen(url, timeout=5) as response:
                    if response.status == 200:
                        country = response.read().decode().strip()
                        if country and len(country) <= 3:
                            return country
            except (urllib.error.URLError, socket.timeout):
                continue
        return None

    def set_user_shell(self, task_id):
        config_shell = getattr(Options, "user_shell", "Bash").strip()
        shell_pkg = self.distro.get_shell_package_name(config_shell)
        shell_bin = shutil.which(shell_pkg) or f"/bin/{shell_pkg}"
        try:
            actual_user = os.environ.get('SUDO_USER') or os.environ.get('USER') or os.environ.get('LOGNAME') or getpass.getuser()
            current_shell = pwd.getpwnam(actual_user).pw_shell
            self.outputReceived.emit(f"Target user: '{actual_user}' (current shell: {current_shell})", "info")
        except Exception as e:
            self.outputReceived.emit(f"Error when determining the target user: {e}", "error")
            self.taskStatusChanged.emit(task_id, "error")
            return False
        if current_shell == shell_bin:
            self.outputReceived.emit(f"Current user shell already '{config_shell}'. No changes required...", "success")
            self.taskStatusChanged.emit(task_id, "success")
            return True
        if not self.distro.package_is_installed(shell_pkg):
            if not self.install_package_generic(shell_pkg, package_type="Shell Package"):
                self.outputReceived.emit(f"Error when installing '{shell_pkg}'.", "error")
                self.taskStatusChanged.emit(task_id, "error")
                return False
        if not Path(shell_bin).exists():
            self.outputReceived.emit(f"Shell binary '{shell_bin}' does not exist after installation.", "error")
            self.taskStatusChanged.emit(task_id, "error")
            return False
        try:
            with open("/etc/shells", "r") as f:
                shells = [line.strip() for line in f if line.strip()]
            if shell_bin not in shells:
                self.outputReceived.emit(f"Adding '{shell_bin}' to /etc/shells...", "info")
                append_cmd = ['sudo', 'sh', '-c', f'echo "{shell_bin}" >> /etc/shells']
                append_result = self.run_sudo_command(append_cmd)
                if not append_result or append_result.returncode != 0:
                    self.outputReceived.emit(f"Error when adding the shell to /etc/shells.", "error")
                    self.taskStatusChanged.emit(task_id, "error")
                    return False
            self.outputReceived.emit(f"Changing user shell for '{actual_user}' to '{shell_bin}'...", "info")
            chsh_cmd = ['sudo', 'chsh', '-s', shell_bin, actual_user]
            chsh_result = self.run_sudo_command(chsh_cmd)
            if chsh_result and chsh_result.returncode == 0:
                self.outputReceived.emit(f"Shell for user '{actual_user}' successfully changed to '{config_shell}'...", "success")
                self.taskStatusChanged.emit(task_id, "success")
                return True
            else:
                self.outputReceived.emit(f"Error when changing the shell for user '{actual_user}'.", "error")
                self.taskStatusChanged.emit(task_id, "error")
                return False
        except Exception as e:
            self.outputReceived.emit(f"Error while setting shell: {e}", "error")
            self.taskStatusChanged.emit(task_id, "error")
            return False

    def update_system(self, task_id):
        if self.distro.package_is_installed('yay'):
            cmd = "yay --noconfirm"
        else:
            cmd = self.distro.get_pkg_update_cmd()
        result = self.run_sudo_command(cmd.split())
        success = result and result.returncode == 0
        status = "success" if success else "error"
        self.outputReceived.emit(f"System {'successfully updated' if success else 'update failed'}...", status)
        self.taskStatusChanged.emit(task_id, status)
        return success

    def install_kernel_header(self, task_id):
        kernel_pkg = self.distro.get_kernel_headers_pkg()
        success = self.install_package_generic(kernel_pkg, package_type="Header Package")
        status = "success" if success else "error"
        self.taskStatusChanged.emit(task_id, status)
        return success

    def setup_service_with_packages(self, service, packages):
        task_id = {'cups': "enable_printer_support", 'smb': "enable_samba_network_filesharing", 'bluetooth': "enable_bluetooth_service",
                   'atd': "enable_atd_service", 'cronie': "enable_cronie_service", 'ufw': "enable_firewall"}.get(service)
        success = all(self.install_package_generic(pkg, package_type="Service Package") for pkg in packages)
        service_success = self.enable_service(service)
        if task_id:
            self.taskStatusChanged.emit(task_id, "success" if success and service_success else "error")
        return success and service_success

    def enable_service(self, service):
        self.outputReceived.emit(f"Enabling: '{service}.service'...", "info")
        is_active = subprocess.run(['systemctl', 'is-active', '--quiet', f'{service}.service'], check=False).returncode == 0
        if is_active:
            self.outputReceived.emit(f"'{service}.service' already enabled...", "success")
            return True
        self.outputReceived.emit("\n", "info")
        result = self.run_sudo_command(['sudo', 'systemctl', 'enable', '--now', f'{service}.service'])
        success = result and result.returncode == 0
        if success:
            self.outputReceived.emit(f"'{service}.service' successfully enabled...", "success")
            if service == "ufw":
                for ufw_cmd in (['sudo', 'ufw', 'default', 'deny'], ['sudo', 'ufw', 'enable'], ['sudo', 'ufw', 'reload']):
                    if not (self.run_sudo_command(ufw_cmd) or False):
                        success = False
        else:
            error = getattr(result, 'stderr', 'Unknown error')
            self.outputReceived.emit(f"Error enabling '{service}.service': {error}", "error")
        return success

    def install_yay(self):
        task_id = "install_yay"
        if not self.distro.supports_aur():
            self.outputReceived.emit("'yay' (AUR) is not supported on this distribution.", "warning")
            self.taskStatusChanged.emit(task_id, "warning")
            return True
        if self.distro.package_is_installed("yay"):
            self.outputReceived.emit("'yay' already present...", "success")
            self.taskStatusChanged.emit(task_id, "success")
            return True
        if not (self.run_sudo_command(self.distro.get_pkg_install_cmd("base-devel git go").split()) or False):
            self.taskStatusChanged.emit(task_id, "error")
            return False
        yay_build_path = Path(home_user) / "yay"
        if yay_build_path.exists():
            shutil.rmtree(yay_build_path) if yay_build_path.is_dir() else yay_build_path.unlink()
        def run_and_stream(cmd, cwd):
            try:
                with subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, cwd=cwd) as proc:
                    for line in proc.stdout:
                        self.outputReceived.emit(line.rstrip(), "subprocess")
                    return proc.wait() == 0
            except Exception as e:
                self.outputReceived.emit(f"Exception: {e}", "error")
                return False
        self.outputReceived.emit("Cloning 'yay' from git...", "subprocess")
        if not run_and_stream(['git', 'clone', 'https://aur.archlinux.org/yay.git'], home_user):
            self.taskStatusChanged.emit(task_id, "error")
            return False
        self.outputReceived.emit("Building package 'yay'...", "subprocess")
        if not run_and_stream(['makepkg', '-c', '--noconfirm'], yay_build_path):
            self.taskStatusChanged.emit(task_id, "error")
            return False
        to_remove = [pkg for pkg in ['yay-debug', 'go'] if self.distro.package_is_installed(pkg)]
        if to_remove:
            result = self.run_sudo_command(['sudo', 'pacman', '-R', '--noconfirm'] + to_remove)
            self.outputReceived.emit(f"{'Successfully removed' if result and result.returncode == 0 else 'Error during uninstallation of'}: '{', '.join(to_remove)}'", "subprocess" if result and result.returncode == 0 else "warning")
        pkg_files = sorted(f for f in os.listdir(yay_build_path) if f.endswith('.pkg.tar.zst'))
        if not pkg_files:
            self.outputReceived.emit("No package file found for installation.", "warning")
            self.taskStatusChanged.emit(task_id, "error")
            return False
        pkg_path = os.path.join(str(yay_build_path), pkg_files[0])
        result = self.run_sudo_command(['sudo', 'pacman', '-U', '--noconfirm', str(pkg_path)])
        success = result and result.returncode == 0
        shutil.rmtree(yay_build_path, ignore_errors=True)
        shutil.rmtree(Path(home_config) / "go", ignore_errors=True)
        self.outputReceived.emit(f"'yay' {'successfully installed' if success else 'installation failed'}...", "success" if success else "error")
        self.taskStatusChanged.emit(task_id, "success" if success else "error")
        return success

    def install_specific_packages_based_on_session(self):
        task_id = "install_specific_packages"
        session = None
        for var in ['XDG_CURRENT_DESKTOP', 'XDG_SESSION_DESKTOP', 'DESKTOP_SESSION']:
            val = os.getenv(var)
            if val:
                parts = [p.strip() for p in val.split(':') if p.strip()]
                for part in parts:
                    for env in SESSIONS:
                        if part.lower() == env.lower():
                            session = env
                            break
                    if session:
                        break
            if session:
                break
        if not session:
            self.outputReceived.emit("Unable to determine current desktop environment or window manager.", "warning")
            self.taskStatusChanged.emit(task_id, "error")
            return False
        self.outputReceived.emit(f"Detected session: {session}", "success")
        matching_packages = [spec_pkg.get('package') for spec_pkg in Options.specific_packages if spec_pkg.get('session') == session and 'package' in spec_pkg]
        pkgs_to_install = self.distro.filter_not_installed(matching_packages)
        if not pkgs_to_install:
            self.outputReceived.emit(f"All 'Specific Packages' for {session} already present...", "success")
            self.taskStatusChanged.emit(task_id, "success")
            return True
        cmd = self.distro.get_pkg_install_cmd(" ".join(pkgs_to_install))
        self.run_sudo_command(cmd.split())
        failed = []
        for pkg in pkgs_to_install:
            if not self.distro.package_is_installed(pkg):
                failed.append(pkg)
        success = not failed
        if failed:
            self.outputReceived.emit(f"Failed to install: {', '.join(failed)}", "warning")
        self.taskStatusChanged.emit(task_id, "success" if success else "error")
        return success

    def remove_orphaned_packages(self):
        task_id = "remove_orphaned_packages"
        find_orphans_cmd = self.distro.get_find_orphans_cmd()
        orphaned_packages = subprocess.run(find_orphans_cmd.split(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True).stdout.strip()
        if orphaned_packages:
            packages_list = orphaned_packages.split('\n')
            result = self.run_sudo_command(self.distro.get_pkg_remove_cmd(' '.join(packages_list)).split())
            success = result and result.returncode == 0
            self.outputReceived.emit(f"Orphaned packages {'successfully removed' if success else 'could not be removed'}...", "success" if success else "error")
        else:
            self.outputReceived.emit("No orphaned packages found...", "success")
            success = True
        self.taskStatusChanged.emit(task_id, "success" if success else "error")
        return success

    def clean_cache(self):
        task_id = "clean_cache"
        result = self.run_sudo_command(self.distro.get_clean_cache_cmd().split())
        success = result and result.returncode == 0
        self.outputReceived.emit("Cache of system package manager successfully cleaned..." if success else "Error cleaning cache...", "success" if success else "error")
        if self.distro.package_is_installed('yay'):
            self.outputReceived.emit("<br>Cleaning 'yay' cache...", "info")
            result_yay = self.run_sudo_command(['yay', '-Scc', '--noconfirm'])
            if result_yay and result_yay.returncode == 0:
                self.outputReceived.emit("'yay' cache successfully cleaned...", "success")
            else:
                self.outputReceived.emit("Error cleaning 'yay' cache...", "error")
                success = False
        self.taskStatusChanged.emit(task_id, "success" if success else "error")
        return success

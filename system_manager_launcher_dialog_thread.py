from __future__ import annotations
from pathlib import Path
from options import Options, SESSIONS
from drive_manager import DriveManager
from sudo_password import SecureString
from linux_distro_helper import LinuxDistroHelper
from PyQt6.QtGui import QColor, QIcon, QTextCursor
from PyQt6.QtCore import Qt, QElapsedTimer, QThread, QTimer, pyqtSignal
import ast, getpass, os, pwd, queue, shutil, socket, tempfile, threading, time, subprocess, urllib.error, urllib.request
from PyQt6.QtWidgets import (QApplication, QCheckBox, QDialog, QDialogButtonBox, QGraphicsDropShadowEffect, QHBoxLayout,
                             QLabel, QListWidget, QListWidgetItem, QPushButton, QScrollArea, QTextEdit, QVBoxLayout,
                             QWidget)

from logging_config import setup_logger
logger = setup_logger(__name__)

_USER        = pwd.getpwuid(os.getuid()).pw_name
_HOME        = os.getenv("HOME") or str(Path.home())
_HOME_CONFIG = Path(_HOME) / ".config"


# noinspection PyUnresolvedReferences
class SystemManagerLauncher:
    def __init__(self, parent=None):
        self.parent         = parent
        self.config         = getattr(parent, "config", {}) if parent else {}
        self.drive_manager  = DriveManager()
        self.failed_attempts = getattr(parent, "failed_attempts", 0)
        self.distro_helper  = LinuxDistroHelper()
        self.distro_name    = self.distro_helper.distro_pretty_name
        self.session        = self.distro_helper.detect_session()

        self.system_manager_thread: SystemManagerThread | None = None
        self.system_manager_dialog: SystemManagerDialog | None = None
        self.sudo_checkbox:  QCheckBox | None = None
        self.distro_label:   QLabel | None    = None

    def launch(self):
        if self.parent:
            self.parent.hide()
        try:
            self._confirm_and_start()
        finally:
            if self.parent:
                self.parent.show()

    def _confirm_and_start(self):
        ops        = self.config.get("system_manager_operations", [])
        _, _, tips = Options.generate_tooltip()
        op_text    = Options.get_system_manager_operation_text(self.distro_helper)
        op_text    = {k: v.replace("&&", "&") for k, v in op_text.items()}

        dialog, content_widget, content_layout = self._build_confirmation_dialog()
        self._populate_operations(ops, op_text, tips, content_layout)

        if self._run_dialog(dialog, content_widget):
            self._on_dialog_accepted(ops)

    def _build_confirmation_dialog(self):
        dialog = QDialog()
        dialog.setWindowTitle("System Manager")
        layout = QVBoxLayout()

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

        content_widget = QWidget()
        content_layout = QVBoxLayout(content_widget)
        content_layout.addWidget(self.distro_label)
        content_layout.addWidget(QLabel(
            "<span style='font-size:18px;'>System Manager will perform the following operations:<br></span>"
        ))
        content_layout.itemAt(content_layout.count() - 1).widget().setTextFormat(
            Qt.TextFormat.RichText
        )

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        scroll.setWidget(content_widget)
        layout.addWidget(scroll)
        dialog.setLayout(layout)
        return dialog, content_widget, content_layout

    def _populate_operations(self, ops, op_text, tips, layout):
        for i, key in enumerate(ops):
            if key in op_text:
                self._add_operation_row(i, op_text[key], tips.get(key, ""), layout)

    @staticmethod
    def _add_operation_row(index, text, tooltip, layout):
        has_tip    = bool(tooltip)
        colour     = "#9891c2;" if has_tip else "#c8beff;"
        decoration = "text-decoration:underline dotted;" if has_tip else ""
        icon       = "󰔨 " if has_tip else ""
        html = (
            f"{icon}   <span style='font-size:16px;padding:5px;"
            f"color:{colour}{decoration}'>{text}</span>"
        )
        row = QHBoxLayout()
        num = QLabel(f"{index + 1}:")
        num.setStyleSheet("font-size:16px;padding:5px;qproperty-alignment:AlignLeft")
        lbl = QLabel(html)
        lbl.setTextFormat(Qt.TextFormat.RichText)
        lbl.setStyleSheet("font-size:16px;padding:5px;qproperty-alignment:AlignLeft")
        if has_tip:
            lbl.setToolTip(tooltip)
            lbl.setCursor(Qt.CursorShape.WhatsThisCursor)
            lbl.setToolTipDuration(30000)
        row.addWidget(num)
        row.addWidget(lbl)
        row.addStretch(1)
        layout.addLayout(row)

    def _run_dialog(self, dialog, content_widget) -> bool:
        confirm = QLabel(
            "<span style='font-size:16px;'>Start System Manager?<br>"
            "(Check 'Enter sudo password' if a sudo password is set.)<br></span>"
        )
        btn_layout = QHBoxLayout()
        btn_box    = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Yes | QDialogButtonBox.StandardButton.No # type: ignore
        )
        btn_box.accepted.connect(dialog.accept)
        btn_box.rejected.connect(dialog.reject)

        self.sudo_checkbox = QCheckBox("Enter sudo password")
        self.sudo_checkbox.setStyleSheet("font-size:16px;color:#6ffff5")
        if self.failed_attempts:
            self.sudo_checkbox.setText("Sudo password must be entered!")
            self.sudo_checkbox.setChecked(True)
            self.sudo_checkbox.setEnabled(False)
            self.sudo_checkbox.setStyleSheet("color:#787878")

        btn_layout.addWidget(self.sudo_checkbox)
        btn_layout.addWidget(btn_box)
        content_widget.layout().addWidget(confirm)
        content_widget.layout().addLayout(btn_layout)

        screen = QApplication.primaryScreen()
        if screen:
            sg = screen.availableGeometry()
            sz = content_widget.sizeHint()
            dialog.resize(min(sz.width() + 40, sg.width()), min(sz.height() + 40, sg.height()))

        btn_box.button(QDialogButtonBox.StandardButton.No).setFocus()
        return dialog.exec() == QDialog.DialogCode.Accepted

    def _on_dialog_accepted(self, ops):
        if "copy_system_files" in ops:
            paths = []
            for f in self.config.get("system_files", []):
                if isinstance(f, dict):
                    paths.extend(f[k] for k in ("source", "destination") if k in f)
            drives = self.drive_manager.check_drives_to_mount(paths)
            if drives and not self.drive_manager.mount_required_drives(drives, self.parent):
                return

        if self.sudo_checkbox.isChecked():
            self._show_sudo_dialog()
        else:
            self._start_thread("")

    def _start_thread(self, sudo_password: str):
        self.system_manager_thread = SystemManagerThread(sudo_password)
        self.system_manager_dialog = SystemManagerDialog(self.parent)

        t, d = self.system_manager_thread, self.system_manager_dialog
        t.thread_started.connect(self._show_dialog)
        t.passwordFailed.connect(self._on_password_failed)
        t.passwordSuccess.connect(self._on_password_success)
        t.outputReceived.connect(d.update_operation_dialog)
        t.taskStatusChanged.connect(d.update_task_checklist_status)
        t.finished.connect(self._on_thread_finished)
        t.start()

    def _show_dialog(self):
        try:
            self.system_manager_dialog.exec()
        finally:
            self.drive_manager.unmount_drives()

    def _show_sudo_dialog(self):
        from sudo_password import SudoPasswordDialog
        dlg = SudoPasswordDialog(self.parent)
        dlg.sudo_password_entered.connect(self._start_thread)
        dlg.update_failed_attempts(self.failed_attempts)
        dlg.exec()

    def _on_password_failed(self):
        self.failed_attempts += 1
        if self.parent:
            self.parent.failed_attempts = self.failed_attempts
        d = self.system_manager_dialog
        if d:
            d.update_failed_attempts(self.failed_attempts)
            d.auth_failed = True
            d.update_operation_dialog(
                "<p style='color:#ff4a4d;font-size:18px;font-weight:bold;'>"
                "<br>Authentication failed. Canceling process to prevent account lockout."
                "<br>This could be due to:"
                "<ul><li>Incorrect or missing password</li>"
                "<li>Password is unauthorized</li>"
                "<li>User not in sudoers file</li>"
                "<li>Sudo configuration issue</li></ul>"
                "System Manager has been aborted to protect your system.</p>"
            )
            d.completed_message_shown = True
            d.update_timer.stop()
            d.has_error    = True
            d.ok_button.setEnabled(True)
        if self.system_manager_thread:
            self.system_manager_thread.terminated = True
            self.system_manager_thread.quit()
            self.system_manager_thread.wait(2000)

    def _on_password_success(self):
        self.failed_attempts = 0
        if self.parent:
            self.parent.failed_attempts = 0
        d = self.system_manager_dialog
        if d:
            d.update_failed_attempts(0)
            d.auth_failed = False

    def _on_thread_finished(self):
        self.system_manager_thread = None
        self.system_manager_dialog = None


class StyleConfig:
    FONT_MAIN      = "DejaVu Sans Mono, Fira Code, monospace"
    FONT_SUBPROCESS = "Hack, Fira Mono, monospace"
    COLORS = {
        "primary":                  "#7aa2f7",
        "success":                  "#8fffab",
        "warning":                  "#e0af68",
        "error":                    "#ff5555",
        "info":                     "#7dcfff",
        "text":                     "#c0caf5",
        "background_primary":       "#1a1b26",
        "background_secondary":     "#24283b",
        "background_gradient_start":"#11141d",
        "background_gradient_end":  "#222a3b",
        "border":                   "#414868",
        "muted":                    "#7c7c7c",
    }
    STYLE_MAP = {
        "operation":  (FONT_MAIN,       16, COLORS["info"],    1.2),
        "info":       (FONT_MAIN,       15, COLORS["success"], 1.0),
        "subprocess": (FONT_SUBPROCESS, 13, COLORS["text"],    0.6),
        "success":    (FONT_MAIN,       15, COLORS["success"], 1.0),
        "warning":    (FONT_MAIN,       15, COLORS["warning"], 1.0),
        "error":      (FONT_MAIN,       15, COLORS["error"],   1.0),
    }

    @classmethod
    def get_style_string(cls, style_name: str) -> str:
        style = cls.STYLE_MAP.get(style_name)
        if not style:
            return ""
        font, size, color, line_height = style
        return (
            f"font-family:{font};font-size:{size}px;color:{color};"
            f"padding:5px;line-height:{line_height};word-break:break-word;"
        )

    @classmethod
    def get_border_style(cls) -> str:
        p = cls.COLORS["primary"]
        return (
            f"border-radius:8px;"
            f"border-right:1px solid {p};"
            f"border-top:1px solid {p};"
            f"border-bottom:1px solid {p};"
            f"border-left:4px solid {p};"
        )


class TaskStatus:
    PENDING     = "pending"
    IN_PROGRESS = "in_progress"
    SUCCESS     = "success"
    WARNING     = "warning"
    ERROR       = "error"


class SystemManagerDialog(QDialog):
    outputReceived  = pyqtSignal(str, str)
    DIALOG_SIZE     = (1550, 1100)
    CHECKLIST_WIDTH = 370
    BUTTON_SIZE     = (145, 40)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.task_status:          dict[str, str]           = {}
        self.task_descriptions:    list[tuple[str, str]]    = []
        self.installer_thread:     SystemManagerThread | None = None
        self.completed_message_shown = False
        self.has_error    = False
        self.auth_failed  = False
        self.update_timer = QTimer(self)
        self.timer        = QElapsedTimer()
        self._build_ui()

    def _build_ui(self):
        self.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
        self.setFixedSize(*self.DIALOG_SIZE)

        shadow = QGraphicsDropShadowEffect()
        shadow.setBlurRadius(80)
        shadow.setXOffset(15)
        shadow.setYOffset(15)
        shadow.setColor(QColor(0, 0, 0, 160))

        c = StyleConfig.COLORS
        self.setStyleSheet(
            f"QTextEdit {{background:qlineargradient(x1:0,y1:0,x2:1,y2:0,"
            f"stop:0 {c['background_gradient_start']},stop:1 {c['background_gradient_end']});"
            f"color:{c['text']};border:none;border-radius:8px;}}"
        )

        self.scroll_area           = QScrollArea()
        self.text_edit             = QTextEdit()
        self.failed_attempts_label = QLabel(self)
        self.checklist_label       = QLabel("  Pending Operations:")
        self.checklist             = QListWidget()
        self.elapsed_time_label    = QLabel("\nElapsed time:\n00s\n")
        self.ok_button             = QPushButton("Close")

        self._style_checklist()
        self._style_elapsed_time(shadow)

        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll_area.setWidget(self.text_edit)

        self.text_edit.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        self.text_edit.setReadOnly(True)
        self.text_edit.setHtml(
            "<p style='color:#55ff55;font-size:20px;text-align:center;margin-top:25px;'>"
            "<b>System Manager</b><br>Initialization completed. Starting System Manager</p>"
        )

        self.ok_button.setFixedSize(*self.BUTTON_SIZE)
        self.ok_button.clicked.connect(self.accept)
        self.ok_button.setEnabled(False)

        self.failed_attempts_label.setStyleSheet(
            f"color:{c['error']};font-size:16px;font-weight:bold;padding:10px;"
            f"margin-top:8px;border-radius:8px;"
            f"background-color:rgba(247,118,142,0.15);"
            f"border-left:4px solid {c['error']};"
        )
        self.failed_attempts_label.setVisible(False)

        main_layout  = QHBoxLayout(self)
        left_panel   = QVBoxLayout()
        right_panel  = QVBoxLayout()

        left_panel.addWidget(self.scroll_area)
        left_panel.addWidget(self.failed_attempts_label)

        right_panel.addWidget(self.checklist_label)
        right_panel.addWidget(self.checklist)
        right_panel.addStretch(1)
        right_panel.addWidget(self.elapsed_time_label)
        right_panel.addStretch(1)
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        btn_row.addWidget(self.ok_button)
        right_panel.addLayout(btn_row)

        main_layout.addLayout(left_panel,  3)
        main_layout.addSpacing(10)
        main_layout.addLayout(right_panel, 1)

        self.update_timer.timeout.connect(self._update_elapsed_time)
        self.timer.start()
        self.update_timer.start(1000)

    def _style_checklist(self):
        c  = StyleConfig.COLORS
        bs = StyleConfig.get_border_style()
        self.checklist_label.setStyleSheet(
            f"color:{c['info']};font-size:18px;font-weight:bold;padding:10px;"
            f"background:qlineargradient(x1:0,y1:0,x2:1,y2:0,"
            f"stop:0 {c['background_secondary']},stop:1 {c['border']});{bs}"
        )
        self.checklist_label.setFixedWidth(self.CHECKLIST_WIDTH)
        self.checklist_label.setFixedSize(self.checklist_label.sizeHint())
        self.checklist.setStyleSheet(
            f"QListWidget{{background:qlineargradient(x1:0,y1:0,x2:1,y2:0,"
            f"stop:0 {c['background_secondary']},stop:1 {c['border']});"
            f"font-size:15px;padding:4px;{bs}}}"
            f"QListWidget::item{{padding:4px;border-radius:4px;border:1px solid transparent;}}"
        )
        self.checklist.setFixedWidth(self.CHECKLIST_WIDTH)

    def _style_elapsed_time(self, shadow):
        c  = StyleConfig.COLORS
        bs = StyleConfig.get_border_style()
        self.elapsed_time_label.setGraphicsEffect(shadow)
        self.elapsed_time_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.elapsed_time_label.setStyleSheet(
            f"color:{c['info']};font-size:17px;{bs}"
            f"background:qlineargradient(x1:0,y1:0,x2:1,y2:0,"
            f"stop:0 {c['background_secondary']},stop:1 {c['border']});"
            f"text-align:center;font-weight:bold;padding:3px;"
        )

    def initialize_checklist(self):
        self.checklist.clear()
        self.task_status.clear()
        for task_id, desc in self.task_descriptions:
            clean_desc = desc.replace("...", "").replace("with 'yay'", "")
            item = QListWidgetItem(clean_desc)
            item.setData(Qt.ItemDataRole.UserRole, task_id)
            item.setIcon(QIcon.fromTheme("dialog-question"))
            item.setForeground(QColor(StyleConfig.COLORS["muted"]))
            self.checklist.addItem(item)
            self.task_status[task_id] = TaskStatus.PENDING
        self._adjust_checklist_height()

    def _adjust_checklist_height(self):
        if not self.checklist.count():
            self.checklist.setFixedHeight(40)
            return
        total = sum(self.checklist.sizeHintForRow(i) for i in range(self.checklist.count()))
        total += 2 * self.checklist.frameWidth()
        self.checklist.setFixedHeight(max(total, 40))

    def update_task_checklist_status(self, task_id: str, status: str):
        if not task_id or task_id not in self.task_status:
            return
        if self.task_status.get(task_id) == status:
            return
        self.task_status[task_id] = status
        if status in (TaskStatus.ERROR, TaskStatus.WARNING):
            self.has_error = True
        status_cfg = {
            TaskStatus.SUCCESS:     (StyleConfig.COLORS["success"], "dialog-ok-apply"),
            TaskStatus.ERROR:       (StyleConfig.COLORS["error"],   "dialog-error"),
            TaskStatus.WARNING:     (StyleConfig.COLORS["warning"], "dialog-warning"),
            TaskStatus.IN_PROGRESS: (StyleConfig.COLORS["info"],    "media-playback-start"),
        }
        if status not in status_cfg:
            return
        colour, icon_name = status_cfg[status]
        for i in range(self.checklist.count()):
            item = self.checklist.item(i)
            if item and item.data(Qt.ItemDataRole.UserRole) == task_id:
                item.setIcon(QIcon.fromTheme(icon_name))
                item.setForeground(QColor(colour))
                bg = QColor(colour)
                bg.setAlpha(25)
                item.setBackground(bg)
                self.checklist.scrollToItem(item)
                break

    def update_operation_dialog(self, output: str, message_type: str = "info"):
        if "/var/lib/pacman/db.lck" in output:
            self._show_db_lock_error()
            return
        dispatch = {
            "finish":     self._show_completion_message,
            "task_list":  lambda: self._handle_task_list(output),
        }
        handler = dispatch.get(message_type)
        if handler:
            handler()
        else:
            self._process_output(output, message_type)

    def _show_db_lock_error(self):
        cursor = self.text_edit.textCursor()
        c      = StyleConfig.COLORS
        html   = (
            f"<hr style='border:none;margin:10px 20px;border-top:1px dashed rgba(247,118,142,0.4);'>"
            f"<div style='padding:15px;margin:10px;border-radius:10px;border-left:4px solid {c}[\"error\"];'>"
            f"<p style='color:{c}[\"error\"];font-size:18px;text-align:center;'>"
            f"<b>⚠️ Installation Aborted</b><br>"
            f"<span style='font-size:16px;'>'/var/lib/pacman/db.lck' detected!</span><br>"
            f"<span style='color:{c}[\"text\"];font-size:14px;'>"
            f"Remove using: <code>sudo rm -r /var/lib/pacman/db.lck</code></span></p></div>"
        )
        self._append_html(cursor, html)
        self.ok_button.setEnabled(True)
        self.ok_button.setFocus()
        self.update_timer.stop()
        if self.installer_thread and self.installer_thread.isRunning():
            self.installer_thread.terminated = True

    def _handle_task_list(self, output: str):
        try:
            self.task_descriptions = ast.literal_eval(output)
            self.initialize_checklist()
        except (SyntaxError, ValueError) as exc:
            self.outputReceived.emit(f"Error parsing task list: {exc}", "error")

    def _process_output(self, output: str, message_type: str):
        if message_type not in StyleConfig.STYLE_MAP and "<span " not in output:
            return
        cursor = self.text_edit.textCursor()
        html   = output if "<span " in output else self._format_html(output, message_type)
        for old, new in Options.text_replacements:
            html = html.replace(old, new)
        self._append_html(cursor, html)

    @staticmethod
    def _format_html(output: str, message_type: str) -> str:
        style = StyleConfig.get_style_string(message_type)
        if message_type == "operation":
            return (
                "<hr style='border:none;margin:15px 30px;"
                "border-top:1px dashed rgba(111,255,245,0.3);'>"
                f"<div style='padding:10px;border-radius:8px;margin:5px 0;'>"
                f"<p style='{style}'>{output}</p></div><br>"
            )
        lines = [f"<p style='{style}'>{line}</p>"
                 for line in output.splitlines() if line.strip()]
        return "\n".join(lines) + "<br>"

    def _append_html(self, cursor: QTextCursor, html: str):
        try:
            cursor = cursor or self.text_edit.textCursor()
            cursor.movePosition(QTextCursor.MoveOperation.End)
            if html:
                cursor.insertHtml(html)
            self.text_edit.setTextCursor(cursor)
            sb = self.text_edit.verticalScrollBar()
            if sb:
                sb.setValue(sb.maximum())
        except Exception as exc:
            logger.exception("Text edit update failed: %s", exc)

    def _show_completion_message(self):
        if self.completed_message_shown or self.auth_failed:
            return
        self.completed_message_shown = True
        self.update_timer.stop()

        is_error = self.has_error
        colour   = StyleConfig.COLORS["warning" if is_error else "success"]
        summary  = "Completed with issues" if is_error else "Successfully Completed"
        icon     = " ️" if is_error else "🗹"
        message  = ("completed with warnings/errors" if is_error
                    else "successfully completed all operations<br>")
        co       = QColor(colour)
        r, g, b  = co.red(), co.green(), co.blue()

        html = (
            f"<hr style='border:none;margin:25px 50px;border-top:2px solid {colour};'>"
            f"<div style='text-align:center;padding:20px;margin:15px 30px;"
            f"border-radius:15px;border:1px solid rgba({r},{g},{b},0.3);'>"
            f"<p style='color:{colour};font-size:20px;font-weight:bold;'>"
            f"{icon} {summary}</p>"
            f"<p style='color:{colour};font-size:18px;'>"
            f"System Manager {message}</p></div>"
        )
        cursor = self.text_edit.textCursor()
        cursor.insertHtml(html)
        self.text_edit.setTextCursor(cursor)

        self.ok_button.setEnabled(True)
        self.ok_button.setFocus()

        bs = StyleConfig.get_border_style()
        self.checklist_label.setText(f"{icon} {summary}")
        self.checklist_label.setStyleSheet(
            f"color:{colour};font-size:18px;font-weight:bold;padding:10px;"
            f"background-color:rgba({r},{g},{b},0.15);{bs}"
        )

    def _update_elapsed_time(self):
        try:
            elapsed = max(0, int(self.timer.elapsed() / 1000))
            h, rem  = divmod(elapsed, 3600)
            m, s    = divmod(rem, 60)
            txt = (f"\nElapsed time:\n{h:02}h {m:02}m {s:02}s\n" if h
                   else f"\nElapsed time:\n{m:02}m {s:02}s\n" if m
                   else f"\nElapsed time:\n{s:02}s\n")
            self.elapsed_time_label.setText(txt)
        except Exception as exc:
            logger.error("update_elapsed_time error: %s", exc)
            self.elapsed_time_label.setText("\nElapsed time:\n--\n")

    def update_failed_attempts(self, count: int):
        if count > 0:
            self.failed_attempts_label.setText(f" ️ Failed Authentication Attempts: {count}")
            self.failed_attempts_label.setVisible(True)
            self.auth_failed = True
            self.ok_button.setEnabled(True)
            self.ok_button.setFocus()

    def keyPressEvent(self, event):
        if not event:
            return
        key = event.key()
        if key == Qt.Key.Key_Down:
            sb = self.scroll_area.verticalScrollBar()
            sb.setValue(sb.maximum())
            self.ok_button.setFocus()
        elif key == Qt.Key.Key_Escape:
            if self.completed_message_shown:
                self.close()
        elif key == Qt.Key.Key_Tab:
            self.focusNextChild()
        else:
            super().keyPressEvent(event)

    def closeEvent(self, event):
        if not self.completed_message_shown and not self.auth_failed:
            event.ignore()
            return
        if self.installer_thread and self.installer_thread.isRunning():
            self.installer_thread.terminated = True
            self.installer_thread.quit()
            self.installer_thread.wait(2000)
        super().closeEvent(event)


class SystemManagerThread(QThread):
    thread_started    = pyqtSignal()
    outputReceived    = pyqtSignal(str, str)
    passwordFailed    = pyqtSignal()
    passwordSuccess   = pyqtSignal()
    taskStatusChanged = pyqtSignal(str, str)
    finished          = pyqtSignal()

    def __init__(self, sudo_password: str):
        super().__init__()
        self.sudo_password   = SecureString(sudo_password or "")
        self.enabled_tasks:  dict[str, tuple] = {}
        self.task_descriptions: list[tuple]   = []
        self.auth_failed     = False
        self.has_error       = False
        self.terminated      = False
        self.temp_dir:       str | None  = None
        self.askpass_script_path: Path | None = None
        self.current_task:   str | None  = None
        self.task_status:    dict        = {}

        try:
            self.distro       = LinuxDistroHelper()
            self.package_cache = PackageCache(self.distro)
        except Exception as exc:
            logger.warning("Could not initialize distro helper: %s", exc)
            self.distro        = None
            self.package_cache = None

    def run(self):
        self.thread_started.emit()
        self._prepare_tasks()
        try:
            if self.terminated:
                return
            if not self._test_sudo_access():
                self.auth_failed = True
                self.passwordFailed.emit()
                return
            self.passwordSuccess.emit()
            if not self.auth_failed and not self.terminated:
                self._run_all_tasks()
        except Exception as exc:
            self.outputReceived.emit(f"Critical error: {exc}", "error")
            self.has_error = True
        finally:
            self._cleanup_temp_files()
            self.sudo_password.clear()
            self.finished.emit()

    def _prepare_tasks(self):
        Options.load_config(Options.config_file_path)
        ops   = Options.system_manager_operations
        tasks = {**self._base_tasks(), **self._service_tasks()}
        tasks.update({
            "remove_orphaned_packages": ("Removing orphaned packages…", self._remove_orphaned_packages),
            "clean_cache":              ("Cleaning cache…",             self._clean_cache),
        })
        self.enabled_tasks      = {k: v for k, v in tasks.items() if k in ops}
        self.task_descriptions  = [(k, desc) for k, (desc, _) in self.enabled_tasks.items()]
        self.outputReceived.emit(str(self.task_descriptions), "task_list")

    def _base_tasks(self) -> dict:
        return {
            "copy_system_files":       ("Copying 'System Files'…",
                                        lambda: self._copy_files(self._parse_system_files(Options.system_files))),
            "update_mirrors":          ("Updating mirrors…",
                                        lambda: self._update_mirrors("update_mirrors")),
            "set_user_shell":          ("Setting user shell…",
                                        lambda: self._set_user_shell("set_user_shell")),
            "update_system":           ("Updating system…",
                                        lambda: self._update_system("update_system")),
            "install_kernel_header":   ("Installing kernel headers…",
                                        lambda: self._install_kernel_header("install_kernel_header")),
            "install_basic_packages":  ("Installing 'Basic Packages'…",
                                        lambda: self._batch_install(Options.basic_packages, "Basic Package")),
            "install_yay":             ("Installing 'yay'…",                self._install_yay),
            "install_aur_packages":    ("Installing 'AUR Packages' with 'yay'…",
                                        lambda: self._batch_install(Options.aur_packages, "AUR Package")),
            "install_specific_packages": ("Installing 'Specific Packages'…",
                                          self._install_specific_packages_for_session),
            "install_flatpak":         ("Installing Flatpak…", self._install_flatpak),
            "install_snap":            ("Installing Snap…",    self._install_snap),
        }

    def _service_tasks(self) -> dict:
        if not self.distro:
            return {}
        g = lambda m: getattr(self.distro, m, lambda: [])()
        ssh_svc = self.distro.get_ssh_service_name() if hasattr(self.distro, "get_ssh_service_name") else "sshd"

        def make_service_task(name, pkgs):
            return lambda n=name, p=pkgs: self._setup_service_with_packages(n, list(p))

        return {
            "enable_printer_support":           ("Initializing printer support…", make_service_task("cups",      g("get_printer_packages"))),
            "enable_ssh_service":               ("Initializing SSH server…",      make_service_task(ssh_svc,    g("get_ssh_packages"))),
            "enable_samba_network_filesharing": ("Initializing samba…",           make_service_task("smb",      g("get_samba_packages"))),
            "enable_bluetooth_service":         ("Initializing bluetooth…",       make_service_task("bluetooth",g("get_bluetooth_packages"))),
            "enable_atd_service":               ("Initializing atd…",             make_service_task("atd",      g("get_at_packages"))),
            "enable_cronie_service":            ("Initializing cronie…",          make_service_task("cronie",   g("get_cron_packages"))),
            "enable_firewall":                  ("Initializing firewall…",        make_service_task("ufw",      g("get_firewall_packages"))),
        }

    def _run_all_tasks(self):
        for task_id, (desc, fn) in self.enabled_tasks.items():
            if self.terminated:
                break
            self.current_task = task_id
            self.taskStatusChanged.emit(task_id, TaskStatus.IN_PROGRESS)
            self.outputReceived.emit(desc, "operation")
            try:
                success = fn()
                status  = "success" if success is not False else "error"
                self.taskStatusChanged.emit(task_id, status)
                if status == "error":
                    self.has_error = True
            except Exception as exc:
                self.has_error = True
                self.outputReceived.emit(f"Task '{task_id}' failed: {exc}", "error")
                self.taskStatusChanged.emit(task_id, "error")
        self.outputReceived.emit("", "finish")

    def _reset_sudo_timeout(self):
        try:
            subprocess.run(["sudo", "-K"],
                           stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
            time.sleep(0.5)
        except Exception as exc:
            self.outputReceived.emit(f"Warning: Could not reset sudo state: {exc}", "warning")

    def _test_sudo_access(self) -> bool:
        self.outputReceived.emit("Verifying sudo access…", "operation")
        self._reset_sudo_timeout()
        self._cleanup_temp_files()
        if not self._create_askpass_script():
            return False
        try:
            env = os.environ.copy()
            env["SUDO_ASKPASS"] = str(self.askpass_script_path)
            proc = subprocess.run(
                ["sudo", "-A", "echo", "Sudo access successfully verified…"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, env=env, timeout=5,
            )
            if proc.stdout:
                self.outputReceived.emit(proc.stdout.strip(), "success")
            if proc.stderr:
                self.outputReceived.emit(proc.stderr.strip(), "error")
            return proc.returncode == 0
        except subprocess.TimeoutExpired:
            self.auth_failed = self.has_error = True
            return False
        except Exception as exc:
            self.auth_failed = self.has_error = True
            self.outputReceived.emit(f"Sudo authentication error: {exc}", "error")
            return False

    def _create_askpass_script(self) -> bool:
        try:
            self.temp_dir = tempfile.mkdtemp(prefix="installer_")
            os.chmod(self.temp_dir, 0o700)
            self.askpass_script_path = Path(self.temp_dir, "askpass.sh")
            self.askpass_script_path.write_text('#!/bin/sh\ncat "$SUDO_PASSWORD_FILE"', encoding="utf-8")
            os.chmod(self.askpass_script_path, 0o700)
            pw_file = Path(self.temp_dir, "sudo_pass")
            pw_file.write_text(self.sudo_password.get_value(), encoding="utf-8")
            os.chmod(pw_file, 0o600)
            os.environ["SUDO_PASSWORD_FILE"] = str(pw_file)
            return True
        except Exception as exc:
            self.outputReceived.emit(f"Error creating askpass script: {exc}", "error")
            return False

    def _cleanup_temp_files(self):
        if not self.temp_dir:
            return
        tmp = Path(self.temp_dir)
        if not tmp.exists():
            return
        for name in ("sudo_pass", "askpass.sh"):
            fp = tmp / name
            if fp.exists():
                try:
                    size = max(fp.stat().st_size, 4096)
                    for pattern in (b"\x00" * 1024, b"\xFF" * 1024, os.urandom(1024)):
                        with open(fp, "wb") as f:
                            for off in range(0, size, len(pattern)):
                                f.write(pattern[: min(len(pattern), size - off)])
                            f.flush()
                            os.fsync(f.fileno())
                    fp.unlink()
                except (OSError, IOError) as exc:
                    logger.warning("Secure deletion failed for %s: %s", name, exc)
                    fp.unlink(missing_ok=True)
        os.environ.pop("SUDO_PASSWORD_FILE", None)
        try:
            shutil.rmtree(tmp, ignore_errors=True)
            self.temp_dir = self.askpass_script_path = None
        except Exception as exc:
            logger.warning("Temp dir cleanup failed: %s", exc)

    def _run_sudo_command(self, command: list[str]):
        if self.terminated:
            return None
        try:
            env = os.environ.copy()
            env["SUDO_ASKPASS"] = str(self.askpass_script_path)
            if command and command[0] == "sudo" and "-A" not in command:
                command.insert(1, "-A")
            elif command and command[0] == "yay" and not any(a.startswith("--sudoflags=") for a in command):
                command.insert(1, "--sudoflags=-A")
            proc = subprocess.Popen(
                command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, env=env, bufsize=4096,
            )
            return self._stream_process(proc)
        except Exception as exc:
            self.outputReceived.emit(f"<span>Error: {exc}</span>", "error")
            return None

    def _stream_process(self, proc):
        q: queue.Queue = queue.Queue()

        def reader(stream, label):
            try:
                for line in iter(stream.readline, ""):
                    if self.terminated:
                        break
                    line = line.strip()
                    if line:
                        q.put(("output", line))
            except Exception as exc:
                q.put(("error", f"Error reading {label}: {exc}"))
            finally:
                q.put(("done", label))

        threads = [
            threading.Thread(target=reader, args=(proc.stdout, "stdout"), daemon=True),
            threading.Thread(target=reader, args=(proc.stderr, "stderr"), daemon=True),
        ]
        for t in threads:
            t.start()

        done = 0
        idle = 0
        while done < 2:
            try:
                kind, content = q.get(timeout=1)
                idle = 0
                if kind == "output":
                    self.outputReceived.emit(f"<span>{content}</span>", "subprocess")
                elif kind == "error":
                    self.outputReceived.emit(f"<span>{content}</span>", "error")
                elif kind == "done":
                    done += 1
            except queue.Empty:
                idle += 1
                if self.terminated or idle > 300:
                    break

        rc = proc.poll()
        if rc is None:
            try:
                rc = proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                self.outputReceived.emit("<span>Command timeout. Terminating…</span>", "error")
                proc.kill()
                proc.wait(timeout=5)
                return None

        class _Result:
            def __init__(self, code): self.returncode = code
        return _Result(rc)

    def _parse_system_files(self, files):
        parsed = []
        for f in files:
            if not isinstance(f, dict):
                self.outputReceived.emit(f"Expected dict, got {type(f)}", "error")
                continue
            if f.get("disabled", False):
                self.outputReceived.emit(f"Skipping disabled 'System File': '{f.get('source','')}'", "info")
                continue
            src, dst = f.get("source", "").strip(), f.get("destination", "").strip()
            if src and dst:
                parsed.append((src, dst))
            else:
                self.outputReceived.emit(f"Invalid system file entry: {f}", "error")
        return parsed

    def _copy_files(self, files):
        task_id, ok = "copy_system_files", True
        if not files:
            self.outputReceived.emit("No 'System Files' to copy", "warning")
            self.taskStatusChanged.emit(task_id, "warning")
            return True
        for src, dst in files:
            if not Path(src).exists():
                self.outputReceived.emit(f"Source does not exist: '{src}'", "error")
                ok = False
                continue
            dst_dir = Path(dst).parent
            if not dst_dir.exists() and not self._create_directory(dst_dir):
                ok = False
                continue
            is_dir = Path(src).is_dir()
            cmd    = ["sudo", "cp", "-r"] if is_dir else ["sudo", "cp"]
            cmd   += [str(src), str(dst)]
            result = self._run_sudo_command(cmd)
            if result and result.returncode == 0:
                self.outputReceived.emit(f"Successfully copied:\n'{src}' → '{dst}'", "success")
            else:
                self.outputReceived.emit(f"Error copying:\n'{Path(src).name}'", "error")
                ok = False
        self.taskStatusChanged.emit(task_id, "success" if ok else "error")
        return ok

    def _create_directory(self, dst_dir) -> bool:
        try:
            resolved = Path(dst_dir).resolve()
            if not str(resolved).startswith("/"):
                self.outputReceived.emit(f"Invalid path: '{dst_dir}'", "error")
                return False
        except (OSError, ValueError) as exc:
            self.outputReceived.emit(f"Path error: {exc}", "error")
            return False
        result  = self._run_sudo_command(["sudo", "mkdir", "-p", str(resolved)])
        success = result and result.returncode == 0
        self.outputReceived.emit(
            f"{'Created' if success else 'Error creating'} directory: '{resolved}'",
            "info" if success else "error",
        )
        return success

    def _install_package(self, package: str, package_type: str | None = None) -> bool:
        kind = package_type or "package"
        self.outputReceived.emit(f"Installing '{kind}': '{package}'…", "info")
        if self.package_cache.is_installed(package):
            self.outputReceived.emit(f"'{package}' already present…", "success")
            return True
        cmd     = self.distro.get_pkg_install_cmd(package)
        result  = self._run_sudo_command(cmd.split())
        success = result and result.returncode == 0
        if success:
            self.package_cache.mark_installed(package)
            self.outputReceived.emit(f"'{package}' installed…", "success")
        else:
            self.outputReceived.emit(f"'{package}' failed to install…", "error")
        return success

    def _batch_install(self, packages, package_type: str) -> bool:
        task_id_map = {
            "Basic Package": "install_basic_packages",
            "AUR Package":   "install_aur_packages",
        }
        task_id = task_id_map.get(package_type)

        if not packages:
            self.outputReceived.emit(f"No '{package_type}s' specified…", "warning")
            if task_id: self.taskStatusChanged.emit(task_id, "warning")
            return True

        pkgs = []
        for p in packages:
            name       = (p.get("name", "").strip() if isinstance(p, dict) else str(p).strip())
            is_disabled = (p.get("disabled", False) if isinstance(p, dict) else False)
            if is_disabled:
                self.outputReceived.emit(f"Skipping disabled '{package_type}': '{name}'", "info")
                continue
            if name and all(c.isalnum() or c in "-_.+" for c in name):
                pkgs.append(name)
            elif name:
                self.outputReceived.emit(f"Invalid package name skipped: '{name}'", "warning")

        to_install = self.distro.filter_not_installed(pkgs)
        if not to_install:
            self.outputReceived.emit(f"All '{package_type}s' already present…", "success")
            if task_id: self.taskStatusChanged.emit(task_id, "success")
            return True

        batch_size     = 15 if package_type == "Basic Package" else 25
        failed, done   = [], []
        for i in range(0, len(to_install), batch_size):
            if self.terminated:
                break
            batch = to_install[i:i + batch_size]
            self.outputReceived.emit(f"Installing {len(batch)} package(s): {', '.join(batch)}…", "info")
            cmd = (self.distro.get_pkg_install_cmd(" ".join(batch))
                   if package_type == "Basic Package"
                   else f"yay -S --noconfirm --needed {' '.join(batch)}")
            self._run_sudo_command(cmd.split())
            for pkg in batch:
                (done if self.distro.package_is_installed(pkg) else failed).append(pkg)
                if self.distro.package_is_installed(pkg):
                    self.package_cache.mark_installed(pkg)
                else:
                    self.package_cache.invalidate(pkg)

        if failed:
            self.outputReceived.emit(
                f"Warning: Failed to install {len(failed)} '{package_type}s': {', '.join(failed)}", "warning")
        if done:
            self.outputReceived.emit(
                f"Installed {len(done)} of {len(to_install)} '{package_type}s'", "success")
        if task_id:
            status = "success" if not failed else "warning" if done else "error"
            self.taskStatusChanged.emit(task_id, status)
        return not failed

    def _update_mirrors(self, task_id: str) -> bool:
        if self.distro.distro_id != "arch":
            self.outputReceived.emit("Mirror update is only supported for Arch Linux.", "info")
            self.taskStatusChanged.emit(task_id, "success")
            return True
        self._install_package("reflector", "Service Package")
        country = self._detect_country()
        cmd     = ["sudo", "reflector", "--verbose", "--latest", "10",
                   "--protocol", "https", "--sort", "rate",
                   "--save", "/etc/pacman.d/mirrorlist"]
        if country:
            self.outputReceived.emit(f"<br>Detected country: {country}", "success")
            cmd += ["--country", country]
        else:
            self.outputReceived.emit("Unable to detect country. Searching globally.", "info")
        result  = self._run_sudo_command(cmd)
        success = result and result.returncode == 0
        status  = "success" if success else "error"
        self.outputReceived.emit(f"Mirrors {'updated' if success else 'failed to update'}…", status)
        self.taskStatusChanged.emit(task_id, status)
        return success

    @staticmethod
    def _detect_country() -> str | None:
        for url in ("https://ipinfo.io/country", "https://ifconfig.co/country-iso"):
            try:
                with urllib.request.urlopen(url, timeout=5) as resp:
                    if resp.status == 200:
                        country = resp.read().decode().strip()
                        if country and len(country) <= 3:
                            return country
            except (urllib.error.URLError, socket.timeout):
                continue
        return None

    def _set_user_shell(self, task_id: str) -> bool:
        config_shell = getattr(Options, "user_shell", "Bash").strip()
        shell_pkg    = self.distro.get_shell_package_name(config_shell)
        shell_bin    = shutil.which(shell_pkg) or f"/bin/{shell_pkg}"
        try:
            actual = (os.environ.get("SUDO_USER") or os.environ.get("USER")
                      or os.environ.get("LOGNAME") or getpass.getuser())
            current_shell = pwd.getpwnam(actual).pw_shell
            self.outputReceived.emit(f"Target user: '{actual}' (shell: {current_shell})", "info")
        except Exception as exc:
            self.outputReceived.emit(f"Error determining target user: {exc}", "error")
            self.taskStatusChanged.emit(task_id, "error")
            return False

        if current_shell == shell_bin:
            self.outputReceived.emit(f"Shell already '{config_shell}'. No changes needed…", "success")
            self.taskStatusChanged.emit(task_id, "success")
            return True
        if not self.distro.package_is_installed(shell_pkg):
            if not self._install_package(shell_pkg, "Shell Package"):
                self.taskStatusChanged.emit(task_id, "error")
                return False
        if not Path(shell_bin).exists():
            self.outputReceived.emit(f"Shell binary '{shell_bin}' not found after install.", "error")
            self.taskStatusChanged.emit(task_id, "error")
            return False

        try:
            with open("/etc/shells") as f:
                shells = [l.strip() for l in f if l.strip()]
            if shell_bin not in shells:
                self.outputReceived.emit(f"Adding '{shell_bin}' to /etc/shells…", "info")
                env = os.environ.copy()
                env["SUDO_ASKPASS"] = str(self.askpass_script_path)
                proc = subprocess.run(
                    ["sudo", "tee", "-a", "/etc/shells"],
                    input=shell_bin + "\n", capture_output=True, text=True, env=env, timeout=10,
                )
                if proc.returncode != 0:
                    self.outputReceived.emit("Error adding shell to /etc/shells.", "error")
                    self.taskStatusChanged.emit(task_id, "error")
                    return False
            result  = self._run_sudo_command(["sudo", "chsh", "-s", shell_bin, actual])
            success = result and result.returncode == 0
            self.outputReceived.emit(
                f"Shell for '{actual}' {'set to' if success else 'failed to change to'} '{config_shell}'…",
                "success" if success else "error",
            )
            self.taskStatusChanged.emit(task_id, "success" if success else "error")
            return success
        except Exception as exc:
            self.outputReceived.emit(f"Error setting shell: {exc}", "error")
            self.taskStatusChanged.emit(task_id, "error")
            return False

    def _update_system(self, task_id: str) -> bool:
        cmd     = ("yay --noconfirm" if self.distro.package_is_installed("yay")
                   else self.distro.get_pkg_update_cmd())
        result  = self._run_sudo_command(cmd.split())
        success = result and result.returncode == 0
        status  = "success" if success else "error"
        self.outputReceived.emit(f"System {'updated' if success else 'update failed'}…", status)
        self.taskStatusChanged.emit(task_id, status)
        return success

    def _install_kernel_header(self, task_id: str) -> bool:
        pkg     = self.distro.get_kernel_headers_pkg()
        success = self._install_package(pkg, "Header Package")
        self.taskStatusChanged.emit(task_id, "success" if success else "error")
        return success

    def _setup_service_with_packages(self, service: str, packages: list) -> bool:
        task_id_map = {
            "cups": "enable_printer_support", "sshd": "enable_ssh_service",
            "ssh":  "enable_ssh_service",     "smb":  "enable_samba_network_filesharing",
            "bluetooth": "enable_bluetooth_service", "atd": "enable_atd_service",
            "cronie": "enable_cronie_service", "ufw": "enable_firewall",
        }
        task_id = task_id_map.get(service)
        pkg_ok  = all(self._install_package(p, "Service Package") for p in packages)
        svc_ok  = self._enable_service(service)
        success = pkg_ok and svc_ok
        if task_id:
            self.taskStatusChanged.emit(task_id, "success" if success else "error")
        return success

    def _enable_service(self, service: str) -> bool:
        self.outputReceived.emit(f"Enabling: '{service}.service'…", "info")
        if subprocess.run(
            ["systemctl", "is-active", "--quiet", f"{service}.service"], check=False
        ).returncode == 0:
            self.outputReceived.emit(f"'{service}.service' already enabled…", "success")
            return True
        result  = self._run_sudo_command(["sudo", "systemctl", "enable", "--now", f"{service}.service"])
        success = result and result.returncode == 0
        if success:
            self.outputReceived.emit(f"'{service}.service' enabled…", "success")
            if service == "ufw":
                for ufw_cmd in (["sudo", "ufw", "default", "deny"],
                                ["sudo", "ufw", "enable"],
                                ["sudo", "ufw", "reload"]):
                    r = self._run_sudo_command(ufw_cmd)
                    if not r or r.returncode != 0:
                        success = False
        else:
            rc = getattr(result, "returncode", "N/A")
            self.outputReceived.emit(f"Error enabling '{service}.service' (exit: {rc})", "error")
        return success

    def _install_yay(self) -> bool:
        task_id = "install_yay"
        if not self.distro.supports_aur():
            self.outputReceived.emit("'yay' is not supported on this distribution.", "warning")
            self.taskStatusChanged.emit(task_id, "warning")
            return True
        if self.distro.package_is_installed("yay"):
            self.outputReceived.emit("'yay' already present…", "success")
            self.taskStatusChanged.emit(task_id, "success")
            return True

        required = ["base-devel", "git", "go"]
        missing  = [p for p in required if not self.distro.package_is_installed(p)]
        if missing:
            result = self._run_sudo_command(self.distro.get_pkg_install_cmd(" ".join(missing)).split())
            if not result or result.returncode != 0:
                self.taskStatusChanged.emit(task_id, "error")
                return False

        yay_dir = Path(_HOME) / "yay"
        if yay_dir.exists():
            shutil.rmtree(yay_dir) if yay_dir.is_dir() else yay_dir.unlink()

        def stream(cmd, cwd):
            try:
                with subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                      stderr=subprocess.STDOUT, text=True, cwd=cwd) as p:
                    for line in p.stdout:
                        self.outputReceived.emit(line.rstrip(), "subprocess")
                    return p.wait() == 0
            except Exception as exc:
                self.outputReceived.emit(f"Exception: {exc}", "error")
                return False

        self.outputReceived.emit("Cloning 'yay'…", "subprocess")
        if not stream(["git", "clone", "https://aur.archlinux.org/yay.git"], _HOME):
            self.taskStatusChanged.emit(task_id, "error")
            return False

        self.outputReceived.emit("Building 'yay'…", "subprocess")
        if not stream(["makepkg", "-c", "--noconfirm"], yay_dir):
            self.taskStatusChanged.emit(task_id, "error")
            return False

        to_remove = [p for p in ("yay-debug", "go") if self.distro.package_is_installed(p)]
        if to_remove:
            r = self._run_sudo_command(["sudo", "pacman", "-R", "--noconfirm"] + to_remove)
            ok = r and r.returncode == 0
            self.outputReceived.emit(
                f"{'Removed' if ok else 'Error removing'}: '{', '.join(to_remove)}'",
                "subprocess" if ok else "warning",
            )

        pkg_files = sorted(f for f in os.listdir(yay_dir) if f.endswith(".pkg.tar.zst"))
        if not pkg_files:
            self.outputReceived.emit("No package file found.", "warning")
            self.taskStatusChanged.emit(task_id, "error")
            return False

        result  = self._run_sudo_command(["sudo", "pacman", "-U", "--noconfirm",
                                          str(yay_dir / pkg_files[0])])
        success = result and result.returncode == 0
        shutil.rmtree(yay_dir, ignore_errors=True)
        shutil.rmtree(_HOME_CONFIG / "go", ignore_errors=True)
        self.outputReceived.emit(
            f"'yay' {'installed' if success else 'installation failed'}…",
            "success" if success else "error",
        )
        self.taskStatusChanged.emit(task_id, "success" if success else "error")
        return success

    def _install_specific_packages_for_session(self) -> bool:
        task_id = "install_specific_packages"
        session = None
        for var in ("XDG_CURRENT_DESKTOP", "XDG_SESSION_DESKTOP", "DESKTOP_SESSION"):
            val = os.getenv(var)
            if val:
                for part in (p.strip() for p in val.split(":")):
                    for s in SESSIONS:
                        if part.lower() == s.lower():
                            session = s
                            break
                    if session:
                        break
            if session:
                break

        if not session:
            self.outputReceived.emit("Unable to determine desktop environment.", "warning")
            self.taskStatusChanged.emit(task_id, "error")
            return False

        self.outputReceived.emit(f"Detected session: {session}", "success")
        matching = [
            p["package"] for p in Options.specific_packages
            if p.get("session") == session and "package" in p and not p.get("disabled", False)
        ]
        to_install = self.distro.filter_not_installed(matching)
        if not to_install:
            self.outputReceived.emit(f"All 'Specific Packages' for {session} already present…", "success")
            self.taskStatusChanged.emit(task_id, "success")
            return True

        cmd    = self.distro.get_pkg_install_cmd(" ".join(to_install))
        self._run_sudo_command(cmd.split())
        failed = [p for p in to_install if not self.distro.package_is_installed(p)]
        if failed:
            self.outputReceived.emit(f"Failed to install: {', '.join(failed)}", "warning")
        success = not failed
        self.taskStatusChanged.emit(task_id, "success" if success else "error")
        return success

    def _install_flatpak(self) -> bool:
        task_id  = "install_flatpak"
        packages = self.distro.get_flatpak_packages()
        success  = all(self._install_package(p, "Flatpak Package") for p in packages)
        if success:
            flathub = self.distro.flatpak_add_flathub()
            self.outputReceived.emit("Adding Flathub remote…", "info")
            try:
                r = subprocess.run(flathub.split(), capture_output=True, text=True, timeout=30)
                if r.returncode == 0:
                    self.outputReceived.emit("Flathub remote added…", "success")
                else:
                    self.outputReceived.emit(f"Warning: Could not add Flathub: {r.stderr.strip()}", "warning")
            except Exception as exc:
                self.outputReceived.emit(f"Warning: Flathub setup failed: {exc}", "warning")
        self.taskStatusChanged.emit(task_id, "success" if success else "error")
        return success

    def _install_snap(self) -> bool:
        task_id  = "install_snap"
        packages = self.distro.get_snap_packages()
        success  = all(self._install_package(p, "Snap Package") for p in packages)
        if success:
            success = self._enable_service("snapd")
        self.taskStatusChanged.emit(task_id, "success" if success else "error")
        return success

    def _remove_orphaned_packages(self) -> bool:
        task_id = "remove_orphaned_packages"
        try:
            out = subprocess.run(
                self.distro.get_find_orphans_cmd(),
                shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            ).stdout.strip()
        except Exception as exc:
            self.outputReceived.emit(f"Error finding orphaned packages: {exc}", "error")
            self.taskStatusChanged.emit(task_id, "error")
            return False

        pkgs = [p for p in out.splitlines() if p.strip()]
        if not pkgs:
            self.outputReceived.emit("No orphaned packages found…", "success")
            self.taskStatusChanged.emit(task_id, "success")
            return True

        result  = self._run_sudo_command(self.distro.get_pkg_remove_cmd(" ".join(pkgs)).split())
        success = result and result.returncode == 0
        self.outputReceived.emit(
            f"Orphaned packages {'removed' if success else 'could not be removed'}…",
            "success" if success else "error",
        )
        self.taskStatusChanged.emit(task_id, "success" if success else "error")
        return success

    def _clean_cache(self) -> bool:
        task_id = "clean_cache"
        result  = self._run_sudo_command(self.distro.get_clean_cache_cmd().split())
        success = result and result.returncode == 0
        self.outputReceived.emit(
            "Cache cleaned…" if success else "Error cleaning cache…",
            "success" if success else "error",
        )
        if self.distro.package_is_installed("yay"):
            self.outputReceived.emit("<br>Cleaning 'yay' cache…", "info")
            ry = self._run_sudo_command(["yay", "-Scc", "--noconfirm"])
            if ry and ry.returncode == 0:
                self.outputReceived.emit("'yay' cache cleaned…", "success")
            else:
                self.outputReceived.emit("Error cleaning 'yay' cache…", "error")
                success = False
        self.taskStatusChanged.emit(task_id, "success" if success else "error")
        return success


class PackageCache:
    _CACHE_DURATION  = 600
    _MAX_CACHE_SIZE  = 1000

    def __init__(self, distro_helper):
        self.distro     = distro_helper
        self._cache:    dict[str, bool] = {}
        self._ts        = 0.0
        self._lock      = threading.Lock()

    def is_installed(self, package: str) -> bool:
        now = time.time()
        with self._lock:
            if now - self._ts > self._CACHE_DURATION:
                self._cache.clear()
                self._ts = now
            elif len(self._cache) > self._MAX_CACHE_SIZE:
                for k in list(self._cache)[:len(self._cache) // 2]:
                    self._cache.pop(k, None)
            if package not in self._cache:
                try:
                    self._cache[package] = self.distro.package_is_installed(package)
                except Exception as exc:
                    logger.warning("Package check failed for %s: %s", package, exc)
                    return False
            return self._cache[package]

    def mark_installed(self, package: str):
        with self._lock:
            self._cache[package] = True

    def invalidate(self, package: str | None = None):
        with self._lock:
            if package:
                self._cache.pop(package, None)
            else:
                self._cache.clear()

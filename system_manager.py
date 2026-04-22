import html as _html
import os
import pwd
import queue
import re
import secrets
import select as _select
import shlex
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from typing import Optional

from PyQt6.QtCore import Qt, QElapsedTimer, QThread, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QIcon, QTextCursor
from PyQt6.QtWidgets import (
    QApplication, QDialog, QGraphicsDropShadowEffect, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QPushButton, QTextEdit, QVBoxLayout, QWidget,
)

from linux_distro_helper import LinuxDistroHelper, ARCH_KERNEL_VARIANTS
from state import S, _HOME, _USER, logger, apply_replacements, _ANSI_RE, active_pkg_names, active_system_files
from themes import current_theme, font_sz
from ui_utils import _StandardKeysMixin


def _zero(buf: bytearray) -> None: buf[:] = bytearray(len(buf))


def _pw_bytes(pw) -> bytearray: return bytearray(pw.get_bytes() + b"\n") if pw else bytearray()


_INFO_RE = re.compile(
    r"INFO:|rating|mirror|download|synchroniz|\$srcdir/|Success|Branch|Transmission|"
    r"avg speed|=====|OK|Status Explanation|Status Legend:|Clone|PGP|MiB|Done|\.\.\.",
    re.IGNORECASE,
)


_PACMAN_PROGRESS_RE = re.compile(r'\[[-Co# ]+]\s*\d+%')


class _Status: PENDING, IN_PROGRESS, SUCCESS, WARNING, ERROR = ("pending", "in_progress", "success", "warning", "error")


_FONT_MONO = "DejaVu Sans Mono, Noto Sans Mono"
_FONT_SANS = "Hack, Noto Serif, monospace"


class _Style:
    KIND_CFG: dict[str, tuple] = {"operation": (_FONT_MONO, 16, 1.2, "info"), "info": (_FONT_MONO, 15, 1.0, "success"),
                                  "subprocess":(_FONT_SANS, 13, 0.85, "text"), "success": (_FONT_MONO, 15, 1.0, "success"),
                                  "warning": (_FONT_MONO, 15, 1.0, "warning"), "error": (_FONT_MONO, 15, 1.0, "error"),
                                  "dimmed": (_FONT_SANS, 12, 0.95, "muted")}

    STATUS_CFG: dict[str, tuple] = {_Status.SUCCESS: ("success", "dialog-ok-apply"), _Status.ERROR: ("error", "dialog-error"),
                                    _Status.WARNING: ("warning", "dialog-warning"),  _Status.IN_PROGRESS: ("info", "media-playback-start")}

    @classmethod
    def style_str(cls, kind: str) -> str:
        cfg = cls.KIND_CFG.get(kind)
        if not cfg: return ""
        font, size, lh, ck = cfg
        color = current_theme().get(ck, current_theme()["text"])
        return f"font-family:{font};font-size:{size}px;color:{color};padding:5px;line-height:{lh};word-break:break-word;"

    @staticmethod
    def border_style() -> str:
        p = current_theme()["accent"]
        return (f"border-radius:8px;border-right:1px solid {p};border-top:1px solid {p};"
                f"border-bottom:1px solid {p};border-left:4px solid {p};")


def _fmt_html(text: str, kind: str) -> str:
    t, style = current_theme(), _Style.style_str(kind)
    if kind == "operation":
        return (f"<hr style='border:none;margin:15px 30px;border-top:1px dashed {t['header_sep']};'>"
                f"<div style='padding:10px;border-radius:8px;margin:5px 0;'>"
                f"<p style='{style}'>{_html.escape(apply_replacements(text))}</p></div><br>")
    lines = [f"<p style='{style}'>{_html.escape(apply_replacements(ln))}</p>"
             for ln in text.splitlines() if ln.strip()]
    return "\n".join(lines) + "<br>"


class _SudoKeepalive(threading.Thread):
    _INTERVAL = 240

    def __init__(self, stop_event: threading.Event, pw=None) -> None:
        super().__init__(daemon=True, name="sudo-keepalive")
        self._stop, self._pw = stop_event, pw

    def run(self) -> None:
        while not self._stop.wait(self._INTERVAL):
            try:
                r = subprocess.run(["sudo", "-n", "-v"], capture_output=True, timeout=10)
                if r.returncode != 0 and self._pw:
                    buf = _pw_bytes(self._pw)
                    try:
                        subprocess.run(["sudo", "-S", "-v"], input=buf, capture_output=True, timeout=10)
                    finally:
                        _zero(buf)
            except Exception as exc:
                logger.debug("_SudoKeepalive: %s", exc)


class _PackageCache:
    _TTL, _MAX = 600, 1000

    def __init__(self, distro) -> None:
        self._distro = distro
        self._cache: dict[str, tuple[bool, float]] = {}
        self._lock = threading.Lock()

    def is_installed(self, pkg: str) -> bool:
        now = time.monotonic()
        with self._lock:
            entry = self._cache.get(pkg)
            if entry is not None and now - entry[1] < self._TTL:
                return entry[0]
        result = self._distro.package_is_installed(pkg)
        with self._lock:
            if len(self._cache) >= self._MAX:
                oldest = min(self._cache, key=lambda k: self._cache[k][1])
                del self._cache[oldest]
            self._cache[pkg] = (result, time.monotonic())
        return result

    def mark_installed(self, pkg: str) -> None:
        with self._lock:
            self._cache[pkg] = (True, time.monotonic())


class SystemManagerDialog(_StandardKeysMixin, QDialog):
    DIALOG_SIZE = (1875, 1000)
    BUTTON_SIZE = (160, 50)
    RIGHT_W = 370
    cancelRequested = pyqtSignal()
    inputProvided   = pyqtSignal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("System Manager")
        self._task_status: dict[str, str] = {}
        self._done = self._auth_failed = self._has_error = False
        self._timer, self._ticker = QElapsedTimer(), QTimer(self)
        self._build_ui()

    def _build_ui(self) -> None:
        t, bs = current_theme(), _Style.border_style()
        grad = f"background:qlineargradient(x1:0,y1:0,x2:1,y2:0,stop:0 {t['bg2']},stop:1 {t['header_sep']});"

        self.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
        self.setMinimumSize(*self.DIALOG_SIZE)
        self.setStyleSheet(f"QTextEdit{{background:qlineargradient(x1:0,y1:0,x2:1,y2:0,stop:0 {t['bg']},stop:1 {t['bg2']});"
                           f"color:{t['text']};border:none;border-radius:8px;}}")

        self._text_edit = QTextEdit()
        self._text_edit.setReadOnly(True)
        self._text_edit.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        self._text_edit.setHtml(f"<p style='color:{t['success']};font-size:{font_sz(6)}px;text-align:center;margin-top:25px;'>"
                                f"<b>System Manager</b><br>Initialising…</p>")

        self._fail_lbl = QLabel()
        self._fail_lbl.setWordWrap(True)
        self._fail_lbl.setVisible(False)
        self._fail_lbl.setMinimumHeight(52)
        self._fail_lbl.setStyleSheet(f"color:{t['error']};font-size:{font_sz(2)}px;font-weight:bold;padding:10px;"
                                     f"margin-top:8px;border-radius:8px;background-color:{t['bg3']};border-left:4px solid {t['error']};")

        shadow = QGraphicsDropShadowEffect()
        shadow.setBlurRadius(80)
        shadow.setXOffset(15)
        shadow.setYOffset(15)

        self._checklist_lbl = QLabel("Pending Operations:")
        self._checklist_lbl.setMinimumWidth(self.RIGHT_W)
        self._checklist_lbl.setStyleSheet(f"color:{t['info']};font-size:{font_sz(4)}px;font-weight:bold;padding:10px;{grad}{bs}")

        self._checklist = QListWidget()
        self._checklist.setMinimumWidth(self.RIGHT_W)
        self._checklist.setStyleSheet(f"QListWidget{{font-size:{font_sz()}px;padding:4px;{grad}{bs}}}"
                                      "QListWidget::item{padding:4px;border-radius:4px;border:1px solid transparent;}")

        self._elapsed_lbl = QLabel("Elapsed time:\n00s")
        self._elapsed_lbl.setGraphicsEffect(shadow)
        self._elapsed_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._elapsed_lbl.setMinimumSize(self.RIGHT_W, 75)
        self._elapsed_lbl.setStyleSheet(
            f"color:{t['info']};font-size:{font_sz(3)}px;font-weight:bold;padding:3px;text-align:center;{grad}{bs}")

        self._close_btn = QPushButton("Close")
        self._close_btn.setEnabled(False)
        self._close_btn.setMinimumSize(*self.BUTTON_SIZE)
        self._close_btn.clicked.connect(self.accept)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        btn_row.addWidget(self._close_btn)

        self._input_panel = QWidget()
        self._input_panel.setVisible(False)
        ip_layout = QVBoxLayout(self._input_panel)
        ip_layout.setContentsMargins(0, 6, 0, 0)
        ip_layout.setSpacing(4)

        self._input_prompt_lbl = QLabel()
        self._input_prompt_lbl.setWordWrap(True)
        self._input_prompt_lbl.setStyleSheet(f"color:{t['warning']};font-size:{font_sz(1)}px;font-weight:bold;"
                                             f"padding:6px 8px;border-left:3px solid {t['warning']};background:{t['bg3']};")
        ip_layout.addWidget(self._input_prompt_lbl)

        ip_row = QHBoxLayout()
        ip_row.setSpacing(6)
        self._input_edit = QLineEdit()
        self._input_edit.setFixedHeight(36)
        self._input_edit.returnPressed.connect(self._on_input_confirmed)
        self._input_send_btn = QPushButton("↵  Send")
        self._input_send_btn.setFixedHeight(36)
        self._input_send_btn.setMinimumWidth(90)
        self._input_send_btn.clicked.connect(self._on_input_confirmed)
        ip_row.addWidget(self._input_edit, 1)
        ip_row.addWidget(self._input_send_btn)
        ip_layout.addLayout(ip_row)

        left = QVBoxLayout()
        left.addWidget(self._text_edit)
        left.addWidget(self._fail_lbl)
        left.addWidget(self._input_panel)

        right = QVBoxLayout()
        right.addWidget(self._checklist_lbl)
        right.addWidget(self._checklist)
        right.addStretch(1)
        right.addWidget(self._elapsed_lbl)
        right.addStretch(1)
        right.addLayout(btn_row)

        main = QHBoxLayout(self)
        main.setContentsMargins(8, 8, 8, 8)
        main.setSpacing(10)
        main.addLayout(left, 4)
        main.addLayout(right, 1)

        self._ticker.timeout.connect(self._update_elapsed)
        self._timer.start()
        self._ticker.start(1000)

    def on_output(self, text: str, kind: str) -> None:
        if "/var/lib/pacman/db.lck" in text: self._show_db_lock_error(); return
        if kind == "finish": self._show_completion(); return
        if kind in _Style.KIND_CFG:
            self._append_html(text if ("<span " in text or "<p " in text) else _fmt_html(text, kind))

    def on_task_list(self, task_descs: list) -> None:
        try:
            self._init_checklist(task_descs)
        except Exception as exc:
            logger.error("on_task_list: %s", exc)

    def on_task_status(self, task_id: str, status: str) -> None:
        if status in (_Status.ERROR, _Status.WARNING): self._has_error = True
        if self._task_status.get(task_id) == status: return
        self._task_status[task_id] = status
        cfg = _Style.STATUS_CFG.get(status)
        if not cfg: return
        colour = current_theme()[cfg[0]]
        for i in range(self._checklist.count()):
            item = self._checklist.item(i)
            if item and item.data(Qt.ItemDataRole.UserRole) == task_id:
                item.setIcon(QIcon.fromTheme(cfg[1]))
                item.setForeground(QColor(colour))
                bg = QColor(colour)
                bg.setAlpha(25)
                item.setBackground(bg)
                self._checklist.scrollToItem(item)
                break

    def mark_done(self, failed_count: int = 0) -> None:
        if self._done: return
        if failed_count:
            self._fail_lbl.setText(f" ⚠️ Failed authentication attempts: {failed_count}")
        self._fail_lbl.setVisible(bool(failed_count))
        self._auth_failed = bool(failed_count)
        self._finalize_ui()

    def _finalize_ui(self) -> None:
        self._done = True
        self._ticker.stop()
        self._close_btn.setEnabled(True)
        self._close_btn.setFocus()

    def _init_checklist(self, task_descs: list[tuple[str, str]]) -> None:
        t = current_theme()
        self._checklist.clear()
        self._task_status.clear()
        for task_id, desc in task_descs:
            item = QListWidgetItem(desc.replace("…", "").replace("...", "").strip())
            item.setData(Qt.ItemDataRole.UserRole, task_id)
            item.setIcon(QIcon.fromTheme("dialog-question"))
            item.setForeground(QColor(t["muted"]))
            self._checklist.addItem(item)
            self._task_status[task_id] = _Status.PENDING
        n = self._checklist.count()
        h = sum(max(self._checklist.sizeHintForRow(i), 28) for i in range(n)) + 2 * self._checklist.frameWidth()
        self._checklist.setFixedHeight(max(h, 40) if n else 40)

    def _append_html(self, html: str) -> None:
        if not html: return
        try:
            cur = self._text_edit.textCursor()
            cur.movePosition(QTextCursor.MoveOperation.End)
            cur.insertHtml(html)
            self._text_edit.setTextCursor(cur)
            if sb := self._text_edit.verticalScrollBar(): sb.setValue(sb.maximum())
        except Exception as exc:
            logger.error("_append_html: %s", exc)

    def _show_db_lock_error(self) -> None:
        t = current_theme()

        for i in range(self._checklist.count()):
            item = self._checklist.item(i)
            if item:
                task_id = item.data(Qt.ItemDataRole.UserRole)
                if self._task_status.get(task_id) == _Status.IN_PROGRESS:
                    self.on_task_status(task_id, _Status.ERROR)
                    break

        self._append_html(f"<p style='{_Style.style_str('error')}'>Operation failed!</p>")

        ec = QColor(t["error"])
        r, g, b = ec.red(), ec.green(), ec.blue()
        self._append_html(f"<hr style='border:none;margin:10px 20px;border-top:1px dashed rgba({r},{g},{b},0.4);'>"
                          f"<div style='padding:15px;margin:10px;border-radius:10px;border-left:4px solid {t['error']};'>"
                          f"<p style='color:{t['error']};font-size:{font_sz(4)}px;text-align:center;'>"
                          f"<b>⚠️ System Manager Aborted</b><br>"
                          f"<span style='font-size:{font_sz(2)}px;'>/var/lib/pacman/db.lck detected!</span><br>"
                          f"<span style='color:{t['text']};font-size:{font_sz()}px;'>"
                          f"(Remove with: <code>'sudo rm /var/lib/pacman/db.lck</code>')</span></p></div><br>")

        self.mark_done()
        self.cancelRequested.emit()

    def _show_completion(self) -> None:
        if self._done or self._auth_failed: return
        t = current_theme()
        err = self._has_error
        colour = t["warning" if err else "success"]
        icon, summary = ("⚠️", "Completed with issues") if err else ("✅", "Successfully Completed")
        co = QColor(colour)
        r, g, b = co.red(), co.green(), co.blue()
        outcome = "completed with warnings/errors" if err else "successfully completed all operations"
        self._append_html(f"<hr style='border:none;margin:25px 50px;border-top:2px solid {colour};'>"
                          f"<div style='text-align:center;padding:20px;margin:15px 30px;border-radius:15px;"
                          f"border:1px solid rgba({r},{g},{b},0.3);'><p style='color:{colour};font-size:{font_sz(6)}px;"
                          f"font-weight:bold;'>{icon} {summary}</p><p style='color:{colour};font-size:{font_sz(4)}px;"
                          f"'>System Manager {outcome}<br></p></div>")
        self._checklist_lbl.setText(f"{icon} {summary}")
        self._checklist_lbl.setStyleSheet(f"color:{colour};font-size:{font_sz(4)}px;font-weight:bold;padding:10px;"
                                          f"background-color:rgba({r},{g},{b},0.15);{_Style.border_style()}")
        self._finalize_ui()

    def _update_elapsed(self) -> None:
        try:
            s = max(0, int(self._timer.elapsed() / 1000))
            h, rem = divmod(s, 3600)
            m, s = divmod(rem, 60)
            self._elapsed_lbl.setText(f"Elapsed time:\n{h:02}h {m:02}m {s:02}s" if h else
                                      f"Elapsed time:\n{m:02}m {s:02}s" if m else f"Elapsed time:\n{s:02}s")
        except (RuntimeError, AttributeError):
            self._ticker.stop()
        except Exception as exc:
            logger.error("_update_elapsed: %s", exc)

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Tab:
            self.focusNextChild()
        else:
            super().keyPressEvent(event)

    def on_input_requested(self, prompt: str) -> None:
        t = current_theme()
        QApplication.processEvents()
        self._input_prompt_lbl.setStyleSheet(f"color:{t['warning']};font-size:{font_sz(1)}px;font-weight:bold;"
                                             f"padding:6px 8px;border-left:3px solid {t['warning']};background:{t['bg3']};")
        self._input_prompt_lbl.setText(f"⚠ Provider selection required:")
        self._input_edit.clear()
        self._input_edit.setPlaceholderText(f"{prompt}")
        self._input_panel.setVisible(True)
        self._input_edit.setFocus()
        if sb := self._text_edit.verticalScrollBar():
            sb.setValue(sb.maximum())

    def _on_input_confirmed(self) -> None:
        answer = self._input_edit.text().strip()
        self._input_panel.setVisible(False)
        self._input_edit.clear()
        self.inputProvided.emit(answer)

    def closeEvent(self, event) -> None: super().closeEvent(event) if (self._done or self._auth_failed) else event.ignore()


class SystemManagerThread(QThread):
    thread_started    = pyqtSignal()
    outputReceived    = pyqtSignal(str, str)
    taskStatusChanged = pyqtSignal(str, str)
    taskListReady     = pyqtSignal(list)
    passwordFailed    = pyqtSignal()
    passwordSuccess   = pyqtSignal()
    inputRequested    = pyqtSignal(str)

    def __init__(self, sudo_password, distro: Optional[LinuxDistroHelper] = None) -> None:
        super().__init__()
        from sudo_password import SecureString
        self._pw = sudo_password if isinstance(sudo_password, SecureString) else SecureString(sudo_password or "")
        self._stop = threading.Event()
        self._stop_keepalive = threading.Event()
        self._keepalive: Optional[_SudoKeepalive] = None
        self._enabled_tasks: dict[str, tuple] = {}
        self._input_event: threading.Event = threading.Event()
        self._input_value: str = ""
        self._env_snapshot: dict = os.environ.copy()
        self._env_snapshot.update({"LC_ALL": "C", "LANG": "C", "LANGUAGE": "C"})
        self.distro: Optional[LinuxDistroHelper] = None
        self._pkg_cache: Optional[_PackageCache] = None
        try:
            self.distro = distro
            self._pkg_cache = _PackageCache(distro) if distro is not None else None
        except Exception as exc:
            logger.warning("distro init: %s", exc)
            self.distro = self._pkg_cache = None

    @property
    def terminated(self) -> bool: return self._stop.is_set()

    @terminated.setter
    def terminated(self, v: bool) -> None:
        if v:
            self._stop.set()
        else:
            self._stop.clear()

    def run(self) -> None:
        self.thread_started.emit()
        self._prepare_tasks()
        try:
            if self.terminated: return
            if not self._verify_sudo(): self.passwordFailed.emit(); return
            self.passwordSuccess.emit()
            self._stop_keepalive.clear()
            _keepalive = _SudoKeepalive(self._stop_keepalive, self._pw)
            self._keepalive = _keepalive
            _keepalive.start()
            if not self.terminated: self._run_all_tasks()
        except Exception as exc:
            try:
                self.outputReceived.emit(f"Critical error: {exc}", "error")
            except RuntimeError:
                logger.error("SystemManagerThread: dialog gone during error emit: %s", exc)
        finally:
            self._cleanup()
            try:
                self.outputReceived.emit("", "finish")
            except RuntimeError:
                pass

    def provide_input(self, answer: str) -> None:
        self._input_value = answer.strip()
        self._input_event.set()

    def _cleanup(self) -> None:
        self._stop_keepalive.set()
        if self._keepalive: self._keepalive.join(timeout=5); self._keepalive = None
        self._pw.clear()
        try: subprocess.run(["sudo", "-k"], capture_output=True, timeout=5)
        except (subprocess.SubprocessError, OSError): pass

    def _prepare_tasks(self) -> None:
        all_tasks = {**self._base_tasks(), **self._service_tasks(), "remove_orphaned_packages":
            ("Removing orphaned packages…", self._remove_orphans), "clean_cache": ("Cleaning cache…", self._clean_cache)}
        self._enabled_tasks = {k: v for k, v in all_tasks.items() if k in S.system_manager_ops}
        self.taskListReady.emit([(k, d) for k, (d, _) in self._enabled_tasks.items()])

    def _base_tasks(self) -> dict:
        return {"copy_system_files": ("Copying System Files…", self._copy_sysfiles),
                "update_mirrors": ("Updating mirrors…", self._update_mirrors), "update_system": ("Updating system…", self._update_system),
                "set_user_shell": ("Setting user shell…", self._set_shell),
                "install_ucode": ("Installing CPU microcode…", self._install_ucode),
                "install_kernels": ("Installing kernel(s)…", self._install_kernels),
                "install_kernel_headers": ("Installing headers…", self._install_kernel_headers),
                "set_default_kernel": ("Setting default boot kernel…", self._set_default_kernel),
                "install_basic_packages": ("Installing Basic Packages…", lambda: self._batch_install(S.basic_packages, "Basic Package")),
                "install_yay": ("Installing yay…", self._install_yay),
                "install_aur_packages": ("Installing AUR Packages with yay…", lambda: self._batch_install(S.aur_packages, "AUR Package", use_aur=True)),
                "install_specific_packages": ("Installing Specific Packages…", self._install_specific),
                "enable_flatpak_integration": ("Enabling Flatpak integration…", self._install_flatpak)}

    def _service_tasks(self) -> dict:
        if not self.distro: return {}
        d = self.distro
        specs = {"enable_printer_support": ("Initialising printer support…", "cups", d.get_printer_packages),
                 "enable_ssh_service": ("Initialising SSH server…", d.get_ssh_service_name(), d.get_ssh_packages),
                 "enable_samba_network_filesharing": ("Initialising Samba…", d.get_samba_service_name(), d.get_samba_packages),
                 "enable_bluetooth_service": ("Initialising Bluetooth…", "bluetooth", d.get_bluetooth_packages),
                 "enable_atd_service": ("Initialising atd…", "atd", d.get_at_packages),
                 "enable_cronie_service": (f"Initialising {d.get_cron_service_name()}…", d.get_cron_service_name(), d.get_cron_packages),
                 "install_snap": ("Installing Snap…", "snapd", d.get_snap_packages),
                 "enable_firewall": ("Initialising firewall…", "ufw", d.get_firewall_packages)}
        return {k: (desc, lambda s=svc, p=pkg_fn: self._setup_service(s, p())) for k, (desc, svc, pkg_fn) in specs.items()}

    def _run_all_tasks(self) -> None:
        task_items = list(self._enabled_tasks.items())
        for idx, (task_id, (desc, fn)) in enumerate(task_items):
            if self.terminated:
                for skipped_id, _ in task_items[idx:]:
                    self.taskStatusChanged.emit(skipped_id, _Status.WARNING)
                break
            self.taskStatusChanged.emit(task_id, _Status.IN_PROGRESS)
            self.outputReceived.emit(desc, "operation")
            try:
                res = fn()
                status = _Status.ERROR if res is False else _Status.WARNING if res == _Status.WARNING else _Status.SUCCESS
            except Exception as exc:
                self.outputReceived.emit(f"Task '{task_id}' failed: {exc}", "error")
                status = _Status.ERROR
            self.taskStatusChanged.emit(task_id, status)
            if status == _Status.ERROR:
                self.outputReceived.emit(f"Aborting remaining tasks due to failure in '{task_id}'.", "error")
                for remaining_id, _ in task_items[idx + 1:]:
                    self.taskStatusChanged.emit(remaining_id, _Status.WARNING)
                break

    @staticmethod
    def _inject(cmd: list[str]) -> list[str]:
        if cmd and cmd[0] == "sudo" and not (len(cmd) > 1 and cmd[1] == "-S"):
            return ["sudo", "-S"] + cmd[1:]
        return cmd

    def _exec(self, cmd: list[str] | str, stream: bool = False, timeout: Optional[int] = 15,
              cwd: Optional[str] = None) -> SimpleNamespace:

        if self.terminated:
            return SimpleNamespace(returncode=1, stdout="", stderr="")

        if isinstance(cmd, str):
            cmd = self._inject(shlex.split(cmd))
        elif isinstance(cmd, list):
            cmd = self._inject(list(cmd))

        pw_to_send = None
        if self._pw and isinstance(cmd, list):
            if cmd[:2] == ["sudo", "-S"] or cmd[:1] == ["yay"]:
                pw_to_send = self._pw
                if cmd[:1] == ["yay"] and "--sudoflags=-S" not in cmd:
                    cmd.append("--sudoflags=-S")

        if not stream:
            input_data = _pw_bytes(pw_to_send) if pw_to_send else None
            try:
                r = subprocess.run(
                    cmd,
                    input=input_data,
                    capture_output=True,
                    env=self._env_snapshot,
                    timeout=timeout,
                    cwd=cwd
                )

                return SimpleNamespace(
                    returncode=r.returncode,
                    stdout=r.stdout.decode("utf-8", "replace"),
                    stderr=r.stderr.decode("utf-8", "replace")
                )

            except subprocess.TimeoutExpired:
                return SimpleNamespace(returncode=124, stdout="", stderr="Timeout")

            except (OSError, subprocess.SubprocessError) as exc:
                return SimpleNamespace(returncode=1, stdout="", stderr=str(exc))

            finally:
                if input_data:
                    _zero(input_data)

        proc = None

        try:
            import pty as _pty_mod

            try:
                _pty_master, _pty_slave = _pty_mod.openpty()

                try:
                    proc = subprocess.Popen(
                        cmd,
                        stdout=_pty_slave,
                        stderr=subprocess.PIPE,
                        stdin=subprocess.PIPE,
                        cwd=cwd,
                        env=self._env_snapshot
                    )

                    os.close(_pty_slave)
                    _pty_slave = -1
                    proc.stdout = os.fdopen(_pty_master, 'rb', buffering=0)

                except (OSError, subprocess.SubprocessError) as exc:
                    logger.exception("Failed to launch subprocess with PTY")

                    if _pty_slave != -1:
                        try:
                            os.close(_pty_slave)
                        except OSError:
                            pass

                    try:
                        os.close(_pty_master)
                    except OSError:
                        pass

                    if proc is not None:
                        try:
                            proc.kill()
                            proc.wait()
                        except (OSError, subprocess.SubprocessError):
                            pass

                    raise exc

            except OSError:
                proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    stdin=subprocess.PIPE,
                    cwd=cwd,
                    env=self._env_snapshot
                )

        except (OSError, subprocess.SubprocessError) as exc:
            self.outputReceived.emit(f"Command launch error: {exc}", "error")
            return SimpleNamespace(returncode=1, stdout="", stderr=str(exc))

        out_q: queue.Queue = queue.Queue()

        def _make_pipe_reader(pipe, _is_err: bool, proc_ref):
            def _reader() -> None:
                buf = b""
                try:
                    _fd = pipe.fileno()
                except (AttributeError, ValueError):
                    out_q.put(None)
                    return

                try:
                    while not self.terminated:
                        try:
                            ready, _, _ = _select.select([_fd], [], [], 0.1)
                        except OSError:
                            break

                        if ready:
                            chunk = os.read(_fd, 4096)
                            if not chunk:
                                break
                            buf += chunk

                        while b"\n" in buf:
                            raw, buf = buf.split(b"\n", 1)
                            line = raw.decode("utf-8", errors="replace").rstrip("\r")
                            clean = _ANSI_RE.sub("", line).strip()
                            if clean:
                                if re.search(r"Enter a number|Enter a selection", clean, re.IGNORECASE):
                                    out_q.put((_is_err, clean, "select"))
                                elif re.search(r"\[N]one\s+\[A]ll", clean, re.IGNORECASE):
                                    out_q.put((_is_err, clean, "auto_none"))
                                else:
                                    out_q.put((_is_err, clean, None))

                        if buf:
                            decoded = buf.decode("utf-8", errors="replace")
                            clean = _ANSI_RE.sub("", decoded)

                            if "[sudo]" in clean.lower() and "password" in clean.lower():
                                out_q.put((_is_err, clean, "sudo_pw"))
                                buf = b""

                            elif re.search(r"Enter a number|Enter a selection", clean, re.IGNORECASE):
                                out_q.put((_is_err, clean, "select"))
                                buf = b""

                            elif "[y/n]" in clean.lower():
                                out_q.put((_is_err, clean, "confirm"))
                                buf = b""

                            elif re.search(r"\[N]one\s+\[A]ll", clean, re.IGNORECASE):
                                out_q.put((_is_err, clean, "auto_none"))
                                buf = b""

                        if not ready and proc_ref.poll() is not None:
                            break

                except (OSError, EOFError):
                    pass

                finally:
                    if buf:
                        clean = _ANSI_RE.sub("", buf.decode("utf-8", errors="replace")).strip()
                        if clean:
                            out_q.put((_is_err, clean, None))
                    out_q.put(None)

            return _reader

        t_out = threading.Thread(target=_make_pipe_reader(proc.stdout, False, proc), daemon=True)
        t_err = threading.Thread(target=_make_pipe_reader(proc.stderr, True, proc), daemon=True)
        t_out.start()
        t_err.start()

        sentinels = 0

        while sentinels < 2:
            try:
                item = out_q.get(timeout=0.25)
            except queue.Empty:
                if self.terminated and proc:
                    proc.terminate()
                    break
                continue

            if item is None:
                sentinels += 1
                continue

            is_err, text, tag = item

            if tag == "sudo_pw":
                if pw_to_send and proc.stdin:
                    tmp_pw = _pw_bytes(pw_to_send)
                    try:
                        proc.stdin.write(tmp_pw)
                        proc.stdin.flush()
                    except OSError:
                        pass
                    finally:
                        _zero(tmp_pw)
                continue

            if tag == "confirm":
                try:
                    if proc.stdin and not proc.stdin.closed:
                        proc.stdin.write(b"y\n")
                        proc.stdin.flush()
                except OSError:
                    pass
                continue

            if tag == "auto_none":
                self.outputReceived.emit(text, "subprocess")
                try:
                    if proc.stdin and not proc.stdin.closed:
                        proc.stdin.write(b"\n")
                        proc.stdin.flush()
                except OSError:
                    pass
                continue

            if "[sudo]" in text:
                text = re.sub(r"\[sudo].*?:\s*", "", text)

            if _PACMAN_PROGRESS_RE.search(text):
                continue

            if len(text) <= 2 and text.lower().strip() in ("y", "n"):
                continue

            _kind = "error" if is_err and not _INFO_RE.search(text) else "subprocess"

            if re.search(r'\d+\s*(?:MiB|KiB|B)/s|\d+:\d{2}\s+\d+[KM]iB', text):
                _kind = "dimmed"

            self.outputReceived.emit(text, _kind)

        _wait_timeout = timeout if (not self.terminated and timeout is not None) else 30

        try:
            rc = proc.wait(timeout=_wait_timeout) if proc and proc.poll() is None else proc.returncode
        except subprocess.TimeoutExpired:
            if proc:
                proc.kill()
                rc = proc.wait()
            else:
                rc = 1

        t_out.join(3)
        t_err.join(3)

        return SimpleNamespace(returncode=rc if rc is not None else 1)

    def _emit_result(self, ok: bool, msg_ok: str, msg_err: str): self.outputReceived.emit(msg_ok if ok else msg_err, "success" if ok else "error")

    def _verify_sudo(self) -> bool:
        self.outputReceived.emit("Verifying sudo access…", "operation")
        try:
            subprocess.run(["sudo", "-k"], capture_output=True, timeout=5)
        except (OSError, subprocess.SubprocessError):
            pass
        token = secrets.token_hex(16)
        pw_buf = _pw_bytes(self._pw)
        ok, proc = False, None
        try:
            proc = subprocess.Popen(["sudo", "-S", "sh", "-c", f"printf '%s\\n' {shlex.quote(token)}"],
                                    env=self._env_snapshot, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                if proc.stdin:
                    proc.stdin.write(pw_buf)
                    proc.stdin.flush()
                    proc.stdin.close()
            except OSError:
                pass

            chunks: list[bytes] = []

            def _read_out() -> None:
                try:
                    if proc and proc.stdout:
                        chunks.append(proc.stdout.read())
                except (OSError, ValueError):
                    pass

            def _kill_on_retry() -> None:
                n = 0
                try:
                    if not proc or not proc.stderr:
                        return
                    for raw in iter(proc.stderr.readline, b""):
                        line = raw.decode("utf-8", errors="replace")
                        if "[sudo]" in line.lower() and "password" in line.lower():
                            n += 1
                            if n >= 2:
                                try:
                                    proc.kill()
                                except (ProcessLookupError, AttributeError):
                                    pass
                                return
                except (OSError, ValueError):
                    pass

            t1 = threading.Thread(target=_read_out, daemon=True); t2 = threading.Thread(target=_kill_on_retry, daemon=True)
            t1.start(); t2.start()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass
            t1.join(2); t2.join(2)
            output = b"".join(chunks)
            ok = proc.returncode == 0 and output.strip() == token.encode()
        except Exception as exc: logger.error("_verify_sudo: %s", exc)
        finally:
            _zero(pw_buf)
            if proc and proc.returncode is None:
                try:
                    proc.kill()
                except OSError:
                    pass
            try:
                self.outputReceived.emit("Sudo access successfully verified" if ok else "Authentication failed: Invalid Password",
                                         "success" if ok else "error")
            except RuntimeError:
                pass
        return ok

    def _install_pkg(self, name: str, label: str = "Package") -> bool:
        if not self.distro or not self._pkg_cache: return False
        self.outputReceived.emit(f"Installing {label}: {name}", "info")
        if self._pkg_cache.is_installed(name):
            self.outputReceived.emit(f"{name} already installed", "success")
            return True
        ok = (self._exec(self.distro.get_pkg_install_cmd(name), stream=True).returncode == 0)
        if ok: self._pkg_cache.mark_installed(name)
        self._emit_result(ok, f"{name} successfully installed", f"failed to install {name}")
        return ok

    def _install_with_retry(self, pkgs: list[str], bulk_fn, single_fn) -> list[str]:
        if not self.distro:
            return pkgs
        bulk_fn(pkgs)
        still_missing = self.distro.filter_not_installed(pkgs)
        for p in pkgs:
            if p not in still_missing and self._pkg_cache: self._pkg_cache.mark_installed(p)
        failed = []
        if still_missing:
            self.outputReceived.emit("Some packages were not installed — trying them individually…", "warning")
            for i, pkg in enumerate(still_missing):
                if self.terminated:
                    failed.extend(still_missing[i:])
                    break
                single_fn(pkg)
                if self.distro.package_is_installed(pkg):
                    if self._pkg_cache: self._pkg_cache.mark_installed(pkg)
                else:
                    failed.append(pkg)
        return failed

    def _batch_install(self, pkg_list, label: str, *, use_aur: bool = False) -> str | bool:
        if not self.distro:
            self.outputReceived.emit(f"Cannot install {label}s: no distro helper", "error")
            return False
        all_names = active_pkg_names(pkg_list)
        items = []
        for n in all_names:
            if self.distro.valid(n):
                items.append(n)
            else:
                logger.warning("_batch_install: skipping invalid package name %r", n)

        if not items:
            self.outputReceived.emit(f"No active {label}s configured", "info")
            return _Status.SUCCESS

        to_install = self.distro.filter_not_installed(items)
        if not to_install:
            self.outputReceived.emit(f"All {label}s already installed", "success")
            return _Status.SUCCESS

        failed = []
        if use_aur:
            bulk = lambda b: self._exec(["yay", "-S", "--needed"] + b, stream=True)
            single = lambda _p: self._exec(["yay", "-S", "--needed", _p], stream=True)
        else:
            _distro = self.distro
            bulk = lambda b: self._exec(_distro.get_batch_install_cmd(b), stream=True)
            single = lambda p_: self._exec(_distro.get_pkg_install_cmd(p_), stream=True)

        for i in range(0, len(to_install), 20):
            if self.terminated: break
            batch = to_install[i:i + 20]
            self.outputReceived.emit(f"Installing: {', '.join(batch)}", "info")
            failed.extend(self._install_with_retry(batch, bulk, single))

        self._emit_result(not failed, f"All {label}s successfully installed", f"Failed {label}(s): {', '.join(failed)}")
        return _Status.SUCCESS if not failed else _Status.WARNING

    def _copy_sysfiles(self) -> bool:
        files = active_system_files()
        if not files:
            self.outputReceived.emit("No system files configured", "warning")
            return True

        from drive_utils import check_drives_to_mount, mount_drive
        for drive in check_drives_to_mount([p for f in files for p in (f["source"], f["destination"])]):
            name = drive.get("drive_name", "?")
            self.outputReceived.emit(f"Mounting drive: '{name}'…", "info")
            ok, err = mount_drive(drive)
            if not ok: self.outputReceived.emit(f"Failed to mount '{name}': {err}", "error"); return False
            self.outputReceived.emit(f"Mounted '{name}'", "success")

        overall = True
        for f in files:
            src, dst = f["source"].strip(), f["destination"].strip()
            if not Path(src).exists():
                self.outputReceived.emit(f"Source not found: {src}", "error")
                overall = False
                continue

            dst_dir = Path(dst).parent
            if not dst_dir.exists() and self._exec(["sudo", "mkdir", "-p", "--mode=755", str(dst_dir)]).returncode != 0:
                self.outputReceived.emit(f"Cannot create directory: {dst_dir}", "error")
                overall = False
                continue

            cmd = ["sudo", "cp"] + (["-r"] if Path(src).is_dir() else []) + [src, dst]
            if self._exec(cmd, stream=True).returncode == 0:
                self.outputReceived.emit(f"Copied successfully:\n'{src}' 󰧂 '{dst}'", "success")
            else:
                self.outputReceived.emit(f"Error copying: {src}", "error")
                overall = False
        return overall

    @staticmethod
    def _detect_country() -> str:
        for url in ("https://ipinfo.io/country", "https://ifconfig.co/country-iso"):
            try:
                with urllib.request.urlopen(url, timeout=5) as r:
                    if r.status == 200:
                        code = r.read().decode().strip().upper()
                        if len(code) == 2 and code.isalpha(): return code
            except (urllib.error.URLError, TimeoutError, OSError): continue
        return ""

    def _update_mirrors(self) -> bool:
        if not self.distro: return False
        if self.distro.family() != "arch":
            self.outputReceived.emit("Mirror update is only supported on Arch Linux", "info")
            return True

        country = self._detect_country()
        self.outputReceived.emit(f"Detected country: {country}" if country else "No country detected — using worldwide mirrors",
                                 "info" if country else "warning")

        if shutil.which("reflector"):
            self.outputReceived.emit("Package reflector already installed", "info")
        elif self._exec(self.distro.get_pkg_install_cmd("reflector"), stream=True).returncode != 0:
            self.outputReceived.emit("Failed to install reflector", "error")
            return False

        cmd = ["sudo", "-S", "reflector", "--verbose", "--latest", "10",
               "--protocol", "https", "--sort", "rate", "--save", "/etc/pacman.d/mirrorlist"]
        if country: cmd += ["--country", country]
        self.outputReceived.emit(f"Running: {' '.join(cmd)}", "info")
        return self._exec(cmd, stream=True).returncode == 0

    def _update_system(self) -> bool:
        if not self.distro: return False
        if self.distro.has_aur and self._pkg_cache and self._pkg_cache.is_installed("yay"):
            ok = (self._exec(["yay", "--noconfirm"], stream=True).returncode == 0)
        else:
            cmd_str = self.distro.get_update_system_cmd()
            if any(seq in cmd_str for seq in ("&&", "||", " | ")):
                _stripped = cmd_str.lstrip()
                inner_cmd = _stripped[5:] if _stripped.startswith("sudo ") else _stripped
                cmd = ["sudo", "sh", "-c", inner_cmd]
            else:
                cmd = cmd_str
            ok = (self._exec(cmd, stream=True, timeout=None).returncode == 0)
        self._emit_result(ok, "System successfully updated", "System update failed")
        return ok

    def _set_shell(self) -> bool:
        if not self.distro: return False
        target = S.effective_shell
        pkg = self.distro.get_shell_package_name(target)
        binary = self.distro.get_shell_binary_name(target)
        shell = shutil.which(binary) or f"/bin/{binary}"

        try:
            current = pwd.getpwnam(_USER).pw_shell
            self.outputReceived.emit(f"User: '{_USER}'  current shell: {current}", "info")
        except KeyError as exc:
            self.outputReceived.emit(f"Cannot determine current shell: {exc}", "error")
            return False

        if current == shell:
            self.outputReceived.emit(f"Shell is already '{target}' ({shell})", "success")
            return True

        if not self.distro.package_is_installed(pkg):
            self.outputReceived.emit(f"Installing shell package: {pkg}", "info")
            if not self._install_pkg(pkg, "Shell Package"): return False
            shell = shutil.which(binary) or shell

        if not Path(shell).exists():
            self.outputReceived.emit(f"Shell binary '{shell}' not found.", "error")
            return False

        try:
            shells_path = Path("/etc/shells")
            if shells_path.exists():
                if shell not in [s.strip() for s in shells_path.read_text(encoding="utf-8").splitlines()]:
                    self.outputReceived.emit(f"Adding '{shell}' to /etc/shells…", "info")
                    self._exec(["sudo", "sh", "-c", f"echo {shlex.quote(shell)} >> /etc/shells"])
        except OSError as exc:
            self.outputReceived.emit(f"Could not verify /etc/shells: {exc}", "warning")

        ok = (self._exec(["sudo", "chsh", "-s", shell, _USER], stream=True).returncode == 0)
        self._emit_result(ok, f"Shell for '{_USER}' set to '{shell}'", f"Shell for '{_USER}' failed to change to '{shell}'")
        return ok

    def _install_ucode(self) -> bool | str:
        if not self.distro:
            return False
        pkg = self.distro.get_ucode_package()
        if not pkg:
            vendor = self.distro.detect_cpu_vendor() or "unknown"
            self.outputReceived.emit(f"No microcode package available for {vendor} CPU on {self.distro.pkg_manager_name()} — skipping", "warning")
            return _Status.WARNING
        if self._pkg_cache and self._pkg_cache.is_installed(pkg):
            self.outputReceived.emit(f"Microcode already installed ({pkg})", "success")
            return True
        return self._install_pkg(pkg, "CPU Microcode")

    def _install_kernels(self) -> bool | str:
        if not self.distro:
            return False
        targets = S.effective_kernels
        bootloader = LinuxDistroHelper.detect_bootloader()
        overall = True
        for variant in targets:
            pkgs = ARCH_KERNEL_VARIANTS.get(variant)

            if not pkgs:
                self.outputReceived.emit(f"Unknown kernel variant: {variant!r} — skipping", "warning")
                continue

            if self._pkg_cache and self._pkg_cache.is_installed(pkgs[0]):
                self.outputReceived.emit(f"{variant} already installed — skipping", "success")
                continue

            kernel_pkg = pkgs[0]
            if not self._install_pkg(kernel_pkg, "Kernel Package"):
                overall = False
            elif bootloader == "systemd-boot":
                entries_dir = Path("/boot/loader/entries")
                self._exec(["sudo", "mkdir", "-p", str(entries_dir)], stream=False)
                if not self._create_systemd_boot_entry(kernel_pkg, entries_dir):
                    self.outputReceived.emit(
                        f"Kernel '{variant}' installed, but failed to create systemd-boot entry — "
                        f"please create it manually.", "warning"
                    )
                else:
                    self.outputReceived.emit(f"systemd-boot entry created for {variant}", "success")

        self._emit_result(overall, "Kernel(s) successfully installed", "One or more kernels failed to install")
        return overall

    def _install_kernel_headers(self) -> bool:
        if not self.distro:
            return False
        if self.distro.family() == "arch":
            installed_variants = self.distro.detect_installed_kernel_variants()
            if not installed_variants:
                return self._install_pkg(self.distro.get_kernel_headers_pkg(), "Headers Package")
            overall = True
            for variant in sorted(installed_variants):
                pkgs = ARCH_KERNEL_VARIANTS.get(variant)
                if not pkgs or len(pkgs) < 2:
                    continue
                header_pkg = pkgs[1]
                if self._pkg_cache and self._pkg_cache.is_installed(header_pkg):
                    self.outputReceived.emit(f"{header_pkg} already installed — skipping", "success")
                    continue
                if not self._install_pkg(header_pkg, "Headers Package"):
                    overall = False
            return overall
        return self._install_pkg(self.distro.get_kernel_headers_pkg(), "Headers Package")

    def _set_default_kernel(self) -> bool | str:
        target = (S.default_kernel or "").strip()
        if not target:
            self.outputReceived.emit("No default kernel configured — skipping", "warning")
            return _Status.WARNING

        pkgs = ARCH_KERNEL_VARIANTS.get(target)
        if not pkgs:
            self.outputReceived.emit(f"Unknown kernel variant: {target!r}", "error")
            return False

        kernel_pkg = pkgs[0]
        self.outputReceived.emit(f"Setting default kernel to: {kernel_pkg}", "info")

        bootloader = LinuxDistroHelper.detect_bootloader()
        current_default_variant = LinuxDistroHelper.detect_system_default_kernel(bootloader)
        if current_default_variant and current_default_variant == target:
            self.outputReceived.emit(f"{kernel_pkg} is already default", "success")
            return True

        if bootloader == "grub":
            return self._set_grub_default(kernel_pkg)
        if bootloader == "systemd-boot":
            return self._set_systemd_boot_default(kernel_pkg)

        self.outputReceived.emit("No supported bootloader found (/boot/grub/grub.cfg or /boot/loader). "
                                 "Please set the default kernel manually.", "warning")
        return _Status.WARNING

    def _set_grub_default(self, kernel_pkg: str) -> bool | str:
        grub_env = Path("/etc/default/grub")

        try:
            text = grub_env.read_text(encoding="utf-8")
        except OSError as exc:
            self.outputReceived.emit(f"Cannot read {grub_env}: {exc}", "error")
            return False

        if "GRUB_DEFAULT=saved" not in text:
            self.outputReceived.emit("Patching /etc/default/grub: setting GRUB_DEFAULT=saved…", "info")
            new_text = re.sub(r"^GRUB_DEFAULT=.*$", "GRUB_DEFAULT=saved", text, flags=re.MULTILINE)
            if "GRUB_DEFAULT=" not in text:
                new_text = "GRUB_DEFAULT=saved\n" + new_text
            ok = self._exec([
                "sudo", "sh", "-c", f"printf '%s' {shlex.quote(new_text)} > /etc/default/grub"], stream=True).returncode == 0
            if not ok:
                self.outputReceived.emit("Failed to patch /etc/default/grub", "error")
                return False

        self.outputReceived.emit("Regenerating /boot/grub/grub.cfg…", "info")
        rc = self._exec(["sudo", "grub-mkconfig", "-o", "/boot/grub/grub.cfg"], stream=True, timeout=120).returncode
        if rc != 0:
            self.outputReceived.emit("grub-mkconfig failed — aborting default-kernel set", "error")
            return False

        entry_id = self._find_grub_entry(kernel_pkg)
        if entry_id is None:
            self.outputReceived.emit(f"Could not locate a GRUB menu entry for '{kernel_pkg}' in grub.cfg.\n"
                                     "The kernel packages were installed — please set the boot default manually via GRUB.", "warning")
            return _Status.WARNING

        self.outputReceived.emit(f"Found GRUB entry: {entry_id!r}", "info")
        ok = self._exec(["sudo", "grub-set-default", entry_id], stream=True).returncode == 0
        self._emit_result(ok, f"GRUB default set to '{kernel_pkg}'", "grub-set-default failed")
        return ok

    @staticmethod
    def _find_grub_entry(kernel_pkg: str) -> str | None:
        grub_cfg = Path("/boot/grub/grub.cfg")
        try:
            lines = grub_cfg.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return None

        _title_re = re.compile(r"""menuentry\s+['"]([^'"]+)['"]""")
        _sub_re = re.compile(r"""submenu\s+['"]([^'"]+)['"]""")

        _exact_re = re.compile(r"(?:^|[^\w-])" + re.escape(kernel_pkg) + r"(?:[^\w-]|$)", re.IGNORECASE)

        top_idx = 0
        current_sub = None
        depth = 0

        for line in lines:
            stripped = line.strip()

            m_sub = _sub_re.match(stripped)
            if m_sub and depth == 0:
                current_sub = m_sub.group(1)
                depth = 1
                top_idx += 1
                continue

            m_entry = _title_re.match(stripped)
            if m_entry:
                title = m_entry.group(1)
                if _exact_re.search(title):
                    if current_sub:
                        return f"{current_sub}>{title}"
                    return str(top_idx)
                if depth == 0:
                    top_idx += 1
                continue

            if stripped == "}":
                if depth > 0:
                    depth -= 1
                    if depth == 0:
                        current_sub = None

        return None

    def _set_systemd_boot_default(self, kernel_pkg: str) -> bool | str:
        entries_dir = Path("/boot/loader/entries")
        entry_paths = self._list_entry_files(entries_dir)
        _kern_exact = re.compile(r"vmlinuz-" + re.escape(kernel_pkg) + r"(?:[^\w-]|$)")
        canonical = entries_dir / f"{kernel_pkg}.conf"

        canonical_exists = self._exec(["sudo", "test", "-f", str(canonical)], stream=False).returncode == 0

        target_conf: str | None = None
        if canonical_exists or any(p.stem == kernel_pkg for p in entry_paths):
            target_conf = f"{kernel_pkg}.conf"
        else:
            for fpath in entry_paths:
                content = self._read_file_sudo(fpath)
                if content and _kern_exact.search(content):
                    target_conf = f"{fpath.stem}.conf"
                    break

        if target_conf is None:
            created = self._create_systemd_boot_entry(kernel_pkg, entries_dir)
            if not created: return _Status.WARNING
            target_conf = created
        elif not canonical_exists:
            created = self._create_systemd_boot_entry(kernel_pkg, entries_dir)
            if created: target_conf = created

        return self._apply_systemd_boot_default(str(target_conf))

    def _list_entry_files(self, entries_dir: Path) -> list[Path]:
        paths = []
        try:
            if entries_dir.is_dir():
                paths = sorted(entries_dir.glob("*.conf"))
        except OSError:
            pass

        if not paths:
            r = self._exec(["sudo", "ls", "-1", str(entries_dir)], stream=False)
            if r.returncode == 0:
                paths = [entries_dir / n.strip() for n in r.stdout.splitlines() if n.strip().endswith(".conf")]
        return paths

    def _create_systemd_boot_entry(self, kernel_pkg: str, entries_dir: Path) -> str | None:
        vmlinuz = Path(f"/boot/vmlinuz-{kernel_pkg}")
        initramfs = Path(f"/boot/initramfs-{kernel_pkg}.img")

        for img in (vmlinuz, initramfs):
            if self._exec(["sudo", "test", "-f", str(img)], stream=False).returncode != 0:
                self.outputReceived.emit(f"Kernel image not found: {img}", "warning")
                return None

        running_variant = LinuxDistroHelper.detect_running_kernel_variant()
        running_kern_pkg = ARCH_KERNEL_VARIANTS.get(running_variant, ("linux", "linux-headers"))[0]

        template_content: str | None = None
        entry_files = sorted(entries_dir.glob("*.conf")) if entries_dir.exists() else []

        for fpath in entry_files:
            content = self._read_file_sudo(fpath)
            if content and running_kern_pkg in content:
                template_content = content
                break

        if not template_content and entry_files:
            template_content = self._read_file_sudo(entry_files[0])

        if not template_content:
            self.outputReceived.emit("No template found in /boot/loader/entries/", "error")
            return None

        new_content = template_content.replace(running_kern_pkg, kernel_pkg)
        dest_path = entries_dir / f"{kernel_pkg}.conf"

        escaped_content = shlex.quote(new_content)
        try:
            r = self._exec(["sudo", "sh", "-c", f"printf '%s' {escaped_content} > {shlex.quote(str(dest_path))}"],
                           stream=False, timeout=10)
            self._update_sort_keys(kernel_pkg, Path("/boot/loader/entries"))
            if r.returncode == 0:
                self._exec(["sync"], stream=False)
                self.outputReceived.emit(f"Created boot entry: {dest_path}", "success")
                return f"{kernel_pkg}.conf"
            else:
                self.outputReceived.emit(f"Failed to write boot entry", "error")
                return None
        except Exception as exc:
            self.outputReceived.emit(f"Error creating boot entry: {exc}", "error")
            return None

    def _apply_systemd_boot_default(self, entry_conf: str) -> bool | str:
        ok = self._exec(["sudo", "bootctl", "set-default", entry_conf], stream=False, timeout=15).returncode == 0
        if not ok:
            loader_conf = Path("/boot/loader/loader.conf")
            self.outputReceived.emit(f"Writing default to loader.conf: {entry_conf}", "info")
            conf_text = self._read_file_sudo(loader_conf) or ""
            if re.search(r"^default\s+", conf_text, re.MULTILINE):
                new_conf = re.sub(r"^default\s+\S+", f"default {entry_conf}", conf_text, flags=re.MULTILINE)
            else:
                new_conf = f"default {entry_conf}\n" + conf_text
            ok = self._exec(["sudo", "sh", "-c", f"printf '%s' {shlex.quote(new_conf)} > /boot/loader/loader.conf"],
                            stream=False).returncode == 0
            if not ok:
                self._emit_result(False, "", "Failed to update /boot/loader/loader.conf")
                return False

        self._emit_result(True, f"systemd-boot default set to '{entry_conf}'", "")
        self._update_sort_keys(entry_conf, Path("/boot/loader/entries"))
        return True

    def _update_sort_keys(self, new_kernel_conf: str, entries_dir: Path) -> None:
        kernel_entries = self._list_entry_files(entries_dir)
        if not kernel_entries:
            return

        loader_conf_text = self._read_file_sudo(Path("/boot/loader/loader.conf")) or ""
        loader_default_match = re.search(r"^default\s+(\S+)", loader_conf_text, re.MULTILINE)
        if loader_default_match:
            default_filename = Path(loader_default_match.group(1)).name
        else:
            default_filename = Path(new_kernel_conf).name

        default_path = entries_dir / default_filename
        default_content = self._read_file_sudo(default_path)

        options_line = ""
        if default_content:
            for line in default_content.splitlines():
                if line.strip().startswith("options "):
                    options_line = line.strip()
                    break

        def _entry_sort_key(p):
            stem = p.stem.lower()
            base, _, suffix = stem.partition("-")
            return 0 if p.name == default_filename else 1, base, suffix

        kernel_entries.sort(key=_entry_sort_key)

        distro_pretty = self.distro.distro_pretty_name if self.distro else "Linux"
        new_kernel_filename = Path(new_kernel_conf).name

        for idx, fpath in enumerate(kernel_entries, start=1):
            sort_val = f"{idx:02d}"
            content = self._read_file_sudo(fpath)
            if not content:
                continue

            variant = fpath.stem
            target_title = f"{distro_pretty} ({variant})"
            new_content = content

            if re.search(r"^sort-key\s+", new_content, re.MULTILINE):
                new_content = re.sub(r"^sort-key\s+\S+", f"sort-key {sort_val}", new_content, flags=re.MULTILINE)
            else:
                new_content = new_content.rstrip() + f"\nsort-key {sort_val}\n"

            if fpath.name == new_kernel_filename:
                current_title = ""
                for line in content.splitlines():
                    if line.strip().startswith("title "):
                        current_title = line.strip()[6:].strip()
                        break

                if f"({variant})" not in current_title:
                    if re.search(r"^title\s+", new_content, re.MULTILINE):
                        new_content = re.sub(r"^title\s+.*$", f"title {target_title}", new_content, flags=re.MULTILINE)
                    else:
                        new_content = f"title {target_title}\n" + new_content

                if options_line and fpath.name != default_filename:
                    if re.search(r"^options\s+", new_content, re.MULTILINE):
                        new_content = re.sub(r"^options\s+.*$", options_line, new_content, flags=re.MULTILINE)
                    else:
                        new_content = new_content.rstrip() + f"\n{options_line}\n"

            if new_content != content:
                self._exec(["sudo", "sh", "-c", f"printf '%s' {shlex.quote(new_content)} > {shlex.quote(str(fpath))}"],
                           stream=False, timeout=10)

    def _read_file_sudo(self, path: Path) -> str | None:
        r = self._exec(["sudo", "cat", str(path)], stream=False)
        return r.stdout if r.returncode == 0 else None

    def _install_yay(self) -> bool:
        if not self.distro or not self.distro.has_aur:
            self.outputReceived.emit("yay is not supported on this distribution", "warning")
            return True
        if self._pkg_cache and self._pkg_cache.is_installed("yay"):
            self.outputReceived.emit("yay already installed", "success")
            return True

        build_deps = ("base-devel", "git", "go")
        missing_deps = [p for p in build_deps if not self.distro.package_is_installed(p)]
        freshly_added = set(missing_deps)

        if missing_deps and self._exec(self.distro.get_batch_install_cmd(missing_deps), stream=True).returncode != 0:
            return False

        yay_dir = _HOME / "yay"
        shutil.rmtree(yay_dir, ignore_errors=True)
        for msg, cmd, kw in [("Cloning yay…", ["git", "clone", "https://aur.archlinux.org/yay.git"], {"cwd": str(_HOME)}),
                             ("Building yay…", ["makepkg", "-c", "--noconfirm"], {"cwd": str(yay_dir)})]:
            self.outputReceived.emit(msg, "subprocess")
            if self._exec(cmd, stream=True, **kw).returncode != 0:
                shutil.rmtree(yay_dir, ignore_errors=True)
                return False

        to_remove = []
        if "go" in freshly_added and self.distro.package_is_installed("go"):
            to_remove.append("go")
            self._exec(["go", "clean", "-modcache"], timeout=30)
            for d in (_HOME / ".config" / "go", _HOME / "go"):
                shutil.rmtree(d, ignore_errors=True)

        if self.distro.package_is_installed("yay-debug"): to_remove.append("yay-debug")
        if to_remove and self._exec(["sudo", "pacman", "-R", "--noconfirm"] + to_remove, stream=True).returncode != 0:
            logger.warning("_install_yay: cleanup of %s failed", to_remove)

        def _pkg_key(f):
            try:
                mtime = -f.stat().st_mtime
            except OSError:
                mtime = 0
            return 0 if "-debug-" not in f.name else 1, mtime
        pkgs = sorted((f for f in yay_dir.iterdir() if ".pkg.tar." in f.name), key=_pkg_key)

        if not pkgs:
            self.outputReceived.emit("No yay package file found after build", "error")
            return False

        ok = (self._exec(["sudo", "pacman", "-U", "--noconfirm", str(pkgs[0])], stream=True).returncode == 0)
        shutil.rmtree(yay_dir, ignore_errors=True)
        if ok and self._pkg_cache: self._pkg_cache.mark_installed("yay")
        self._emit_result(ok, "yay successfully installed", "yay installation failed")
        return ok

    def _install_specific(self) -> str | bool:
        if not self.distro: return False
        if not (session := self.distro.detect_session()):
            self.outputReceived.emit("Cannot determine desktop session — skipping specific packages", "warning")
            return _Status.SUCCESS
        self.outputReceived.emit(f"Detected session: {session}", "success")

        pkgs = [p["package"] for p in (S.specific_packages or []) if isinstance(p, dict) and p.get("session") == session
                and "package" in p and not p.get("disabled", False)]
        to_install = self.distro.filter_not_installed(pkgs) if pkgs else []
        if not to_install:
            self.outputReceived.emit(f"All Specific Packages for {session} already installed", "success")
            return _Status.SUCCESS

        self.outputReceived.emit(f"Installing: {', '.join(to_install)}", "info")
        _distro = self.distro
        failed = self._install_with_retry(to_install, lambda batch: self._exec(_distro.get_batch_install_cmd(batch), stream=True),
                                          lambda pkg: self._exec(_distro.get_pkg_install_cmd(pkg), stream=True))

        self._emit_result(not failed, "All Specific Packages successfully installed", f"Failed to install: {', '.join(failed)}")
        return _Status.SUCCESS if not failed else _Status.WARNING

    def _install_flatpak(self) -> bool:
        if not self.distro: return False
        results = [self._install_pkg(p, "Flatpak") for p in self.distro.get_flatpak_packages()]
        if not all(results):
            return False
        self.outputReceived.emit("Adding Flathub remote…", "info")
        try:
            cmd = self.distro.flatpak_add_flathub()
            self.outputReceived.emit(f"Running: {cmd}", "info")
            ok = (self._exec(cmd, stream=True).returncode == 0)
            self._emit_result(ok, "Flathub remote successfully added", "Failed to add Flathub remote")
            return ok
        except Exception as exc:
            self.outputReceived.emit(f"Flathub setup error: {exc}", "error")
            return False

    def _setup_service(self, service: str, packages: list) -> bool:
        return (not packages or all(self._install_pkg(p, "Service Package") for p in packages)) and self._enable_service(service)

    def _enable_service(self, service: str) -> bool:
        svc = f"{service}.service"
        self.outputReceived.emit(f"Enabling {svc}", "info")
        if self._exec(["systemctl", "is-active", "--quiet", svc]).returncode == 0:
            self.outputReceived.emit(f"{svc} already active", "success")
            return True
        ok = (self._exec(["sudo", "systemctl", "enable", "--now", svc], stream=True).returncode == 0)
        if ok:
            self.outputReceived.emit(f"{svc} successfully enabled", "success")
            if service == "ufw":
                for c in (["sudo", "ufw", "default", "deny"], ["sudo", "ufw", "enable"], ["sudo", "ufw", "reload"]):
                    if self._exec(c, stream=True).returncode != 0: return False
        else:
            self.outputReceived.emit(f"Failed to enable {svc}", "error")
        return ok

    def _remove_orphans(self) -> bool:
        if not self.distro: return False
        cmd = self.distro.get_find_orphans_cmd()
        if not cmd:
            self.outputReceived.emit("Orphan removal not supported on this distribution", "info")
            return True
        raw = self._exec(cmd, stream=False, timeout=60).stdout.strip()
        pkgs = self.distro.parse_orphan_output(raw) if raw else []
        if not pkgs:
            self.outputReceived.emit("No orphaned packages found", "success")
            return True
        self.outputReceived.emit(f"Found orphaned packages: {', '.join(pkgs)}", "info")
        fam = self.distro.family()
        if fam == "nixos":
            cmd = f"nix-env -e {' '.join(pkgs)}"
        else:
            cmd = self.distro.get_pkg_remove_cmd(" ".join(pkgs))
        ok = (self._exec(cmd, stream=True).returncode == 0)
        self._emit_result(ok, "Orphaned packages successfully removed", "Could not remove orphaned packages")
        return ok

    def _clean_cache(self) -> bool:
        if not self.distro: return False
        pm = self.distro.pkg_manager_name()
        self.outputReceived.emit(f"Cleaning {pm} cache", "info")
        ok = (self._exec(self.distro.get_clean_cache_cmd(), stream=True).returncode == 0)
        self._emit_result(ok, f"{pm} cache successfully cleaned", f"{pm} cache cleaning failed")
        if ok and self._pkg_cache and self._pkg_cache.is_installed("yay"):
            self.outputReceived.emit("", "info")
            self.outputReceived.emit("Cleaning yay cache", "info")
            yay_ok = (self._exec(["yay", "-Scc", "--noconfirm"], stream=True).returncode == 0)
            self._emit_result(yay_ok, "yay cache successfully cleaned", "yay cache cleaning failed")
            if not yay_ok:
                logger.warning("_clean_cache: yay cache cleaning failed (non-critical)")
        return ok
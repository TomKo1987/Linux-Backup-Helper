import html as _html
import os
import pwd
import queue
import re
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
    QDialog, QGraphicsDropShadowEffect, QHBoxLayout,
    QLabel, QListWidget, QListWidgetItem, QPushButton, QTextEdit, QVBoxLayout
)

from linux_distro_helper import distro_family
from state import S, _HOME, _USER, logger, apply_replacements
from themes import current_theme, font_sz


def _zero(buf: bytearray) -> None:
    buf[:] = bytearray(len(buf))


def _pw_bytes(pw) -> bytearray:
    return pw.get_bytes() + b"\n" if pw else bytearray()


_INFO_RE = re.compile(
    r"INFO:|rating|mirror|download|synchroniz|\$srcdir/|Erfolg|Übertragung|"
    r"avg speed|=====|OK|Statuserläuterung|Klon|PGP|MiB|Fertig|\.\.\.",
    re.IGNORECASE,
)


class _Status: PENDING, IN_PROGRESS, SUCCESS, WARNING, ERROR = ("pending", "in_progress", "success", "warning", "error")


class _Style:
    _FM = "DejaVu Sans Mono, Fira Code, monospace"; _FS = "Hack, Fira Mono, monospace"

    KIND_CFG: dict[str, tuple] = {"operation": (_FM, 16, 1.2, "info"), "info": (_FM, 15, 1.0, "success"),
                                  "subprocess": (_FS, 13, 0.6, "text"), "success": (_FM, 15, 1.0, "success"),
                                  "warning": (_FM, 15, 1.0, "warning"), "error": (_FM, 15, 1.0, "error")}

    STATUS_CFG: dict[str, tuple] = {_Status.SUCCESS: ("success", "dialog-ok-apply"), _Status.ERROR: ("error", "dialog-error"),
                                    _Status.WARNING: ("warning", "dialog-warning"),  _Status.IN_PROGRESS: ("info", "media-playback-start")}

    @classmethod
    def style_str(cls, kind: str) -> str:
        cfg = cls.KIND_CFG.get(kind)
        if not cfg: return ""
        font, size, lh, ck = cfg
        color = current_theme().get(ck, current_theme()["text"])
        return f"font-family:{font};font-size:{size}px;color:{color};padding:5px;line-height:{lh};word-break:break-word;"

    @classmethod
    def border_style(cls) -> str:
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
                        subprocess.run(["sudo", "-S", "-v"], input=bytes(buf), capture_output=True, timeout=10)
                    finally:
                        _zero(buf)
            except Exception as exc:
                logger.debug("_SudoKeepalive: %s", exc)


class _PackageCache:
    _TTL, _MAX = 600, 1000

    def __init__(self, distro) -> None: self._distro, self._cache, self._ts, self._lock = distro, {}, 0.0, threading.Lock()

    def is_installed(self, pkg: str) -> bool:
        now = time.monotonic()
        with self._lock:
            if now - self._ts > self._TTL:
                self._cache.clear()
                self._ts = now
            elif len(self._cache) > self._MAX:
                self._cache = dict(list(self._cache.items())[self._MAX // 2:])
            if pkg in self._cache:
                return self._cache[pkg]
        result = self._distro.package_is_installed(pkg)
        with self._lock:
            self._cache.setdefault(pkg, result)
        return result

    def mark_installed(self, pkg: str) -> None:
        with self._lock: self._cache[pkg] = True


class SystemManagerDialog(QDialog):
    DIALOG_SIZE = (1875, 1000)
    BUTTON_SIZE = (160, 50)
    RIGHT_W = 370
    cancelRequested = pyqtSignal()

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

        left = QVBoxLayout()
        left.addWidget(self._text_edit)
        left.addWidget(self._fail_lbl)

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
        ec = QColor(t["error"])
        r, g, b = ec.red(), ec.green(), ec.blue()
        self._append_html(f"<hr style='border:none;margin:10px 20px;border-top:1px dashed rgba({r},{g},{b},0.4);'>"
                          f"<div style='padding:15px;margin:10px;border-radius:10px;border-left:4px solid {t['error']};'>"
                          f"<p style='color:{t['error']};font-size:{font_sz(4)}px;text-align:center;'>"
                          f"<b>⚠️ Installation Aborted</b><br>"
                          f"<span style='font-size:{font_sz(2)}px;'>/var/lib/pacman/db.lck detected!</span><br>"
                          f"<span style='color:{t['text']};font-size:{font_sz()}px;'>"
                          f"Remove with: <code>sudo rm /var/lib/pacman/db.lck</code></span></p></div>")
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
        self._append_html(f"<hr style='border:none;margin:25px 50px;border-top:2px solid {colour};'>"
                          f"<div style='text-align:center;padding:20px;margin:15px 30px;border-radius:15px;"
                          f"border:1px solid rgba({r},{g},{b},0.3);'><p style='color:{colour};font-size:{font_sz(6)}px;"
                          f"font-weight:bold;'>{icon} {summary}</p><p style='color:{colour};font-size:{font_sz(4)}px;"
                          f"'>System Manager {'completed with warnings/errors' 
                          if err else 'successfully completed all operations'}<br></p></div>")
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
        k = event.key()
        if k == Qt.Key.Key_Escape and not self._done and not self._auth_failed: event.ignore()
        elif k == Qt.Key.Key_Tab: self.focusNextChild()
        else: super().keyPressEvent(event)

    def closeEvent(self, event) -> None: super().closeEvent(event) if (self._done or self._auth_failed) else event.ignore()


class SystemManagerThread(QThread):
    thread_started = pyqtSignal()
    outputReceived = pyqtSignal(str, str)
    taskStatusChanged = pyqtSignal(str, str)
    taskListReady = pyqtSignal(list)
    passwordFailed = pyqtSignal()
    passwordSuccess = pyqtSignal()

    def __init__(self, sudo_password, distro) -> None:
        super().__init__()
        from sudo_password import SecureString
        self._pw = sudo_password if isinstance(sudo_password, SecureString) else SecureString(sudo_password or "")
        self._stop = threading.Event()
        self._stop_keepalive = threading.Event()
        self._keepalive: Optional[_SudoKeepalive] = None
        self._enabled_tasks: dict[str, tuple] = {}
        try:
            self.distro, self._pkg_cache = distro, _PackageCache(distro)
        except Exception as exc:
            logger.warning("distro init: %s", exc)
            self.distro = self._pkg_cache = None

    @property
    def terminated(self) -> bool: return self._stop.is_set()

    @terminated.setter
    def terminated(self, v: bool) -> None: self._stop.set() if v else self._stop.clear()

    def run(self) -> None:
        self.thread_started.emit()
        self._prepare_tasks()
        try:
            if self.terminated: return
            if not self._verify_sudo(): self.passwordFailed.emit(); return
            self.passwordSuccess.emit()
            self._stop_keepalive.clear()
            self._keepalive = _SudoKeepalive(self._stop_keepalive, self._pw)
            self._keepalive.start()
            if not self.terminated: self._run_all_tasks()
        except Exception as exc:
            self.outputReceived.emit(f"Critical error: {exc}", "error")
        finally:
            self._cleanup(); self.outputReceived.emit("", "finish")

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
                "update_mirrors": ("Updating mirrors…", self._update_mirrors), "set_user_shell": ("Setting user shell…", self._set_shell),
                "update_system": ("Updating system…", self._update_system),
                "install_kernel_header": ("Installing kernel headers…", self._install_kernel_header),
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
        for task_id, (desc, fn) in self._enabled_tasks.items():
            if self.terminated: break
            self.taskStatusChanged.emit(task_id, _Status.IN_PROGRESS)
            self.outputReceived.emit(desc, "operation")
            try:
                res    = fn()
                status = _Status.ERROR if res is False else _Status.WARNING if res == _Status.WARNING else _Status.SUCCESS
            except Exception as exc:
                self.outputReceived.emit(f"Task '{task_id}' failed: {exc}", "error"); status = _Status.ERROR
            self.taskStatusChanged.emit(task_id, status)
            if status == _Status.ERROR:
                self.outputReceived.emit(f"Aborting remaining tasks due to failure in '{task_id}'.", "error"); break

    @staticmethod
    def _env() -> dict: return dict(os.environ)

    @staticmethod
    def _inject(cmd: list[str]) -> list[str]:
        if cmd and cmd[0] == "sudo" and not (len(cmd) > 1 and cmd[1] == "-S"):
            return ["sudo", "-S"] + cmd[1:]
        return cmd

    def _exec(self, cmd: list[str] | str, stream: bool = False,
              timeout: Optional[int] = 15, cwd: Optional[str] = None) -> SimpleNamespace:
        if self.terminated:
            return SimpleNamespace(returncode=1, stdout="", stderr="")

        if isinstance(cmd, str):
            if stream or cmd.startswith("sudo"):
                cmd = self._inject(shlex.split(cmd))
            else:
                cmd = shlex.split(cmd)
        elif isinstance(cmd, list):
            cmd = list(cmd)
            cmd = self._inject(cmd)

        input_data = None
        if self._pw and isinstance(cmd, list):
            if cmd[:2] == ["sudo", "-S"] or cmd[:1] == ["yay"]:
                input_data = _pw_bytes(self._pw)
                if cmd[:1] == ["yay"] and "--sudoflags=-S" not in cmd:
                    cmd.append("--sudoflags=-S")

        if not stream:
            try:
                r = subprocess.run(cmd, input=input_data, capture_output=True, env=self._env(), timeout=timeout)
                return SimpleNamespace(returncode=r.returncode, stdout=r.stdout.decode("utf-8", "replace"),
                                       stderr=r.stderr.decode("utf-8", "replace"))
            except subprocess.TimeoutExpired:
                return SimpleNamespace(returncode=124, stdout="", stderr="Timeout")
            except Exception as exc:
                return SimpleNamespace(returncode=1, stdout="", stderr=str(exc))
            finally:
                if isinstance(input_data, bytearray): _zero(input_data)

        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=cwd, env=self._env(),
                                    stdin=subprocess.PIPE if input_data else None)
        except Exception as exc:
            if isinstance(input_data, bytearray): _zero(input_data)
            self.outputReceived.emit(f"Command launch error: {exc}", "error")
            return SimpleNamespace(returncode=1)

        if input_data:
            def _write_stdin() -> None:
                try:
                    if proc.stdin: proc.stdin.write(input_data); proc.stdin.flush()
                except OSError:
                    pass
                finally:
                    try:
                        proc.stdin and proc.stdin.close()
                    except OSError:
                        pass
                    if isinstance(input_data, bytearray): _zero(input_data)
            threading.Thread(target=_write_stdin, daemon=True).start()
        out_q = queue.Queue()

        def _reader(_stream, _is_err):
            for raw in iter(_stream.readline, b""):
                if self.terminated: break
                line = raw.decode("utf-8", errors="replace").strip()
                if line: out_q.put((_is_err, line))
            out_q.put(None)

        t1 = threading.Thread(target=_reader, args=(proc.stdout, False), daemon=True)
        t2 = threading.Thread(target=_reader, args=(proc.stderr, True), daemon=True)
        t1.start(); t2.start()

        sentinels = 0
        while sentinels < 2:
            try:
                item = out_q.get(timeout=0.1)
            except queue.Empty:
                if self.terminated: proc.terminate(); break
                continue
            if item is None:
                sentinels += 1
            else:
                is_err, text = item
                if "[sudo]" in text: text = re.sub(r"\[sudo].*?:\s*", "", text)
                self.outputReceived.emit(text, "error" if is_err and not _INFO_RE.search(text) else "subprocess")

        try:
            rc = proc.wait(timeout=5) if proc.poll() is None else proc.returncode
        except subprocess.TimeoutExpired:
            proc.kill()
            rc = proc.wait()
        t1.join(1); t2.join(1)
        return SimpleNamespace(returncode=rc if rc is not None else 1)

    def _emit_result(self, ok: bool, msg_ok: str, msg_err: str): self.outputReceived.emit(msg_ok if ok else msg_err, "success" if ok else "error")

    def _verify_sudo(self) -> bool:
        self.outputReceived.emit("Verifying sudo access…", "operation")
        subprocess.run(["sudo", "-k"], capture_output=True, timeout=5)
        token = "SUDO_VERIFY_4a8f2b"
        pw_buf = _pw_bytes(self._pw)
        ok, proc = False, None
        try:
            proc = subprocess.Popen(["sudo", "-S", "sh", "-c", f"printf '%s\\n' {shlex.quote(token)}"], env=self._env(),
                                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try: proc.stdin.write(bytes(pw_buf)); proc.stdin.flush(); proc.stdin.close()
            except OSError: pass

            chunks: list[bytes] = []
            def _read_out() -> None:
                try: chunks.append(proc.stdout.read())
                except (OSError, ValueError): pass

            def _kill_on_retry() -> None:
                n = 0
                try:
                    for raw in iter(proc.stderr.readline, b""):
                        line = raw.decode("utf-8", errors="replace")
                        if "[sudo]" in line or "password" in line.lower():
                            n += 1
                            if n >= 2:
                                try:
                                    proc.kill()
                                except ProcessLookupError:
                                    pass
                                return
                except (OSError, ValueError):
                    pass

            t1 = threading.Thread(target=_read_out, daemon=True); t2 = threading.Thread(target=_kill_on_retry, daemon=True)
            t1.start(); t2.start()
            try: proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                try: proc.wait(timeout=2)
                except subprocess.TimeoutExpired: pass
            t1.join(2); t2.join(2)
            ok = proc.returncode == 0 and token.encode() in (chunks[0] if chunks else b"")
        except Exception as exc: logger.error("_verify_sudo: %s", exc)
        finally:
            _zero(pw_buf)
            if proc and proc.returncode is None:
                try: proc.kill()
                except OSError: pass
            self.outputReceived.emit("Sudo access successfully verified" if ok else "Authentication failed: Invalid Password",
                                     "success" if ok else "error")
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
        bulk_fn(pkgs)
        still_missing = self.distro.filter_not_installed(pkgs)
        for p in pkgs:
            if p not in still_missing and self._pkg_cache: self._pkg_cache.mark_installed(p)
        failed = []
        if still_missing:
            self.outputReceived.emit("Some packages were not installed — trying them individually…", "warning")
            for pkg in still_missing:
                if self.terminated: break
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
        items = []
        for p in (pkg_list or []):
            if isinstance(p, dict):
                n = (p.get("name") or p.get("package") or "").strip()
                if not n or p.get("disabled"):
                    continue
            else:
                n = str(p).strip()
                if not n:
                    continue
            if all(c.isalnum() or c in "-_.+" for c in n):
                items.append(n)

        if not items:
            self.outputReceived.emit(f"No active {label}s configured", "info")
            return True

        to_install = self.distro.filter_not_installed(items)
        if not to_install:
            self.outputReceived.emit(f"All {label}s already installed", "success")
            return True

        failed = []
        if use_aur:
            bulk = lambda b: self._exec(["yay", "-S", "--noconfirm", "--needed"] + b, stream=True)
            single = lambda _p: self._exec(["yay", "-S", "--noconfirm", "--needed", _p], stream=True)
        else:
            bulk = lambda b: self._exec(self.distro.get_batch_install_cmd(b), stream=True)
            single = lambda p_: self._exec(self.distro.get_pkg_install_cmd(p_), stream=True)

        for i in range(0, len(to_install), 20):
            if self.terminated: break
            batch = to_install[i:i + 20]
            self.outputReceived.emit(f"Installing: {', '.join(batch)}", "info")
            failed.extend(self._install_with_retry(batch, bulk, single))

        self._emit_result(not failed, f"All {label}s successfully installed", f"Failed {label}(s): {', '.join(failed)}")
        return _Status.SUCCESS if not failed else _Status.WARNING

    def _copy_sysfiles(self) -> bool:
        files = [f for f in (S.system_files or []) if isinstance(f, dict) and not f.get("disabled")
                 and f.get("source", "").strip() and f.get("destination", "").strip()]
        if not files:
            self.outputReceived.emit("No system files configured", "warning")
            return True

        from drive_utils import check_drives_to_mount, mount_drive
        for drive in check_drives_to_mount([p for f in files for p in (f["source"], f["destination"])]):
            name = drive.get("drive_name", "?")
            self.outputReceived.emit(f"Mounting drive: {name}…", "info")
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
        if distro_family(self.distro.distro_id) != "arch":
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

    def _set_shell(self) -> bool:
        if not self.distro: return False
        target = (S.user_shell or "bash").strip()
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

    def _update_system(self) -> bool:
        if not self.distro: return False
        if self.distro.has_aur and self._pkg_cache and self._pkg_cache.is_installed("yay"):
            ok = (self._exec(["yay", "-Syu", "--noconfirm"], stream=True).returncode == 0)
        else:
            cmd_str = self.distro.get_update_system_cmd()
            cmd = ["sudo", "sh", "-c", cmd_str] if any(c in cmd_str for c in "&|") else cmd_str
            ok = (self._exec(cmd, stream=True).returncode == 0)
        self._emit_result(ok, "System successfully updated", "System update failed")
        return ok

    def _install_kernel_header(self) -> bool:
        return self._install_pkg(self.distro.get_kernel_headers_pkg(), "Kernel Header") if self.distro else False

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
            if self._exec(cmd, stream=True, **kw).returncode != 0: return False

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
        pkgs = sorted((f for f in yay_dir.iterdir() if f.name.endswith(".pkg.tar.zst")), key=_pkg_key)

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
            self.outputReceived.emit("Cannot determine desktop session", "warning")
            return False
        self.outputReceived.emit(f"Detected session: {session}", "success")

        pkgs = [p["package"] for p in (S.specific_packages or []) if isinstance(p, dict) and p.get("session") == session
                and "package" in p and not p.get("disabled", False)]
        to_install = self.distro.filter_not_installed(pkgs) if pkgs else []
        if not to_install:
            self.outputReceived.emit(f"All Specific Packages for {session} already installed", "success")
            return True

        self.outputReceived.emit(f"Installing: {', '.join(to_install)}", "info")
        failed = self._install_with_retry(to_install, lambda batch: self._exec(self.distro.get_batch_install_cmd(batch), stream=True),
                                          lambda pkg: self._exec(self.distro.get_pkg_install_cmd(pkg), stream=True))

        self._emit_result(not failed, "All Specific Packages successfully installed", f"Failed to install: {', '.join(failed)}")
        return _Status.SUCCESS if not failed else _Status.WARNING

    def _install_flatpak(self) -> bool:
        if not self.distro: return False
        if not all(self._install_pkg(p, "Flatpak") for p in self.distro.get_flatpak_packages()): return False
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
        raw = self._exec(cmd, timeout=60).stdout.strip()
        pkgs = self.distro.parse_orphan_output(raw) if raw else []
        if not pkgs:
            self.outputReceived.emit("No orphaned packages found", "success")
            return True
        self.outputReceived.emit(f"Found orphaned packages: {', '.join(pkgs)}", "info")
        ok = (self._exec(self.distro.get_pkg_remove_cmd(" ".join(pkgs)), stream=True).returncode == 0)
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
            ok = ok and yay_ok
        return ok
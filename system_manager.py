from pathlib import Path
from typing import Optional
import atexit, gc, html as _html, os, queue, re, select, shlex, shutil, signal
import subprocess, tempfile, threading, time, urllib.error, urllib.request, pwd
from types import SimpleNamespace

from PyQt6.QtGui import QColor, QIcon, QTextCursor
from PyQt6.QtCore import Qt, QElapsedTimer, QThread, QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QDialog, QGraphicsDropShadowEffect, QHBoxLayout,
    QLabel, QListWidget, QListWidgetItem, QPushButton, QTextEdit, QVBoxLayout
)

from themes import current_theme
from linux_distro_helper import distro_family
from state import S, _HOME, _USER, logger, apply_replacements


_cleanup_lock = threading.Lock()
_cleanup_paths: list[str] = []


def _register_tmpdir(path: str) -> None:
    with _cleanup_lock:
        _cleanup_paths.append(path)


def _unregister_tmpdir(path: str) -> None:
    with _cleanup_lock:
        try:
            _cleanup_paths.remove(path)
        except ValueError:
            pass


def _wipe_tmpdir(path: str) -> None:
    if not path:
        return
    pw_file = Path(path, "sudo_pass")
    if pw_file.exists():
        try:
            size = pw_file.stat().st_size
            if size:
                with open(pw_file, "r+b") as fh:
                    fh.write(os.urandom(size))
                    fh.flush()
                    os.fsync(fh.fileno())
            pw_file.unlink()
        except Exception as exc:
            logger.warning("_wipe_tmpdir pw: %s", exc)
    try:
        shutil.rmtree(path, ignore_errors=True)
    except Exception as exc:
        logger.warning("_wipe_tmpdir rmtree: %s", exc)


def _emergency_cleanup() -> None:
    with _cleanup_lock:
        paths = list(_cleanup_paths)
        _cleanup_paths.clear()
    for p in paths:
        _wipe_tmpdir(p)


atexit.register(_emergency_cleanup)

_orig_sigterm = signal.getsignal(signal.SIGTERM)


def _sigterm_handler(signum, frame):
    logger.warning("SIGTERM — emergency cleanup")
    _emergency_cleanup()
    if callable(_orig_sigterm):
        _orig_sigterm(signum, frame)
    else:
        raise SystemExit(0)


signal.signal(signal.SIGTERM, _sigterm_handler)


def _make_askpass(pw_secure) -> Optional[tuple[str, Path]]:
    try:
        base = "/dev/shm" if os.path.isdir("/dev/shm") and os.access("/dev/shm", os.W_OK) else None
        tmp_dir = tempfile.mkdtemp(prefix="sm_", dir=base)
        os.chmod(tmp_dir, 0o700)
        _register_tmpdir(tmp_dir)

        pw_path = Path(tmp_dir, "sudo_pass")
        ask_path = Path(tmp_dir, "askpass.sh")

        pw_raw = pw_secure.get()
        pw_buf = bytearray(pw_raw.encode("utf-8"))
        del pw_raw
        gc.collect()
        try:
            fd = os.open(str(pw_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            try:
                os.write(fd, pw_buf)
                os.fsync(fd)
            finally:
                os.close(fd)
        finally:
            for i in range(len(pw_buf)):
                pw_buf[i] = 0
            del pw_buf

        ask_path.write_text(f'#!/bin/sh\ncat {shlex.quote(str(pw_path))}\n', encoding="ascii")
        os.chmod(ask_path, 0o700)
        return tmp_dir, ask_path

    except Exception as exc:
        logger.error("_make_askpass: %s", exc)
        return None


class SystemManagerDialog(QDialog):
    DIALOG_SIZE = (1850, 1000)
    BUTTON_SIZE = (160, 50)
    RIGHT_ITEMS_WIDTH = 370

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("System Manager")
        self._task_status: dict[str, str] = {}
        self._done = self._auth_failed = self._has_error = False
        self._timer = QElapsedTimer()
        self._ticker = QTimer(self)
        self._build_ui()

    def _build_ui(self) -> None:
        t = current_theme()
        self.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
        self.setFixedSize(*self.DIALOG_SIZE)
        self.setStyleSheet(f"QTextEdit{{background:qlineargradient(x1:0,y1:0,x2:1,y2:0,"
                           f"stop:0 {t['bg']},stop:1 {t['bg2']});color:{t['text']};border:none;border-radius:8px;}}")

        self._text_edit = QTextEdit()
        self._text_edit.setReadOnly(True)
        self._text_edit.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        self._text_edit.setHtml(f"<p style='color:{t['success']};font-size:20px;text-align:center;margin-top:25px;'>"
                                "<b>System Manager</b><br>Initialising…</p>")

        self._fail_lbl = QLabel()
        self._fail_lbl.setStyleSheet(f"color:{t['error']};font-size:16px;font-weight:bold;padding:10px;"
                                     f"margin-top:8px;border-radius:8px;"
                                     f"background-color:{t['bg3']};border-left:4px solid {t['error']};")
        self._fail_lbl.setMinimumHeight(52)
        self._fail_lbl.setWordWrap(True)
        self._fail_lbl.setVisible(False)

        left = QVBoxLayout()
        left.addWidget(self._text_edit)
        left.addWidget(self._fail_lbl)

        shadow = QGraphicsDropShadowEffect()
        shadow.setBlurRadius(80)
        shadow.setXOffset(15)
        shadow.setYOffset(15)
        shadow.setColor(QColor(0, 0, 0, 160))

        self._checklist_lbl = QLabel("Pending Operations:")
        self._checklist = QListWidget()
        self._style_checklist()

        self._elapsed_lbl = QLabel("Elapsed time:\n00s")
        self._style_elapsed(shadow)

        self._close_btn = QPushButton("Close")
        self._close_btn.setFixedSize(*self.BUTTON_SIZE)
        self._close_btn.clicked.connect(self.accept)
        self._close_btn.setEnabled(False)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        btn_row.addWidget(self._close_btn)

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

    def _style_checklist(self) -> None:
        t, bs = current_theme(), _Style.border_style()
        self._checklist_lbl.setStyleSheet(f"color:{t['info']};font-size:18px;font-weight:bold;padding:10px;"
                                          f"background:qlineargradient(x1:0,y1:0,x2:1,y2:0,stop:0 {t['bg2']},stop:1 {t['header_sep']});{bs}")
        self._checklist_lbl.setFixedWidth(self.RIGHT_ITEMS_WIDTH)
        self._checklist.setStyleSheet(f"QListWidget{{background:qlineargradient(x1:0,y1:0,x2:1,y2:0,"
                                      f"stop:0 {t['bg2']},stop:1 {t['header_sep']});font-size:15px;padding:4px;{bs}}}"
                                      "QListWidget::item{padding:4px;border-radius:4px;border:1px solid transparent;}")
        self._checklist.setFixedWidth(self.RIGHT_ITEMS_WIDTH)

    def _style_elapsed(self, shadow: QGraphicsDropShadowEffect) -> None:
        t, bs = current_theme(), _Style.border_style()
        self._elapsed_lbl.setGraphicsEffect(shadow)
        self._elapsed_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._elapsed_lbl.setFixedSize(self.RIGHT_ITEMS_WIDTH, 75)
        self._elapsed_lbl.setStyleSheet(f"color:{t['info']};font-size:17px;{bs}"
                                        f"background:qlineargradient(x1:0,y1:0,x2:1,y2:0,stop:0 {t['bg2']},stop:1 {t['header_sep']});"
                                        "text-align:center;font-weight:bold;padding:3px;")

    def on_output(self, text: str, kind: str) -> None:
        if "/var/lib/pacman/db.lck" in text:
            self._show_db_lock_error()
            return
        if kind == "finish":
            self._show_completion()
            return
        if kind not in _Style.KIND_CFG:
            return
        self._append_html(text if ("<span " in text or "<p " in text) else _fmt_html(text, kind))

    def on_task_list(self, task_descs: list) -> None:
        try:
            self._init_checklist(task_descs)
        except Exception as exc:
            logger.error("on_task_list: %s", exc)

    def on_task_status(self, task_id: str, status: str) -> None:
        if status in (_Status.ERROR, _Status.WARNING):
            self._has_error = True
        if self._task_status.get(task_id) == status:
            return
        self._task_status[task_id] = status
        cfg = _Style.STATUS_CFG.get(status)
        if not cfg:
            return
        t = current_theme()
        color_key, icon_name = cfg
        colour = t[color_key]
        for i in range(self._checklist.count()):
            item = self._checklist.item(i)
            if item and item.data(Qt.ItemDataRole.UserRole) == task_id:
                item.setIcon(QIcon.fromTheme(icon_name))
                item.setForeground(QColor(colour))
                bg = QColor(colour)
                bg.setAlpha(25)
                item.setBackground(bg)
                self._checklist.scrollToItem(item)
                break

    def mark_done(self, failed_count: int = 0) -> None:
        if self._done:
            return
        self._done = True
        self._ticker.stop()
        if failed_count == 0:
            self._fail_lbl.setVisible(False)
            self._close_btn.setEnabled(True)
            self._close_btn.setFocus()
        else:
            self._fail_lbl.setText(f" ⚠️ Failed authentication attempts: {failed_count}")
            self._fail_lbl.setVisible(True)
            self._auth_failed = True
            self._close_btn.setEnabled(True)
            self._close_btn.setFocus()

    def _init_checklist(self, task_descs: list[tuple[str, str]]) -> None:
        t = current_theme()
        self._checklist.clear()
        self._task_status.clear()
        for task_id, desc in task_descs:
            clean = desc.replace("…", "").replace("...", "").strip()
            item = QListWidgetItem(clean)
            item.setData(Qt.ItemDataRole.UserRole, task_id)
            item.setIcon(QIcon.fromTheme("dialog-question"))
            item.setForeground(QColor(t["muted"]))
            self._checklist.addItem(item)
            self._task_status[task_id] = _Status.PENDING
        n = self._checklist.count()
        total = sum(max(self._checklist.sizeHintForRow(i), 28) for i in range(n)) + 2 * self._checklist.frameWidth()
        self._checklist.setFixedHeight(max(total, 40) if n else 40)

    def _append_html(self, html: str) -> None:
        if not html:
            return
        try:
            cur = self._text_edit.textCursor()
            cur.movePosition(QTextCursor.MoveOperation.End)
            cur.insertHtml(html)
            self._text_edit.setTextCursor(cur)
            sb = self._text_edit.verticalScrollBar()
            if sb:
                sb.setValue(sb.maximum())
        except Exception as exc:
            logger.error("_append_html: %s", exc)

    def _show_db_lock_error(self) -> None:
        t = current_theme()
        ec = QColor(t["error"])
        r, g, b = ec.red(), ec.green(), ec.blue()
        self._append_html(f"<hr style='border:none;margin:10px 20px;border-top:1px dashed rgba({r},{g},{b},0.4);'>"
                          f"<div style='padding:15px;margin:10px;border-radius:10px;border-left:4px solid {t['error']};'>"
                          f"<p style='color:{t['error']};font-size:18px;text-align:center;'>"
                          f"<b>⚠️ Installation Aborted</b><br>"
                          f"<span style='font-size:16px;'>/var/lib/pacman/db.lck detected!</span><br>"
                          f"<span style='color:{t['text']};font-size:14px;'>"
                          f"Remove with: <code>sudo rm /var/lib/pacman/db.lck</code></span></p></div>")
        self.mark_done()

    def _show_completion(self) -> None:
        if self._done or self._auth_failed:
            return
        self._done = True
        self._ticker.stop()
        t = current_theme()
        colour = t["warning" if self._has_error else "success"]
        icon = "⚠️" if self._has_error else "✅"
        summary = "Completed with issues" if self._has_error else "Successfully Completed"
        msg = "completed with warnings/errors" if self._has_error else "successfully completed all operations<br>"
        co = QColor(colour)
        r, g, b = co.red(), co.green(), co.blue()
        self._append_html(f"<hr style='border:none;margin:25px 50px;border-top:2px solid {colour};'>"
                          f"<div style='text-align:center;padding:20px;margin:15px 30px;"
                          f"border-radius:15px;border:1px solid rgba({r},{g},{b},0.3);'>"
                          f"<p style='color:{colour};font-size:20px;font-weight:bold;'>{icon} {summary}</p>"
                          f"<p style='color:{colour};font-size:18px;'>System Manager {msg}</p></div>")
        self._close_btn.setEnabled(True)
        self._close_btn.setFocus()
        bs = _Style.border_style()
        self._checklist_lbl.setText(f"{icon} {summary}")
        self._checklist_lbl.setStyleSheet(f"color:{colour};font-size:18px;font-weight:bold;padding:10px;"
                                          f"background-color:rgba({r},{g},{b},0.15);{bs}")

    def _update_elapsed(self) -> None:
        try:
            s = max(0, int(self._timer.elapsed() / 1000))
            h, rem = divmod(s, 3600)
            m, s = divmod(rem, 60)
            txt = (f"Elapsed time:\n{h:02}h {m:02}m {s:02}s" if h else
                   f"Elapsed time:\n{m:02}m {s:02}s" if m else
                   f"Elapsed time:\n{s:02}s")
            self._elapsed_lbl.setText(txt)
        except (RuntimeError, AttributeError):
            self._ticker.stop()
        except Exception as exc:
            logger.error("_update_elapsed: %s", exc)

    def keyPressEvent(self, event) -> None:
        k = event.key()
        if k == Qt.Key.Key_Escape and not self._done and not self._auth_failed:
            event.ignore()
        elif k == Qt.Key.Key_Tab:
            self.focusNextChild()
        else:
            super().keyPressEvent(event)

    def closeEvent(self, event) -> None:
        if not self._done and not self._auth_failed:
            event.ignore()
        else:
            super().closeEvent(event)


class SystemManagerThread(QThread):
    thread_started = pyqtSignal()
    outputReceived = pyqtSignal(str, str)
    taskStatusChanged = pyqtSignal(str, str)
    taskListReady = pyqtSignal(list)
    passwordFailed = pyqtSignal()
    passwordSuccess = pyqtSignal()

    def __init__(self, sudo_password) -> None:
        super().__init__()
        from sudo_password import SecureString
        self._pw = sudo_password if isinstance(sudo_password, SecureString) else SecureString(sudo_password or "")
        self._stop = threading.Event()
        self._tmp_dir: Optional[str] = None
        self._askpass: Optional[Path] = None
        self._enabled_tasks: dict[str, tuple] = {}

        try:
            from linux_distro_helper import LinuxDistroHelper
            self.distro = LinuxDistroHelper()
            self._pkg_cache = _PackageCache(self.distro)
        except Exception as exc:
            logger.warning("distro init: %s", exc)
            self.distro = None
            self._pkg_cache = None

    @property
    def terminated(self) -> bool:
        return self._stop.is_set()

    @terminated.setter
    def terminated(self, value: bool) -> None:
        if value:
            self._stop.set()
        else:
            self._stop.clear()

    def run(self) -> None:
        self.thread_started.emit()
        self._prepare_tasks()
        try:
            if self.terminated:
                return
            result = _make_askpass(self._pw)
            if result is None:
                self.outputReceived.emit("Failed to initialise secure askpass context", "error")
                self.passwordFailed.emit()
                return
            self._tmp_dir, self._askpass = result

            if not self._verify_sudo():
                self.passwordFailed.emit()
                return
            self.passwordSuccess.emit()
            if not self.terminated:
                self._run_all_tasks()
        except Exception as exc:
            self.outputReceived.emit(f"Critical error: {exc}", "error")
        finally:
            self._cleanup()
            self.outputReceived.emit("", "finish")

    def _cleanup(self) -> None:
        tmp = self._tmp_dir
        self._tmp_dir = self._askpass = None
        if tmp:
            _unregister_tmpdir(tmp)
            _wipe_tmpdir(tmp)
        self._pw.clear()

    def _prepare_tasks(self) -> None:
        all_tasks = {**self._base_tasks(), **self._service_tasks(),
                     "remove_orphaned_packages": ("Removing orphaned packages…", self._remove_orphans),
                     "clean_cache": ("Cleaning cache…", self._clean_cache)}
        self._enabled_tasks = {k: v for k, v in all_tasks.items() if k in S.system_manager_ops}
        self.taskListReady.emit([(k, desc) for k, (desc, _) in self._enabled_tasks.items()])

    def _base_tasks(self) -> dict:
        return {"copy_system_files": ("Copying System Files…", self._copy_sysfiles),
                "update_mirrors": ("Updating mirrors…", self._update_mirrors),
                "set_user_shell": ("Setting user shell…", self._set_shell),
                "update_system": ("Updating system…", self._update_system),
                "install_kernel_header": ("Installing kernel headers…", self._install_kernel_header),
                "install_basic_packages": ("Installing Basic Packages…",
                                           lambda: self._batch_install(S.basic_packages, "Basic Package")),
                "install_yay": ("Installing yay…", self._install_yay),
                "install_aur_packages": ("Installing AUR Packages with yay…",
                                         lambda: self._batch_install(S.aur_packages, "AUR Package", use_aur=True)),
                "install_specific_packages": ("Installing Specific Packages…", self._install_specific),
                "enable_flatpak_integration": ("Enabling Flatpak integration…", self._install_flatpak)}

    def _service_tasks(self) -> dict:
        if not self.distro:
            return {}
        d = self.distro
        specs = {"enable_printer_support": ("Initialising printer support…", "cups", d.get_printer_packages),
                 "enable_ssh_service": ("Initialising SSH server…", d.get_ssh_service_name(), d.get_ssh_packages),
                 "enable_samba_network_filesharing": ("Initialising Samba…", d.get_samba_service_name(),
                                                      d.get_samba_packages),
                 "enable_bluetooth_service": ("Initialising Bluetooth…", "bluetooth", d.get_bluetooth_packages),
                 "enable_atd_service": ("Initialising atd…", "atd", d.get_at_packages),
                 "enable_cronie_service": (f"Initialising {d.get_cron_service_name()}…", d.get_cron_service_name(),
                                           d.get_cron_packages),
                 "install_snap": ("Installing Snap…", "snapd", d.get_snap_packages),
                 "enable_firewall": ("Initialising firewall…", "ufw", d.get_firewall_packages)}
        return {k: (desc, (lambda svc, fn: lambda: self._setup_service(svc, fn()))(svc, pkg_fn)) for
                k, (desc, svc, pkg_fn) in specs.items()}

    def _run_all_tasks(self) -> None:
        for task_id, (desc, fn) in self._enabled_tasks.items():
            if self.terminated:
                break
            self.taskStatusChanged.emit(task_id, _Status.IN_PROGRESS)
            self.outputReceived.emit(desc, "operation")
            try:
                status = _Status.SUCCESS if fn() is not False else _Status.ERROR
            except Exception as exc:
                self.outputReceived.emit(f"Task '{task_id}' failed: {exc}", "error")
                status = _Status.ERROR
            self.taskStatusChanged.emit(task_id, status)

    def _env(self) -> dict:
        return {**os.environ, "SUDO_ASKPASS": str(self._askpass)}

    @staticmethod
    def _inject(cmd: list[str]) -> list[str]:
        if not cmd:
            return cmd
        if cmd[0] == "sudo" and "-A" not in cmd:
            return ["sudo", "-A"] + cmd[1:]
        if cmd[0] == "yay" and not any(a.startswith("--sudoflags=") for a in cmd):
            return ["yay", "--sudoflags=-A"] + cmd[1:]
        return cmd

    def _stream_cmd(self, cmd: list[str], cwd: Optional[str] = None, timeout: Optional[int] = None):
        if self.terminated:
            return SimpleNamespace(returncode=1)

        cmd = self._inject(list(cmd))
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=self._env(),
                                    cwd=cwd, bufsize=1)
        except Exception as exc:
            self.outputReceived.emit(f"Command launch error: {exc}", "error")
            return SimpleNamespace(returncode=1)

        out_q: queue.Queue = queue.Queue()

        def _reader(stream, is_stderr: bool) -> None:
            try:
                for line in iter(stream.readline, ""):
                    if self.terminated:
                        break
                    line = line.rstrip("\n\r")
                    if line.strip():
                        out_q.put((is_stderr, line))
            except (OSError, ValueError) as _exc:
                out_q.put((True, f"Read error: {_exc}"))
            finally:
                out_q.put(None)

        t1 = threading.Thread(target=_reader, args=(proc.stdout, False), daemon=True)
        t2 = threading.Thread(target=_reader, args=(proc.stderr, True), daemon=True)
        t1.start()
        t2.start()

        deadline = time.monotonic() + timeout if timeout else float("inf")
        sentinels = 0

        while sentinels < 2:
            if self.terminated or time.monotonic() > deadline:
                proc.terminate()
                break
            try:
                item = out_q.get(timeout=0.5)
            except queue.Empty:
                continue
            if item is None:
                sentinels += 1
                continue
            is_err, text = item
            self.outputReceived.emit(text, "error" if is_err and not _INFO_RE.search(text) else "subprocess")

        rc = proc.poll()
        if rc is None:
            try:
                rc = proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    rc = proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    rc = 1

        t1.join(5)
        t2.join(5)
        return SimpleNamespace(returncode=rc if rc is not None else 1)

    def _run_cmd(self, cmd: list[str] | str, shell: bool = False, input_text: Optional[str] = None, timeout: int = 15):
        if isinstance(cmd, list):
            cmd = self._inject(list(cmd))
        try:
            r = subprocess.run(cmd, shell=shell, input=input_text, capture_output=True, text=True, env=self._env(),
                               timeout=timeout)
            return SimpleNamespace(returncode=r.returncode, stdout=r.stdout, stderr=r.stderr)
        except subprocess.TimeoutExpired:
            logger.warning("_run_cmd timeout: %s", cmd)
            return SimpleNamespace(returncode=124, stdout="", stderr="Timeout")
        except (OSError, ValueError) as exc:
            logger.error("_run_cmd: %s", exc)
            return SimpleNamespace(returncode=1, stdout="", stderr=str(exc))

    @staticmethod
    def _ok(r) -> bool:
        return bool(r and r.returncode == 0)

    def _verify_sudo(self) -> bool:
        self.outputReceived.emit("Verifying sudo access…", "operation")
        subprocess.run(["sudo", "-k"], capture_output=True, timeout=5)
        _TOKEN = "SUDO_VERIFY_4a8f2b"
        ok = False
        proc = None

        try:
            proc = subprocess.Popen(
                ["sudo", "-A", "sh", "-c",
                 f"printf '%s\\n' {shlex.quote(_TOKEN)} && sleep 4"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=self._env(),
            )

            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                readable, _, _ = select.select(
                    [proc.stdout, proc.stderr], [], [], 0.05
                )
                for stream in readable:
                    line = stream.readline()
                    if not line:
                        continue
                    if stream is proc.stdout and _TOKEN in line:
                        ok = True
                        return ok
                    if stream is proc.stderr and line.strip():
                        return ok

                if proc.poll() is not None:
                    chunks = []
                    while True:
                        rr, _, _ = select.select([proc.stdout], [], [], 0)
                        if not rr:
                            break
                        chunk = proc.stdout.readline()
                        if not chunk:
                            break
                        chunks.append(chunk)
                    ok = _TOKEN in "".join(chunks)
                    return ok

        except Exception as exc:
            logger.error("_verify_sudo: %s", exc)

        finally:
            if proc is not None:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()

            self.outputReceived.emit(
                "Sudo access successfully verified" if ok
                else "Authentication failed: Invalid Password",
                "success" if ok else "error",
            )

        return ok

    def _copy_sysfiles(self) -> bool:
        files = [f for f in (S.system_files or []) if isinstance(f, dict) and not f.get("disabled")
                 and f.get("source", "").strip() and f.get("destination", "").strip()]

        if not files:
            self.outputReceived.emit("No system files configured", "warning")
            return True

        from drive_utils import check_drives_to_mount, mount_drive
        paths = [p for f in files for p in (f["source"], f["destination"])]
        for drive in check_drives_to_mount(paths):
            name = drive.get("drive_name", "?")
            self.outputReceived.emit(f"Mounting drive: {name}…", "info")
            ok, err = mount_drive(drive)
            if not ok:
                self.outputReceived.emit(f"Failed to mount '{name}': {err}", "error")
                return False
            self.outputReceived.emit(f"Mounted '{name}'", "success")

        overall = True
        for f in files:
            src, dst = f["source"].strip(), f["destination"].strip()
            if not Path(src).exists():
                self.outputReceived.emit(f"Source not found: {src}", "error")
                overall = False
                continue
            dst_dir = Path(dst).parent
            if not dst_dir.exists():
                if not self._ok(self._stream_cmd(["sudo", "mkdir", "-p", "--mode=755", str(dst_dir)])):
                    self.outputReceived.emit(f"Cannot create directory: {dst_dir}", "error")
                    overall = False
                    continue
            cmd = ["sudo", "cp"] + (["-r"] if Path(src).is_dir() else []) + [src, dst]
            if self._ok(self._stream_cmd(cmd)):
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
                        if len(code) == 2 and code.isalpha():
                            return code
            except (urllib.error.URLError, TimeoutError, OSError):
                continue
        return ""

    def _update_mirrors(self) -> bool:
        if not self.distro:
            return False
        if distro_family(self.distro.distro_id) != "arch":
            self.outputReceived.emit("Mirror update is only supported on Arch Linux", "info")
            return True
        country = self._detect_country()
        self.outputReceived.emit(
            f"Detected country: {country}" if country else "Country detection failed — using worldwide mirrors",
            "info" if country else "warning")

        if not shutil.which("reflector"):
            self.outputReceived.emit("Installing reflector", "info")
            self._stream_cmd(shlex.split(self.distro.get_pkg_install_cmd("reflector")))
        else:
            self.outputReceived.emit("Package reflector already installed", "info")

        cmd = ["sudo", "-A", "reflector", "--verbose", "--latest", "10", "--protocol", "https", "--sort", "rate",
               "--save", "/etc/pacman.d/mirrorlist"]
        if country:
            cmd += ["--country", country]
        self.outputReceived.emit(f"Running: {' '.join(cmd)}", "info")
        ok = self._ok(self._stream_cmd(cmd, timeout=180))
        self.outputReceived.emit("Mirrors successfully updated" if ok else "Mirror update failed",
                                 "success" if ok else "error")
        return ok

    def _set_shell(self) -> bool:
        if not self.distro:
            return False
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
            if not self._install_pkg(pkg, "Shell Package"):
                return False
            shell = shutil.which(binary) or shell

        if not Path(shell).exists():
            self.outputReceived.emit(f"Shell binary '{shell}' not found.", "error")
            return False

        try:
            shells_path = Path("/etc/shells")
            if shells_path.exists():
                known = [s.strip() for s in shells_path.read_text(encoding="utf-8").splitlines()]
                if shell not in known:
                    self.outputReceived.emit(f"Adding '{shell}' to /etc/shells…", "info")
                    if not self._ok(
                            self._run_cmd(["sudo", "tee", "-a", "/etc/shells"], input_text=shell + "\n", timeout=10)):
                        self.outputReceived.emit("Could not update /etc/shells", "error")
                        return False
        except OSError as exc:
            self.outputReceived.emit(f"Could not verify /etc/shells: {exc}", "warning")

        ok = self._ok(self._stream_cmd(["sudo", "chsh", "-s", shell, _USER]))
        self.outputReceived.emit(f"Shell for '{_USER}' {'set to' if ok else 'failed to change to'} '{shell}'",
                                 "success" if ok else "error")
        return ok

    def _update_system(self) -> bool:
        if not self.distro:
            return False
        if self.distro.has_aur and self._pkg_cache and self._pkg_cache.is_installed("yay"):
            ok = self._ok(self._stream_cmd(["yay", "-Syu", "--noconfirm"]))
        else:
            cmd = self.distro.get_update_system_cmd()
            ok = self._ok(
                self._stream_cmd(["sudo", "sh", "-c", cmd]) if ("&" in cmd or "|" in cmd) else self._stream_cmd(
                    shlex.split(cmd)))
        self.outputReceived.emit("System successfully updated" if ok else "System update failed",
                                 "success" if ok else "error")
        return ok

    def _install_kernel_header(self) -> bool:
        return self._install_pkg(self.distro.get_kernel_headers_pkg(), "Kernel Header") if self.distro else False

    def _install_pkg(self, name: str, label: str = "Package") -> bool:
        if not self.distro or not self._pkg_cache:
            return False
        self.outputReceived.emit(f"Installing {label}: {name}", "info")
        if self._pkg_cache.is_installed(name):
            self.outputReceived.emit(f"{name} already installed", "success")
            return True
        ok = self._ok(self._stream_cmd(shlex.split(self.distro.get_pkg_install_cmd(name))))
        if ok:
            self._pkg_cache.mark_installed(name)
            self.outputReceived.emit(f"{name} successfully installed", "success")
        else:
            self.outputReceived.emit(f"failed to install {name}", "error")
        return ok

    def _batch_install(self, pkg_list, label: str, *, use_aur: bool = False) -> bool:
        if not self.distro:
            self.outputReceived.emit(f"Cannot install {label}s: no distro helper", "error")
            return False

        items: list[str] = []
        for p in (pkg_list or []):
            if isinstance(p, dict):
                if p.get("disabled", False):
                    continue
                name = (p.get("name") or p.get("package") or "").strip()
            else:
                name = str(p).strip()
            if name and all(c.isalnum() or c in "-_.+" for c in name):
                items.append(name)

        if not items:
            self.outputReceived.emit(f"No active {label}s configured", "info")
            return True

        to_install = self.distro.filter_not_installed(items) if self.distro else items
        if not to_install:
            self.outputReceived.emit(f"All {label}s already installed", "success")
            return True

        failed: list[str] = []
        for i in range(0, len(to_install), 20):
            if self.terminated:
                break
            batch = to_install[i:i + 20]
            self.outputReceived.emit(f"Installing: {', '.join(batch)}", "info")
            cmd = (["yay", "-S", "--noconfirm", "--needed"] + batch if use_aur else shlex.split(
                self.distro.get_batch_install_cmd(batch)))
            if self._ok(self._stream_cmd(cmd)):
                for pkg in batch:
                    self._pkg_cache.mark_installed(pkg)
            else:
                for pkg in batch:
                    if self.distro.package_is_installed(pkg):
                        self._pkg_cache.mark_installed(pkg)
                    else:
                        failed.append(pkg)

        self.outputReceived.emit(
            f"All {label}s successfully installed" if not failed else f"Failed {label}(s): {', '.join(failed)}",
            "success" if not failed else "warning")
        return not failed

    def _install_yay(self) -> bool:
        if not self.distro or not self.distro.has_aur:
            self.outputReceived.emit("yay is not supported on this distribution", "warning")
            return True
        if self._pkg_cache and self._pkg_cache.is_installed("yay"):
            self.outputReceived.emit("yay already installed", "success")
            return True

        build_deps = ("base-devel", "git", "go")
        deps = [p for p in build_deps if not self.distro.package_is_installed(p)]
        freshly_got = set(deps)
        if deps and not self._ok(self._stream_cmd(shlex.split(self.distro.get_pkg_install_cmd(" ".join(deps))))):
            return False

        yay_dir = _HOME / "yay"
        shutil.rmtree(yay_dir, ignore_errors=True)

        self.outputReceived.emit("Cloning yay…", "subprocess")
        if not self._ok(self._stream_cmd(["git", "clone", "https://aur.archlinux.org/yay.git"], cwd=str(_HOME))):
            return False

        self.outputReceived.emit("Building yay…", "subprocess")
        if not self._ok(self._stream_cmd(["makepkg", "-c", "--noconfirm"], cwd=str(yay_dir))):
            return False

        to_rm = (["go"] if "go" in freshly_got and self.distro.package_is_installed("go") else []) + \
                (["yay-debug"] if self.distro.package_is_installed("yay-debug") else [])
        if to_rm:
            self._stream_cmd(["sudo", "pacman", "-R", "--noconfirm"] + to_rm)

        pkgs = sorted(
            (f for f in yay_dir.iterdir() if f.name.endswith(".pkg.tar.zst")),
            key=lambda f: (0 if "-debug-" not in f.name else 1, -f.stat().st_mtime),
        )
        if not pkgs:
            self.outputReceived.emit("No yay package file found after build", "error")
            return False

        ok = self._ok(self._stream_cmd(["sudo", "pacman", "-U", "--noconfirm", str(pkgs[0])]))
        shutil.rmtree(yay_dir, ignore_errors=True)
        shutil.rmtree(Path(_HOME) / ".config" / "go", ignore_errors=True)
        if ok and self._pkg_cache:
            self._pkg_cache.mark_installed("yay")
        self.outputReceived.emit("yay successfully installed" if ok else "yay installation failed",
                                 "success" if ok else "error")
        return ok

    def _install_specific(self) -> bool:
        if not self.distro:
            return False
        session = self.distro.detect_session()
        if not session:
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
        self._stream_cmd(shlex.split(self.distro.get_pkg_install_cmd(" ".join(to_install))))
        failed = [p for p in to_install if not self.distro.package_is_installed(p)]
        for pkg in to_install:
            if pkg not in failed and self._pkg_cache:
                self._pkg_cache.mark_installed(pkg)
        self.outputReceived.emit(
            "All Specific Packages successfully installed" if not failed else f"Failed to install: {', '.join(failed)}",
            "success" if not failed else "warning")
        return not failed

    def _install_flatpak(self) -> bool:
        if not self.distro:
            return False
        if not all(self._install_pkg(p, "Flatpak") for p in self.distro.get_flatpak_packages()):
            return False
        self.outputReceived.emit("Adding Flathub remote…", "info")
        try:
            cmd = shlex.split(self.distro.flatpak_add_flathub())
            self.outputReceived.emit(f"Running: {' '.join(cmd)}", "info")
            ok = self._ok(self._stream_cmd(cmd))
            self.outputReceived.emit("Flathub remote successfully added" if ok else "Failed to add Flathub remote",
                                     "success" if ok else "warning")
            return ok
        except Exception as exc:
            self.outputReceived.emit(f"Flathub setup error: {exc}", "error")
            return False

    def _setup_service(self, service: str, packages: list) -> bool:
        return (all(self._install_pkg(p, label="Service Package") for p in
                    packages) if packages else True) and self._enable_service(service)

    def _enable_service(self, service: str) -> bool:
        self.outputReceived.emit(f"Enabling {service}.service", "info")
        if self._ok(self._run_cmd(["systemctl", "is-active", "--quiet", f"{service}.service"])):
            self.outputReceived.emit(f"{service}.service already active", "success")
            return True
        ok = self._ok(self._stream_cmd(["sudo", "systemctl", "enable", "--now", f"{service}.service"]))
        if ok:
            self.outputReceived.emit(f"{service}.service successfully enabled", "success")
            if service == "ufw":
                for c in (["sudo", "ufw", "default", "deny"], ["sudo", "ufw", "enable"], ["sudo", "ufw", "reload"]):
                    if not self._ok(self._stream_cmd(c)):
                        return False
        else:
            self.outputReceived.emit(f"Failed to enable {service}.service", "error")
        return ok

    def _remove_orphans(self) -> bool:
        if not self.distro:
            return False
        cmd = self.distro.get_find_orphans_cmd()
        if not cmd:
            self.outputReceived.emit("Orphan removal not supported on this distribution", "info")
            return True
        needs_shell = "|" in cmd or "&&" in cmd
        try:
            r = self._run_cmd(cmd if needs_shell else shlex.split(cmd), shell=needs_shell, timeout=60)
            raw = r.stdout.strip() if r.stdout else ""
        except ValueError as exc:
            self.outputReceived.emit(f"Orphan search failed: {exc}", "error")
            return False
        pkgs = self.distro.parse_orphan_output(raw) if raw else []
        if not pkgs:
            self.outputReceived.emit("No orphaned packages found", "success")
            return True
        self.outputReceived.emit(f"Found orphaned packages: {', '.join(pkgs)}", "info")
        ok = self._ok(self._stream_cmd(shlex.split(self.distro.get_pkg_remove_cmd(" ".join(pkgs)))))
        self.outputReceived.emit(
            "Orphaned packages successfully removed" if ok else "Could not remove orphaned packages",
            "success" if ok else "error")
        return ok

    def _clean_cache(self) -> bool:
        if not self.distro:
            return False
        pm_name = self.distro.pkg_manager_name()
        self.outputReceived.emit(f"Cleaning {pm_name} cache", "info")
        cmd = self.distro.get_clean_cache_cmd()
        ok = self._ok(self._stream_cmd(["sudo", "sh", "-c", cmd]) if ("&" in cmd or "|" in cmd) else self._stream_cmd(
            shlex.split(cmd)))
        self.outputReceived.emit(
            f"{pm_name} cache successfully cleaned" if ok else f"{pm_name} cache cleaning failed",
            "success" if ok else "error")
        if ok and self._pkg_cache and self._pkg_cache.is_installed("yay"):
            self.outputReceived.emit("", "info")
            self.outputReceived.emit("Cleaning yay cache", "info")
            yay_ok = self._ok(self._stream_cmd(["yay", "-Scc", "--noconfirm"]))
            self.outputReceived.emit("yay cache successfully cleaned" if yay_ok else "yay cache cleaning failed",
                                     "success" if yay_ok else "error")
            ok = ok and yay_ok
        return ok


class _Status:
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    SUCCESS = "success"
    WARNING = "warning"
    ERROR = "error"


class _Style:
    _FONT_MAIN = "DejaVu Sans Mono, Fira Code, monospace"
    _FONT_SUB = "Hack, Fira Mono, monospace"

    KIND_CFG: dict[str, tuple[str, int, float, str]] = {"operation": (_FONT_MAIN, 16, 1.2, "info"),
                                                        "info": (_FONT_MAIN, 15, 1.0, "success"),
                                                        "subprocess": (_FONT_SUB, 13, 0.6, "text"),
                                                        "success": (_FONT_MAIN, 15, 1.0, "success"),
                                                        "warning": (_FONT_MAIN, 15, 1.0, "warning"),
                                                        "error": (_FONT_MAIN, 15, 1.0, "error")}

    STATUS_CFG: dict[str, tuple[str, str]] = {_Status.SUCCESS: ("success", "dialog-ok-apply"),
                                              _Status.ERROR: ("error", "dialog-error"),
                                              _Status.WARNING: ("warning", "dialog-warning"),
                                              _Status.IN_PROGRESS: ("info", "media-playback-start")}

    @classmethod
    def style_str(cls, kind: str) -> str:
        cfg = cls.KIND_CFG.get(kind)
        if not cfg:
            return ""
        font, size, lh, color_key = cfg
        color = current_theme().get(color_key, current_theme()["text"])
        return f"font-family:{font};font-size:{size}px;color:{color};padding:5px;line-height:{lh};word-break:break-word;"

    @classmethod
    def border_style(cls) -> str:
        p = current_theme()["accent"]
        return (f"border-radius:8px;border-right:1px solid {p};border-top:1px solid {p};"
                f"border-bottom:1px solid {p};border-left:4px solid {p};")


def _fmt_html(text: str, kind: str) -> str:
    style = _Style.style_str(kind)
    if kind == "operation":
        esc = _html.escape(apply_replacements(text))
        return ("<hr style='border:none;margin:15px 30px;border-top:1px dashed rgba(111,255,245,0.3);'>"
                f"<div style='padding:10px;border-radius:8px;margin:5px 0;'>"
                f"<p style='{style}'>{esc}</p></div><br>")
    lines = [f"<p style='{style}'>{_html.escape(apply_replacements(ln))}</p>"
             for ln in text.splitlines() if ln.strip()]
    return "\n".join(lines) + "<br>"


class _PackageCache:
    _TTL = 600
    _MAX_SIZE = 1000

    def __init__(self, distro) -> None:
        self._distro = distro
        self._cache: dict[str, bool] = {}
        self._ts = 0.0
        self._lock = threading.Lock()

    def is_installed(self, pkg: str) -> bool:
        now = time.monotonic()
        with self._lock:
            if now - self._ts > self._TTL:
                self._cache.clear()
                self._ts = now
            elif len(self._cache) > self._MAX_SIZE:
                items = list(self._cache.items())
                self._cache = dict(items[self._MAX_SIZE // 2:])
            if pkg in self._cache:
                return self._cache[pkg]

        try:
            result = self._distro.package_is_installed(pkg)
        except Exception as exc:
            logger.warning("PackageCache(%s): %s", pkg, exc)
            return False

        with self._lock:
            self._cache[pkg] = result
        return result

    def mark_installed(self, pkg: str) -> None:
        with self._lock:
            self._cache[pkg] = True


_INFO_RE = re.compile(r"INFO:|rating|mirror|download|synchroniz|\$srcdir/|Erfolg|Übertragung|avg speed|=====|OK|"
                      r"Statuserläuterung|Klon|PGP|MiB|Fertig|\.\.\.", re.IGNORECASE)
from pathlib import Path
from typing import Optional
import ast, os, queue, shlex, shutil, signal, subprocess, socket, tempfile, threading, time, types, urllib.error, urllib.request, pwd

from PyQt6.QtGui     import QColor, QIcon, QTextCursor
from PyQt6.QtCore    import Qt, QElapsedTimer, QThread, QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QDialog, QGraphicsDropShadowEffect, QHBoxLayout, QLabel,
    QListWidget, QListWidgetItem, QPushButton, QTextEdit, QVBoxLayout,
)

from themes import current_theme
from linux_distro_helper import distro_family
from state  import S, _HOME, _USER, logger, apply_replacements


def _ram_tmpdir() -> Optional[str]:
    shm = "/dev/shm"
    if os.path.isdir(shm) and os.access(shm, os.W_OK):
        return shm
    return None


_cleanup_lock    = threading.Lock()
_cleanup_paths:  list[str] = []


def _register_tmpdir(path: str) -> None:
    with _cleanup_lock:
        _cleanup_paths.append(path)


def _unregister_tmpdir(path: str) -> None:
    with _cleanup_lock:
        try:
            _cleanup_paths.remove(path)
        except ValueError:
            pass


def _emergency_cleanup() -> None:
    with _cleanup_lock:
        paths = list(_cleanup_paths)
    for p in paths:
        _wipe_tmpdir(p)


def _wipe_tmpdir(tmp_path_str: str) -> None:
    if not tmp_path_str:
        return
    tmp_path = Path(tmp_path_str)
    pw_file  = tmp_path / "sudo_pass"
    if pw_file.exists():
        try:
            size = pw_file.stat().st_size
            if size > 0:
                with open(pw_file, "r+b") as f:
                    f.write(os.urandom(size))
                    f.flush()
                    os.fsync(f.fileno())
            pw_file.unlink()
        except Exception as exc:
            logger.warning("_wipe_tmpdir: sudo_pass cleanup error: %s", exc)
    try:
        shutil.rmtree(tmp_path, ignore_errors=True)
    except Exception as exc:
        logger.warning("_wipe_tmpdir: rmtree error: %s", exc)


_original_sigterm = signal.getsignal(signal.SIGTERM)

def _sigterm_handler(signum, frame):  # noqa: ANN001
    logger.warning("SIGTERM received — running emergency cleanup")
    _emergency_cleanup()
    if callable(_original_sigterm):
        _original_sigterm(signum, frame)
    else:
        raise SystemExit(0)

signal.signal(signal.SIGTERM, _sigterm_handler)


class SystemManagerDialog(QDialog):
    DIALOG_SIZE     = (1750, 1100)
    BUTTON_SIZE     = (145, 40)
    CHECKLIST_WIDTH = 370

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("System Manager")
        self._task_status: dict[str, str] = {}
        self._done = self._auth_failed = self._has_error = False
        self._timer  = QElapsedTimer()
        self._ticker = QTimer(self)
        self._build_ui()

    def _build_ui(self) -> None:
        t = current_theme()
        self.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
        self.setFixedSize(*self.DIALOG_SIZE)

        shadow = QGraphicsDropShadowEffect()
        shadow.setBlurRadius(80)
        shadow.setXOffset(15)
        shadow.setYOffset(15)
        shadow.setColor(QColor(0, 0, 0, 160))

        self.setStyleSheet(f"QTextEdit {{background:qlineargradient(x1:0,y1:0,x2:1,y2:0,"
                           f"stop:0 {t['bg']},stop:1 {t['bg2']});"
                           f"color:{t['text']};border:none;border-radius:8px;}}")

        self._text_edit = QTextEdit()
        self._text_edit.setReadOnly(True)
        self._text_edit.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        self._text_edit.setHtml(f"<p style='color:{t['success']};font-size:20px;text-align:center;margin-top:25px;'>"
                                f"<b>System Manager</b><br>Initialising…</p>")

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

        self._checklist_lbl = QLabel("  Pending Operations:")
        self._checklist     = QListWidget()
        self._style_checklist()

        self._elapsed_lbl = QLabel("\nElapsed time:\n00s\n")
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
        main.addLayout(left, 3)
        main.addLayout(right, 1)

        self._ticker.timeout.connect(self._update_elapsed)
        self._timer.start()
        self._ticker.start(1000)

    def _style_checklist(self) -> None:
        t  = current_theme()
        bs = _Style.border_style()
        self._checklist_lbl.setStyleSheet(f"color:{t['info']};font-size:18px;font-weight:bold;padding:10px;"
                                          f"background:qlineargradient(x1:0,y1:0,x2:1,y2:0,"
                                          f"stop:0 {t['bg2']},stop:1 {t['header_sep']});{bs}")
        self._checklist_lbl.setFixedWidth(self.CHECKLIST_WIDTH)
        self._checklist_lbl.setFixedSize(self._checklist_lbl.sizeHint())
        self._checklist.setStyleSheet(f"QListWidget{{background:qlineargradient(x1:0,y1:0,x2:1,y2:0,"
                                      f"stop:0 {t['bg2']},stop:1 {t['header_sep']});font-size:15px;padding:4px;{bs}}}"
                                      f"QListWidget::item{{padding:4px;border-radius:4px;border:1px solid transparent;}}")
        self._checklist.setFixedWidth(self.CHECKLIST_WIDTH)

    def _style_elapsed(self, shadow: QGraphicsDropShadowEffect) -> None:
        t  = current_theme()
        bs = _Style.border_style()
        self._elapsed_lbl.setGraphicsEffect(shadow)
        self._elapsed_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._elapsed_lbl.setStyleSheet(f"color:{t['info']};font-size:17px;{bs}"
                                        f"background:qlineargradient(x1:0,y1:0,x2:1,y2:0,"
                                        f"stop:0 {t['bg2']},stop:1 {t['header_sep']});"
                                        f"text-align:center;font-weight:bold;padding:3px;")

    def on_output(self, text: str, kind: str) -> None:
        if "/var/lib/pacman/db.lck" in text:
            self._show_db_lock_error()
            return
        if kind == "finish":
            self._show_completion()
            return
        if kind == "task_list":
            try:
                self._init_checklist(ast.literal_eval(text))
            except (ValueError, SyntaxError):
                pass
            return
        if kind not in _Style.KIND_CFG:
            return
        self._append_html(text if ("<span " in text or "<p " in text) else _fmt_html(text, kind))

    def on_task_status(self, task_id: str, status: str) -> None:
        if status in (_Status.ERROR, _Status.WARNING):
            self._has_error = True
        if self._task_status.get(task_id) == status:
            return
        self._task_status[task_id] = status
        cfg = _Style.STATUS_CFG
        if status not in cfg:
            return
        t                    = current_theme()
        color_key, icon_name = cfg[status]
        colour               = t[color_key]
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

    def mark_done(self, failed_count: int = 1) -> None:
        if self._done:
            return
        self._done = True
        self._ticker.stop()
        self.update_failed_attempts(failed_count)

    def update_failed_attempts(self, count: int) -> None:
        if count == 0:
            self._fail_lbl.setVisible(False)
            self._close_btn.setEnabled(False)
        else:
            self._fail_lbl.setText(f" ⚠️ Failed authentication attempts: {count}")
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
            item  = QListWidgetItem(clean)
            item.setData(Qt.ItemDataRole.UserRole, task_id)
            item.setIcon(QIcon.fromTheme("dialog-question"))
            item.setForeground(QColor(t["muted"]))
            self._checklist.addItem(item)
            self._task_status[task_id] = _Status.PENDING
        n     = self._checklist.count()
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
            logger.error("UI Update Error: %s", exc)

    def _show_db_lock_error(self) -> None:
        t = current_theme()
        ec = QColor(t["error"])
        r, g, b = ec.red(), ec.green(), ec.blue()
        self._append_html(f"<hr style='border:none;margin:10px 20px;"
                          f"border-top:1px dashed rgba({r},{g},{b},0.4);'>"
                          f"<div style='padding:15px;margin:10px;border-radius:10px;"
                          f"border-left:4px solid {t['error']};'>"
                          f"<p style='color:{t['error']};font-size:18px;text-align:center;'>"
                          f"<b>⚠️ Installation Aborted</b><br>"
                          f"<span style='font-size:16px;'>/var/lib/pacman/db.lck detected!</span><br>"
                          f"<span style='color:{t['text']};font-size:14px;'>"
                          f"Remove with: <code>sudo rm /var/lib/pacman/db.lck</code></span></p></div>")
        self._done = True
        self._ticker.stop()
        self._close_btn.setEnabled(True)
        self._close_btn.setFocus()

    def _show_completion(self) -> None:
        if self._done or self._auth_failed:
            return
        self._done = True
        self._ticker.stop()

        t       = current_theme()
        colour  = t["warning" if self._has_error else "success"]
        summary = "Completed with issues" if self._has_error else "Successfully Completed"
        icon    = "⚠️" if self._has_error else "✅"
        message = ("completed with warnings/errors" if self._has_error else "successfully completed all operations<br>")
        co      = QColor(colour)
        r, g, b = co.red(), co.green(), co.blue()

        self._append_html(f"<hr style='border:none;margin:25px 50px;border-top:2px solid {colour};'>"
                          f"<div style='text-align:center;padding:20px;margin:15px 30px;"
                          f"border-radius:15px;border:1px solid rgba({r},{g},{b},0.3);'>"
                          f"<p style='color:{colour};font-size:20px;font-weight:bold;'>{icon} {summary}</p>"
                          f"<p style='color:{colour};font-size:18px;'>System Manager {message}</p></div>")
        self._close_btn.setEnabled(True)
        self._close_btn.setFocus()

        bs = _Style.border_style()
        self._checklist_lbl.setText(f"{icon} {summary}")
        self._checklist_lbl.setStyleSheet(f"color:{colour};font-size:18px;font-weight:bold;padding:10px;"
                                          f"background-color:rgba({r},{g},{b},0.15);{bs}")

    def _update_elapsed(self) -> None:
        try:
            elapsed = max(0, int(self._timer.elapsed() / 1000))
            h, rem  = divmod(elapsed, 3600)
            m, s    = divmod(rem, 60)
            if h:
                txt = f"\nElapsed time:\n{h:02}h {m:02}m {s:02}s\n"
            elif m:
                txt = f"\nElapsed time:\n{m:02}m {s:02}s\n"
            else:
                txt = f"\nElapsed time:\n{s:02}s\n"
            self._elapsed_lbl.setText(txt)
        except (RuntimeError, AttributeError):
            self._ticker.stop()
        except Exception as exc:
            logger.error("_update_elapsed: %s", exc)

    def keyPressEvent(self, event) -> None:
        key = event.key()
        if key == Qt.Key.Key_Escape and (self._done or self._auth_failed):
            self.close()
        elif key == Qt.Key.Key_Tab:
            self.focusNextChild()
        else:
            super().keyPressEvent(event)

    def closeEvent(self, event) -> None:
        if not self._done and not self._auth_failed:
            event.ignore()
        else:
            super().closeEvent(event)


class SystemManagerThread(QThread):
    thread_started    = pyqtSignal()
    outputReceived    = pyqtSignal(str, str)
    taskStatusChanged = pyqtSignal(str, str)
    passwordFailed    = pyqtSignal()
    passwordSuccess   = pyqtSignal()

    def __init__(self, sudo_password):
        super().__init__()
        from sudo_password import SecureString
        self._pw = sudo_password if isinstance(sudo_password, SecureString) \
                   else SecureString(sudo_password or "")
        self.terminated      = False
        self.temp_dir: Optional[str]  = None
        self.askpass:  Optional[Path] = None
        self._enabled_tasks: dict[str, tuple] = {}
        try:
            from linux_distro_helper import LinuxDistroHelper
            self.distro     = LinuxDistroHelper()
            self._pkg_cache = _PackageCache(self.distro)
        except Exception as exc:
            logger.warning("distro helper init: %s", exc)
            self.distro     = None
            self._pkg_cache = None

    def run(self) -> None:
        self.thread_started.emit()
        self._prepare_tasks()
        try:
            if self.terminated:
                return
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
            self._pw.clear()
            self.outputReceived.emit("", "finish")

    def _prepare_tasks(self) -> None:
        ops   = S.system_manager_ops
        tasks = {
            **self._base_tasks(),
            **self._service_tasks(),
            "remove_orphaned_packages": ("Removing orphaned packages…", self._remove_orphans),
            "clean_cache":              ("Cleaning cache…",             self._clean_cache),
        }
        self._enabled_tasks = {k: v for k, v in tasks.items() if k in ops}
        task_descs = [(k, desc) for k, (desc, _) in self._enabled_tasks.items()]
        self.outputReceived.emit(str(task_descs), "task_list")

    def _base_tasks(self) -> dict:
        return {
            "copy_system_files":         ("Copying System Files…",         self._copy_sysfiles),
            "update_mirrors":            ("Updating mirrors…",              self._update_mirrors),
            "set_user_shell":            ("Setting user shell…",            self._set_shell),
            "update_system":             ("Updating system…",               self._update_system),
            "install_kernel_header":     ("Installing kernel headers…",     self._install_kernel_header),
            "install_basic_packages":    ("Installing Basic Packages…",
                                          lambda: self._batch_install(S.basic_packages, "Basic Package")),
            "install_yay":               ("Installing yay…",                self._install_yay),
            "install_aur_packages":      ("Installing AUR Packages with yay…",
                                          lambda: self._batch_install(S.aur_packages, "AUR Package")),
            "install_specific_packages": ("Installing Specific Packages…",  self._install_specific),
            "install_flatpak":           ("Installing Flatpak…",            self._install_flatpak),
        }

    def _service_tasks(self) -> dict:
        if not self.distro:
            return {}

        ssh_svc   = self.distro.get_ssh_service_name()
        samba_svc = self.distro.get_samba_service_name()
        cron_svc  = self.distro.get_cron_service_name()

        def _make(svc: str, pkgs: list):
            return lambda: self._setup_service(svc, list(pkgs))

        return {
            "enable_printer_support":           ("Initialising printer support…", _make("cups",      self.distro.get_printer_packages())),
            "enable_ssh_service":               ("Initialising SSH server…",      _make(ssh_svc,         self.distro.get_ssh_packages())),
            "enable_samba_network_filesharing": ("Initialising Samba…",           _make(samba_svc,       self.distro.get_samba_packages())),
            "enable_bluetooth_service":         ("Initialising Bluetooth…",       _make("bluetooth", self.distro.get_bluetooth_packages())),
            "enable_atd_service":               ("Initialising atd…",             _make("atd",       self.distro.get_at_packages())),
            "enable_cronie_service":            (f"Initialising {cron_svc}…",     _make(cron_svc,    self.distro.get_cron_packages())),
            "install_snap":                     ("Installing Snap…",              _make("snapd",     self.distro.get_snap_packages())),
            "enable_firewall":                  ("Initialising firewall…",        _make("ufw",       self.distro.get_firewall_packages())),
        }

    def _run_all_tasks(self) -> None:
        for task_id, (desc, fn) in self._enabled_tasks.items():
            if self.terminated:
                break
            self.taskStatusChanged.emit(task_id, _Status.IN_PROGRESS)
            self.outputReceived.emit(desc, "operation")
            try:
                success = fn()
                status  = _Status.SUCCESS if success is not False else _Status.ERROR
            except Exception as exc:
                self.outputReceived.emit(f"Task '{task_id}' failed: {exc}", "error")
                status = _Status.ERROR
            self.taskStatusChanged.emit(task_id, status)

    def _verify_sudo(self) -> bool:
        self.outputReceived.emit("Verifying sudo access…", "operation")
        if not self._make_askpass():
            return False

        env = {**os.environ, "SUDO_ASKPASS": str(self.askpass)}
        try:
            res = subprocess.run(["sudo", "-A", "-v"], capture_output=True, text=True, env=env, timeout=1)
            if res.returncode == 0:
                self.outputReceived.emit("Sudo access verified.", "success")
                return True
            self.outputReceived.emit("Authentication failed: Invalid Password.", "error")
            return False
        except subprocess.TimeoutExpired:
            self.outputReceived.emit("Error during sudo verification!", "error")
            logger.warning("Sudo authentication failed: Aborting to prevent account lockout.")
            return False
        except Exception as exc:
            logger.error("Sudo verification error: %s", exc)
            return False

    def _make_askpass(self) -> bool:
        try:
            tmp_base = _ram_tmpdir()
            self.temp_dir = tempfile.mkdtemp(prefix="sm_", dir=tmp_base)
            os.chmod(self.temp_dir, 0o700)
            _register_tmpdir(self.temp_dir)

            pw_path = Path(self.temp_dir, "sudo_pass")
            askpass = Path(self.temp_dir, "askpass.sh")

            pw_str   = self._pw.get()
            pw_bytes = bytearray(pw_str.encode("utf-8"))
            del pw_str

            try:
                fd = os.open(str(pw_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                try:
                    os.write(fd, bytes(pw_bytes))
                finally:
                    os.close(fd)
            finally:
                mv = memoryview(pw_bytes)
                for i in range(len(mv)):
                    mv[i] = 0
                del pw_bytes, mv

            askpass.write_text(f'#!/bin/sh\ncat "{pw_path}"\n', encoding="utf-8")
            os.chmod(askpass, 0o700)
            self.askpass = askpass
            return True
        except Exception as exc:
            self.outputReceived.emit(f"Askpass setup failed: {exc}", "error")
            return False

    def _cleanup(self) -> None:
        tmp = self.temp_dir
        if not tmp:
            return
        _unregister_tmpdir(tmp)
        _wipe_tmpdir(tmp)
        self.temp_dir = None
        self.askpass  = None

    def _sudo(self, cmd: list[str]):
        if self.terminated:
            return None
        try:
            env = {**os.environ, "SUDO_ASKPASS": str(self.askpass)}
            if cmd and cmd[0] == "sudo" and "-A" not in cmd:
                cmd.insert(1, "-A")
            elif cmd and cmd[0] == "yay" and not any(a.startswith("--sudoflags=") for a in cmd):
                cmd.insert(1, "--sudoflags=-A")
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env, bufsize=4096)
            return self._stream(proc)
        except Exception as exc:
            self.outputReceived.emit(f"Command error: {exc}", "error")
            return None

    def _stream(self, proc: subprocess.Popen):
        if not proc.stdout or not proc.stderr:
            return None

        q: queue.Queue = queue.Queue()

        def _read(stream, label: str) -> None:
            try:
                for line in iter(stream.readline, ""):
                    if self.terminated:
                        break
                    line = line.rstrip("\n\r")
                    if line.strip():
                        q.put(("out" if label == "stdout" else "err", line))
            except (OSError, ValueError) as exc:
                q.put(("err", f"Read error ({label}): {exc}"))
            finally:
                q.put(("done", label))

        t1 = threading.Thread(target=_read, args=(proc.stdout, "stdout"), daemon=True)
        t2 = threading.Thread(target=_read, args=(proc.stderr, "stderr"), daemon=True)
        t1.start()
        t2.start()

        done_count = 0
        while done_count < 2:
            try:
                kind, text = q.get(timeout=0.5)
                if kind == "out":
                    self.outputReceived.emit(text, "subprocess")
                elif kind == "err":
                    self.outputReceived.emit(text, "error")
                elif kind == "done":
                    done_count += 1
            except queue.Empty:
                if self.terminated:
                    proc.terminate()
                    break

        rc = proc.poll()
        if rc is None:
            try:
                rc = proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                rc = proc.wait()

        return types.SimpleNamespace(returncode=rc if rc is not None else 1)

    def _sudo_shell(self, cmd_str: str):
        if self.terminated:
            return None
        try:
            env  = {**os.environ, "SUDO_ASKPASS": str(self.askpass)}
            proc = subprocess.Popen(["sudo", "-A", "sh", "-c", cmd_str],
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env, bufsize=4096)
            return self._stream(proc)
        except Exception as exc:
            self.outputReceived.emit(f"Shell command error: {exc}", "error")
            return None

    @staticmethod
    def _ok(r) -> bool:
        return bool(r and r.returncode == 0)

    def _copy_sysfiles(self) -> bool:
        files = [f for f in (S.system_files or []) if isinstance(f, dict)
                 and not f.get("disabled") and f.get("source", "").strip() and f.get("destination", "").strip()]
        if not files:
            self.outputReceived.emit("No system files configured.", "warning")
            return True

        from drive_utils import check_drives_to_mount, mount_drive
        paths = [p for f in files for p in (f["source"], f["destination"])]
        for drive in check_drives_to_mount(paths):
            name = drive.get("drive_name", "?")
            self.outputReceived.emit(f"Mounting drive: {name}…", "info")
            success, err_msg = mount_drive(drive)
            if success:
                self.outputReceived.emit(f"Mounted '{name}'.", "success")
            else:
                self.outputReceived.emit(f"Failed to mount '{name}': {err_msg}", "error")
                return False

        ok = True
        for f in files:
            src, dst = f["source"].strip(), f["destination"].strip()
            if not Path(src).exists():
                self.outputReceived.emit(f"Source not found: {src}", "error")
                ok = False
                continue
            dst_dir = Path(dst).parent
            if not dst_dir.exists():
                if not self._ok(self._sudo(["sudo", "mkdir", "-p", "--mode=755", str(dst_dir)])):
                    self.outputReceived.emit(f"Cannot create directory: {dst_dir}", "error")
                    ok = False
                    continue
            cmd = ["sudo", "cp"] + (["-r"] if Path(src).is_dir() else []) + [src, dst]
            if self._ok(self._sudo(cmd)):
                self.outputReceived.emit(f"Successfully copied:\n'{src}' ⇨ '{dst}'", "success")
            else:
                self.outputReceived.emit(f"Error copying:\n{src}", "error")
                ok = False
        return ok

    def _update_mirrors(self) -> bool:
        if not self.distro:
            return False
        if distro_family(self.distro.distro_id) != "arch":
            self.outputReceived.emit("Mirror update is only supported on Arch Linux.", "info")
            return True

        if not shutil.which("reflector"):
            self.outputReceived.emit("Installing reflector…", "info")
            self._sudo(self.distro.get_pkg_install_cmd("reflector").split())

        country = self._detect_country()
        self.outputReceived.emit(f"Detected country: {country}" if country
                                 else "Country detection failed – using worldwide mirrors.", "info" if country else "warning")

        cmd = ["sudo", "-A", "reflector", "--verbose", "--latest", "10", "--protocol", "https",
               "--sort", "rate", "--save", "/etc/pacman.d/mirrorlist"]
        if country:
            cmd += ["--country", country]

        self.outputReceived.emit(f"Running: {' '.join(cmd)}", "info")

        env = {**os.environ, "SUDO_ASKPASS": str(self.askpass)}
        ok  = False
        try:
            with subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env, bufsize=1) as proc:
                q: queue.Queue = queue.Queue()

                def _read():
                    if proc.stdout:
                        for _line in iter(proc.stdout.readline, ""):
                            if self.terminated:
                                break
                            q.put(_line.strip())
                    q.put(None)

                thread = threading.Thread(target=_read, daemon=True)
                thread.start()

                deadline = time.monotonic() + 180
                while thread.is_alive():
                    if self.terminated or time.monotonic() > deadline:
                        proc.terminate()
                        break
                    try:
                        line = q.get(timeout=1)
                        if line is None:
                            break
                        self.outputReceived.emit(line, "subprocess")
                    except queue.Empty:
                        continue

                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
                ok = proc.returncode == 0
        except Exception as exc:
            self.outputReceived.emit(f"Mirror update error: {exc}", "error")

        self.outputReceived.emit("Mirrors updated." if ok else "Mirror update failed.", "success" if ok else "error")
        return ok

    @staticmethod
    def _detect_country() -> str:
        for url in ("https://ipinfo.io/country", "https://ifconfig.co/country-iso"):
            try:
                with urllib.request.urlopen(url, timeout=5) as resp:
                    if resp.status == 200:
                        code = resp.read().decode().strip().upper()
                        if len(code) == 2 and code.isalpha():
                            return code
            except (urllib.error.URLError, socket.timeout, OSError):
                continue
        return ""

    def _set_shell(self) -> bool:
        config_shell = (S.user_shell or "bash").strip()
        shell_pkg    = self.distro.get_shell_package_name(config_shell) if self.distro else config_shell
        shell_bin    = shutil.which(shell_pkg) or f"/bin/{shell_pkg}"

        try:
            current_shell = pwd.getpwnam(_USER).pw_shell
            self.outputReceived.emit(f"Target user: '{_USER}' (current shell: {current_shell})", "info")
        except Exception as exc:
            self.outputReceived.emit(f"Error determining target user info: {exc}", "error")
            return False

        if current_shell == shell_bin:
            self.outputReceived.emit(f"Shell is already '{config_shell}' ({shell_bin}).", "success")
            return True

        if self.distro and not self.distro.package_is_installed(shell_pkg):
            self.outputReceived.emit(f"Installing shell package: {shell_pkg}...", "info")
            if not self._install_pkg(shell_pkg, "Shell Package"):
                return False

        if not Path(shell_bin).exists():
            shell_bin = shutil.which(shell_pkg) or shell_bin
            if not Path(shell_bin).exists():
                self.outputReceived.emit(f"Shell binary '{shell_bin}' not found after install.", "error")
                return False

        try:
            shells_file = Path("/etc/shells")
            if shells_file.exists():
                known = [s.strip() for s in shells_file.read_text(encoding="utf-8").splitlines()]
                if shell_bin not in known:
                    self.outputReceived.emit(f"Adding '{shell_bin}' to /etc/shells…", "info")
                    env  = {**os.environ, "SUDO_ASKPASS": str(self.askpass)}
                    proc = subprocess.run(["sudo", "-A", "tee", "-a", "/etc/shells"],
                                          input=shell_bin + "\n", capture_output=True, text=True, env=env, timeout=10)
                    if proc.returncode != 0:
                        self.outputReceived.emit("Could not update /etc/shells.", "error")
                        return False
        except Exception as exc:
            self.outputReceived.emit(f"Failed to verify /etc/shells: {exc}", "warning")

        ok = self._ok(self._sudo(["sudo", "chsh", "-s", shell_bin, _USER]))
        self.outputReceived.emit(f"Shell for '{_USER}' {'set to' if ok else 'failed to change to'} '{shell_bin}'",
                                 "success" if ok else "error")
        return ok

    def _update_system(self) -> bool:
        if not self.distro:
            return False
        if self.distro.has_aur and self._pkg_cache and self._pkg_cache.is_installed("yay"):
            ok = self._ok(self._sudo(["yay", "-Syu", "--noconfirm"]))
        else:
            update_cmd = self.distro.get_update_system_cmd()
            ok = self._ok(self._sudo_shell(update_cmd) if ("&" in update_cmd or "|" in update_cmd)
                          else self._sudo(update_cmd.split()))
        self.outputReceived.emit("System updated." if ok else "System update failed.", "success" if ok else "error")
        return ok

    def _install_kernel_header(self) -> bool:
        return self._install_pkg(self.distro.get_kernel_headers_pkg(), "Kernel Header") \
               if self.distro else False

    def _install_pkg(self, name: str, label: str = "Package") -> bool:
        if not self.distro or not self._pkg_cache:
            return False
        self.outputReceived.emit(f"Installing {label}: '{name}'…", "info")
        if self._pkg_cache.is_installed(name):
            self.outputReceived.emit(f"'{name}' already installed.", "success")
            return True
        ok = self._ok(self._sudo(self.distro.get_pkg_install_cmd(name).split()))
        if ok:
            self._pkg_cache.mark_installed(name)
            self.outputReceived.emit(f"'{name}' installed.", "success")
        else:
            self.outputReceived.emit(f"'{name}' failed to install.", "error")
        return ok

    def _batch_install(self, pkg_list, label: str) -> bool:
        if not self.distro:
            self.outputReceived.emit(f"Cannot install {label}s: no distro helper available.", "error")
            return False

        items = []
        for p in (pkg_list or []):
            if isinstance(p, dict):
                if p.get("disabled", False):
                    continue
                name = p.get("name", "").strip()
            else:
                name = str(p).strip()
            if name and all(c.isalnum() or c in "-_.+" for c in name):
                items.append(name)

        if not items:
            self.outputReceived.emit(f"No active {label}s configured.", "info")
            return True

        to_install = self.distro.filter_not_installed(items) if self._pkg_cache else items
        if not to_install:
            self.outputReceived.emit(f"All {label}s already installed.", "success")
            return True

        failed: list[str] = []
        for i in range(0, len(to_install), 20):
            if self.terminated:
                break
            batch = to_install[i:i + 20]
            self.outputReceived.emit(f"Installing: {', '.join(batch)}", "info")

            cmd = (["yay", "-S", "--noconfirm", "--needed"] + batch if label == "AUR Package"
                   else self.distro.get_pkg_install_cmd(" ".join(batch)).split())
            if self._ok(self._sudo(cmd)):
                for pkg in batch:
                    self._pkg_cache.mark_installed(pkg)
            else:
                for pkg in batch:
                    if self.distro.package_is_installed(pkg):
                        self._pkg_cache.mark_installed(pkg)
                    else:
                        failed.append(pkg)

        if failed:
            self.outputReceived.emit(f"Failed {label}(s): {', '.join(failed)}", "warning")
            return False
        self.outputReceived.emit(f"Successfully installed all {label}s.", "success")
        return True

    def _install_yay(self) -> bool:
        if not self.distro or not self.distro.has_aur:
            self.outputReceived.emit("'yay' is not supported on this distribution.", "warning")
            return True
        if self._pkg_cache and self._pkg_cache.is_installed("yay"):
            self.outputReceived.emit("'yay' already installed.", "success")
            return True

        required = [p for p in ("base-devel", "git", "go")
                    if not self.distro.package_is_installed(p)]
        if required:
            if not self._ok(self._sudo(self.distro.get_pkg_install_cmd(" ".join(required)).split())):
                return False

        yay_dir = Path(_HOME) / "yay"
        shutil.rmtree(yay_dir, ignore_errors=True)

        def _stream_build(cmd: list[str], cwd) -> bool:
            try:
                _env = {**os.environ, "SUDO_ASKPASS": str(self.askpass)} if self.askpass else None
                with subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                      text=True, cwd=cwd, env=_env) as p:
                    for line in (p.stdout or []):
                        self.outputReceived.emit(line.rstrip(), "subprocess")
                    return p.wait() == 0
            except Exception as exc:
                self.outputReceived.emit(f"Build error: {exc}", "error")
                return False

        self.outputReceived.emit("Cloning yay…", "subprocess")
        if not _stream_build(["git", "clone", "https://aur.archlinux.org/yay.git"], _HOME):
            return False
        self.outputReceived.emit("Building yay…", "subprocess")
        if not _stream_build(["makepkg", "-c", "--noconfirm"], yay_dir):
            return False

        to_remove = []
        if "go" in required and self.distro.package_is_installed("go"):
            to_remove.append("go")
        if self.distro.package_is_installed("yay-debug"):
            to_remove.append("yay-debug")
        if to_remove:
            self._sudo(["sudo", "pacman", "-R", "--noconfirm"] + to_remove)

        pkg_files = [f for f in os.listdir(yay_dir) if f.endswith(".pkg.tar.zst") and "-debug-" not in f]
        if not pkg_files:
            pkg_files = [f for f in os.listdir(yay_dir) if f.endswith(".pkg.tar.zst")]
        if not pkg_files:
            self.outputReceived.emit("No yay package file found after build.", "error")
            return False

        pkg_files.sort(key=lambda f: os.path.getmtime(yay_dir / f), reverse=True)
        ok = self._ok(self._sudo(["sudo", "pacman", "-U", "--noconfirm", str(yay_dir / pkg_files[0])]))
        shutil.rmtree(yay_dir, ignore_errors=True)
        shutil.rmtree(Path(_HOME) / ".config" / "go", ignore_errors=True)
        if ok and self._pkg_cache:
            self._pkg_cache.mark_installed("yay")
        self.outputReceived.emit("'yay' installed." if ok else "'yay' installation failed.", "success" if ok else "error")
        return ok

    def _install_specific(self) -> bool:
        if not self.distro:
            return False
        session = self.distro.detect_session()
        if not session:
            self.outputReceived.emit("Cannot determine desktop session.", "warning")
            return False
        self.outputReceived.emit(f"Detected session: {session}", "success")

        pkgs = [p["package"] for p in (S.specific_packages or []) if isinstance(p, dict)
                and p.get("session") == session and "package" in p and not p.get("disabled", False)]
        to_install = self.distro.filter_not_installed(pkgs) if pkgs else []
        if not to_install:
            self.outputReceived.emit(f"All specific packages for {session} already installed.", "success")
            return True

        self._sudo(self.distro.get_pkg_install_cmd(" ".join(to_install)).split())
        failed = [p for p in to_install if not self.distro.package_is_installed(p)]
        for pkg in to_install:
            if pkg not in failed and self._pkg_cache:
                self._pkg_cache.mark_installed(pkg)
        if failed:
            self.outputReceived.emit(f"Failed to install: {', '.join(failed)}", "warning")
        return not failed

    def _install_flatpak(self) -> bool:
        if not self.distro:
            return False
        pkgs = self.distro.get_flatpak_packages()
        pkgs_ok = all(self._install_pkg(p, "Flatpak") for p in pkgs)
        if not pkgs_ok:
            return False
        self.outputReceived.emit("Adding Flathub remote...", "info")
        try:
            cmd_parts = self.distro.flatpak_add_flathub().split()
            if not cmd_parts:
                self.outputReceived.emit("Flathub command is empty.", "error")
                return False
            full_cmd = ["sudo"] + cmd_parts if cmd_parts[0] != "sudo" else cmd_parts
            res = self._sudo(full_cmd)
            if self._ok(res):
                self.outputReceived.emit("Flathub remote added successfully.", "success")
                return True
            else:
                self.outputReceived.emit("Failed to add Flathub remote.", "warning")
                return False
        except Exception as exc:
            self.outputReceived.emit(f"Flathub setup error: {exc}", "error")
            return False

    def _setup_service(self, service: str, packages: list) -> bool:
        pkg_ok = all(self._install_pkg(p) for p in packages) if packages else True
        return pkg_ok and self._enable_service(service)

    def _enable_service(self, service: str) -> bool:
        self.outputReceived.emit(f"Enabling '{service}.service'…", "info")
        if subprocess.run(["systemctl", "is-active", "--quiet", f"{service}.service"],
                          check=False, timeout=10, capture_output=True).returncode == 0:
            self.outputReceived.emit(f"'{service}.service' already active.", "success")
            return True
        r  = self._sudo(["sudo", "systemctl", "enable", "--now", f"{service}.service"])
        ok = self._ok(r)
        if ok:
            self.outputReceived.emit(f"'{service}.service' enabled.", "success")
            if service == "ufw":
                for ufw_cmd in (["sudo", "ufw", "default", "deny"], ["sudo", "ufw", "enable"], ["sudo", "ufw", "reload"]):
                    if not self._ok(self._sudo(ufw_cmd)):
                        ok = False
                        break
        else:
            rc = getattr(r, "returncode", "N/A")
            self.outputReceived.emit(f"Failed to enable '{service}.service' (exit: {rc}).", "error")
        return ok

    def _remove_orphans(self) -> bool:
        if not self.distro:
            return False
        cmd = self.distro.get_find_orphans_cmd()
        if not cmd:
            self.outputReceived.emit("Orphan removal is not supported on this distribution.", "info")
            return True
        try:
            tokens = shlex.split(cmd)
            needs_shell = "|" in tokens or "&&" in tokens
            proc = subprocess.run(cmd if needs_shell else tokens, shell=needs_shell,
                                  capture_output=True, text=True, timeout=60)
            raw_pkgs = proc.stdout.strip()
        except Exception as exc:
            self.outputReceived.emit(f"Orphan search failed: {exc}", "error")
            return False

        pkgs = self.distro.parse_orphan_output(raw_pkgs) if raw_pkgs else []

        if not pkgs:
            self.outputReceived.emit("No orphaned packages found.", "success")
            return True

        self.outputReceived.emit(f"Found orphaned packages: {', '.join(pkgs)}", "info")
        ok = self._ok(self._sudo(self.distro.get_pkg_remove_cmd(" ".join(pkgs)).split()))
        self.outputReceived.emit("Orphaned packages removed." if ok else "Could not remove orphaned packages.",
                                 "success" if ok else "error")
        return ok

    def _clean_cache(self) -> bool:
        if not self.distro:
            return False
        clean_cmd = self.distro.get_clean_cache_cmd()
        ok = self._ok(self._sudo_shell(clean_cmd) if ("&" in clean_cmd or "|" in clean_cmd) else self._sudo(clean_cmd.split()))
        self.outputReceived.emit("Cache cleaned." if ok else "Cache clean failed.", "success" if ok else "error")
        if ok and self._pkg_cache and self._pkg_cache.is_installed("yay"):
            self.outputReceived.emit("Cleaning yay cache…", "info")
            yay_ok = self._ok(self._sudo(["yay", "-Scc", "--noconfirm"]))
            self.outputReceived.emit("'yay' cache cleaned." if yay_ok else "'yay' cache clean failed.",
                                     "success" if yay_ok else "error")
            ok = ok and yay_ok
        return ok


class _Status:
    PENDING     = "pending"
    IN_PROGRESS = "in_progress"
    SUCCESS     = "success"
    WARNING     = "warning"
    ERROR       = "error"


class _Style:
    FONT_MAIN       = "DejaVu Sans Mono, Fira Code, monospace"
    FONT_SUBPROCESS = "Hack, Fira Mono, monospace"

    KIND_CFG: dict[str, tuple[str, int, float]] = {
        "operation":  (FONT_MAIN,       16, 1.2),
        "info":       (FONT_MAIN,       15, 1.0),
        "subprocess": (FONT_SUBPROCESS, 13, 0.6),
        "success":    (FONT_MAIN,       15, 1.0),
        "warning":    (FONT_MAIN,       15, 1.0),
        "error":      (FONT_MAIN,       15, 1.0),
    }

    _KIND_COLOR_KEY: dict[str, str] = {
        "operation":  "info",
        "info":       "success",
        "subprocess": "text",
        "success":    "success",
        "warning":    "warning",
        "error":      "error",
    }

    @classmethod
    def style_str(cls, kind: str) -> str:
        cfg = cls.KIND_CFG.get(kind)
        if not cfg:
            return ""
        font, size, lh = cfg
        t     = current_theme()
        color = t.get(cls._KIND_COLOR_KEY.get(kind, "text"), t["text"])
        return (f"font-family:{font};font-size:{size}px;color:{color};"
                f"padding:5px;line-height:{lh};word-break:break-word;")

    @classmethod
    def border_style(cls) -> str:
        p = current_theme()["accent"]
        return (f"border-radius:8px;border-right:1px solid {p};border-top:1px solid {p};"
                f"border-bottom:1px solid {p};border-left:4px solid {p};")

    STATUS_CFG: dict[str, tuple[str, str]] = {
        _Status.SUCCESS:     ("success", "dialog-ok-apply"),
        _Status.ERROR:       ("error",   "dialog-error"),
        _Status.WARNING:     ("warning", "dialog-warning"),
        _Status.IN_PROGRESS: ("info",    "media-playback-start"),
    }


def _fmt_html(text: str, kind: str) -> str:
    style = _Style.style_str(kind)
    if kind == "operation":
        esc = apply_replacements(text)
        return (
            "<hr style='border:none;margin:15px 30px;"
            "border-top:1px dashed rgba(111,255,245,0.3);'>"
            f"<div style='padding:10px;border-radius:8px;margin:5px 0;'>"
            f"<p style='{style}'>{esc}</p></div><br>"
        )
    lines = [f"<p style='{style}'>{apply_replacements(line)}</p>"
             for line in text.splitlines() if line.strip()]
    return "\n".join(lines) + "<br>"


class _PackageCache:
    _TTL      = 600
    _MAX_SIZE = 1000

    def __init__(self, distro_helper) -> None:
        self._distro = distro_helper
        self._cache: dict[str, bool] = {}
        self._ts   = 0.0
        self._lock = threading.Lock()

    def is_installed(self, package: str) -> bool:
        now = time.time()
        with self._lock:
            if now - self._ts > self._TTL:
                self._cache.clear()
                self._ts = now
            elif len(self._cache) > self._MAX_SIZE:
                # Drop the oldest half
                self._cache = dict(list(self._cache.items())[len(self._cache) // 2:])
            if package in self._cache:
                return self._cache[package]
        try:
            installed = self._distro.package_is_installed(package)
            with self._lock:
                self._cache[package] = installed
            return installed
        except Exception as exc:
            logger.warning("Package check failed for %s: %s", package, exc)
            return False

    def mark_installed(self, package: str) -> None:
        with self._lock:
            self._cache[package] = True
import base64 as _b64
import binascii
import json
import re
import sys
import os
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import QTime
from PyQt6.QtWidgets import (
    QComboBox, QDialog, QHBoxLayout, QLabel, QMessageBox,
    QPushButton, QVBoxLayout, QCheckBox, QTimeEdit, QFrame,
    QGridLayout, )

from state import S, logger, _HOME, frozen_safe_env
from themes import current_theme, font_sz
from translations import tr
from ui_utils import _StandardKeysMixin

_SYSTEMD_USER_DIR = Path(os.environ.get("XDG_CONFIG_HOME") or _HOME / ".config") / "systemd" / "user"
_SERVICE_NAME = "backup-helper-auto"

_INTERVALS: dict[str, str] = {
    "Hourly":        "*-*-* *:00:00",
    "Daily":         "*-*-* 02:00:00",
    "Every 2 days":  "*-*-*/2 02:00:00",
    "Weekly":        "Mon *-*-* 02:00:00",
    "Monthly":       "*-*-01 02:00:00",
    "Custom time …": "",
}

def _unit_names() -> tuple[Path, Path]:
    return (
        _SYSTEMD_USER_DIR / f"{_SERVICE_NAME}.service",
        _SYSTEMD_USER_DIR / f"{_SERVICE_NAME}.timer",
    )

def scheduler_supported() -> bool:
    return os.path.isdir("/run/systemd/system") and shutil.which("systemctl") is not None


def _systemctl(*args: str, check: bool = False, capture: bool = True) -> "subprocess.CompletedProcess":
    return subprocess.run(["systemctl", "--user", *args], capture_output=capture, text=True,
                          timeout=10, check=check, env=frozen_safe_env())


def _calendar_error(expr: str) -> str:
    analyzer = shutil.which("systemd-analyze")
    if not analyzer:
        return ""
    try:
        r = subprocess.run([analyzer, "calendar", expr], capture_output=True, text=True, timeout=10,
                           env=frozen_safe_env(), check=False)
    except (subprocess.SubprocessError, OSError):
        return ""
    return "" if r.returncode == 0 else (r.stderr or r.stdout).strip()


def is_timer_active() -> bool:
    if not scheduler_supported():
        return False
    try:
        r = subprocess.run(
            ["systemctl", "--user", "is-enabled", f"{_SERVICE_NAME}.timer"],
            capture_output=True, text=True, timeout=5, env=frozen_safe_env(), check=False,
        )
        return r.returncode == 0 and r.stdout.strip() not in ("disabled", "masked", "invalid")
    except (subprocess.SubprocessError, OSError):
        return False

def get_next_run_time() -> str:
    if not scheduler_supported():
        return ""
    try:
        r = subprocess.run(
            ["systemctl", "--user", "show",
             f"{_SERVICE_NAME}.timer", "--property=NextElapseUSecRealtime"],
            capture_output=True, text=True, timeout=5, env=frozen_safe_env(), check=False,
        )
        for line in r.stdout.splitlines():
            if line.startswith("NextElapseUSecRealtime="):
                val = line.split("=", 1)[1].strip()
                if val and val != "0":
                    try:
                        dt = datetime.fromtimestamp(int(val) / 1_000_000)
                        return dt.strftime("%Y-%m-%d %H:%M:%S")
                    except (ValueError, OSError, OverflowError):
                        return val
    except (subprocess.SubprocessError, OSError):
        pass
    return ""

def _systemd_quote(value: str) -> str:
    escaped = (value.replace("\\", "\\\\").replace('"', '\\"')
               .replace("$", "$$").replace("%", "%%"))
    return f'"{escaped}"'


def _exec_start_prefix() -> str:
    if getattr(sys, "frozen", False):
        return _systemd_quote(sys.executable)
    script = str(Path(__file__).resolve().parent / "main.py")
    return f"{_systemd_quote(sys.executable)} {_systemd_quote(script)}"


def install_timer(interval_key: str, backup_headers: list[str], *, on_calendar: str = "", only_on_ac: bool = False) -> tuple[bool, str]:
    if interval_key not in _INTERVALS:
        return False, tr("Unknown interval")
    if not scheduler_supported():
        return False, tr("Automatic backups need systemd (systemctl was not found). "
                         "Use cron or your init system to run: {cmd}", cmd=_exec_start_prefix() + " --headless-backup-b64 <groups>")

    on_cal = on_calendar.strip().replace("\n", "").replace("\r", "")
    if not on_cal:
        return False, tr("No OnCalendar expression provided.")

    cal_err = _calendar_error(on_cal)
    if cal_err:
        return False, tr("Invalid OnCalendar expression: {e}", e=cal_err)

    headers_b64 = _b64.b64encode(json.dumps(backup_headers).encode()).decode()

    ac_condition = "ConditionACPower=true\n" if only_on_ac else ""

    exec_prefix = _exec_start_prefix()

    service = (
        "[Unit]\n"
        "Description=Backup Helper Automatic Backup\n"
        "Wants=network-online.target\n"
        "After=network-online.target graphical-session.target\n"
        f"{ac_condition}"
        "\n"
        "[Service]\n"
        "Type=oneshot\n"
        "SuccessExitStatus=75\n"
        f"ExecStart={exec_prefix} --headless-backup-b64 {_systemd_quote(headers_b64)}\n"
        "Nice=10\n"
        "IOSchedulingClass=idle\n"
    )

    timer = (
        "[Unit]\n"
        "Description=Backup Helper Timer\n"
        "\n"
        "[Timer]\n"
        f"OnCalendar={on_cal}\n"
        "Persistent=true\n"
        "\n"
        "[Install]\n"
        "WantedBy=timers.target\n"
    )

    svc_path, tmr_path = _unit_names()
    try:
        _SYSTEMD_USER_DIR.mkdir(parents=True, exist_ok=True)
        for file_path, content in ((svc_path, service), (tmr_path, timer)):
            tmp = file_path.with_suffix(".tmp")
            try:
                tmp.write_text(content, encoding="utf-8")
                with open(tmp, "r+b") as fh:
                    os.fsync(fh.fileno())
                os.replace(tmp, file_path)
                try:
                    dfd = os.open(str(file_path.parent), os.O_RDONLY)
                    try:
                        os.fsync(dfd)
                    finally:
                        os.close(dfd)
                except OSError:
                    pass
            except Exception:
                tmp.unlink(missing_ok=True)
                raise
        _systemctl("daemon-reload", check=True)
        _systemctl("enable", "--now", f"{_SERVICE_NAME}.timer", check=True)
        logger.info("scheduler: Timer installed (%s, ac_only=%s)", interval_key, only_on_ac)
        return True, ""
    except subprocess.CalledProcessError as exc:
        return False, (exc.stderr or exc.stdout or str(exc)).strip()
    except Exception as exc:
        return False, str(exc)

def remove_timer() -> tuple[bool, str]:
    try:
        if scheduler_supported():
            _systemctl("disable", "--now", f"{_SERVICE_NAME}.timer")
        for p in _unit_names():
            p.unlink(missing_ok=True)
        if scheduler_supported():
            _systemctl("daemon-reload")
        return True, ""
    except Exception as exc:
        return False, str(exc)

def get_active_interval() -> str:
    _, tmr_path = _unit_names()
    try:
        text = tmr_path.read_text(encoding="utf-8")
        for line in text.splitlines():
            if line.strip().startswith("OnCalendar="):
                on_cal = line.split("=", 1)[1].strip()
                for label, cal in _INTERVALS.items():
                    if cal and cal == on_cal:
                        return label
                return on_cal
    except OSError:
        pass
    return ""

_TIME_RE = r"(\d{2}):(\d{2}):00"
_CAL_PATTERNS: tuple[tuple[str, "re.Pattern[str]"], ...] = (
    ("Daily",        re.compile(rf"^\*-\*-\* {_TIME_RE}$")),
    ("Every 2 days", re.compile(rf"^\*-\*-\*/2 {_TIME_RE}$")),
    ("Weekly",       re.compile(rf"^Mon \*-\*-\* {_TIME_RE}$")),
    ("Monthly",      re.compile(rf"^\*-\*-01 {_TIME_RE}$")),
)


def _parse_on_calendar(on_cal: str) -> "tuple[str, tuple[int, int] | None] | None":
    on_cal = on_cal.strip()
    if on_cal == _INTERVALS["Hourly"]:
        return "Hourly", None
    for key, pattern in _CAL_PATTERNS:
        m = pattern.match(on_cal)
        if m:
            hh, mm = int(m.group(1)), int(m.group(2))
            if 0 <= hh < 24 and 0 <= mm < 60:
                return key, (hh, mm)
    return None


def get_active_headers() -> "list[str] | None":
    svc_path, _ = _unit_names()
    try:
        text = svc_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    m = re.search(r'--headless-backup-b64\s+"?([A-Za-z0-9+/=]+)"?', text)
    if not m:
        return None
    try:
        parsed = json.loads(_b64.b64decode(m.group(1), validate=True).decode("utf-8"))
    except (ValueError, binascii.Error):
        return None
    return parsed if isinstance(parsed, list) and all(isinstance(h, str) for h in parsed) else None


def get_active_config() -> "dict | None":
    _, tmr_path = _unit_names()
    try:
        text = tmr_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    for line in text.splitlines():
        if line.strip().startswith("OnCalendar="):
            parsed = _parse_on_calendar(line.split("=", 1)[1])
            if parsed is None:
                return None
            return {"interval": parsed[0], "time": parsed[1], "headers": get_active_headers()}
    return None


def get_ac_only() -> bool:
    svc_path, _ = _unit_names()
    try:
        return "ConditionACPower=true" in svc_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False

class SchedulerDialog(_StandardKeysMixin, QDialog):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(tr("Automatic Backup"))
        self.setMinimumWidth(900)
        self._build_ui()

    def _build_ui(self) -> None:
        t = current_theme()
        lay = QVBoxLayout(self)
        lay.setSpacing(12)
        lay.setContentsMargins(18, 18, 18, 18)

        title = QLabel(tr("⏰  Automatic Backup"))
        title.setStyleSheet(
            f"font-size:{font_sz(3)}px;font-weight:bold;color:{t['accent']};"
        )
        lay.addWidget(title)

        interval_frame = QFrame()
        interval_frame.setStyleSheet(
            f"background:{t['bg3']};border-radius:6px;"
        )
        igrid = QGridLayout(interval_frame)
        igrid.setContentsMargins(12, 10, 12, 10)
        igrid.setSpacing(8)

        igrid.addWidget(QLabel(tr("Interval:")), 0, 0)
        self._combo = QComboBox()
        for _key in _INTERVALS:
            self._combo.addItem(tr(_key), _key)
        self._combo.currentIndexChanged.connect(lambda _i: self._on_interval_changed(self._combo.currentData()))
        igrid.addWidget(self._combo, 0, 1)

        self._time_label = QLabel(tr("Start time:"))
        self._time_edit = QTimeEdit()
        self._time_edit.setDisplayFormat("HH:mm")
        self._time_edit.setTime(QTime(2, 0))
        self._time_edit.setToolTip(
            tr("Time of day for daily/weekly backups.\n"
               "For Hourly this is ignored.\n"
               "For 'Custom time …' you can pick any hour:minute.")
        )
        igrid.addWidget(self._time_label, 0, 2)
        igrid.addWidget(self._time_edit, 0, 3)

        self._ac_cb = QCheckBox(tr("💡 Only run when on AC power (skip on battery)"))
        self._ac_cb.setToolTip(
            tr("Adds ConditionACPower=true to the systemd service unit.\n"
               "The backup will be silently skipped when the machine runs on battery.")
        )
        self._ac_cb.setChecked(get_ac_only())
        igrid.addWidget(self._ac_cb, 1, 0, 1, 4)

        lay.addWidget(interval_frame)

        lay.addWidget(QLabel(tr("Include backup groups:")))
        self._checks: list[tuple[QCheckBox, str]] = []
        headers = sorted({e.get("header", "") for e in S.entries if e.get("header")})
        for h in headers:
            cb = QCheckBox(h)
            cb.setChecked(True)
            lay.addWidget(cb)
            self._checks.append((cb, h))

        if not headers:
            lay.addWidget(
                QLabel(tr("  (no groups defined — all entries will be backed up)"))
            )

        _status_text = ""
        if is_timer_active():
            _interval = get_active_interval()
            _status_text = tr("✓ Timer active — {i}", i=tr(_interval)) if _interval else tr("✓ Timer active")
            nxt = get_next_run_time()
            if nxt:
                _status_text += tr("\nNext run: {n}", n=nxt)

        self._status = QLabel(_status_text)
        self._status.setStyleSheet(f"color:{t['success']};white-space:pre;")
        lay.addWidget(self._status)

        btn_lay = QHBoxLayout()
        install_btn = QPushButton(tr("✅ Enable timer"))
        install_btn.clicked.connect(self._install)
        remove_btn = QPushButton(tr("🗑 Remove timer"))
        remove_btn.clicked.connect(self._remove)
        close_btn = QPushButton(tr("Close"))
        close_btn.clicked.connect(self.accept)
        btn_lay.addWidget(install_btn)
        btn_lay.addWidget(remove_btn)
        btn_lay.addStretch()
        btn_lay.addWidget(close_btn)
        lay.addLayout(btn_lay)

        self._on_interval_changed(self._combo.currentData())

        cfg = get_active_config()
        if cfg:
            idx = self._combo.findData(cfg["interval"])
            if idx >= 0:
                self._combo.setCurrentIndex(idx)
            if cfg["time"]:
                self._time_edit.setTime(QTime(*cfg["time"]))
            if cfg["headers"] is not None and self._checks:
                for cb, h in self._checks:
                    cb.setChecked(h in cfg["headers"])

    def _on_interval_changed(self, text: str) -> None:
        is_custom = (text == "Custom time …")
        show_time = is_custom or text in ("Daily", "Weekly", "Every 2 days", "Monthly")
        self._time_label.setVisible(show_time)
        self._time_edit.setVisible(show_time)

        if is_custom:
            self._time_label.setText(tr("Time (HH:MM):"))
        else:
            self._time_label.setText(tr("Start time:"))

    def _build_on_calendar(self, interval_key: str) -> str:
        t = self._time_edit.time()
        hh = t.hour()
        mm = t.minute()
        time_str = f"{hh:02d}:{mm:02d}:00"

        if interval_key in ("Custom time …", "Daily"):
            return f"*-*-* {time_str}"
        if interval_key == "Every 2 days":
            return f"*-*-*/2 {time_str}"
        if interval_key == "Weekly":
            return f"Mon *-*-* {time_str}"
        if interval_key == "Monthly":
            return f"*-*-01 {time_str}"
        return _INTERVALS[interval_key]

    def _install(self) -> None:
        interval_key = self._combo.currentData()
        headers = [h for cb, h in self._checks if cb.isChecked()]

        if self._checks and not headers:
            QMessageBox.warning(
                self,
                tr("No backup groups selected"),
                tr("No backup group is checked, so the scheduled backup would run "
                   "with no scope and back up everything instead of nothing.\n\n"
                   "Please select at least one backup group, or uncheck all groups "
                   "only if you actually intend to back up all entries.")
            )
            return

        on_cal = self._build_on_calendar(interval_key)

        ok, err = install_timer(
            interval_key,
            headers,
            on_calendar=on_cal,
            only_on_ac=self._ac_cb.isChecked(),
        )

        tc = current_theme()
        if ok:
            _interval = get_active_interval()
            nxt = get_next_run_time()
            msg = tr("✓ Timer enabled — {i}", i=tr(_interval)) if _interval else tr("✓ Timer enabled")
            if nxt:
                msg += tr("\nNext run: {n}", n=nxt)
            self._status.setText(msg)
            self._status.setStyleSheet(f"color:{tc['success']};white-space:pre;")
        else:
            QMessageBox.critical(self, tr("Error"), tr("Timer could not be installed:\n{e}", e=err))

    def _remove(self) -> None:
        ok, err = remove_timer()
        tc = current_theme()
        if ok:
            self._status.setText(tr("Timer removed"))
            self._status.setStyleSheet(f"color:{tc['text_dim']};")
        else:
            QMessageBox.critical(self, tr("Error"), tr("Error during removal:\n{e}", e=err))

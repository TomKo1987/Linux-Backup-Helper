import base64 as _b64
import json
import os
import subprocess
from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import QTime
from PyQt6.QtWidgets import (
    QComboBox, QDialog, QHBoxLayout, QLabel, QMessageBox,
    QPushButton, QVBoxLayout, QCheckBox, QTimeEdit, QFrame,
    QGridLayout, )

from state import S, logger, _HOME
from themes import current_theme, font_sz
from ui_utils import _StandardKeysMixin

_SYSTEMD_USER_DIR = _HOME / ".config" / "systemd" / "user"
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

def is_timer_active() -> bool:
    try:
        r = subprocess.run(
            ["systemctl", "--user", "is-enabled", f"{_SERVICE_NAME}.timer"],
            capture_output=True, text=True, timeout=5
        )
        return r.returncode == 0
    except (subprocess.SubprocessError, OSError):
        return False

def get_next_run_time() -> str:
    try:
        r = subprocess.run(
            ["systemctl", "--user", "show",
             f"{_SERVICE_NAME}.timer", "--property=NextElapseUSecRealtime"],
            capture_output=True, text=True, timeout=5,
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

def install_timer(interval_key: str, backup_headers: list[str], *, on_calendar: str = "", only_on_ac: bool = False) -> tuple[bool, str]:
    if interval_key not in _INTERVALS:
        return False, "Unknown interval"

    on_cal = on_calendar.strip().replace("\n", "").replace("\r", "")
    if not on_cal:
        return False, "No OnCalendar expression provided."

    exe = Path(__file__).resolve().parent / "main.py"
    headers_b64 = _b64.b64encode(json.dumps(backup_headers).encode()).decode()

    ac_condition = "ConditionACPower=true\n" if only_on_ac else ""

    service = (
        "[Unit]\n"
        "Description=Backup Helper Automatic Backup\n"
        "After=network-online.target\n"
        f"{ac_condition}"
        "\n"
        "[Service]\n"
        "Type=oneshot\n"
        f"ExecStart=/usr/bin/python3 \"{exe}\" --headless-backup-b64 \"{headers_b64}\"\n"
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
                tmp.rename(file_path)
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
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True, timeout=10)
        subprocess.run(
            ["systemctl", "--user", "enable", "--now", f"{_SERVICE_NAME}.timer"],
            check=True, timeout=10,
        )
        logger.info("scheduler: Timer installed (%s, ac_only=%s)", interval_key, only_on_ac)
        return True, ""
    except Exception as exc:
        return False, str(exc)

def remove_timer() -> tuple[bool, str]:
    try:
        subprocess.run(
            ["systemctl", "--user", "disable", "--now", f"{_SERVICE_NAME}.timer"],
            capture_output=True, timeout=10,
        )
        for p in _unit_names():
            p.unlink(missing_ok=True)
        subprocess.run(
            ["systemctl", "--user", "daemon-reload"],
            capture_output=True, timeout=10,
        )
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

def get_ac_only() -> bool:
    svc_path, _ = _unit_names()
    try:
        return "ConditionACPower=true" in svc_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False

class SchedulerDialog(_StandardKeysMixin, QDialog):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Automatic backup")
        self.setMinimumWidth(900)
        self._build_ui()

    def _build_ui(self) -> None:
        t = current_theme()
        lay = QVBoxLayout(self)
        lay.setSpacing(12)
        lay.setContentsMargins(18, 18, 18, 18)

        title = QLabel("⏰  Set up automatic backup")
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

        igrid.addWidget(QLabel("Interval:"), 0, 0)
        self._combo = QComboBox()
        self._combo.addItems(list(_INTERVALS.keys()))
        self._combo.currentTextChanged.connect(self._on_interval_changed)
        igrid.addWidget(self._combo, 0, 1)

        self._time_label = QLabel("Start time:")
        self._time_edit = QTimeEdit()
        self._time_edit.setDisplayFormat("HH:mm")
        self._time_edit.setTime(QTime(2, 0))
        self._time_edit.setToolTip(
            "Time of day for daily/weekly backups.\n"
            "For Hourly this is ignored.\n"
            "For 'Custom time …' you can pick any hour:minute."
        )
        igrid.addWidget(self._time_label, 0, 2)
        igrid.addWidget(self._time_edit, 0, 3)

        self._ac_cb = QCheckBox("💡 Only run when on AC power (skip on battery)")
        self._ac_cb.setToolTip(
            "Adds ConditionACPower=true to the systemd service unit.\n"
            "The backup will be silently skipped when the machine runs on battery."
        )
        self._ac_cb.setChecked(get_ac_only())
        igrid.addWidget(self._ac_cb, 1, 0, 1, 4)

        lay.addWidget(interval_frame)

        lay.addWidget(QLabel("Include backup groups:"))
        self._checks: list[tuple[QCheckBox, str]] = []
        headers = sorted({e.get("header", "") for e in S.entries if e.get("header")})
        for h in headers:
            cb = QCheckBox(h)
            cb.setChecked(True)
            lay.addWidget(cb)
            self._checks.append((cb, h))

        if not headers:
            lay.addWidget(
                QLabel("  (no groups defined — all entries will be backed up)")
            )

        _status_text = ""
        if is_timer_active():
            _interval = get_active_interval()
            _status_text = f"✓ Timer active — {_interval}" if _interval else "✓ Timer active"
            nxt = get_next_run_time()
            if nxt:
                _status_text += f"\nNext run: {nxt}"

        self._status = QLabel(_status_text)
        self._status.setStyleSheet(f"color:{t['success']};white-space:pre;")
        lay.addWidget(self._status)

        btn_lay = QHBoxLayout()
        install_btn = QPushButton("✅ Enable timer")
        install_btn.clicked.connect(self._install)
        remove_btn = QPushButton("🗑 Remove timer")
        remove_btn.clicked.connect(self._remove)
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        btn_lay.addWidget(install_btn)
        btn_lay.addWidget(remove_btn)
        btn_lay.addStretch()
        btn_lay.addWidget(close_btn)
        lay.addLayout(btn_lay)

        self._on_interval_changed(self._combo.currentText())

        active = get_active_interval()
        if active and active in _INTERVALS:
            self._combo.setCurrentText(active)

    def _on_interval_changed(self, text: str) -> None:
        is_custom = (text == "Custom time …")
        show_time = is_custom or text in ("Daily", "Weekly", "Every 2 days", "Monthly")
        self._time_label.setVisible(show_time)
        self._time_edit.setVisible(show_time)

        if is_custom:
            self._time_label.setText("Time (HH:MM):")
        else:
            self._time_label.setText("Start time:")

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
        interval_key = self._combo.currentText()
        headers = [h for cb, h in self._checks if cb.isChecked()]

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
            msg = f"✓ Timer enabled — {_interval}" if _interval else "✓ Timer enabled"
            if nxt:
                msg += f"\nNext run: {nxt}"
            self._status.setText(msg)
            self._status.setStyleSheet(f"color:{tc['success']};white-space:pre;")
        else:
            QMessageBox.critical(self, "Error", f"Timer could not be installed:\n{err}")

    def _remove(self) -> None:
        ok, err = remove_timer()
        tc = current_theme()
        if ok:
            self._status.setText("Timer removed")
            self._status.setStyleSheet(f"color:{tc['text_dim']};")
        else:
            QMessageBox.critical(self, "Error", f"Error during removal:\n{err}")

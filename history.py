import json
import os
from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QDialog, QFrame, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
    QMessageBox, QPushButton, QTextEdit, QVBoxLayout
)

from state import S, _LOG_HIST_DIR
from themes import current_theme, font_sz
from ui_utils import _StandardKeysMixin


def _history_path(profile_name: str) -> Path:
    return _LOG_HIST_DIR / f"{profile_name}.history.json"


def append_history(operation: str, copied: int, skipped: int, errors: int, duration_s: int, cancelled: bool) -> None:
    name = S.profile_name
    if not name:
        return
    path = _history_path(name)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        existing: list = []
        if path.exists():
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(existing, list):
                    existing = []
            except (json.JSONDecodeError, OSError):
                existing = []
        entry = {"timestamp":  datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                 "operation":  operation,
                 "copied":     copied,
                 "skipped":    skipped,
                 "errors":     errors,
                 "duration_s": duration_s,
                 "cancelled":  cancelled}
        existing.append(entry)
        tmp_path = path.with_suffix(".tmp")
        try:
            tmp_path.write_text(json.dumps(existing, indent=2, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp_path, path)
        except (OSError, PermissionError):
            tmp_path.unlink(missing_ok=True)
            raise
    except (OSError, PermissionError):
        pass


def load_history(profile_name: str) -> list[dict]:
    path = _history_path(profile_name)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def _fmt_duration(s: int) -> str:
    if s < 60:
        return f"{s}s"
    return f"{s // 60}m {s % 60:02d}s"


def _op_classify(op: str) -> tuple[bool, bool]:
    lo = op.lower()
    is_restore = "restore" in lo
    is_backup  = "backup" in lo and not is_restore
    return is_backup, is_restore


def _entry_detail_html(e: dict, t: dict) -> str:
    ts      = e.get("timestamp", "?")
    op      = e.get("operation", "?")
    copied  = e.get("copied",    0)
    skipped = e.get("skipped",   0)
    errors  = e.get("errors",    0)
    dur     = _fmt_duration(e.get("duration_s", 0))
    can     = e.get("cancelled", False)

    ok_col = t["success"]
    sk_col = t["warning"]
    er_col = t["error"]
    dim    = t["text_dim"]
    fg     = t["text"]
    acc    = t["accent"]
    sep    = t["header_sep"]
    fs     = font_sz()
    fs_sm  = font_sz(-1)

    def row(label: str, value: str, color: str = "") -> str:
        val_style = f"color:{color};" if color else f"color:{fg};"
        return (f"<tr>"
                f"<td style='color:{dim};padding:6px 20px 6px 0;"
                f"font-size:{fs_sm}px;white-space:nowrap;'>{label}</td>"
                f"<td style='{val_style}font-size:{fs}px;font-weight:bold;padding:6px 0;'>{value}</td>"
                f"</tr>")

    can_html  = (f"<span style='color:{sk_col};'>yes  ⏹</span>" if can else f"<span style='color:{ok_col};'>no</span>")
    err_color = er_col if errors > 0 else ok_col
    is_backup, is_restore = _op_classify(op)
    op_label = "Backup created" if is_backup else ("Restored from backup" if is_restore else op)
    op_icon  = "⤵" if is_backup else ("⤴" if is_restore else "▶")

    return (f"<div style='font-family:monospace;padding:4px;'>"
            f"<div style='font-size:{font_sz(4)}px;font-weight:bold;color:{acc};"
            f"padding:4px 0 14px 0;border-bottom:1px solid {sep};margin-bottom:14px;'>"
            f"{op_icon}  {op_label}</div>"
            f"<table style='border-collapse:collapse;width:100%;'>"
            f"{row('Timestamp', ts)}"
            f"{row('Copied',    f'{copied:,}',  ok_col if copied  > 0 else dim)}"
            f"{row('Skipped',   f'{skipped:,}', sk_col if skipped > 0 else dim)}"
            f"{row('Errors',    f'{errors:,}',  err_color)}"
            f"{row('Duration',  dur)}"
            f"<tr>"
            f"<td style='color:{dim};padding:6px 20px 6px 0;font-size:{fs_sm}px;'>Cancelled</td>"
            f"<td style='padding:6px 0;'>{can_html}</td>"
            f"</tr>"
            f"</table>"
            f"</div>")


class HistoryDialog(_StandardKeysMixin, QDialog):
    def __init__(self, parent) -> None:
        super().__init__(parent)
        self.setWindowTitle("History")
        self.setMinimumSize(980, 580)
        t   = current_theme()
        bg  = t["bg"]
        bg2 = t["bg2"]
        bg3 = t["bg3"]
        sep = t["header_sep"]
        acc = t["accent"]
        acc2 = t["accent2"]
        fg  = t["text"]
        dim = t["text_dim"]

        self.setStyleSheet(f"background:{bg};")

        header_bar = QFrame()
        header_bar.setStyleSheet(f"background:{bg2}; border-bottom:1px solid {sep};")
        hb_lay = QHBoxLayout(header_bar)
        hb_lay.setContentsMargins(14, 10, 14, 10)

        title_lbl = QLabel("📜  History")
        title_lbl.setStyleSheet(f"font-size:{font_sz(5)}px; font-weight:bold; color:{acc}; background:transparent;")
        self._profile_lbl = QLabel()
        self._profile_lbl.setStyleSheet(f"font-size:{font_sz(1)}px; color:{acc2}; background:transparent;")
        hb_lay.addWidget(title_lbl)
        hb_lay.addStretch()
        hb_lay.addWidget(self._profile_lbl)

        content = QHBoxLayout()
        content.setSpacing(10)
        content.setContentsMargins(10, 10, 10, 6)

        list_frame = QFrame()
        list_frame.setStyleSheet(f"QFrame {{ background:{bg3}; border:1px solid {sep}; border-radius:6px; }}")
        lf_lay = QVBoxLayout(list_frame)
        lf_lay.setContentsMargins(0, 0, 0, 0)
        lf_lay.setSpacing(0)

        list_hdr = QLabel("  Runs  (newest first)")
        list_hdr.setStyleSheet(f"background:{bg2}; color:{dim}; font-size:{font_sz(-1)}px;"
                               f"padding:6px 10px; border-bottom:1px solid {sep}; border-radius:6px 6px 0 0;")
        lf_lay.addWidget(list_hdr)

        self._list = QListWidget()
        self._list.setStyleSheet(f"QListWidget {{ background:transparent; border:none;"
                                 f"  font-family:monospace; font-size:{font_sz(-1)}px; color:{fg}; outline:none; }}"
                                 f"QListWidget::item {{ padding:8px 10px; border-bottom:1px solid {sep}; }}"
                                 f"QListWidget::item:selected {{ background:{bg2}; color:{acc};"
                                 f"  border-left:3px solid {acc}; }}"
                                 f"QListWidget::item:hover:!selected {{ background:{bg2}; }}")
        self._list.currentRowChanged.connect(self._on_select)
        lf_lay.addWidget(self._list)
        content.addWidget(list_frame, 3)

        detail_frame = QFrame()
        detail_frame.setStyleSheet(f"QFrame {{ background:{bg3}; border:1px solid {sep}; border-radius:6px; }}")
        df_lay = QVBoxLayout(detail_frame)
        df_lay.setContentsMargins(0, 0, 0, 0)
        df_lay.setSpacing(0)

        detail_hdr = QLabel("  Details")
        detail_hdr.setStyleSheet(f"background:{bg2}; color:{dim}; font-size:{font_sz(-1)}px;"
                                 f"padding:6px 10px; border-bottom:1px solid {sep}; border-radius:6px 6px 0 0;")
        df_lay.addWidget(detail_hdr)

        self._detail = QTextEdit()
        self._detail.setReadOnly(True)
        self._detail.setStyleSheet(f"QTextEdit {{ background:transparent; border:none;"
                                   f"  font-family:monospace; font-size:{font_sz()}px; color:{fg}; padding:12px; }}")
        df_lay.addWidget(self._detail)
        content.addWidget(detail_frame, 2)

        bottom = QFrame()
        bottom.setStyleSheet(f"background:{bg2}; border-top:1px solid {sep};")
        bot_lay = QHBoxLayout(bottom)
        bot_lay.setContentsMargins(12, 8, 12, 8)

        self._count_lbl = QLabel()
        self._count_lbl.setStyleSheet(f"color:{dim}; font-size:{font_sz(-1)}px; background:transparent;")
        bot_lay.addWidget(self._count_lbl)
        bot_lay.addStretch()

        clear_btn = QPushButton("🗑 Clear History")
        clear_btn.setMinimumHeight(32)
        clear_btn.setMinimumWidth(130)
        clear_btn.setStyleSheet(f"QPushButton {{ background:{bg3}; border:1px solid {sep}; border-radius:4px;"
                                f"  color:{t['error']}; padding:2px 14px; }}"
                                f"QPushButton:hover {{ background:{bg2}; border-color:{t['error']}; }}")
        clear_btn.clicked.connect(self._clear_history)
        bot_lay.addWidget(clear_btn)

        close_btn = QPushButton("Close")
        close_btn.setMinimumHeight(32)
        close_btn.setMinimumWidth(100)
        close_btn.setStyleSheet(f"QPushButton {{ background:{bg3}; border:1px solid {sep}; border-radius:4px;"
                                f"  color:{fg}; padding:2px 18px; }}"
                                f"QPushButton:hover {{ background:{bg2}; border-color:{acc}; }}")
        close_btn.clicked.connect(self.accept)
        bot_lay.addWidget(close_btn)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(header_bar)
        lay.addLayout(content, 1)
        lay.addWidget(bottom)

        self._entries: list[dict] = []
        self._theme = t
        self._load()

    def _load(self) -> None:
        name = S.profile_name or "(no profile)"
        self._profile_lbl.setText(f"Profile:  {name}")
        self._entries = load_history(S.profile_name or "")
        self._list.clear()
        self._detail.clear()
        t   = self._theme
        dim = t["text_dim"]

        if not self._entries:
            item = QListWidgetItem("  No runs recorded yet.")
            item.setForeground(QColor(dim))
            item.setFlags(Qt.ItemFlag.NoItemFlags)
            self._list.addItem(item)
            self._count_lbl.setText("0 runs")
            return

        ok_col = t["success"]
        er_col = t["error"]
        sk_col = t["warning"]
        row_bg_even = QColor(t["bg3"])
        row_bg_odd  = QColor(t["bg2"])

        for idx, e in enumerate(reversed(self._entries)):
            ts      = e.get("timestamp", "?")
            op      = e.get("operation", "?")
            copied  = e.get("copied",  0)
            skipped = e.get("skipped", 0)
            errors  = e.get("errors",  0)
            dur     = _fmt_duration(e.get("duration_s", 0))
            can     = e.get("cancelled", False)

            is_backup, is_restore = _op_classify(op)
            op_label = "Backup created" if is_backup else ("Restored from backup" if is_restore else op)
            can_tag  = "  ⏹" if can else ""
            line1    = f"{op_label}  {ts}{can_tag}"
            line2    = f"    ⤵ {copied:,}   ↷ {skipped:,}   ✗ {errors:,}   ⏱ {dur}"

            item = QListWidgetItem(f"{line1}\n{line2}")
            item.setBackground(row_bg_even if idx % 2 == 0 else row_bg_odd)

            if errors > 0:
                item.setForeground(QColor(er_col))
            elif can:
                item.setForeground(QColor(sk_col))
            else:
                item.setForeground(QColor(ok_col))

            self._list.addItem(item)

        n = len(self._entries)
        self._count_lbl.setText(f"{n} run{'s' if n != 1 else ''}")
        self._list.setCurrentRow(0)

    def _clear_history(self) -> None:
        name = S.profile_name or ""
        if not name:
            return
        ans = QMessageBox.question(
            self, "Clear History",
            f"Delete the entire run history for profile '{name}'?\n\nThis cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if ans != QMessageBox.StandardButton.Yes:
            return
        path = _history_path(name)
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        self._load()

    def _on_select(self, row: int) -> None:
        if row < 0 or row >= len(self._entries):
            self._detail.clear()
            return
        e = self._entries[len(self._entries) - 1 - row]
        self._detail.setHtml(_entry_detail_html(e, self._theme))

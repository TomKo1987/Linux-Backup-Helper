import csv
import html
import io
import json
from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QDialog, QFileDialog, QFrame, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
    QMessageBox, QPushButton, QTextEdit, QVBoxLayout
)

from state import S, _LOG_HIST_DIR, _atomic_write, logger
from themes import current_theme, font_sz, register_style_listener, unregister_style_listener
from translations import tr, LANGUAGES, register_language_listener, unregister_language_listener
from ui_utils import ask_yes_no, fit_button_width, footer_bar_style, header_bar_style, _StandardKeysMixin, size_to_screen


def _history_path(profile_name: str) -> Path:
    return _LOG_HIST_DIR / f"{profile_name}.history.json"


_MAX_HISTORY_ENTRIES = 500


def append_history(operation: str, copied: int, skipped: int, errors: int, duration_s: int, cancelled: bool,
                   deleted: int = 0, op_kind: "str | None" = None) -> None:
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
                 "op_kind":    op_kind,
                 "copied":     copied,
                 "skipped":    skipped,
                 "deleted":    deleted,
                 "errors":     errors,
                 "duration_s": duration_s,
                 "cancelled":  cancelled}
        existing.append(entry)
        if len(existing) > _MAX_HISTORY_ENTRIES:
            existing = existing[-_MAX_HISTORY_ENTRIES:]
        _atomic_write(path, existing)
    except (OSError, PermissionError) as exc:
        logger.warning("append_history: could not write history for '%s': %s", name, exc)


def load_history(profile_name: str) -> list[dict]:
    path = _history_path(profile_name)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def export_history_csv(profile_name: str) -> str:
    entries = load_history(profile_name)
    out = io.StringIO()
    writer = csv.DictWriter(
        out,
        fieldnames=["timestamp", "operation", "copied", "skipped", "deleted", "errors", "duration_s", "cancelled"],
        extrasaction="ignore",
    )
    writer.writeheader()
    writer.writerows({"deleted": 0, **e} for e in entries)
    return out.getvalue()


def _fmt_duration(s: int) -> str:
    s = max(0, s)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60:02d}s"
    h = s // 3600
    m = (s % 3600) // 60
    return f"{h}h {m:02d}m {s % 60:02d}s"

def _op_classify(op: str) -> tuple[bool, bool]:
    lo = op.lower()
    restore_words = {"restore"}
    backup_words = {"backup"}
    for lang_map in LANGUAGES.values():
        r = lang_map.get("Restore")
        if r:
            restore_words.add(r.lower())
        b = lang_map.get("Backup")
        if b:
            backup_words.add(b.lower())
    is_restore = any(w in lo for w in restore_words)
    is_backup = any(w in lo for w in backup_words)
    return is_backup and not is_restore, is_restore


def _classify_entry(e: dict) -> tuple[bool, bool]:
    op_kind = e.get("op_kind")
    if op_kind == "backup":
        return True, False
    if op_kind == "restore":
        return False, True
    return _op_classify(str(e.get("operation", "?")))


def _entry_detail_html(e: dict, t: dict) -> str:
    ts      = html.escape(str(e.get("timestamp", "?")))
    op      = str(e.get("operation", "?"))
    copied  = e.get("copied",    0)
    skipped = e.get("skipped",   0)
    deleted = e.get("deleted",   0)
    errors  = e.get("errors",    0)
    dur     = _fmt_duration(e.get("duration_s", 0))
    can     = e.get("cancelled", False)

    ok_col = t["success"]
    sk_col = t["warning"]
    de_col = t["deleted"]
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
    is_backup, is_restore = _classify_entry(e)
    op_label = tr("Backup created") if is_backup else (tr("Restored from backup") if is_restore else html.escape(tr(op)))
    op_icon  = "⤵" if is_backup else ("⤴" if is_restore else "▶")

    return (f"<div style='font-family:monospace;padding:4px;'>"
            f"<div style='font-size:{font_sz(4)}px;font-weight:bold;color:{acc};"
            f"padding:4px 0 14px 0;border-bottom:1px solid {sep};margin-bottom:14px;'>"
            f"{op_icon}  {op_label}</div>"
            f"<table style='border-collapse:collapse;width:100%;'>"
            f"{row(tr('Timestamp'), ts)}"
            f"{row(tr('Copied'),    f'{copied:,}',  ok_col if copied  > 0 else dim)}"
            f"{row(tr('Skipped'),   f'{skipped:,}', sk_col if skipped > 0 else dim)}"
            f"{row(tr('Deleted'),   f'{deleted:,}', de_col if deleted > 0 else dim)}"
            f"{row(tr('Errors'),    f'{errors:,}',  err_color)}"
            f"{row(tr('Duration'),  dur)}"
            f"<tr>"
            f"<td style='color:{dim};padding:6px 20px 6px 0;font-size:{fs_sm}px;'>{tr('Cancelled')}</td>"
            f"<td style='padding:6px 0;'>{can_html}</td>"
            f"</tr>"
            f"</table>"
            f"</div>")


class HistoryDialog(_StandardKeysMixin, QDialog):
    def __init__(self, parent) -> None:
        super().__init__(parent)
        self.setWindowTitle(tr("History"))
        size_to_screen(self, 1500, 850, fallback_w=1000, fallback_h=650)
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
        header_bar.setStyleSheet(header_bar_style(bg2, sep))
        hb_lay = QHBoxLayout(header_bar)
        hb_lay.setContentsMargins(14, 10, 14, 10)

        title_lbl = QLabel(tr("📜  History"))
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

        list_hdr = QLabel(tr("  Runs  (newest first)"))
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

        detail_hdr = QLabel(tr("  Details"))
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
        bottom.setStyleSheet(footer_bar_style(bg2, sep))
        bot_lay = QHBoxLayout(bottom)
        bot_lay.setContentsMargins(12, 8, 12, 8)

        self._count_lbl = QLabel()
        self._count_lbl.setStyleSheet(f"color:{dim}; font-size:{font_sz(-1)}px; background:transparent;")
        bot_lay.addWidget(self._count_lbl)
        bot_lay.addStretch()

        export_btn = QPushButton(tr("📤 Export CSV"))
        export_btn.setMinimumHeight(32)
        fit_button_width(export_btn, min_floor=120)
        export_btn.setStyleSheet(
            f"QPushButton {{ background:{bg3}; border:1px solid {sep}; border-radius:4px;"
            f"  color:{fg}; padding:2px 14px; }}"
            f"QPushButton:hover {{ background:{bg2}; border-color:{acc}; color:{t['highlight']}; }}"
            f"QPushButton:focus {{ border-color:{acc}; color:{t['highlight']}; outline:none; }}"
            f"QPushButton:pressed {{ background:{bg}; border-color:{acc2}; color:{acc2}; }}")
        export_btn.clicked.connect(self._export_history)
        bot_lay.addWidget(export_btn)

        clear_btn = QPushButton(tr("🗑 Clear History"))
        clear_btn.setMinimumHeight(32)
        fit_button_width(clear_btn, min_floor=130)
        clear_btn.setStyleSheet(f"QPushButton {{ background:{bg3}; border:1px solid {sep}; border-radius:4px;"
                                f"  color:{t['error']}; padding:2px 14px; }}"
                                f"QPushButton:hover {{ background:{bg2}; border-color:{t['error']}; }}"
                                f"QPushButton:focus {{ border-color:{t['error']}; outline:none; }}"
                                f"QPushButton:pressed {{ background:{bg}; border-color:{t['error']}; }}")
        clear_btn.clicked.connect(self._clear_history)
        bot_lay.addWidget(clear_btn)

        close_btn = QPushButton(tr("Close"))
        close_btn.setMinimumHeight(32)
        fit_button_width(close_btn, min_floor=100)
        close_btn.setStyleSheet(f"QPushButton {{ background:{bg3}; border:1px solid {sep}; border-radius:4px;"
                                f"  color:{fg}; padding:2px 18px; }}"
                                f"QPushButton:hover {{ background:{bg2}; border-color:{acc}; color:{t['highlight']}; }}"
                                f"QPushButton:focus {{ border-color:{acc}; color:{t['highlight']}; outline:none; }}"
                                f"QPushButton:pressed {{ background:{bg}; border-color:{acc2}; color:{acc2}; }}")
        close_btn.clicked.connect(self.accept)
        bot_lay.addWidget(close_btn)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(header_bar)
        lay.addLayout(content, 1)
        lay.addWidget(bottom)

        self._title_lbl = title_lbl
        self._list_hdr = list_hdr
        self._detail_hdr = detail_hdr
        self._export_btn = export_btn
        self._clear_btn = clear_btn
        self._close_btn = close_btn

        self._entries: list[dict] = []
        self._load()
        register_style_listener(self._refresh_on_theme)
        register_language_listener(self._retranslate)
        self.finished.connect(lambda _r: unregister_style_listener(self._refresh_on_theme))
        self.finished.connect(lambda _r: unregister_language_listener(self._retranslate))

    def _retranslate(self) -> None:
        self.setWindowTitle(tr("History"))
        self._title_lbl.setText(tr("📜  History"))
        self._list_hdr.setText(tr("  Runs  (newest first)"))
        self._detail_hdr.setText(tr("  Details"))
        self._export_btn.setText(tr("📤 Export CSV"))
        fit_button_width(self._export_btn, min_floor=120)
        self._clear_btn.setText(tr("🗑 Clear History"))
        fit_button_width(self._clear_btn, min_floor=130)
        self._close_btn.setText(tr("Close"))
        fit_button_width(self._close_btn, min_floor=100)
        self._refresh_on_theme()

    def _load(self) -> None:
        name = S.profile_name or tr("(no profile)")
        self._profile_lbl.setText(tr("Profile:  {name}", name=name))
        self._entries = load_history(S.profile_name or "")
        self._list.clear()
        self._detail.clear()
        t   = current_theme()
        dim = t["text_dim"]

        if not self._entries:
            item = QListWidgetItem(tr("  No runs recorded yet."))
            item.setForeground(QColor(dim))
            item.setFlags(Qt.ItemFlag.NoItemFlags)
            self._list.addItem(item)
            self._count_lbl.setText(tr("0 runs"))
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
            deleted = e.get("deleted", 0)
            errors  = e.get("errors",  0)
            dur     = _fmt_duration(e.get("duration_s", 0))
            can     = e.get("cancelled", False)

            is_backup, is_restore = _classify_entry(e)
            op_label = tr("Backup created") if is_backup else (tr("Restored from backup") if is_restore else tr(op))
            can_tag  = "  ⏹" if can else ""
            line1    = f"{op_label}  {ts}{can_tag}"
            del_part = f"   🗑 {deleted:,}" if deleted else ""
            line2    = f"    ⤵ {copied:,}   ↷ {skipped:,}{del_part}   ✗ {errors:,}   ⏱ {dur}"

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
        total_copied  = sum(e.get("copied",     0) for e in self._entries)
        total_deleted = sum(e.get("deleted",    0) for e in self._entries)
        total_errors  = sum(e.get("errors",     0) for e in self._entries)
        total_dur     = sum(e.get("duration_s", 0) for e in self._entries)
        del_part = tr("{n:,} files deleted  ·  ", n=total_deleted) if total_deleted else ""
        runs_word = tr("runs") if n != 1 else tr("run")
        self._count_lbl.setText(
            tr("{n} {runs}  ·  {copied:,} files copied total  ·  {deleted}{errors:,} errors  ·  Total runtime: {dur}",
               n=n, runs=runs_word, copied=total_copied, deleted=del_part,
               errors=total_errors, dur=_fmt_duration(total_dur))
        )
        self._list.setCurrentRow(0)

    def _refresh_on_theme(self) -> None:
        row = self._list.currentRow()
        self._load()
        if 0 <= row < self._list.count():
            self._list.setCurrentRow(row)

    def _clear_history(self) -> None:
        name = S.profile_name or ""
        if not name:
            return
        ans = ask_yes_no(
            self, tr("Clear History"),
            tr("Delete the entire run history for profile '{name}'?\n\nThis cannot be undone.", name=name),
        )
        if ans != QMessageBox.StandardButton.Yes:
            return
        path = _history_path(name)
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        self._load()

    def _export_history(self) -> None:
        if not self._entries:
            QMessageBox.information(self, tr("Export"), tr("No history entries to export."))
            return
        name = S.profile_name or "profile"
        path, _ = QFileDialog.getSaveFileName(
            self, tr("Export history as a CSV file"),
            f"{name}_history.csv",
            tr("CSV files (*.csv)")
        )
        if not path:
            return
        try:
            csv_data = export_history_csv(S.profile_name or "")
            Path(path).write_text(csv_data, encoding="utf-8")
            QMessageBox.information(
                self, tr("Export complete"),
                tr("{n} Entries exported to:\n{path}", n=len(self._entries), path=path)
            )
        except OSError as exc:
            QMessageBox.critical(self, tr("Export failed"), tr("The file could not be written:\n{err}", err=exc))


    def _on_select(self, row: int) -> None:
        if row < 0 or row >= len(self._entries):
            self._detail.clear()
            return
        e = self._entries[-(row + 1)]
        self._detail.setHtml(_entry_detail_html(e, current_theme()))

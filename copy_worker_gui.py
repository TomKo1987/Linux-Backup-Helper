import html
import re
import threading
import time
from collections import deque
from dataclasses import dataclass

from PyQt6.QtCore import Qt, QElapsedTimer, QRectF, QTimer, pyqtSignal
from PyQt6.QtGui import QBrush, QColor, QIntValidator, QLinearGradient, QPainter, QPainterPath, QPen, QResizeEvent, QCloseEvent
from PyQt6.QtWidgets import (
    QPushButton, QScrollArea, QTabWidget, QVBoxLayout, QApplication, QWidget,
    QDialog, QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QSizePolicy, QTextEdit,
)

from state import apply_replacements, logger
from themes import current_theme, font_sz
from translations import tr
from ui_utils import card_frame_style, _StandardKeysMixin, show_scrollable_message, ask_yes_no_scrollable

from backup_lock import acquire_backup_lock, backup_lock_holder_pid, release_backup_lock
from copy_progress import (
    BAR_RANGE, STEP_COMPARE, STEP_CONFIRM, STEP_COPY, STEP_DELETE, STEP_HOOKS, STEP_SCAN,
    STEP_SYNC, STEP_VERSIONS, ProgressModel, ProgressView, Snapshot, format_duration,
)
from copy_worker_core import _format_unit, _cached_mono_style, _notify
from copy_worker import CopyWorker, _NF_MARK, _ConfirmRequest

_SEARCH_CACHE_MAX = 24
_CONFIRM_PREVIEW_MAX = 200
_UI_TICK_MS = 100
_DRAIN_TICK_MS = 25
_LIVE_DRAIN_BUDGET_S = 0.008
_FINAL_DRAIN_BUDGET_S = 0.015
_DRAIN_CHUNK = 400
_LOG_REFRESH_EVERY = 5


def _luminance(c: QColor) -> float:
    def lin(v: float) -> float:
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    return 0.2126 * lin(c.redF()) + 0.7152 * lin(c.greenF()) + 0.0722 * lin(c.blueF())


def _contrast_text(bg: QColor) -> QColor:
    lum = _luminance(bg)
    dark, light = QColor("#101218"), QColor("#ffffff")
    ratio_dark = (lum + 0.05) / (_luminance(dark) + 0.05)
    ratio_light = 1.05 / (lum + 0.05)
    return dark if ratio_dark >= ratio_light else light


def _mix(a: QColor, b: QColor, f: float) -> QColor:
    f = max(0.0, min(1.0, f))
    return QColor(round(a.red() + (b.red() - a.red()) * f),
                  round(a.green() + (b.green() - a.green()) * f),
                  round(a.blue() + (b.blue() - a.blue()) * f))


class _ContrastBar(QWidget):
    _FRAME_MS = 16

    def __init__(self) -> None:
        super().__init__()
        self._c1 = QColor("#89b4fa")
        self._c2 = QColor("#cba6f7")
        self._track = QColor("#101218")
        self._border = QColor("#333333")
        self._label = ""
        self._mode = "idle"
        self._target = 0.0
        self._shown = 0.0
        self._phase = 0.0
        self._timer = QTimer(self)
        self._timer.setInterval(self._FRAME_MS)
        self._timer.timeout.connect(self._step)
        self.setMinimumHeight(46)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def apply_colors(self, c1: str, c2: str, track: str, border: str, size: int) -> None:
        self._c1, self._c2, self._track, self._border = QColor(c1), QColor(c2), QColor(track), QColor(border)
        font = self.font()
        font.setBold(True)
        font.setPixelSize(max(9, size))
        self.setFont(font)
        self.update()

    def set_label(self, text: str) -> None:
        if text != self._label:
            self._label = text
            self.update()

    def set_fraction(self, fraction: float) -> None:
        fraction = max(0.0, min(1.0, fraction))
        if self._mode != "range":
            self._mode = "range"
            self._shown = 0.0
        if fraction < self._shown:
            self._shown = fraction
        self._target = fraction
        self._kick()

    def set_busy(self) -> None:
        if self._mode != "busy":
            self._mode = "busy"
            self._phase = 0.0
        self._kick()

    def set_idle(self) -> None:
        self._mode = "idle"
        self._target = 0.0
        self._shown = 0.0
        self._timer.stop()
        self.update()

    def _kick(self) -> None:
        if not self._timer.isActive():
            self._timer.start()
        self.update()

    def _step(self) -> None:
        if not self.isVisible():
            self._timer.stop()
            return
        if self._mode == "busy":
            self._phase = (self._phase + 0.0022) % 1.0
        elif self._mode == "range":
            diff = self._target - self._shown
            if abs(diff) < 0.0004:
                self._shown = self._target
                self._timer.stop()
            else:
                self._shown += diff * 0.06
        else:
            self._timer.stop()
        self.update()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if self._mode != "idle" and not self._timer.isActive():
            self._timer.start()

    def _geometry_paths(self):
        rect = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        radius = rect.height() / 2
        track = QPainterPath()
        track.addRoundedRect(rect, radius, radius)
        fill = QPainterPath()
        fill_rect = QRectF()
        if self._mode == "range" and self._shown > 0:
            width = max(rect.height(), rect.width() * self._shown)
            fill_rect = QRectF(rect.left(), rect.top(), min(rect.width(), width), rect.height())
        elif self._mode == "busy":
            seg = rect.width() * 0.3
            travel = rect.width() + seg
            left = rect.left() - seg + travel * self._phase
            fill_rect = QRectF(left, rect.top(), seg, rect.height()).intersected(rect)
        if not fill_rect.isNull() and fill_rect.width() > 0:
            fill.addRoundedRect(fill_rect, radius, radius)
        return rect, track, fill, fill_rect

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect, track, fill, fill_rect = self._geometry_paths()
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(self._track)
        painter.drawPath(track)
        if not fill.isEmpty():
            grad = QLinearGradient(fill_rect.left(), 0, fill_rect.right(), 0)
            grad.setColorAt(0.0, self._c1)
            grad.setColorAt(1.0, self._c2)
            painter.setBrush(QBrush(grad))
            painter.drawPath(fill.intersected(track))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(self._border, 1))
        painter.drawPath(track)
        if self._label:
            self._paint_text(painter, rect, track, fill, fill_rect)
        painter.end()

    def _paint_text(self, painter: QPainter, rect: QRectF, track: QPainterPath, fill: QPainterPath, fill_rect: QRectF) -> None:
        painter.setFont(self.font())
        text_rect = rect.adjusted(12, 0, -12, 0)
        flags = int(Qt.AlignmentFlag.AlignCenter)
        track_col = _contrast_text(self._track)
        if fill.isEmpty():
            painter.setPen(track_col)
            painter.drawText(text_rect, flags, self._label)
            return
        center = rect.center().x()
        if fill_rect.left() <= center <= fill_rect.right() and fill_rect.width() > 0:
            fill_col = _contrast_text(_mix(self._c1, self._c2, (center - fill_rect.left()) / fill_rect.width()))
        else:
            fill_col = _contrast_text(_mix(self._c1, self._c2, 0.5))
        painter.setClipPath(fill.intersected(track))
        painter.setPen(fill_col)
        painter.drawText(text_rect, flags, self._label)
        painter.setClipPath(track.subtracted(fill))
        painter.setPen(track_col)
        painter.drawText(text_rect, flags, self._label)
        painter.setClipping(False)


@dataclass
class _ProgressStatCard:
    frame:    QFrame
    val_lbl:  QLabel
    size_lbl: QLabel

    def set_val(self,  text: str) -> None: self.val_lbl.setText(text)
    def set_size(self, text: str) -> None: self.size_lbl.setText(text)


def _make_stat_card(color: "str | None", title: str, val: str = "0", size_title: int = 0, size_val: int = 0, bold_val: bool = True) -> _ProgressStatCard:
    t       = current_theme()
    s_title = size_title or font_sz(3)
    s_val   = size_val   or font_sz(16)

    frame = QFrame()
    frame.setMinimumWidth(150)
    border = f"border-left:4px solid {color};" if color else ""
    frame.setStyleSheet(f"QFrame {{background:{t['bg3']}; border-radius:8px; {border}}}")

    inner = QVBoxLayout(frame)
    inner.setContentsMargins(16, 14, 16, 14)

    title_lbl = QLabel(title)
    title_lbl.setStyleSheet(_cached_mono_style(s_title, t["text_dim"], extra="border:none;"))

    val_lbl = QLabel(val)
    val_lbl.setStyleSheet(_cached_mono_style(s_val, color or t["text"], bold=bold_val, extra="border:none;"))
    val_lbl.setMinimumWidth(90 if color else 110)
    val_lbl.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignBottom)

    inner.addWidget(title_lbl)

    val_row = QHBoxLayout()
    val_row.setSpacing(5)
    val_row.setContentsMargins(5, 2, 5, 2)
    val_row.addWidget(val_lbl, 0, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignBottom)

    size_lbl = QLabel("")
    if color:
        size_lbl.setText("0 B")
        size_lbl.setStyleSheet(_cached_mono_style(font_sz(14), color, extra="border:none;"))
        size_lbl.setMinimumWidth(110)
        size_lbl.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignBottom)
        val_row.addWidget(size_lbl, 0, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignBottom)

    val_row.addStretch()
    inner.addLayout(val_row)

    return _ProgressStatCard(frame=frame, val_lbl=val_lbl, size_lbl=size_lbl)


class _SummaryWidget(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self._entry_refresh_pending = False
        self._t = current_theme()
        t = self._t

        self._s_ok    = f"color:{t['success']};"
        self._s_skip  = f"color:{t['warning']};"
        self._s_del   = f"color:{t['deleted']};"
        self._s_err   = f"color:{t['error']};"
        self._s_dim   = f"color:{t['text_dim']};"
        self._s_title = f"color:{t['text']};"
        self._s_entry = _cached_mono_style(font_sz(-2), t["text"], extra="border:none; padding:2px 0px;")

        self._entry_results:    dict[str, list[int]] = {}
        self._entry_row_labels: dict[str, QLabel]    = {}
        self._entry_grid_cols = 1
        self._last_seg_counts: tuple[int, int, int, int] = (0, 0, 0, 0)
        self._last_seg_avail = -1

        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 20, 20, 20)
        lay.setSpacing(12)

        self.header_card = QFrame()
        self.header_card.setObjectName("headerCard")
        self.header_card.setStyleSheet(f"#headerCard{{background:{t['bg3']}; border-radius:10px; border-left:4px solid {t['accent']};}}")

        hdr = QGridLayout(self.header_card)
        hdr.setContentsMargins(20, 15, 20, 15)
        for i in range(3):
            hdr.setColumnStretch(i, 1)

        self.op_lbl = QLabel("-")
        self.op_lbl.setStyleSheet(_cached_mono_style(font_sz(10), t["accent"], bold=True))

        self._status_center_lbl = QLabel()
        self._status_center_lbl.setTextFormat(Qt.TextFormat.RichText)
        self._status_center_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._status_center_lbl.setStyleSheet("border:none; background:transparent;")

        self.total_lbl = QLabel("")
        self.total_lbl.setStyleSheet(
            f"font-size:{font_sz(6)}px; color:{t['text']}; "
            f"border:none; background:transparent; padding:0px;")
        self.total_lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        hdr.addWidget(self.op_lbl, 0, 0, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        hdr.addWidget(self._status_center_lbl, 0, 1, Qt.AlignmentFlag.AlignCenter | Qt.AlignmentFlag.AlignVCenter)
        hdr.addWidget(self.total_lbl, 0, 2, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        stats_lay = QGridLayout()
        stats_lay.setSpacing(12)
        for i in range(4):
            stats_lay.setColumnStretch(i, 1)

        self.card_copied  = _make_stat_card(t["success"], tr("  Copied"),  "0")
        self.card_skipped = _make_stat_card(t["warning"],  tr("↷ Skipped"), "0")
        self.card_deleted = _make_stat_card(t["deleted"],  tr("🗑 Deleted"), "0")
        self.card_errors  = _make_stat_card(t["error"],    tr("✗ Errors"),  "0")

        for col, card in enumerate((self.card_copied, self.card_skipped, self.card_deleted, self.card_errors)):
            stats_lay.addWidget(card.frame, 0, col)

        self._progress_card = QFrame()
        self._progress_card.setObjectName("progressCard")
        self._progress_card.setStyleSheet(card_frame_style(t["bg3"]))

        prog_lay = QVBoxLayout(self._progress_card)
        prog_lay.setContentsMargins(20, 14, 20, 14)
        prog_lay.setSpacing(8)

        prog_hdr = QHBoxLayout()
        self._prog_title = QLabel(tr("Progress"))
        self._prog_title.setStyleSheet(_cached_mono_style(font_sz(2), t["text_dim"], extra="border:none;"))
        prog_hdr.addWidget(self._prog_title)
        prog_hdr.addStretch()

        self._prog_pct = QLabel("…")
        self._prog_pct.setFixedWidth(60)
        self._prog_pct.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self._prog_pct.setStyleSheet(_cached_mono_style(font_sz(2), t["text"], bold=True, extra="border:none;"))
        prog_hdr.addWidget(self._prog_pct)

        self._progress_bar = _ContrastBar()
        self._progress_bar.set_busy()
        self._progress_bar.apply_colors(t["accent"], t["accent2"], t["bg"], t["header_sep"], font_sz(7))

        prog_lay.addLayout(prog_hdr)
        prog_lay.addWidget(self._progress_bar)

        metrics_lay = QHBoxLayout()
        metrics_lay.setSpacing(12)

        kw = {"size_title": font_sz(2), "size_val": font_sz(10)}
        self._card_elapsed = _make_stat_card(None, tr("⏲️ Elapsed"), "--:--", **kw)
        self._card_speed   = _make_stat_card(None, tr("🚤 Speed"),   "---",   **kw)
        self._card_eta     = _make_stat_card(None, tr("🏁 ETA"),     "--:--", **kw)

        for card in (self._card_elapsed, self._card_speed, self._card_eta):
            metrics_lay.addWidget(card.frame)

        self._rate_card = QFrame()
        self._rate_card.setObjectName("rateCard")
        self._rate_card.setStyleSheet(card_frame_style(t["bg3"]))

        rate_lay = QVBoxLayout(self._rate_card)
        rate_lay.setContentsMargins(20, 14, 20, 14)
        rate_lay.setSpacing(8)

        bd_lbl = self._lbl(tr("File breakdown"), _cached_mono_style(font_sz(2), t["text_dim"], extra="border:none;"))
        bd_lbl.setMinimumHeight(22)
        rate_lay.addWidget(bd_lbl)

        self._seg_track = QFrame()
        self._seg_track.setFixedHeight(10)
        self._seg_track.setStyleSheet(f"background:{t['header_sep']}; border-radius:5px;")

        seg_row = QHBoxLayout(self._seg_track)
        seg_row.setSpacing(0)
        seg_row.setContentsMargins(0, 0, 0, 0)

        self._seg_copied  = QFrame()
        self._seg_skipped = QFrame()
        self._seg_deleted = QFrame()
        self._seg_errors  = QFrame()
        self._seg_copied.setStyleSheet(f"background:{t['success']};")
        self._seg_skipped.setStyleSheet(f"background:{t['warning']};")
        self._seg_deleted.setStyleSheet(f"background:{t['deleted']};")
        self._seg_errors.setStyleSheet(f"background:{t['error']};")

        for seg in (self._seg_copied, self._seg_skipped, self._seg_deleted, self._seg_errors):
            seg.setFixedHeight(10)
            seg.setFixedWidth(0)
            seg_row.addWidget(seg)
        seg_row.addStretch(1)

        legend_row = QHBoxLayout()
        legend_row.setSpacing(20)
        legend_style = _cached_mono_style(font_sz(), t["text"], extra="border:none;")
        for key, text in (("success", tr("Copied")), ("warning", tr("Skipped")), ("deleted", tr("Deleted")), ("error", tr("Errors"))):
            dot = QLabel(f"<span style='color:{t[key]}'>■</span>  {text}")
            dot.setStyleSheet(legend_style)
            dot.setMinimumHeight(22)
            legend_row.addWidget(dot)
        legend_row.addStretch()

        rate_lay.addWidget(self._seg_track)
        rate_lay.addLayout(legend_row)

        self._entry_card = QFrame()
        self._entry_card.setObjectName("entryCard")
        self._entry_card.setStyleSheet(card_frame_style(t["bg3"]))

        entry_lay = QVBoxLayout(self._entry_card)
        entry_lay.setContentsMargins(15, 10, 15, 10)
        entry_lay.setSpacing(5)
        entry_lay.addWidget(self._lbl(tr("Entries processed"), _cached_mono_style(font_sz(2), t["text_dim"], extra="border:none;")))

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setStyleSheet("QScrollArea { background:transparent; border:none; }")

        self._entry_list_widget = QWidget()
        self._entry_list_widget.setStyleSheet("background:transparent;")

        self._entry_grid = QGridLayout(self._entry_list_widget)
        self._entry_grid.setContentsMargins(1, 0, 1, 0)
        self._entry_grid.setHorizontalSpacing(5)
        self._entry_grid.setVerticalSpacing(8)

        scroll.setWidget(self._entry_list_widget)
        entry_lay.addWidget(scroll)

        lay.addWidget(self.header_card)
        lay.addLayout(stats_lay)
        lay.addLayout(metrics_lay)
        lay.addWidget(self._progress_card)
        lay.addWidget(self._rate_card)
        self._entry_card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        lay.addWidget(self._entry_card)

    @staticmethod
    def _lbl(text: str, style: str) -> QLabel:
        w = QLabel(text)
        w.setStyleSheet(style)
        return w

    def set_status_html(self, _html: str) -> None: self._status_center_lbl.setText(_html)

    def update_stats(self, operation: str, view: ProgressView, title: str, bar_text: str,
                     copied: int, skipped: int, errors: int, deleted: int, elapsed_s: int,
                     size_copied: int, size_skipped: int, size_deleted: int, size_total: int,
                     finished: bool = False, cancelled: bool = False) -> None:
        self.op_lbl.setText(operation)
        self.card_copied.set_val(f"{copied:,}")
        self.card_copied.set_size(_format_unit(size_copied))
        self.card_skipped.set_val(f"{skipped:,}")
        self.card_skipped.set_size(_format_unit(size_skipped))
        self.card_deleted.set_val(f"{deleted:,}")
        self.card_deleted.set_size(_format_unit(size_deleted))
        self.card_errors.set_val(f"{errors:,}")
        self.total_lbl.setText(
            tr("{total:,} files / {size}", total=view.files_total, size=_format_unit(size_total))
            if view.files_total > 0 else "")
        self._update_progress(view, title, bar_text, finished)
        self._update_segments(copied, skipped, deleted, errors)
        self._update_timing(view, elapsed_s, size_copied, finished, cancelled)

    def on_entry_status(self, title: str, ok: int, skip: int, err: int, deleted: int = 0) -> None:
        ec = self._entry_results.setdefault(title, [0, 0, 0, 0])
        ec[0] += ok
        ec[1] += skip
        ec[2] += err
        ec[3] += deleted
        if not self._entry_refresh_pending:
            self._entry_refresh_pending = True
            QTimer.singleShot(300, self._deferred_refresh_entry_labels)

    def _deferred_refresh_entry_labels(self) -> None:
        self._entry_refresh_pending = False
        self._refresh_entry_labels()

    def _update_progress(self, view: ProgressView, title: str, bar_text: str, finished: bool) -> None:
        bar = self._progress_bar
        self._prog_title.setText(title)
        if view.determinate:
            bar.set_fraction(max(0, min(BAR_RANGE, view.value)) / BAR_RANGE)
            self._prog_pct.setText(f"{view.pct}%" if view.pct is not None else "…")
        elif finished:
            bar.set_idle()
            self._prog_pct.setText("—")
        else:
            bar.set_busy()
            self._prog_pct.setText("…")
        bar.set_label(bar_text)

    def _update_segments(self, copied: int, skipped: int, deleted: int, errors: int) -> None:
        counts = (copied, skipped, deleted, errors)
        avail  = max(1, self._rate_card.width() - 40)
        if counts == self._last_seg_counts and avail == self._last_seg_avail:
            return
        self._last_seg_counts = counts
        self._last_seg_avail  = avail
        segs  = (self._seg_copied, self._seg_skipped, self._seg_deleted, self._seg_errors)
        total = copied + skipped + deleted + errors
        if total == 0:
            for seg in segs:
                seg.setFixedWidth(0)
            return
        for seg, count in zip(segs, counts, strict=True):
            seg.setFixedWidth(max(0, int(avail * count / total)))

    def _update_timing(self, view: ProgressView, elapsed_s: int, size_copied: int, finished: bool, cancelled: bool) -> None:
        speed_str = "---"
        eta_str = "--:--"
        if finished:
            eta_str = tr("Cancelled") if cancelled else tr("Done")
            if elapsed_s > 0:
                if size_copied > 0:
                    speed_str = self._fmt_bytes_rate(size_copied / elapsed_s)
                elif view.files_done > 0:
                    speed_str = self._fmt_files_rate(view.files_done / elapsed_s)
        else:
            if view.speed_bps:
                speed_str = self._fmt_bytes_rate(view.speed_bps)
            elif view.files_per_s:
                speed_str = self._fmt_files_rate(view.files_per_s)
            if view.eta_s is not None:
                eta_str = format_duration(view.eta_s)
        self._card_elapsed.set_val(format_duration(elapsed_s))
        self._card_speed.set_val(speed_str)
        self._card_eta.set_val(eta_str)

    @staticmethod
    def _fmt_bytes_rate(bps: float) -> str:
        mb = bps / (1024 * 1024)
        if mb >= 0.1:
            return tr("{rate:.1f} MB/s", rate=mb)
        return f"{_format_unit(int(bps))}/s"

    @staticmethod
    def _fmt_files_rate(rate: float) -> str:
        return tr("{rate:,.1f} files/s", rate=rate) if rate >= 1 else tr("1 file/{s:.1f}s", s=1 / rate)

    def _recalculate_grid(self) -> bool:
        new_cols = max(1, (self._entry_card.width() - 40) // 280)
        if new_cols == self._entry_grid_cols:
            return False
        self._entry_grid_cols = new_cols
        grid = self._entry_grid
        for i in range(max(grid.columnCount(), new_cols + 1)):
            grid.setColumnStretch(i, 0)
        for i in range(new_cols):
            grid.setColumnStretch(i, 1)
        return True

    def _refresh_entry_labels(self) -> None:
        rebuild = self._recalculate_grid()
        cols    = self._entry_grid_cols
        labels  = self._entry_row_labels
        results = self._entry_results

        for title, (ok, skip, err, deleted) in results.items():
            parts = []
            if ok:      parts.append(f"<span style='{self._s_ok}'>  {ok:,}</span>")
            if skip:    parts.append(f"<span style='{self._s_skip}'>↷ {skip:,}</span>")
            if deleted: parts.append(f"<span style='{self._s_del}'>🗑 {deleted:,}</span>")
            if err:     parts.append(f"<span style='{self._s_err}'>✗ {err:,}</span>")

            suffix = "&nbsp; ".join(parts) if parts else f"<span style='{self._s_dim}'></span>"
            safe_title = html.escape(title).replace("&lt;br&gt;", "<br>")
            _html  = f"<span style='{self._s_title}'>{safe_title}</span><br>{suffix}"

            lbl = labels.get(title)
            if lbl is None:
                lbl = QLabel()
                lbl.setTextFormat(Qt.TextFormat.RichText)
                lbl.setWordWrap(False)
                lbl.setStyleSheet(self._s_entry)
                labels[title] = lbl
                rebuild = True
            lbl.setText(_html)

        if not rebuild:
            return

        grid = self._entry_grid
        while grid.count():
            item = grid.takeAt(0)
            if item is None:
                break
            w    = item.widget()
            if w:
                w.hide()

        for idx, title in enumerate(sorted(labels)):
            row, col = divmod(idx, cols)
            labels[title].show()
            grid.addWidget(labels[title], row, col, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)

        self._entry_list_widget.adjustSize()

    def resizeEvent(self, a0: QResizeEvent | None) -> None:
        super().resizeEvent(a0)
        self._update_segments(*self._last_seg_counts)
        if not self._entry_refresh_pending:
            self._entry_refresh_pending = True
            QTimer.singleShot(150, self._deferred_refresh_entry_labels)


class _LogWidget(QWidget):
    _PAGE    = 500
    _LOG_MAX = 150_000
    _NATURAL_SORT_RE = re.compile(r"([0-9]+)")
    _sorted_ready = pyqtSignal(object, object)

    def __init__(self, color: str) -> None:
        super().__init__()
        t = current_theme()
        self._items:       list[str] = []
        self._items_lower: list[str] = []
        self._filtered:    list[str] = []
        self._page          = 0
        self._finalized     = False
        self._truncated     = False
        self._dirty         = False
        self._last_rendered = ""
        self._search_cache: dict[str, list[str]] = {}
        self._sorted_ready.connect(self._apply_sorted)

        style_view   = (f"font-family:monospace; font-size:{font_sz(-1)}px; "
                        f"color:{color}; background:transparent; border:none;")
        style_search = (f"QLineEdit {{ background:{t['bg3']}; border:1px solid {t['header_sep']}; "
                        f"border-radius:6px; padding:0 10px; color:{t['text']}; }}")
        style_spin   = (f"QLineEdit{{border:1px solid {t['header_sep']}; border-radius:4px; "
                        f"padding:0 6px; background:{t['bg3']}; color:{t['text']}; font-weight:bold}}"
                        f"QLineEdit:focus{{border:1px solid {t['accent']}; background:{t['bg2']}}}")
        style_muted  = f"color:{t['muted']}; font-size:{font_sz()}px; margin-left:10px;"
        style_btn = (f"QPushButton {{ background:{t['bg3']}; border:1px solid {t['header_sep']}; "
                     f"border-radius:4px; padding:2px 8px; color:{t['text']}; }}"
                     f"QPushButton:hover {{ background:{t['bg2']}; border-color:{t['accent']}; color:{t['highlight']}; }}"
                     f"QPushButton:focus {{ border-color:{t['accent']}; color:{t['highlight']}; outline:none; }}"
                     f"QPushButton:pressed {{ background:{t['bg']}; border-color:{t['accent2']}; color:{t['accent2']}; }}")

        self._search = QLineEdit()
        self._search.setPlaceholderText(tr(" 🔍  Search…"))
        self._search.setClearButtonEnabled(True)
        self._search.textChanged.connect(self._on_search)
        self._search.setMinimumHeight(44)
        self._search.setStyleSheet(style_search)

        self._view = QTextEdit()
        self._view.setReadOnly(True)
        self._view.setStyleSheet(style_view)

        self._first = QPushButton("««")
        self._prev  = QPushButton(tr("‹ Prev"))
        self._next  = QPushButton(tr("Next ›"))
        self._last  = QPushButton("»»")

        for btn, cb in ((self._first, lambda: self._go(0)), (self._prev,  lambda: self._go(self._page - 1)),
                        (self._next,  lambda: self._go(self._page + 1)), (self._last,  lambda: self._go(self._pages() - 1))):
            btn.clicked.connect(cb)
            btn.setMinimumHeight(28)
            btn.setStyleSheet(style_btn)

        self._spin = QLineEdit("1")
        self._spin.setValidator(QIntValidator(1, 99_999_999, self))
        self._spin.setStyleSheet(style_spin)
        self._spin.setMinimumHeight(28)
        self._spin.setFixedWidth(84)
        self._spin.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._spin.returnPressed.connect(self._spin_changed)

        self._page_lbl  = QLabel("")
        self._page_lbl.setStyleSheet(f"color:{t['text']};")
        self._page_lbl.setMinimumHeight(28)
        self._total_lbl = QLabel("")
        self._total_lbl.setStyleSheet(style_muted)
        self._total_lbl.setMinimumHeight(28)

        nav = QHBoxLayout()
        nav.setContentsMargins(5, 5, 5, 5)
        nav.setSpacing(8)
        nav.addWidget(self._first)
        nav.addWidget(self._prev)
        nav.addStretch(1)

        pg = QHBoxLayout()
        pg.setSpacing(5)
        for w in (QLabel(tr("Page")), self._spin, QLabel(tr("of")), self._page_lbl):
            w.setMinimumHeight(28)
            pg.addWidget(w)

        nav.addLayout(pg)
        nav.addStretch(1)
        nav.addWidget(self._total_lbl)

        self._copy_vis_btn = QPushButton(tr("📋 Copy Visible"))
        self._copy_vis_btn.setMinimumHeight(28)
        self._copy_vis_btn.setStyleSheet(style_btn)
        self._copy_vis_btn.setToolTip(tr("Copy visible entries on this page to the clipboard."))
        self._copy_vis_btn.clicked.connect(lambda: self._copy_to_clipboard(copy_all=False))

        self._copy_all_btn = QPushButton(tr("📋 Copy All"))
        self._copy_all_btn.setMinimumHeight(28)
        self._copy_all_btn.setStyleSheet(style_btn)
        self._copy_all_btn.setToolTip(tr("Copy all entries to the clipboard."))
        self._copy_all_btn.clicked.connect(lambda: self._copy_to_clipboard(copy_all=True))

        nav.addWidget(self._copy_vis_btn)
        nav.addWidget(self._copy_all_btn)

        nav.addWidget(self._next)
        nav.addWidget(self._last)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(3, 3, 3, 3)
        lay.setSpacing(3)
        lay.addWidget(self._search)
        lay.addWidget(self._view)
        lay.addLayout(nav)

        self._html_prefix = (f"<style>body {{font-family: monospace; font-size: {font_sz(-1)}px; color: {color}}}"
                             f"hr {{background-color: {t['header_sep']}}} "
                             f".entry-odd  {{padding: 2px; background-color: rgba(0, 0, 0, 0.15)}} "
                             f".entry-even {{padding: 2px; background-color: rgba(255, 255, 255, 0.05)}}</style>")

    def _copy_to_clipboard(self, copy_all: bool = False) -> None:
        if copy_all:
            chunk = self._filtered
            active_btn = self._copy_all_btn
        else:
            start = self._page * self._PAGE
            chunk = self._filtered[start: start + self._PAGE]
            active_btn = self._copy_vis_btn

        if not chunk:
            return

        text = "\n\n".join(chunk)

        clipboard = QApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(text)

        original = active_btn.text()
        active_btn.setText(tr("✓ Copied!"))
        active_btn.setEnabled(False)

        def restore_btn() -> None:
            active_btn.setText(original)
            active_btn.setEnabled(True)

        QTimer.singleShot(1500, restore_btn)

    @property
    def is_truncated(self) -> bool: return self._truncated

    @property
    def log_max(self) -> int: return self._LOG_MAX

    @property
    def item_count(self) -> int: return len(self._items)

    def _pages(self) -> int: return max(1, (len(self._filtered) + self._PAGE - 1) // self._PAGE)

    def _go(self, page: int) -> None:
        self._page = max(0, min(page, self._pages() - 1))
        self._render()

    def _spin_changed(self) -> None:
        try:
            target = int(self._spin.text()) - 1
        except ValueError:
            target = self._page
        if target != self._page:
            self._go(target)
        else:
            self._spin.setText(str(self._page + 1))

    def _render(self) -> None:
        self._render_page()
        self._update_nav()

    def refresh_live(self) -> None:
        if self._dirty and not self._finalized and self.isVisible():
            self._dirty = False
            self._render()

    def _render_page(self) -> None:
        start = self._page * self._PAGE
        chunk = self._filtered[start: start + self._PAGE]
        parts = [self._html_prefix]

        for i, item in enumerate(chunk):
            idx       = start + i + 1
            safe      = html.escape(item).replace("\n", "<br>")
            row_class = "entry-even" if i % 2 == 0 else "entry-odd"
            parts.append(f'<div class="{row_class}"><b>{idx:,}:</b> {safe}</div>')
            if i < len(chunk) - 1:
                parts.append("<hr>")

        new_html = "".join(parts)
        if new_html != self._last_rendered:
            bar = self._view.verticalScrollBar()
            keep = bar.value() if bar is not None else 0
            self._view.setHtml(new_html)
            if bar is not None:
                bar.setValue(keep)
            self._last_rendered = new_html

    def _update_nav(self) -> None:
        pages = self._pages()
        total = len(self._filtered)
        self._page_lbl.setText(f"<b>{pages}</b>")
        self._total_lbl.setText(tr("({total:,} {word})", total=total, word=tr("entry") if total == 1 else tr("entries")))

        self._spin.setText(str(self._page + 1))

        can_back = self._page > 0
        can_fwd  = self._page < pages - 1
        self._first.setEnabled(can_back)
        self._prev.setEnabled(can_back)
        self._next.setEnabled(can_fwd)
        self._last.setEnabled(can_fwd)

    def bulk_add(self, entries: list[str]) -> None:
        if not entries or self._truncated:
            return
        remaining = max(0, self._LOG_MAX - len(self._items))
        if len(entries) > remaining:
            entries = entries[:remaining]
            self._truncated = True
        needle        = self._search.text().lower().strip()
        entries_lower = [e.lower() for e in entries]
        self._items.extend(entries)
        self._items_lower.extend(entries_lower)
        if not self._finalized:
            self._dirty = True
            if needle:
                self._filtered.extend(e for e, el in zip(entries, entries_lower, strict=True) if needle in el)
            else:
                self._filtered.extend(entries)
            self._search_cache.clear()
        if self._truncated:
            cap_msg = tr("⚠ Log capped at {max:,} entries — use search to find specific files", max=self._LOG_MAX)
            self._items.append(cap_msg)
            self._items_lower.append(cap_msg.lower())
            if not self._finalized:
                self._filtered.append(cap_msg)

    def flush_final(self) -> None:
        self._finalized = True
        self._page = 0
        self._render()
        pairs = list(zip(self._items, self._items_lower, strict=True))

        def _bg_sort() -> None:
            _key = _LogWidget._natural_sort_key
            pairs.sort(key=lambda p: _key(p[0].split('\n', 1)[0]))
            if pairs:
                sorted_items_t, sorted_lower_t = zip(*pairs, strict=True)
                sorted_items = list(sorted_items_t)
                sorted_lower = list(sorted_lower_t)
            else:
                sorted_items, sorted_lower = [], []
            try:
                self._sorted_ready.emit(sorted_items, sorted_lower)
            except RuntimeError:
                pass

        threading.Thread(target=_bg_sort, daemon=True).start()

    def _apply_sorted(self, items: list, items_lower: list) -> None:
        needle            = self._search.text().lower().strip()
        self._items       = items
        self._items_lower = items_lower
        self._search_cache.clear()
        self._filtered    = ([i for i, il in zip(items, items_lower, strict=True) if needle in il] if needle else items)
        self._render()

    def _on_search(self) -> None:
        needle = self._search.text().lower().strip()
        if needle in self._search_cache:
            self._filtered = self._search_cache[needle] if self._finalized else self._search_cache[needle][:]
        else:
            self._filtered = ([i for i, il in zip(self._items, self._items_lower, strict=True) if needle in il]
                              if needle else (self._items if self._finalized else self._items[:]))
            if len(self._search_cache) > _SEARCH_CACHE_MAX:
                self._search_cache.pop(next(iter(self._search_cache)))
            self._search_cache[needle] = self._filtered[:]
        self._page = 0
        self._render()

    @staticmethod
    def _natural_sort_key(s: str) -> list:
        return [int(t) if (i & 1) else t.lower() for i, t in enumerate(_LogWidget._NATURAL_SORT_RE.split(s))]



class CopyDialog(_StandardKeysMixin, QDialog):
    active_workers: "set[CopyWorker]" = set()

    def __init__(self, parent, tasks, operation: str, *,
                 op_kind: "str | None" = None, headless: bool = False,
                 advanced: bool = False, is_restore: bool = False) -> None:
        super().__init__(parent)
        self.setWindowTitle(operation)
        self._op_kind = op_kind
        self._headless = headless
        self._advanced = advanced

        self._owns_lock = acquire_backup_lock()
        self._lock_denied = not self._owns_lock
        if self._lock_denied:
            holder = backup_lock_holder_pid()
            msg = (tr("Another backup is already running (either from the main window "
                      "or a scheduled headless backup)")
                   + (tr(" — process PID {pid}.", pid=holder) if holder else ".")
                   + tr("\n\nPlease wait for it to finish before starting a new one."))
            logger.warning("CopyDialog: refused to start, backup lock already held (pid=%s)", holder)
            if headless:
                QTimer.singleShot(0, self.reject)
            else:
                QTimer.singleShot(0, lambda: (QMessageBox.warning(self, tr("Backup Already Running"), msg), self.reject()))
            self.worker = None
            return

        self.worker = None
        try:
            self._setup(tasks, operation, advanced, is_restore)
        except BaseException:
            if self.worker is not None and self.worker.isRunning():
                self.worker.cancel()
            else:
                CopyDialog.active_workers.discard(self.worker)
            self._release_lock()
            raise

    def _setup(self, tasks, operation: str, advanced: bool, is_restore: bool) -> None:
        self._t = current_theme()
        t       = self._t

        self.c_ok = t["success"]
        self.c_sk = t["warning"]
        self.c_de = t["deleted"]
        self.c_er = t["error"]

        self._status_fs = font_sz(8)

        _screen = QApplication.primaryScreen()
        _geo = _screen.availableGeometry() if _screen else None
        if _geo is not None:
            self.setMinimumSize(min(1000, int(_geo.width() * 0.5)),
                                min(600, int(_geo.height() * 0.5)))
            self.resize(min(1900, int(_geo.width() * 0.9)),
                        min(925, int(_geo.height() * 0.9)))
        else:
            self.setMinimumSize(1000, 600)
            self.resize(1400, 800)

        self._operation = operation
        self.worker     = CopyWorker(tasks, prepare=advanced, is_restore=is_restore, interactive=not self._headless)
        CopyDialog.active_workers.add(self.worker)
        self.copied = self.skipped = self.errors = self.deleted = 0
        self._final_elapsed: int | None = None
        self._finished = False
        self._cancelling = False
        self._draining = False
        self._final_title = ""
        self._tick_count = 0
        self._model = ProgressModel()
        self._last_status = ""
        self._last_tab_counts: tuple[int, int, int, int] = (-1, -1, -1, -1)
        self._pending_ok = deque()
        self._pending_sk = deque()
        self._pending_de = deque()
        self._pending_er = deque()
        self._size_copied = self._size_skipped = self._size_deleted = 0
        self._not_found_paths: list[tuple[str, str]] = []

        self._preseed_deleted = 0
        self._preseed_deleted_size = 0
        self._preseed_errors = 0

        self._summary = _SummaryWidget()

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setFixedHeight(2)
        sep.setStyleSheet(f"background: {t['header_sep']}; border: none;")

        self._w_copied  = _LogWidget(self.c_ok)
        self._w_skipped = _LogWidget(self.c_sk)
        self._w_deleted = _LogWidget(self.c_de)
        self._w_errors  = _LogWidget(self.c_er)
        self._log_groups = (
            (self._pending_ok, self._w_copied,  self._fmt_ok),
            (self._pending_sk, self._w_skipped, self._fmt_sk),
            (self._pending_de, self._w_deleted, self._fmt_de),
            (self._pending_er, self._w_errors,  self._fmt_er),
        )

        summary_page = QWidget()
        sl = QVBoxLayout(summary_page)
        sl.setContentsMargins(0, 0, 0, 0)
        sl.setSpacing(0)
        sl.addWidget(self._summary)

        self.tabs = QTabWidget()
        self.tabs.addTab(summary_page, tr("📋 Summary"))
        self.tabs.addTab(self._w_copied, tr("\uf4bb  Copied (0)"))
        self.tabs.addTab(self._w_skipped, tr("↷ Skipped (0)"))
        self.tabs.addTab(self._w_deleted, tr("🗑 Deleted (0)"))
        self.tabs.addTab(self._w_errors, tr("✗ Errors (0)"))
        self.tabs.setStyleSheet(f"QTabWidget::pane {{border: none}} QTabBar::tab {{min-width: 100px; padding: 10px 16px}}"
                                f"QTabBar::tab:selected {{background: {t['bg3']}; border-bottom: 2px solid {t['accent']}}}")
        self.tabs.setElideMode(Qt.TextElideMode.ElideNone)
        self.tabs.setUsesScrollButtons(True)
        self.tabs.setSizePolicy(QSizePolicy.Policy.Expanding, self.tabs.sizePolicy().verticalPolicy())
        self.tabs.currentChanged.connect(self._on_tab_changed)

        self.cancel_btn = QPushButton(tr("⏹ Cancel"))
        self.cancel_btn.setMinimumHeight(50)
        self.cancel_btn.setStyleSheet(
            f"QPushButton {{background: {t['bg3']}; border: 1px solid {t['header_sep']}; "
            f"border-radius: 4px; color: {t['text']};}}"
            f"QPushButton:hover {{background: {t['bg2']}; border-color: {t['accent']}; color: {t['highlight']};}}"
            f"QPushButton:focus {{border-color: {t['accent']}; color: {t['highlight']}; outline: none;}}"
            f"QPushButton:pressed {{background: {t['bg']}; border-color: {t['accent2']}; color: {t['accent2']};}}")
        self._cancel_connected = True
        self._accept_connected = False
        self.cancel_btn.clicked.connect(self._request_cancel)

        layout = QVBoxLayout(self)
        layout.setSpacing(3)
        layout.setContentsMargins(3, 3, 3, 3)
        layout.addWidget(sep)
        layout.addWidget(self.tabs)
        layout.addWidget(self.cancel_btn)

        self.timer = QElapsedTimer()
        self.timer.start()

        self._tick = QTimer(self)
        self._tick.timeout.connect(self._update_ui_tick)

        self.worker.batch_update.connect(self._on_batch)
        self.worker.finished_work.connect(self._on_done)
        self.worker.entry_status.connect(self._summary.on_entry_status)
        self.worker.prepared.connect(self._on_prepared)
        self.worker.confirm_delete.connect(self._on_confirm_delete)
        self.worker.space_warnings.connect(self._on_space_warnings)

        self._render_progress()
        self.worker.start()
        self._tick.start(_UI_TICK_MS)

    def _elapsed_s(self) -> int: return self._final_elapsed if self._final_elapsed is not None else self.timer.elapsed() // 1000

    def _request_cancel(self) -> None:
        if self._finished or self._cancelling or self.worker is None:
            return
        self._cancelling = True
        self.cancel_btn.setText(tr("⏹ Cancelling…"))
        self.cancel_btn.setEnabled(False)
        self.worker.cancel()
        self._render_progress()

    def _seed_deleted(self, items: "list | None") -> None:
        if not items:
            return
        texts = []
        for it in items:
            self._preseed_deleted += 1
            self._preseed_deleted_size += getattr(it, "size", 0) or 0
            texts.append(self._fmt_de(it.path, it.reason))
        self._w_deleted.bulk_add(texts)

    def _seed_errors(self, errs: "list | None") -> None:
        if not errs:
            return
        texts = []
        for e in errs:
            self._preseed_errors += 1
            texts.append(self._fmt_er(e.path, e.reason))
        self._w_errors.bulk_add(texts)

    @property
    def _display_deleted(self) -> int: return self._preseed_deleted + self.deleted

    @property
    def _display_errors(self) -> int: return self._preseed_errors + self.errors

    @property
    def _display_size_deleted(self) -> int: return self._preseed_deleted_size + self._size_deleted

    def _status_badge(self, icon: str, label: str, color: str, border: "str | None" = None) -> str:
        border = border or color
        return (f"<span style='display:inline-block; font-size:{self._status_fs}px; font-weight:bold; "
                f"font-family:monospace; color:{color};background:{self._t['bg2']}; border-left:5px solid {border}; "
                f"border-radius:7px;padding:6px 18px;'>{icon}&thinsp;{label}</span>")

    def _set_status_html(self, html_text: str) -> None:
        if html_text != self._last_status:
            self._last_status = html_text
            self._summary.set_status_html(html_text)

    def _release_lock(self) -> None:
        if self._owns_lock:
            self._owns_lock = False
            release_backup_lock()

    def _on_prepared(self, deleted: list, errors: list) -> None:
        self._seed_deleted(deleted)
        self._seed_errors(errors)
        if deleted or errors:
            self._update_tab_labels()

    def _on_confirm_delete(self, title: str, paths: list, request: _ConfirmRequest) -> None:
        try:
            shown = paths[:_CONFIRM_PREVIEW_MAX]
            preview = "\n".join(f"  \u2022  {apply_replacements(p)}" for p in shown)
            more = (tr("\n  \u2026and {n} more", n=len(paths) - len(shown))
                    if len(paths) > len(shown) else "")
            intro = tr("Mirror mode is about to delete {n} item(s) from the destination of "
                       "'{title}' because they no longer exist in the source:\n\nDelete these now?",
                       n=len(paths), title=title.replace("<br>", " "))
            request.answer = ask_yes_no_scrollable(
                self, tr("Confirm Mirror Delete"), intro, f"{preview}{more}",
                default_yes=False,
            ) == QMessageBox.StandardButton.Yes
        except Exception as exc:
            logger.error("Mirror delete confirmation failed: %s", exc, exc_info=True)
            request.answer = False
        finally:
            request.event.set()

    def _on_space_warnings(self, lines: list) -> None:
        if self._headless:
            for line in lines:
                logger.warning("Headless backup: low disk space — %s", line)
            return
        show_scrollable_message(
            self,
            tr("Low Disk Space"),
            tr("Warning: one or more destinations are running low on space:"),
            "\n".join(lines) + "\n\n" + tr("The backup will still proceed."),
        )

    def _step_title(self, step: str) -> str:
        if step == STEP_COPY:
            return tr("{op}…", op=self._operation)
        if step == STEP_SCAN:
            return tr("Scanning…")
        if step == STEP_COMPARE:
            return tr("Comparing destination…")
        if step == STEP_DELETE:
            return tr("Deleting…")
        if step == STEP_VERSIONS:
            return tr("Pruning old versions…")
        if step == STEP_HOOKS:
            return tr("Running hooks…")
        if step == STEP_CONFIRM:
            return tr("Waiting for confirmation…")
        if step == STEP_SYNC:
            return tr("Writing data to disk…")
        return tr("Preparing…")

    def _describe(self, view: ProgressView) -> "tuple[str, str, str, str, str, str]":
        t = self._t
        step = view.step
        title = self._step_title(step)
        pct = view.pct if view.pct is not None else 0
        if self._cancelling:
            cancelling = tr("⏹ Cancelling…")
            return cancelling, "⏹", cancelling, t["warning"], t["warning"], cancelling
        if step == STEP_COPY:
            if view.determinate:
                badge = tr("{op}… {pct}%  ({done:,} / {total:,} files)", op=self._operation, pct=pct,
                           done=view.files_done, total=view.files_total)
                bar = tr("{pct}%  —  {done:,} / {total:,} files", pct=pct, done=view.files_done, total=view.files_total)
            else:
                badge, bar = title, title
            return title, "📦", badge, t["accent"], t["accent"], bar
        if step == STEP_SCAN:
            if view.files_done > 0:
                badge = tr("Scanning… ({found:,} found, {done:,} processed)", found=view.files_total, done=view.files_done)
            else:
                badge = tr("Scanning… ({found:,} found)", found=view.files_total)
            return title, "🔍", badge, t["accent2"], t["accent2"], badge
        if step == STEP_COMPARE:
            badge = (tr("Comparing destination… ({n:,} checked)", n=view.step_done)
                     if view.step_done > 0 else title)
            return title, "🔍", badge, t["accent2"], t["accent2"], badge
        if step in (STEP_DELETE, STEP_VERSIONS):
            if view.step_total > 0:
                badge = tr("{name} ({done:,} / {total:,})", name=title, done=view.step_done, total=view.step_total)
                bar = tr("{name}  {pct}%  —  {done:,} / {total:,} items", name=title, pct=pct,
                         done=view.step_done, total=view.step_total)
            else:
                badge, bar = title, title
            return title, "🗑", badge, t["deleted"], t["deleted"], bar
        icon = "💾" if step == STEP_SYNC else "⏳"
        return title, icon, title, t["cyan"], t["accent"], title

    def _snapshot(self) -> Snapshot:
        return self.worker.meter.snapshot()

    def _absorb(self, snap: Snapshot) -> None:
        self.copied, self.skipped, self.errors, self.deleted = snap.copied, snap.skipped, snap.errors, snap.deleted
        self._size_copied = snap.size_copied
        self._size_skipped = snap.size_skipped
        self._size_deleted = snap.size_deleted

    def _render_progress(self, snap: "Snapshot | None" = None, *, finished: bool = False, cancelled: bool = False) -> None:
        if self.worker is None:
            return
        if snap is None:
            snap = self._snapshot()
        self._absorb(snap)
        view = self._model.update(snap, time.monotonic(), finished=finished, cancelled=cancelled)
        elapsed = self._elapsed_s()
        if finished:
            self._summary.update_stats(
                self._operation, view, self._final_title, self._final_bar_text(view, cancelled),
                self.copied, self.skipped, self._display_errors, self._display_deleted, elapsed,
                self._size_copied, self._size_skipped, self._display_size_deleted,
                self._size_total(snap), finished=True, cancelled=cancelled)
            return
        title, icon, badge, color, border, bar = self._describe(view)
        self._set_status_html(self._status_badge(icon, badge, color, border))
        self._summary.update_stats(
            self._operation, view, title, bar,
            self.copied, self.skipped, self._display_errors, self._display_deleted, elapsed,
            self._size_copied, self._size_skipped, self._display_size_deleted,
            self._size_total(snap))

    @staticmethod
    def _size_total(snap: Snapshot) -> int:
        return snap.bytes_total if snap.bytes_total > 0 else snap.size_copied + snap.size_skipped

    @staticmethod
    def _final_bar_text(view: ProgressView, cancelled: bool) -> str:
        if cancelled:
            if view.determinate and view.pct is not None:
                return tr("Cancelled — {pct}%  —  {done:,} / {total:,} files", pct=view.pct,
                          done=view.files_done, total=view.files_total)
            return tr("Cancelled")
        total = view.files_total
        return tr("{pct}%  —  {done:,} / {total:,} files", pct=100, done=total, total=total)

    @staticmethod
    def _enqueue(pending: deque, widget: "_LogWidget", items: list) -> None:
        if not items or widget.is_truncated:
            return
        room = widget.log_max + 1 - widget.item_count - len(pending)
        if room <= 0:
            return
        pending.extend(items if len(items) <= room else items[:room])

    def _drain_pending(self, budget_s: float) -> int:
        processed = 0
        share = budget_s / len(self._log_groups)
        for pending, widget, fmt in self._log_groups:
            if not pending:
                continue
            if widget.is_truncated:
                processed += len(pending)
                pending.clear()
                continue
            deadline = time.monotonic() + share
            while pending:
                n = min(_DRAIN_CHUNK, len(pending))
                batch = [pending.popleft() for _ in range(n)]
                widget.bulk_add([fmt(*args) for args in batch])
                processed += n
                if widget.is_truncated:
                    processed += len(pending)
                    pending.clear()
                    break
                if time.monotonic() >= deadline:
                    break
        return processed

    def _has_pending(self) -> bool:
        return any(group[0] for group in self._log_groups)

    def _refresh_visible_log(self) -> None:
        current = self.tabs.currentWidget()
        if isinstance(current, _LogWidget):
            current.refresh_live()

    def _on_tab_changed(self, _index: int) -> None:
        self._refresh_visible_log()

    def _update_ui_tick(self) -> None:
        if self._finished:
            return
        self._tick_count += 1
        self._drain_pending(_LIVE_DRAIN_BUDGET_S)
        self._render_progress()
        self._update_tab_labels()
        if self._tick_count % _LOG_REFRESH_EVERY == 0:
            self._refresh_visible_log()

    def _update_tab_labels(self) -> None:
        counts = (self.copied, self.skipped, self._display_deleted, self._display_errors)
        if counts == self._last_tab_counts:
            return
        self._last_tab_counts = counts
        self.tabs.setTabText(1, tr("\uf4bb  Copied ({n:,})", n=counts[0]))
        self.tabs.setTabText(2, tr("↷ Skipped ({n:,})", n=counts[1]))
        self.tabs.setTabText(3, tr("🗑 Deleted ({n:,})", n=counts[2]))
        self.tabs.setTabText(4, tr("✗ Errors ({n:,})", n=counts[3]))

    @staticmethod
    def _fmt_ok(s, d) -> str: return f"{apply_replacements(s)}\n{tr('Copied to')} \uf4bb \n{apply_replacements(d)}"

    @staticmethod
    def _fmt_sk(p, r) -> str: return f"{apply_replacements(p)} ↷ {r}"

    @staticmethod
    def _fmt_de(p, r) -> str: return f"{apply_replacements(p)} 🗑 {r}"

    @staticmethod
    def _fmt_er(p, m) -> str: return f"{apply_replacements(p)} ❌ {m}"

    def _on_batch(self, ok, sk, er, de) -> None:
        if self._headless:
            for s, r, _sz in sk:
                if _NF_MARK in r:
                    _r, _, nf_title = r.partition(_NF_MARK)
                    self._not_found_paths.append((apply_replacements(s), nf_title))
            return
        self._enqueue(self._pending_ok, self._w_copied, [(s, d) for s, d, _sz in ok])
        skipped_items = []
        for s, r, _sz in sk:
            if _NF_MARK in r:
                r, _, nf_title = r.partition(_NF_MARK)
                self._not_found_paths.append((apply_replacements(s), nf_title))
            skipped_items.append((s, r))
        self._enqueue(self._pending_sk, self._w_skipped, skipped_items)
        self._enqueue(self._pending_de, self._w_deleted, [(p, r) for p, r, _sz in de])
        self._enqueue(self._pending_er, self._w_errors, [(s, m) for s, m, _sz in er])

    def _on_done(self, c, s, e, d, cancelled) -> None:
        self._tick.stop()
        CopyDialog.active_workers.discard(self.worker)
        self._finished = True
        elapsed = self._final_elapsed = self.timer.elapsed() // 1000
        self._release_lock()

        snap = self._snapshot()
        self._absorb(snap)
        self.copied, self.skipped, self.errors, self.deleted = c, s, e, d

        try:
            from history import append_history
            append_history(operation=self._operation, copied=c, skipped=s, errors=self._display_errors,
                           deleted=self._display_deleted, duration_s=elapsed, cancelled=cancelled,
                           op_kind=self._op_kind)
        except Exception as exc:
            logger.debug("append_history failed: %s", exc)

        tstr = format_duration(elapsed)

        if cancelled:
            icon, label, col = "⏹", tr("Cancelled after {t}", t=tstr), self.c_sk
        elif self._display_errors > 0:
            icon, label, col = "⚠", tr("Done with errors ✗ — {t}", t=tstr), self.c_er
        else:
            icon, label, col = "✓", tr("Done — {t}", t=tstr), self.c_ok

        self._final_title = tr("Cancelled") if cancelled else tr("Done")
        self._set_status_html(self._status_badge(icon, label, col))
        self._render_progress(snap, finished=True, cancelled=cancelled)
        self._update_tab_labels()

        if self._cancel_connected and self.worker is not None:
            try:
                self.cancel_btn.clicked.disconnect(self._request_cancel)
            except (RuntimeError, TypeError):
                pass
            self._cancel_connected = False

        if self._accept_connected:
            try:
                self.cancel_btn.clicked.disconnect(self.accept)
            except (RuntimeError, TypeError):
                pass

        self.cancel_btn.setEnabled(True)
        self.cancel_btn.clicked.connect(self.accept)
        self.cancel_btn.setText(tr("Close"))
        self._accept_connected = True

        if not self.isActiveWindow() and not cancelled:
            disp_err = self._display_errors
            disp_del = self._display_deleted
            del_part = tr(", {n} deleted", n=disp_del) if disp_del else ""
            files_word = tr("files") if c != 1 else tr("file")
            errors_word = tr("errors") if disp_err != 1 else tr("error")
            if disp_err > 0:
                _notify(
                    tr("{op} completed with errors", op=self._operation),
                    tr("{c} {files} copied, {s} skipped{deleted}, {e} {errors}",
                       c=c, files=files_word, s=s, deleted=del_part, e=disp_err, errors=errors_word),
                    urgency="critical",
                )
            else:
                _notify(
                    tr("{op} successfully completed", op=self._operation),
                    tr("{c} {files} copied, {s} skipped{deleted}",
                       c=c, files=files_word, s=s, deleted=del_part),
                    urgency="normal",
                )

        if self._headless:
            for p, t_ in self._not_found_paths:
                logger.warning("Headless backup: configured path not found: %s%s", p, f" ({t_})" if t_ else "")
            QTimer.singleShot(0, self._finish_headless)
            return

        self._draining = True
        self._tick.timeout.disconnect(self._update_ui_tick)
        self._tick.timeout.connect(self._drain_tick)
        self._tick.start(_DRAIN_TICK_MS)

        if self._not_found_paths and not cancelled:
            n = len(self._not_found_paths)
            paths_text = "\n".join(
                f"  • {p} ({t})" if t else f"  • {p}"
                for p, t in self._not_found_paths
            )
            path_word = tr("path was") if n == 1 else tr("paths were")
            intro = tr("{n} configured {word} not found and skipped. "
                       "Please check these entries in your backup profile:",
                       n=n, word=path_word)

            def _show_popup(_intro: str = intro, _paths: str = paths_text) -> None:
                show_scrollable_message(
                    self, tr("Missing Paths Detected"), _intro, _paths,
                )

            QTimer.singleShot(0, _show_popup)

    def _drain_tick(self) -> None:
        self._drain_pending(_FINAL_DRAIN_BUDGET_S)
        if self._has_pending():
            return
        self._tick.stop()
        self._draining = False
        for w in (self._w_copied, self._w_skipped, self._w_deleted, self._w_errors):
            w.flush_final()

    def _finish_headless(self) -> None:
        if self.worker is not None:
            self.worker.wait(5000)
        self.accept()

    def closeEvent(self, a0: QCloseEvent | None) -> None:
        if self.worker is None:
            super().closeEvent(a0)
            return
        if self._finished:
            self.worker.wait(5000)
            self._release_lock()
            super().closeEvent(a0)
        elif self.worker.isRunning():
            self._request_cancel()
            if a0 is not None:
                a0.ignore()
        else:
            self._release_lock()
            super().closeEvent(a0)

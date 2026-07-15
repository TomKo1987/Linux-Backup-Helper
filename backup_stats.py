from datetime import datetime, timedelta

from PyQt6.QtCore import Qt, QPointF
from PyQt6.QtGui import (
    QColor, QPainter, QPainterPath, QPen, QBrush, QFont, QFontMetrics,
    QLinearGradient,
)
from PyQt6.QtWidgets import (
    QComboBox, QDialog, QFrame, QHBoxLayout, QLabel,
    QSizePolicy, QVBoxLayout, QWidget,
)

from history import load_history as _load_all_history, _fmt_duration as _fmt_dur
from state import S
from themes import current_theme, font_sz, register_style_listener, unregister_style_listener
from ui_utils import _StandardKeysMixin, build_dialog_shell, clear_layout, sep, size_to_screen


def _parse_ts(ts: str) -> datetime | None:
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(ts, fmt)
        except ValueError:
            pass
    return None


class _StatCard(QFrame):
    def __init__(self, icon: str, value: str, label: str, color: str, parent=None) -> None:
        super().__init__(parent)
        t = current_theme()
        self.setStyleSheet(
            f"background:{t['bg3']};border-radius:8px;"
            f"border-left:4px solid {color};"
        )
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 10, 14, 10)
        lay.setSpacing(2)

        top = QHBoxLayout()
        ico = QLabel(icon)
        ico.setStyleSheet(f"font-size:{font_sz(5)}px;background:transparent;border:none;color:{color};")
        top.addWidget(ico)
        top.addStretch()
        lay.addLayout(top)

        val_lbl = QLabel(value)
        val_lbl.setStyleSheet(
            f"font-size:{font_sz(6)}px;font-weight:bold;"
            f"color:{t['text']};background:transparent;border:none;"
        )
        lay.addWidget(val_lbl)

        name_lbl = QLabel(label)
        name_lbl.setStyleSheet(
            f"font-size:{font_sz(-1)}px;color:{t['text_dim']};"
            f"background:transparent;border:none;"
        )
        lay.addWidget(name_lbl)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)


class _BarChart(QWidget):
    def __init__(
        self,
        title: str,
        bars: list[tuple[str, float, str]],
        fmt_fn=None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._title  = title
        self._bars   = bars
        self._fmt    = fmt_fn or (lambda v: f"{v:.0f}")
        self.setMinimumHeight(220)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def paintEvent(self, _event) -> None:
        if not self._bars:
            return
        t     = current_theme()
        p     = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        w, h  = self.width(), self.height()
        pad_l, pad_r, pad_t, pad_b = 56, 16, 36, 52
        chart_w = w - pad_l - pad_r
        chart_h = h - pad_t - pad_b

        title_font = QFont()
        title_font.setPixelSize(font_sz(1))
        title_font.setBold(True)
        p.setFont(title_font)
        p.setPen(QColor(t["accent"]))
        p.drawText(pad_l, 0, chart_w, pad_t, Qt.AlignmentFlag.AlignCenter, self._title)

        max_val = max((v for _, v, _ in self._bars), default=1) or 1

        grid_pen = QPen(QColor(t["header_sep"]))
        grid_pen.setWidth(1)
        p.setPen(grid_pen)
        for i in range(5):
            y = pad_t + chart_h - int(chart_h * i / 4)
            p.drawLine(pad_l, y, pad_l + chart_w, y)

        small_font = QFont()
        small_font.setPixelSize(font_sz(-2))
        p.setFont(small_font)
        p.setPen(QColor(t["text_dim"]))
        for i in range(5):
            val  = max_val * i / 4
            y    = pad_t + chart_h - int(chart_h * i / 4)
            p.drawText(0, y - 10, pad_l - 4, 20, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                       self._fmt(val))

        n         = len(self._bars)
        gap       = max(4, chart_w // (n * 8))
        bar_w     = max(8, (chart_w - gap * (n + 1)) // n)

        for i, (lbl, val, color) in enumerate(self._bars):
            x    = pad_l + gap + i * (bar_w + gap)
            bh   = max(2, int(chart_h * val / max_val))
            y    = pad_t + chart_h - bh

            grad = QLinearGradient(x, y, x, pad_t + chart_h)
            grad.setColorAt(0.0, QColor(color))
            grad.setColorAt(1.0, QColor(color + "44"))
            p.setBrush(QBrush(grad))
            p.setPen(Qt.PenStyle.NoPen)
            p.drawRoundedRect(x, y, bar_w, bh, 3, 3)

            p.setPen(QColor(t["text"]))
            p.setFont(small_font)
            p.drawText(x, y - 18, bar_w, 18, Qt.AlignmentFlag.AlignCenter, self._fmt(val))

            p.setPen(QColor(t["text_dim"]))
            fm = QFontMetrics(small_font)
            short = lbl if fm.horizontalAdvance(lbl) <= bar_w + gap else lbl[-5:]
            p.drawText(x - gap // 2, pad_t + chart_h + 4, bar_w + gap, pad_b - 8,
                       Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop, short)

        p.end()


class _Sparkline(QWidget):
    def __init__(
        self,
        title: str,
        points: list[tuple[datetime, float]],
        color: str,
        fmt_fn=None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._title  = title
        self._points = points
        self._color  = color
        self._fmt    = fmt_fn or (lambda v: f"{v:.0f}")
        self.setMinimumHeight(180)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def paintEvent(self, _event) -> None:
        if len(self._points) < 2:
            return
        t    = current_theme()
        p    = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        w, h  = self.width(), self.height()
        pl, pr, pt, pb = 56, 16, 36, 36
        cw, ch = w - pl - pr, h - pt - pb

        title_font = QFont()
        title_font.setPixelSize(font_sz(1))
        title_font.setBold(True)
        p.setFont(title_font)
        p.setPen(QColor(t["accent"]))
        p.drawText(pl, 0, cw, pt, Qt.AlignmentFlag.AlignCenter, self._title)

        timestamps = [pt_.timestamp() for pt_, _ in self._points]
        values     = [v for _, v in self._points]
        t_min, t_max = min(timestamps), max(timestamps)
        v_min, v_max = 0.0, max(values) or 1.0
        t_span = max(t_max - t_min, 1)

        def _px(_i: int) -> QPointF:
            x = pl + (timestamps[_i] - t_min) / t_span * cw
            _y = pt + ch - (values[_i] - v_min) / (v_max - v_min) * ch
            return QPointF(x, _y)

        path = QPainterPath()
        path.moveTo(pl, pt + ch)
        for i in range(len(self._points)):
            path.lineTo(_px(i))
        path.lineTo(pl + cw, pt + ch)
        path.closeSubpath()
        grad = QLinearGradient(0, pt, 0, pt + ch)
        grad.setColorAt(0.0, QColor(self._color + "88"))
        grad.setColorAt(1.0, QColor(self._color + "00"))
        p.setBrush(QBrush(grad))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawPath(path)

        line_pen = QPen(QColor(self._color))
        line_pen.setWidth(2)
        p.setPen(line_pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        for i in range(1, len(self._points)):
            p.drawLine(_px(i - 1), _px(i))

        small = QFont()
        small.setPixelSize(font_sz(-2))
        p.setFont(small)
        p.setPen(QColor(t["text_dim"]))
        for i in range(5):
            v  = v_max * i / 4
            y  = pt + ch - int(ch * i / 4)
            p.drawText(0, y - 10, pl - 4, 20,
                       Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, self._fmt(v))

        if self._points:
            p.drawText(pl, pt + ch + 4, 80, 28, Qt.AlignmentFlag.AlignLeft,
                       self._points[0][0].strftime("%m-%d"))
            p.drawText(pl + cw - 80, pt + ch + 4, 80, 28, Qt.AlignmentFlag.AlignRight,
                       self._points[-1][0].strftime("%m-%d"))
        p.end()


class BackupStatsDialog(_StandardKeysMixin, QDialog):
    _RANGES = {
        "Last 7 days":  7,
        "Last 30 days": 30,
        "Last 90 days": 90,
        "All time":     0,
    }

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Backup Statistics")
        size_to_screen(self, 1500, 1000)
        self._history: list[dict] = []
        self._build_shell()
        self._reload()
        register_style_listener(self._reload)

    def closeEvent(self, event) -> None:
        unregister_style_listener(self._reload)
        super().closeEvent(event)

    def _build_shell(self) -> None:
        t = current_theme()

        range_lbl = QLabel("Range:")
        self._range_combo = QComboBox()
        self._range_combo.addItems(list(self._RANGES.keys()))
        self._range_combo.setCurrentIndex(1)
        self._range_combo.currentIndexChanged.connect(self._reload)

        _, self._body_lay, _ = build_dialog_shell(
            self, t, font_sz, "Backup Statistics", "📊",
            header_extra=[range_lbl, self._range_combo],
        )
        self._body_lay.setSpacing(20)

    def _reload(self) -> None:
        self._history = _load_all_history(S.profile_name or "")
        clear_layout(self._body_lay)

        days = self._RANGES[self._range_combo.currentText()]
        now  = datetime.now()
        if days:
            cutoff = now - timedelta(days=days)
            entries = [
                e for e in self._history
                if (ts := _parse_ts(e.get("timestamp", ""))) and ts >= cutoff
            ]
        else:
            entries = list(self._history)

        if not entries:
            lbl = QLabel("No backup history available for the selected range.")
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lbl.setStyleSheet(f"color:{current_theme()['text_dim']};font-size:{font_sz(1)}px;")
            self._body_lay.addWidget(lbl)
            return

        self._build_stats(entries)

    def _build_stats(self, entries: list[dict]) -> None:
        t = current_theme()

        total       = len(entries)
        successful  = sum(1 for e in entries if not e.get("errors") and not e.get("cancelled"))
        failed      = sum(1 for e in entries if e.get("errors"))
        cancelled   = sum(1 for e in entries if e.get("cancelled"))
        total_copied = sum(e.get("copied", 0) for e in entries)
        total_skip   = sum(e.get("skipped", 0) for e in entries)
        avg_dur      = (sum(e.get("duration_s", 0) for e in entries) // max(total, 1))

        cards_w = QWidget()
        cards_l = QHBoxLayout(cards_w)
        cards_l.setSpacing(12)
        cards_l.setContentsMargins(0, 0, 0, 0)
        for icon, val, label, col in [
            ("🗂", str(total),             "Total Runs",       t["accent"]),
            ("✅", str(successful),         "Successful",       t["success"]),
            ("⚠️", str(failed + cancelled), "Failed/Cancelled", t["warning"] if failed == 0 else t["error"]),
            ("📁", f"{total_copied:,}", "Files Copied",    t["info"]),
            ("⏭", str(total_skip),          "Files Skipped",   t["accent2"]),
            ("⏱",  _fmt_dur(avg_dur),        "Avg Duration",    t["accent2"]),
        ]:
            cards_l.addWidget(_StatCard(icon, val, label, col))
        self._body_lay.addWidget(cards_w)

        self._body_lay.addWidget(sep())

        by_day: dict[str, dict] = {}
        for e in entries:
            ts = _parse_ts(e.get("timestamp", ""))
            if not ts:
                continue
            key = ts.strftime("%Y-%m-%d")
            if key not in by_day:
                by_day[key] = {"copied": 0, "runs": 0, "errors": 0}
            by_day[key]["copied"] += e.get("copied", 0)
            by_day[key]["runs"]   += 1
            by_day[key]["errors"] += 1 if e.get("errors") else 0

        if by_day:
            sorted_days = sorted(by_day.keys())
            tl_points: list[tuple[datetime, float]] = []
            for d in sorted_days:
                dt = datetime.strptime(d, "%Y-%m-%d")
                tl_points.append((dt, by_day[d]["copied"]))

            spark = _Sparkline(
                "Files Copied per Day",
                tl_points,
                t["accent"],
                fmt_fn=lambda v: f"{int(v):,}",
            )
            self._body_lay.addWidget(spark)
            self._body_lay.addWidget(sep())

        op_counts: dict[str, int] = {}
        for e in entries:
            op = e.get("operation", "Unknown")
            op_counts[op] = op_counts.get(op, 0) + 1
        if op_counts:
            colors = [t["accent"], t["accent2"], t["success"], t["info"]]
            bars = [
                (op, float(cnt), colors[i % len(colors)])
                for i, (op, cnt) in enumerate(sorted(op_counts.items(), key=lambda x: -x[1]))
            ]
            chart = _BarChart("Runs by Operation", bars, fmt_fn=lambda v: str(int(v)))
            self._body_lay.addWidget(chart)
            self._body_lay.addWidget(sep())

        if by_day and len(by_day) >= 3:
            err_bars = [
                (d[-5:],
                 100.0 * by_day[d]["errors"] / max(by_day[d]["runs"], 1),
                 t["error"] if by_day[d]["errors"] else t["success"])
                for d in sorted(by_day.keys())[-14:]
            ]
            err_chart = _BarChart(
                "Error Rate % per Day (last 14 days)",
                err_bars,
                fmt_fn=lambda v: f"{v:.0f}%",
            )
            self._body_lay.addWidget(err_chart)
            self._body_lay.addWidget(sep())

        tbl_hdr = QLabel("🕑  Recent Runs")
        tbl_hdr.setStyleSheet(
            f"font-size:{font_sz(2)}px;font-weight:bold;"
            f"color:{t['text']};background:transparent;border:none;"
        )
        self._body_lay.addWidget(tbl_hdr)

        for e in reversed(entries[-20:]):
            row = self._make_row(e, t)
            self._body_lay.addWidget(row)

        self._body_lay.addStretch()

    @staticmethod
    def _make_row(e: dict, t: dict) -> QFrame:
        frame = QFrame()
        frame.setStyleSheet(
            f"background:{t['bg2']};border-radius:6px;"
            f"border:1px solid {t['bg3']};"
        )
        hl = QHBoxLayout(frame)
        hl.setContentsMargins(10, 6, 10, 6)
        hl.setSpacing(16)

        ts      = e.get("timestamp", "?")
        op      = e.get("operation", "?")
        copied  = e.get("copied", 0)
        skipped = e.get("skipped", 0)
        errors  = e.get("errors", 0)
        dur     = e.get("duration_s", 0)
        cancel  = e.get("cancelled", False)

        if cancel:
            icon, color = "⏹", t["warning"]
        elif errors:
            icon, color = "⚠", t["error"]
        else:
            icon, color = "✓", t["success"]

        def _lbl(txt: str, col: str = "", bold: bool = False) -> QLabel:
            lbl = QLabel(txt)
            c   = col or t["text"]
            w   = "bold;" if bold else ""
            lbl.setStyleSheet(
                f"color:{c};font-size:{font_sz(-1)}px;{w}"
                f"background:transparent;border:none;"
            )
            return lbl

        hl.addWidget(_lbl(f"{icon} {ts}", color, bold=True))
        hl.addWidget(_lbl(op, t["text_dim"]))
        hl.addStretch()
        hl.addWidget(_lbl(f"📁 {copied:,}",  t["success"] if copied else t["text_dim"]))
        hl.addWidget(_lbl(f"↷ {skipped:,}", t["text_dim"]))
        if errors:
            hl.addWidget(_lbl(f"✗ {errors}", t["error"]))
        hl.addWidget(_lbl(f"⏱ {_fmt_dur(dur)}", t["text_dim"]))
        return frame

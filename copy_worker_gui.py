import html
import re
import threading
from collections import deque
from dataclasses import dataclass
from itertools import islice

from PyQt6.QtCore import Qt, QElapsedTimer, QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QProgressBar, QPushButton, QScrollArea, QTabWidget, QVBoxLayout, QApplication, QWidget, QSpinBox,
    QDialog, QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QSizePolicy, QTextEdit,
)

from state import apply_replacements, logger
from themes import current_theme, font_sz
from ui_utils import _StandardKeysMixin, size_to_screen

from copy_worker_core import _check_destination_space, _format_unit, _cached_mono_style, _notify
from copy_worker import CopyWorker


@dataclass
class _StatCard:
    frame:    QFrame
    val_lbl:  QLabel
    size_lbl: QLabel

    def set_val(self,  text: str) -> None: self.val_lbl.setText(text)
    def set_size(self, text: str) -> None: self.size_lbl.setText(text)


def _make_stat_card(color: "str | None", title: str, val: str = "0", size_title: int = 0, size_val: int = 0, bold_val: bool = True) -> _StatCard:
    t       = current_theme()
    s_title = size_title or font_sz(3)
    s_val   = size_val   or font_sz(16)

    frame = QFrame()
    frame.setMinimumWidth(240)
    border = f"border-left:4px solid {color};" if color else ""
    frame.setStyleSheet(f"QFrame {{background:{t['bg3']}; border-radius:8px; {border}}}")

    inner = QVBoxLayout(frame)
    inner.setContentsMargins(16, 14, 16, 14)

    title_lbl = QLabel(title)
    title_lbl.setStyleSheet(_cached_mono_style(s_title, t["text_dim"], extra="border:none;"))

    val_lbl = QLabel(val)
    val_lbl.setStyleSheet(_cached_mono_style(s_val, color or t["text"], bold=bold_val, extra="border:none;"))
    val_lbl.setMinimumWidth(225 if color else 250)
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
        size_lbl.setMinimumWidth(200)
        size_lbl.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignBottom)
        val_row.addWidget(size_lbl, 0, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignBottom)

    val_row.addStretch()
    inner.addLayout(val_row)

    return _StatCard(frame=frame, val_lbl=val_lbl, size_lbl=size_lbl)


class _SummaryWidget(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self._entry_refresh_pending = False
        self._t = current_theme()
        t = self._t

        self._s_ok    = f"color:{t['success']};"
        self._s_skip  = f"color:{t['warning']};"
        self._s_err   = f"color:{t['error']};"
        self._s_dim   = f"color:{t['text_dim']};"
        self._s_title = f"color:{t['text']};"
        self._s_entry = _cached_mono_style(font_sz(-2), t["text"], extra="border:none; padding:2px 0px;")

        self._entry_results:    dict[str, list[int]] = {}
        self._entry_row_labels: dict[str, QLabel]    = {}
        self._entry_grid_cols = 1
        self._last_seg_counts: tuple[int, int, int] = (0, 0, 0)

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
        self.total_lbl.setStyleSheet("font-size: 20px; border:none; background:transparent; padding:0px 0px")
        self.total_lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        hdr.addWidget(self.op_lbl, 0, 0, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        hdr.addWidget(self._status_center_lbl, 0, 1, Qt.AlignmentFlag.AlignCenter | Qt.AlignmentFlag.AlignVCenter)
        hdr.addWidget(self.total_lbl, 0, 2, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        stats_lay = QGridLayout()
        stats_lay.setSpacing(12)
        for i in range(3):
            stats_lay.setColumnStretch(i, 1)

        self.card_copied  = _make_stat_card(t["success"], "⤵ Copied",  "0")
        self.card_skipped = _make_stat_card(t["warning"],  "↷ Skipped", "0")
        self.card_errors  = _make_stat_card(t["error"],    "✗ Errors",  "0")

        for col, card in enumerate((self.card_copied, self.card_skipped, self.card_errors)):
            stats_lay.addWidget(card.frame, 0, col)

        self._progress_card = QFrame()
        self._progress_card.setObjectName("progressCard")
        self._progress_card.setStyleSheet(f"QFrame{{background:{t['bg3']}; border-radius:8px;}}")

        prog_lay = QVBoxLayout(self._progress_card)
        prog_lay.setContentsMargins(20, 14, 20, 14)
        prog_lay.setSpacing(8)

        prog_hdr = QHBoxLayout()
        _w = QLabel("Progress")
        _w.setStyleSheet(_cached_mono_style(font_sz(2), t["text_dim"], extra="border:none;"))
        prog_hdr.addWidget(_w)
        prog_hdr.addStretch()

        self._prog_pct = QLabel("0%")
        self._prog_pct.setFixedWidth(60)
        self._prog_pct.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self._prog_pct.setStyleSheet(_cached_mono_style(font_sz(2), t["text"], bold=True, extra="border:none;"))
        prog_hdr.addWidget(self._prog_pct)

        self._progress_bar = QProgressBar()
        self._progress_bar.setRange(0, 0)
        self._progress_bar.setTextVisible(True)
        self._progress_bar.setFormat("0%  —  0 / 0 files")
        self._progress_bar.setMinimumHeight(30)

        prog_lay.addLayout(prog_hdr)
        prog_lay.addWidget(self._progress_bar)

        metrics_lay = QHBoxLayout()
        metrics_lay.setSpacing(12)

        kw = {"size_title": font_sz(2), "size_val": font_sz(10)}
        self._card_elapsed = _make_stat_card(None, "⏲️ Elapsed", "--:--", **kw)
        self._card_speed   = _make_stat_card(None, "🚤 Speed",   "---",   **kw)
        self._card_eta     = _make_stat_card(None, "🏁 ETA",     "--:--", **kw)

        for card in (self._card_elapsed, self._card_speed, self._card_eta):
            metrics_lay.addWidget(card.frame)

        self._rate_card = QFrame()
        self._rate_card.setObjectName("rateCard")
        self._rate_card.setStyleSheet(f"QFrame{{background:{t['bg3']}; border-radius:8px;}}")

        rate_lay = QVBoxLayout(self._rate_card)
        rate_lay.setContentsMargins(20, 14, 20, 14)
        rate_lay.setSpacing(8)

        bd_lbl = self._lbl("File breakdown", _cached_mono_style(font_sz(2), t["text_dim"], extra="border:none;"))
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
        self._seg_errors  = QFrame()
        self._seg_copied.setStyleSheet(f"background:{t['success']};")
        self._seg_skipped.setStyleSheet(f"background:{t['warning']};")
        self._seg_errors.setStyleSheet(f"background:{t['error']};")

        for seg in (self._seg_copied, self._seg_skipped, self._seg_errors):
            seg.setFixedHeight(10)
            seg.setFixedWidth(0)
            seg_row.addWidget(seg)
        seg_row.addStretch(1)

        legend_row = QHBoxLayout()
        legend_row.setSpacing(20)
        legend_style = _cached_mono_style(font_sz(), t["text"], extra="border:none;")
        for key, text in (("success", "Copied"), ("warning", "Skipped"), ("error", "Errors")):
            dot = QLabel(f"<span style='color:{t[key]}'>■</span>  {text}")
            dot.setStyleSheet(legend_style)
            dot.setMinimumHeight(22)
            legend_row.addWidget(dot)
        legend_row.addStretch()

        rate_lay.addWidget(self._seg_track)
        rate_lay.addLayout(legend_row)

        self._entry_card = QFrame()
        self._entry_card.setObjectName("entryCard")
        self._entry_card.setStyleSheet(f"QFrame{{background:{t['bg3']}; border-radius:8px;}}")

        entry_lay = QVBoxLayout(self._entry_card)
        entry_lay.setContentsMargins(15, 10, 15, 10)
        entry_lay.setSpacing(5)
        entry_lay.addWidget(self._lbl("Entries processed", _cached_mono_style(font_sz(2), t["text_dim"], extra="border:none;")))

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

    def update_progress_bar(self, done: int, total: int) -> None: self._update_progress(done, total)

    def update_stats(self, operation: str, done: int, total: int, copied: int, skipped: int, errors: int, elapsed_s: int,
                     size_copied: int, size_skipped: int, finished: bool = False, cancelled: bool = False) -> None:
        self.op_lbl.setText(operation)
        self.card_copied.set_val(f"{copied:,}")
        self.card_copied.set_size(_format_unit(size_copied))
        self.card_skipped.set_val(f"{skipped:,}")
        self.card_skipped.set_size(_format_unit(size_skipped))
        self.card_errors.set_val(f"{errors:,}")
        size_str = _format_unit(size_copied + size_skipped)
        self.total_lbl.setText(f"{total:,} files / {size_str}" if total > 0 else "")
        self._update_progress(done, total, finished, cancelled)
        self._update_segments(copied, skipped, errors)
        self._update_timing(elapsed_s, done, total, finished, cancelled, size_copied=size_copied)

    def on_entry_status(self, title: str, ok: int, skip: int, err: int) -> None:
        ec = self._entry_results.setdefault(title, [0, 0, 0])
        ec[0] += ok
        ec[1] += skip
        ec[2] += err
        if not self._entry_refresh_pending:
            self._entry_refresh_pending = True
            QTimer.singleShot(300, self._deferred_refresh_entry_labels)

    def _deferred_refresh_entry_labels(self) -> None:
        self._entry_refresh_pending = False
        self._refresh_entry_labels()

    def _update_progress(self, done: int, total: int, finished: bool = False, cancelled: bool = False) -> None:
        if finished and cancelled:
            self._progress_bar.setRange(0, 1)
            self._progress_bar.setValue(0)
            self._prog_pct.setText("—")
            self._progress_bar.setFormat("Cancelled")
            return
        if total > 0:
            pct = int(done * 100 / total)
            if self._progress_bar.maximum() != total:
                self._progress_bar.setRange(0, total)
            self._progress_bar.setValue(done)
            self._prog_pct.setText(f"{pct}%")
            self._progress_bar.setFormat(f"{pct}%  —  {done:,} / {total:,} files")
        else:
            if finished:
                self._progress_bar.setRange(0, 1)
                self._progress_bar.setValue(1)
                self._prog_pct.setText("100%")
                self._progress_bar.setFormat("100%  —  0 / 0 files")
            else:
                self._progress_bar.setRange(0, 0)
                self._progress_bar.setValue(0)
                self._prog_pct.setText("…")
                self._progress_bar.setFormat("Scanning…")

    def _update_segments(self, copied: int, skipped: int, errors: int) -> None:
        self._last_seg_counts = (copied, skipped, errors)
        segs  = (self._seg_copied, self._seg_skipped, self._seg_errors)
        total = copied + skipped + errors
        if total == 0:
            for s in segs:
                s.setFixedWidth(0)
            return
        avail = max(1, self._rate_card.width() - 40)
        for seg, count in zip(segs, (copied, skipped, errors), strict=True):
            seg.setFixedWidth(max(0, int(avail * count / total)))

    def _update_timing(self, elapsed_s: int, done: int, total: int, finished: bool, cancelled: bool,
                       size_copied: int = 0) -> None:
        mins, secs = divmod(elapsed_s, 60)
        speed_str = "---"
        eta_str = "--:--"
        if finished:
            eta_str = "Cancelled" if cancelled else "Done"
        if elapsed_s > 0 and done > 0:
            rate = done / elapsed_s
            if size_copied > 0:
                mb_rate = size_copied / elapsed_s / (1024 * 1024)
                speed_str = f"{mb_rate:.1f} MB/s"
            else:
                speed_str = (f"{rate:,.1f} files/s" if rate >= 1 else f"1 file/{1 / rate:.1f}s")
            if not finished and total > done:
                eta_s = int((total - done) / rate)
                eta_str = f"{eta_s // 60:02d}:{eta_s % 60:02d}"
        self._card_elapsed.set_val(f"{mins:02d}:{secs:02d}")
        self._card_speed.set_val(speed_str)
        self._card_eta.set_val(eta_str)

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

        for title, (ok, skip, err) in results.items():
            parts = []
            if ok:   parts.append(f"<span style='{self._s_ok}'>⤵ {ok:,}</span>")
            if skip: parts.append(f"<span style='{self._s_skip}'>↷ {skip:,}</span>")
            if err:  parts.append(f"<span style='{self._s_err}'>✗ {err:,}</span>")

            suffix = "&nbsp; ".join(parts) if parts else f"<span style='{self._s_dim}'></span>"
            _html  = f"<span style='{self._s_title}'>{title}</span><br>{suffix}"

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

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._update_segments(*self._last_seg_counts)
        if not self._entry_refresh_pending:
            self._entry_refresh_pending = True
            QTimer.singleShot(150, self._deferred_refresh_entry_labels)


class _LogWidget(QWidget):
    _PAGE    = 500
    _LOG_MAX = 150_000
    _NATURAL_SORT_RE = re.compile(r"(\d+)")
    _sorted_ready = pyqtSignal(list, list)

    def __init__(self, color: str) -> None:
        super().__init__()
        t = current_theme()
        self._items:       list[str] = []
        self._items_lower: list[str] = []
        self._filtered:    list[str] = []
        self._page          = 0
        self._finalized     = False
        self._truncated     = False
        self._last_rendered = ""
        self._search_cache: dict[str, list[str]] = {}
        self._sorted_ready.connect(self._apply_sorted)

        style_view   = (f"font-family:monospace; font-size:{font_sz(-1)}px; "
                        f"color:{color}; background:transparent; border:none;")
        style_search = (f"QLineEdit {{ background:{t['bg3']}; border:1px solid {t['header_sep']}; "
                        f"border-radius:6px; padding:0 10px; color:{t['text']}; }}")
        style_spin   = (f"QSpinBox{{border:1px solid {t['header_sep']}; border-radius:4px; "
                        f"padding:2px 5px; background:{t['bg3']}; color:{t['text']}; font-weight:bold}}"
                        f"QSpinBox:focus{{border:1px solid {t['accent']}; background:{t['bg2']}}}")
        style_muted  = f"color:{t['muted']}; font-size:{font_sz()}px; margin-left:10px;"
        style_btn = (f"QPushButton {{ background:{t['bg3']}; border:1px solid {t['header_sep']}; "
                     f"border-radius:4px; padding:2px 8px; color:{t['text']}; }}"
                     f"QPushButton:hover {{ background:{t['bg2']}; border-color:{t['accent']}; color:{t['highlight']}; }}"
                     f"QPushButton:focus {{ border-color:{t['accent']}; color:{t['highlight']}; outline:none; }}"
                     f"QPushButton:pressed {{ background:{t['bg']}; border-color:{t['accent2']}; color:{t['accent2']}; }}")

        self._search = QLineEdit()
        self._search.setPlaceholderText(" 🔍  Search…")
        self._search.setClearButtonEnabled(True)
        self._search.textChanged.connect(self._on_search)
        self._search.setMinimumHeight(44)
        self._search.setStyleSheet(style_search)

        self._view = QTextEdit()
        self._view.setReadOnly(True)
        self._view.setStyleSheet(style_view)

        self._first = QPushButton("««")
        self._prev  = QPushButton("‹ Prev")
        self._next  = QPushButton("Next ›")
        self._last  = QPushButton("»»")

        for btn, cb in ((self._first, lambda: self._go(0)), (self._prev,  lambda: self._go(self._page - 1)),
                        (self._next,  lambda: self._go(self._page + 1)), (self._last,  lambda: self._go(self._pages() - 1))):
            btn.clicked.connect(cb)
            btn.setMinimumHeight(28)
            btn.setStyleSheet(style_btn)

        self._spin = QSpinBox()
        self._spin.setMinimum(1)
        self._spin.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)
        self._spin.setStyleSheet(style_spin)
        self._spin.setMinimumHeight(28)
        self._spin.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._spin.editingFinished.connect(self._spin_changed)

        self._page_lbl  = QLabel("")
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
        for w in (QLabel("Page"), self._spin, QLabel("of"), self._page_lbl):
            w.setMinimumHeight(28)
            pg.addWidget(w)

        nav.addLayout(pg)
        nav.addStretch(1)
        nav.addWidget(self._total_lbl)

        self._copy_vis_btn = QPushButton("📋 Copy Visible")
        self._copy_vis_btn.setMinimumHeight(28)
        self._copy_vis_btn.setStyleSheet(style_btn)
        self._copy_vis_btn.setToolTip("Copy visible entries on this page to the clipboard.")
        self._copy_vis_btn.clicked.connect(lambda: self._copy_to_clipboard(copy_all=False))

        self._copy_all_btn = QPushButton("📋 Copy All")
        self._copy_all_btn.setMinimumHeight(28)
        self._copy_all_btn.setStyleSheet(style_btn)
        self._copy_all_btn.setToolTip("Copy all entries to the clipboard.")
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
        active_btn.setText("✓ Copied!")
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
        target = self._spin.value() - 1
        if target != self._page:
            self._go(target)

    def _render(self) -> None:
        pages = self._pages()
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
            self._view.setHtml(new_html)
            self._last_rendered = new_html

        total = len(self._filtered)
        self._page_lbl.setText(f"<b>{pages}</b>")
        self._total_lbl.setText(f"({total:,} {'entry' if total == 1 else 'entries'})")

        self._spin.blockSignals(True)
        self._spin.setMaximum(pages)
        self._spin.setValue(self._page + 1)
        self._spin.blockSignals(False)

        can_back = self._page > 0
        can_fwd  = self._page < pages - 1
        self._first.setEnabled(can_back)
        self._prev.setEnabled(can_back)
        self._next.setEnabled(can_fwd)
        self._last.setEnabled(can_fwd)

    def bulk_add(self, entries: list[str]) -> None:
        if not entries or self._truncated:
            return
        remaining = self._LOG_MAX - len(self._items)
        if remaining <= 0:
            self._truncated = True
            return
        if len(entries) > remaining:
            entries = entries[:remaining]
            self._truncated = True
        needle        = self._search.text().lower().strip()
        entries_lower = [e.lower() for e in entries]
        self._items.extend(entries)
        self._items_lower.extend(entries_lower)
        if not self._finalized:
            if needle:
                self._filtered.extend(e for e, el in zip(entries, entries_lower, strict=True) if needle in el)
            else:
                self._filtered.extend(entries)
            self._search_cache.clear()
        if self._truncated:
            cap_msg = f"⚠ Log capped at {self._LOG_MAX:,} entries — use search to find specific files"
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
                self._sorted_ready.emit(list(sorted_items), list(sorted_lower))
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
            if len(self._search_cache) > 50:
                self._search_cache.pop(next(iter(self._search_cache)))
            self._search_cache[needle] = self._filtered[:]
        self._page = 0
        self._render()

    @staticmethod
    def _natural_sort_key(s: str) -> list:
        return [int(t) if t.isdigit() else t.lower() for t in _LogWidget._NATURAL_SORT_RE.split(s)]



class CopyDialog(_StandardKeysMixin, QDialog):

    def __init__(self, parent, tasks, operation: str) -> None:
        super().__init__(parent)
        self.setWindowTitle(operation)
        self._t = current_theme()
        t       = self._t

        self.c_ok = t["success"]
        self.c_sk = t["warning"]
        self.c_er = t["error"]

        self._status_fs = font_sz(8)

        size_to_screen(self, 1900, 925, fraction=0.9)

        self._operation = operation
        self.worker     = CopyWorker(tasks)
        self.copied = self.skipped = self.errors = 0
        self._done  = self._total = 0
        self._final_elapsed: int | None = None
        self._pending_ok = deque()
        self._pending_sk = deque()
        self._pending_er = deque()
        self._size_copied = self._size_skipped = 0
        self._not_found_paths: list[tuple[str, str]] = []

        self._summary = _SummaryWidget()

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setFixedHeight(2)
        sep.setStyleSheet(f"background: {t['header_sep']}; border: none;")

        self._w_copied  = _LogWidget(self.c_ok)
        self._w_skipped = _LogWidget(self.c_sk)
        self._w_errors  = _LogWidget(self.c_er)

        summary_page = QWidget()
        sl = QVBoxLayout(summary_page)
        sl.setContentsMargins(0, 0, 0, 0)
        sl.setSpacing(0)
        sl.addWidget(self._summary)

        self.tabs = QTabWidget()
        self.tabs.addTab(summary_page, "📋 Summary")
        self.tabs.addTab(self._w_copied, "⤵ Copied (0)")
        self.tabs.addTab(self._w_skipped, "↷ Skipped (0)")
        self.tabs.addTab(self._w_errors, "✗ Errors (0)")
        self.tabs.setStyleSheet(f"QTabWidget::pane {{border: none}} QTabBar::tab {{width: 200px; padding: 10px}}"
                                f"QTabBar::tab:selected {{background: {t['bg3']}; border-bottom: 2px solid {t['accent']}}}")

        self.cancel_btn = QPushButton("⏹ Cancel")
        self.cancel_btn.setMinimumHeight(50)
        self.cancel_btn.setStyleSheet(
            f"QPushButton {{background: {t['bg3']}; border: 1px solid {t['header_sep']}; "
            f"border-radius: 4px; color: {t['text']};}}"
            f"QPushButton:hover {{background: {t['bg2']}; border-color: {t['accent']}; color: {t['highlight']};}}"
            f"QPushButton:focus {{border-color: {t['accent']}; color: {t['highlight']}; outline: none;}}"
            f"QPushButton:pressed {{background: {t['bg']}; border-color: {t['accent2']}; color: {t['accent2']};}}")
        self._cancel_connected = True
        self._accept_connected = False
        self.cancel_btn.clicked.connect(self.worker.cancel)

        layout = QVBoxLayout(self)
        layout.setSpacing(3)
        layout.setContentsMargins(3, 3, 3, 3)
        layout.addWidget(sep)
        layout.addWidget(self.tabs)
        layout.addWidget(self.cancel_btn)

        self._set_status_running()
        self.timer = QElapsedTimer()
        self.timer.start()

        self._tick = QTimer(self)
        self._tick.timeout.connect(self._update_ui_tick)
        self._tick.start(500)

        self.worker.scan_finished.connect(self._on_scan_finished)
        self.worker.batch_update.connect(self._on_batch)
        self.worker.finished_work.connect(self._on_done)
        self.worker.scan_progress.connect(self._on_scan_progress)
        self.worker.entry_status.connect(self._summary.on_entry_status)

        space_warnings = _check_destination_space(tasks)
        if space_warnings:
            QMessageBox.warning(
                self,
                "Low Disk Space",
                "Warning: one or more destinations are running low on space:\n\n"
                + "\n".join(space_warnings)
                + "\n\nThe backup will still proceed.",
            )

        self.worker.start()
        self._summary.update_stats(self._operation, 0, 0, 0, 0, 0, 0, 0, 0, False)

    def _elapsed_s(self) -> int: return self._final_elapsed if self._final_elapsed is not None else self.timer.elapsed() // 1000

    def _status_badge(self, icon: str, label: str, color: str, border: "str | None" = None) -> str:
        border = border or color
        return (f"<span style='display:inline-block; font-size:{self._status_fs}px; font-weight:bold; "
                f"font-family:monospace; color:{color};background:{self._t['bg2']}; border-left:5px solid {border}; "
                f"border-radius:7px;padding:6px 18px;'>{icon}&thinsp;{label}</span>")

    def _set_status_running(self) -> None:
        self._summary.set_status_html(self._status_badge("⏳", f"{self._operation} running…", self._t["cyan"], self._t["accent"]))

    def _set_status_scanning(self, phase: str, scanned: int) -> None:
        self._summary.set_status_html(self._status_badge("🔍", f"{phase}… ({scanned:,} found)", self._t["accent2"]))

    def _set_status_finished(self, icon: str, label: str, color: str) -> None: self._summary.set_status_html(self._status_badge(icon, label, color))

    def _on_scan_progress(self, phase: str, scanned: int) -> None: self._set_status_scanning(phase, scanned)

    def _on_scan_finished(self, total: int) -> None:
        self._total = total
        suffix = "file" if total == 1 else "files"
        self._summary.set_status_html(self._status_badge("📂", f"Scan complete — {total:,} {suffix} found", self._t["accent"]))
        if total > 0:
            self._summary.update_progress_bar(self._done, total)

    def _drain_pending(self) -> int:
        max_per = 750

        def process_batch(pending, widget, fmt):
            if not pending:
                return 0
            if widget.is_truncated:
                n = len(pending)
                pending.clear()
                return n
            n     = min(max_per, len(pending))
            batch = [pending.popleft() for _ in range(n)]
            widget.bulk_add([fmt(*args) for args in batch])
            return n

        return (process_batch(self._pending_ok, self._w_copied,  self._fmt_ok) + process_batch(self._pending_sk, self._w_skipped, self._fmt_sk)
                + process_batch(self._pending_er, self._w_errors,  self._fmt_er))

    def _update_ui_tick(self) -> None:
        elapsed   = self._elapsed_s()
        processed = self._drain_pending()
        if processed:
            self._update_tab_labels()
        self._summary.update_stats(self._operation, self._done, self._total, self.copied, self.skipped, self.errors,
                                   elapsed, self._size_copied, self._size_skipped, finished=False)

    def _update_tab_labels(self) -> None:
        self.tabs.setTabText(1, f"⤵ Copied ({self.copied:,})")
        self.tabs.setTabText(2, f"↷ Skipped ({self.skipped:,})")
        self.tabs.setTabText(3, f"✗ Errors ({self.errors:,})")

    @staticmethod
    def _fmt_ok(s, d) -> str: return f"{apply_replacements(s)}\nCopied to ⤵\n{apply_replacements(d)}"

    @staticmethod
    def _fmt_sk(p, r) -> str: return f"{apply_replacements(p)} ↷ {r}"

    @staticmethod
    def _fmt_er(p, m) -> str: return f"{apply_replacements(p)} ❌ {m}"

    def _on_batch(self, ok, sk, er, done, total) -> None:
        self._done = max(self._done, done)
        self._total = total
        self.copied  += len(ok)
        self.skipped += len(sk)
        self.errors  += len(er)

        for s, d, sz in ok:
            self._size_copied += sz
            self._pending_ok.append((s, d))
        for s, r, sz in sk:
            self._size_skipped += sz
            self._pending_sk.append((s, r))
            if "does not exist" in r:
                _nf_title = (r.rsplit(" (", 1)[1].rstrip(")")
                             if (" (" in r and r.endswith(")")) else "")
                self._not_found_paths.append((apply_replacements(s), _nf_title))
        self._pending_er.extend((s, m) for s, m, _ in er)

        if total > 0:
            self._summary.update_progress_bar(done, total)

    def _on_done(self, c, s, e, cancelled) -> None:
        self._tick.stop()
        elapsed = self._final_elapsed = self.timer.elapsed() // 1000

        try:
            from history import append_history
            append_history(operation=self._operation, copied=c, skipped=s, errors=e, duration_s=elapsed, cancelled=cancelled)
        except Exception as exc:
            logger.debug("append_history failed: %s", exc)

        for pending, widget, fmt in zip((self._pending_ok, self._pending_sk, self._pending_er),
                                        (self._w_copied, self._w_skipped, self._w_errors),
                                        (self._fmt_ok, self._fmt_sk, self._fmt_er), strict=True):
            if pending:
                cap = max(0, widget.log_max - widget.item_count)
                if cap > 0:
                    widget.bulk_add([fmt(*args) for args in islice(pending, cap)])
                pending.clear()

        self.copied, self.skipped, self.errors = c, s, e
        self._done = self._total

        tstr = f"{elapsed // 60:02d}:{elapsed % 60:02d}"

        if cancelled:
            icon, label, col = "⏹", f"Cancelled after {tstr}", self.c_sk
        elif e > 0:
            icon, label, col = "⚠", f"Done with errors ✗ — {tstr}", self.c_er
        else:
            icon, label, col = "✓", f"Done — {tstr}", self.c_ok

        self._set_status_finished(icon, label, col)
        self._summary.update_stats(self._operation, self._done, self._total, self.copied, self.skipped, self.errors,
                                   elapsed, self._size_copied, self._size_skipped, finished=True, cancelled=cancelled)
        self._update_tab_labels()

        for w in (self._w_copied, self._w_skipped, self._w_errors):
            w.flush_final()

        if self._cancel_connected:
            try:
                self.cancel_btn.clicked.disconnect(self.worker.cancel)
            except RuntimeError:
                pass
            self._cancel_connected = False

        if self._accept_connected:
            try:
                self.cancel_btn.clicked.disconnect(self.accept)
            except (RuntimeError, TypeError):
                pass

        self.cancel_btn.setEnabled(True)
        self.cancel_btn.clicked.connect(self.accept)
        self.cancel_btn.setText("Close")
        self._accept_connected = True

        if not self.isActiveWindow() and not cancelled:
            if e > 0:
                _notify(
                    f"{self._operation} completed with errors",
                    f"{c} file{'s' if c != 1 else ''} copied, {s} skipped, {e} error{'s' if e != 1 else ''}",
                    urgency="critical",
                )
            else:
                _notify(
                    f"{self._operation} successfully completed",
                    f"{c} file{'s' if c != 1 else ''} copied, {s} skipped",
                    urgency="normal",
                )

        if self._not_found_paths and not cancelled:
            n = len(self._not_found_paths)
            paths_text = "\n".join(
                f"  \u2022 {p} ({t})" if t else f"  \u2022 {p}"
                for p, t in self._not_found_paths
            )
            msg = (
                f"{n} configured {'path was' if n == 1 else 'paths were'} not found "
                f"and skipped:\n\n{paths_text}\n\n"
                f"Please check these entries in your backup profile."
            )

            def _show_popup(_msg: str = msg) -> None:
                QMessageBox.warning(self, "Missing Paths Detected", _msg)

            QTimer.singleShot(0, _show_popup)

    def closeEvent(self, event) -> None:
        if self.worker.isRunning():
            self.worker.cancel()
            self.cancel_btn.setText("⏹ Cancelling…")
            self.cancel_btn.setEnabled(False)
            event.ignore()
        else:
            super().closeEvent(event)

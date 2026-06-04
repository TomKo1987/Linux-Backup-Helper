from PyQt6.QtWidgets import QHBoxLayout, QLabel, QVBoxLayout, QWidget

from state import S, logger


class StatusPanel(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._build()

    def _build(self) -> None:
        from themes import current_theme, font_sz
        t   = current_theme()
        bg2 = t["bg2"]
        sep = t["header_sep"]
        dim = t["text_dim"]
        self.setStyleSheet(f"background:{bg2};border-top:1px solid {sep};")

        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 6, 12, 6)
        lay.setSpacing(4)

        def _lbl(text: str = "", color: str = "") -> QLabel:
            w = QLabel(text)
            c = color or dim
            w.setStyleSheet(
                f"color:{c};font-size:{font_sz(-2)}px;"
                f"background:transparent;border:none;"
            )
            return w

        self._profile_lbl  = _lbl(color=t["accent"])
        self._drives_lbl   = _lbl()
        self._sched_lbl    = _lbl()
        self._next_run_lbl = _lbl()
        self._last_bak_lbl = _lbl()

        row1 = QHBoxLayout()
        row1.setSpacing(20)
        row1.addStretch()
        row1.addWidget(self._profile_lbl)
        row1.addStretch()
        row1.addWidget(self._drives_lbl)
        row1.addStretch()

        row2 = QHBoxLayout()
        row2.setSpacing(20)
        row2.addStretch(1)
        row2.addWidget(self._sched_lbl)
        row2.addStretch(1)

        row2b = QHBoxLayout()
        row2b.addStretch(1)
        row2b.addWidget(self._next_run_lbl)
        row2b.addStretch(1)

        row3 = QHBoxLayout()
        row3.setSpacing(20)
        row3.addStretch()
        row3.addWidget(self._last_bak_lbl)
        row3.addStretch()

        lay.addLayout(row1)
        lay.addLayout(row2)
        lay.addLayout(row2b)
        lay.addLayout(row3)

    def refresh(self) -> None:
        from themes import current_theme, font_sz
        from history import load_history
        from scheduler import is_timer_active, get_next_run_time, get_active_interval
        from drive_utils import get_mounts, is_mounted as _is_mounted

        t   = current_theme()
        dim = t["text_dim"]
        fs  = font_sz(-2)

        def _style(_color: str) -> str:
            return (
                f"color:{_color};font-size:{fs}px;"
                f"background:transparent;border:none;"
            )

        name = S.profile_name or "(no profile)"
        self._profile_lbl.setText(f"\U0001f464 {name}")
        self._profile_lbl.setStyleSheet(_style(t["accent"]))

        if S.mount_options:
            try:
                mounts    = get_mounts()
                n_mounted = sum(1 for o in S.mount_options if _is_mounted(o, mounts))
                total     = len(S.mount_options)
                color = (
                    t["success"] if n_mounted == total
                    else t["warning"] if n_mounted > 0
                    else dim
                )
                self._drives_lbl.setText(f"\U0001f4be {n_mounted}/{total} drives")
                self._drives_lbl.setStyleSheet(_style(color))
            except (OSError, ValueError) as e:
                logger.debug("Could not load mounts: %s", e)
                self._drives_lbl.setText("")
        else:
            self._drives_lbl.setText("")

        try:
            active = is_timer_active()
        except (OSError, ImportError) as e:
            logger.debug("Could not check scheduler: %s", e)
            active = False

        if active:
            interval  = get_active_interval()
            sched_txt = f"\u23f0 Scheduler: On — {interval}" if interval else "\u23f0 Scheduler: On"
            self._sched_lbl.setText(sched_txt)
            self._sched_lbl.setStyleSheet(_style(t["success"]))
            nxt = get_next_run_time()
            if nxt:
                self._next_run_lbl.setText(f"\u27a1 Next: {nxt}")
                self._next_run_lbl.setStyleSheet(_style(t["text_dim"]))
                self._next_run_lbl.show()
            else:
                self._next_run_lbl.setText("")
                self._next_run_lbl.show()
        else:
            self._sched_lbl.setText("\u23f0 Scheduler: Off")
            self._sched_lbl.setStyleSheet(_style(dim))
            self._next_run_lbl.hide()

        try:
            history = load_history(S.profile_name or "")
        except (OSError, ValueError) as e:
            logger.debug("Could not load history: %s", e)
            history = []

        if history:
            last      = history[-1]
            ts        = last.get("timestamp", "?")
            copied    = last.get("copied",    0)
            errors    = last.get("errors",    0)
            cancelled = last.get("cancelled", False)
            if cancelled:
                icon, color = "\u23f9", t["warning"]
            elif errors:
                icon, color = "\u26a0", t["warning"]
            else:
                icon, color = "\u2713", t["success"]
            self._last_bak_lbl.setText(f"{icon} Last: {ts}  ({copied:,} copied)")
            self._last_bak_lbl.setStyleSheet(_style(color))
        else:
            self._last_bak_lbl.setText("No backup yet")
            self._last_bak_lbl.setStyleSheet(_style(dim))

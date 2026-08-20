import os
import time
from pathlib import Path

from PyQt6.QtCore import QThread, pyqtSignal
from PyQt6.QtGui import QCloseEvent
from PyQt6.QtWidgets import (
    QDialog, QFrame, QHBoxLayout, QLabel, QProgressBar,
    QPushButton, QVBoxLayout,
)

from state import S
from themes import current_theme, font_sz
from translations import tr
from ui_utils import _StandardKeysMixin, build_dialog_shell, clear_layout, size_to_screen
from drive_utils import is_smb, is_ssh

__all__ = ["IntegrityCheckerDialog"]


def _quick_scan(path_str: str) -> dict | None:
    root = Path(os.path.expanduser(os.path.expandvars(path_str)))
    if not root.exists():
        return None
    if root.is_file():
        try:
            st = root.stat()
            return {"file_count": 1, "dir_count": 0,
                    "total_size": st.st_size, "mtime_newest": st.st_mtime}
        except OSError:
            return None

    fc = dc = 0
    total = 0
    newest = 0.0
    try:
        for entry in os.scandir(root):
            try:
                st = entry.stat(follow_symlinks=False)
                if entry.is_dir(follow_symlinks=False):
                    dc += 1
                else:
                    fc += 1
                    total += st.st_size
                newest = max(newest, st.st_mtime)
            except OSError:
                pass
    except OSError:
        return None
    return {"file_count": fc, "dir_count": dc, "total_size": total, "mtime_newest": newest}


def _top_level_names(path_str: str) -> set[str]:
    root = Path(os.path.expanduser(os.path.expandvars(path_str)))
    try:
        return {e.name for e in os.scandir(root)}
    except OSError:
        return set()


def _fmt_bytes(n: int | float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024
    return str(n)


def _age(mtime: float) -> str:
    delta = time.time() - mtime
    if delta < 0:
        return tr("future")
    if delta < 3600:
        return tr("{n}m ago", n=int(delta // 60))
    if delta < 86400:
        return tr("{n}h ago", n=int(delta // 3600))
    return tr("{n}d ago", n=int(delta // 86400))


class _CheckWorker(QThread):
    result_ready = pyqtSignal(dict)
    all_done     = pyqtSignal()

    def __init__(self, entries: list[dict]) -> None:
        super().__init__()
        self._entries = entries

    def run(self) -> None:
        for entry in self._entries:
            title   = entry.get("title", "?")
            sources = entry.get("source", [])
            dests   = entry.get("destination", [])

            issues: list[str] = []
            ok = True

            if len(sources) != len(dests):
                issues.append(
                    tr("Source/destination count mismatch ({n_src} source(s) vs "
                       "{n_dst} destination(s)) — extra entries were not checked",
                       n_src=len(sources), n_dst=len(dests))
                )
                ok = False

            for src_raw, dst_raw in zip(sources, dests):
                if is_smb(src_raw) or is_ssh(src_raw) or is_smb(dst_raw) or is_ssh(dst_raw):
                    remote = src_raw if (is_smb(src_raw) or is_ssh(src_raw)) else dst_raw
                    issues.append(
                        tr("Remote (SMB/SSH) path — not checked by Integrity Checker "
                           "(local-filesystem check only; this is not a failure): {remote}", remote=remote)
                    )
                    continue

                src_info = _quick_scan(src_raw)
                dst_info = _quick_scan(dst_raw)

                src_path = Path(os.path.expanduser(os.path.expandvars(src_raw)))
                dst_path = Path(os.path.expanduser(os.path.expandvars(dst_raw)))

                if not src_path.exists():
                    issues.append(tr("Source missing: {path}", path=src_raw))
                    ok = False
                    continue

                if not dst_path.exists():
                    issues.append(tr("Destination missing: {path}", path=dst_raw))
                    ok = False
                    continue

                if src_info and dst_info:
                    if src_info["total_size"] > 0:
                        ratio = dst_info["total_size"] / src_info["total_size"]
                        if ratio < 0.8:
                            issues.append(
                                tr("Destination is only {pct:.0f}% of source size "
                                   "({dst_size} vs "
                                   "{src_size})",
                                   pct=ratio * 100,
                                   dst_size=_fmt_bytes(dst_info['total_size']),
                                   src_size=_fmt_bytes(src_info['total_size']))
                            )
                            ok = False

                    if src_info["file_count"] > 10:
                        fc_ratio = (dst_info["file_count"] or 0) / src_info["file_count"]
                        if fc_ratio < 0.9:
                            issues.append(
                                tr("Fewer files at destination: "
                                   "{dst_count} vs {src_count}",
                                   dst_count=dst_info['file_count'], src_count=src_info['file_count'])
                            )
                            ok = False

                    if dst_info["mtime_newest"] > 0:
                        age_dst = time.time() - dst_info["mtime_newest"]
                        age_src = time.time() - src_info["mtime_newest"]
                        if age_dst > 7 * 86400 and age_src < age_dst - 86400:
                            issues.append(
                                tr("Backup may be stale — destination last updated "
                                   "{dst_age}, "
                                   "source changed {src_age}",
                                   dst_age=_age(dst_info['mtime_newest']),
                                   src_age=_age(src_info['mtime_newest']))
                            )
                            ok = False

                src_names = _top_level_names(src_raw)
                dst_names = _top_level_names(dst_raw)
                missing   = src_names - dst_names
                if missing and len(missing) <= 5:
                    issues.append(
                        tr("Missing in destination: {names}", names=", ".join(sorted(missing)[:5]))
                    )
                    ok = False
                elif missing:
                    issues.append(tr("{n} top-level items missing in destination", n=len(missing)))
                    ok = False

            self.result_ready.emit({
                "title":  title,
                "header": entry.get("header", ""),
                "ok":     ok and not issues,
                "issues": issues,
                "src":    sources[0] if sources else "",
                "dst":    dests[0]   if dests   else "",
            })

        self.all_done.emit()


class _ResultRow(QFrame):
    def __init__(self, result: dict, parent=None) -> None:
        super().__init__(parent)
        t = current_theme()
        ok = result["ok"]
        color = t["success"] if ok else t["error"]
        icon  = "✅" if ok else "⚠️"

        self.setStyleSheet(
            f"background:{t['bg2']};border-radius:6px;"
            f"border-left:4px solid {color};"
            f"border-top:1px solid {t['bg3']};"
            f"border-right:1px solid {t['bg3']};"
            f"border-bottom:1px solid {t['bg3']};"
        )
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 8, 12, 8)
        lay.setSpacing(4)

        top = QHBoxLayout()
        title_lbl = QLabel(f"{icon}  {result['title']}")
        title_lbl.setStyleSheet(
            f"font-size:{font_sz(1)}px;font-weight:bold;"
            f"color:{t['text']};background:transparent;border:none;"
        )
        hdr_lbl = QLabel(result.get("header", ""))
        hdr_lbl.setStyleSheet(
            f"font-size:{font_sz(-2)}px;color:{t['text_dim']};"
            f"background:transparent;border:none;"
        )
        top.addWidget(title_lbl)
        top.addWidget(hdr_lbl)
        top.addStretch()
        if ok:
            ok_lbl = QLabel(tr("All checks passed"))
            ok_lbl.setStyleSheet(
                f"color:{t['success']};font-size:{font_sz(-1)}px;"
                f"background:transparent;border:none;"
            )
            top.addWidget(ok_lbl)
        lay.addLayout(top)

        for issue in result.get("issues", []):
            issue_lbl = QLabel(f"  • {issue}")
            issue_lbl.setWordWrap(True)
            issue_lbl.setStyleSheet(
                f"color:{t['warning']};font-size:{font_sz(-1)}px;"
                f"background:transparent;border:none;"
            )
            lay.addWidget(issue_lbl)


# noinspection PyUnresolvedReferences
class IntegrityCheckerDialog(_StandardKeysMixin, QDialog):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(tr("Backup Integrity Check"))
        size_to_screen(self, 1500, 1000)
        self._worker: _CheckWorker | None = None
        self._results: list[dict] = []
        self._build()

    def closeEvent(self, a0: QCloseEvent | None) -> None:
        if self._worker and self._worker.isRunning():
            self._worker.quit()
            self._worker.wait(2000)
        super().closeEvent(a0)

    def _build(self) -> None:
        t = current_theme()

        self._run_btn = QPushButton(f"▶  {tr('Run Check')}")
        self._run_btn.setFixedHeight(34)
        self._run_btn.clicked.connect(self._start)

        self._status_lbl = QLabel("")
        self._status_lbl.setStyleSheet(
            f"color:{t['text_dim']};font-size:{font_sz(-1)}px;"
            f"background:transparent;border:none;"
        )

        lay, self._body_lay, _ = build_dialog_shell(
            self, t, font_sz, tr("Backup Integrity Check"), "🔬",
            header_extra=[self._run_btn],
            footer_extra=[self._status_lbl],
        )

        self._progress = QProgressBar()
        self._progress.setRange(0, 0)
        self._progress.setFixedHeight(4)
        self._progress.setTextVisible(False)
        self._progress.hide()
        self._progress.setStyleSheet(
            f"QProgressBar{{background:{t['bg3']};border:none;}}"
            f"QProgressBar::chunk{{background:{t['accent']};}}"
        )
        lay.insertWidget(1, self._progress)

        hint = QLabel(
            tr("Checks each backup entry:\n"
               "  • Source & destination exist\n"
               "  • Destination is not significantly smaller than source\n"
               "  • Top-level items are not missing\n"
               "  • Destination has been updated recently relative to source\n\n"
               "Click ▶ Run Check to begin.")
        )
        hint.setStyleSheet(
            f"color:{t['text_dim']};font-size:{font_sz()}px;"
            f"background:transparent;border:none;"
        )
        self._body_lay.addWidget(hint)
        self._body_lay.addStretch()

    def _start(self) -> None:
        entries = [e for e in S.entries if not e.get("details", {}).get("no_backup")]
        if not entries:
            self._status_lbl.setText(tr("No active entries found."))
            return

        clear_layout(self._body_lay)
        self._run_btn.setEnabled(False)
        self._progress.show()
        self._status_lbl.setText(tr("Checking {n} entries…", n=len(entries)))
        self._results: list[dict] = []

        self._worker = _CheckWorker(entries)
        self._worker.result_ready.connect(self._on_result)
        self._worker.all_done.connect(self._on_done)
        self._worker.start()

    def _on_result(self, result: dict) -> None:
        self._results.append(result)
        row = _ResultRow(result)
        self._body_lay.insertWidget(self._body_lay.count(), row)

    def _on_done(self) -> None:
        self._progress.hide()
        self._run_btn.setEnabled(True)
        total   = len(self._results)
        n_ok    = sum(1 for r in self._results if r["ok"])
        n_warn  = total - n_ok
        t = current_theme()
        color = t["success"] if n_warn == 0 else t["warning"]
        self._status_lbl.setStyleSheet(
            f"color:{color};font-size:{font_sz(-1)}px;"
            f"background:transparent;border:none;"
        )
        self._status_lbl.setText(
            tr("Done — {n_ok}/{total} OK", n_ok=n_ok, total=total)
            + (tr(", {n_warn} with warnings", n_warn=n_warn) if n_warn else "")
        )
        self._body_lay.addStretch()

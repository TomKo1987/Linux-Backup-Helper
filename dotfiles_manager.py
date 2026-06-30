import difflib
import html as _html
import os
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtGui import QColor, QFont
from PyQt6.QtWidgets import (
    QCheckBox, QDialog, QFrame, QHBoxLayout, QLabel, QListWidget,
    QListWidgetItem, QMessageBox, QPushButton, QSplitter, QTextEdit,
    QVBoxLayout, QWidget,
)

from state import active_dotfiles
from themes import current_theme, font_sz, register_style_listener, unregister_style_listener
from ui_utils import _StandardKeysMixin


def first_path(v) -> str:
    if isinstance(v, list):
        return str(v[0]) if v else ""
    return str(v) if v else ""


def _expand(path: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(path)))


def _read_safe(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except PermissionError:
        try:
            r = subprocess.run(["sudo", "-n", "cat", str(path)],
                               capture_output=True, text=True, timeout=10)
            if r.returncode == 0:
                return r.stdout
        except (subprocess.SubprocessError, OSError):
            pass
        return None
    except OSError:
        return None


def _path_exists(path: Path) -> bool:
    try:
        path.stat()
        return True
    except PermissionError:
        return True
    except OSError:
        return False


def _make_backup(dst: Path) -> bool:
    if not dst.exists():
        return True
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    bak = dst.with_suffix(f"{dst.suffix}.bak_{ts}")
    try:
        shutil.copy2(dst, bak)
        return True
    except OSError:
        return False


def _colored_diff_html(src_lines: list[str], dst_lines: list[str], theme: dict) -> str:
    add_col = theme.get("success", "#4ec994")
    rem_col = theme.get("error",   "#f7768e")
    ctx_col = theme.get("text_dim","#565f89")
    bg      = theme.get("bg",     "#1a1b26")
    fs      = font_sz(-1)

    diff = list(difflib.unified_diff(dst_lines, src_lines, lineterm="",
                                     fromfile="current system", tofile="profile source"))
    if not diff:
        return (f"<p style='color:{add_col};font-family:monospace;font-size:{fs}px;'>"
                f"✓  Files are identical — nothing to deploy.</p>")

    truncated = False
    if len(diff) > 2000:
        diff = diff[:2000]
        truncated = True

    rows = []
    for line in diff:
        raw = line.rstrip("\n")
        esc = _html.escape(raw)
        if raw.startswith("+"):
            color = add_col
        elif raw.startswith("-"):
            color = rem_col
        elif raw.startswith("@@"):
            color = ctx_col
        else:
            color = theme.get("text", "#c0caf5")
        rows.append(f"<p style='margin:0;color:{color};font-family:monospace;font-size:{fs}px;"
                    f"white-space:pre;'>{esc}</p>")

    html = (f"<div style='background:{bg};padding:8px;border-radius:4px;'>"
            + "\n".join(rows)
            + "</div>")
    if truncated:
        html += (f"<p style='color:{ctx_col};font-family:monospace;font-size:{fs}px;'>"
                 f"… diff truncated (more than 2000 lines)</p>")
    return html


class _DeployWorker(QThread):
    progress = pyqtSignal(str, bool)
    finished = pyqtSignal(int, int)

    def __init__(self, files: list[dict], backup: bool) -> None:
        super().__init__()
        self._files  = files
        self._backup = backup

    def run(self) -> None:
        ok = err = 0
        for f in self._files:
            src = _expand(first_path(f.get("source", "")))
            dst = _expand(first_path(f.get("destination", "")))
            if not src.exists():
                self.progress.emit(f"  ✗ Source not found: {src}", True)
                err += 1
                continue
            try:
                if self._backup:
                    if not _make_backup(dst):
                        self.progress.emit(f"  ✗ {src.name}: backup failed, skipping overwrite", True)
                        err += 1
                        continue
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
                self.progress.emit(f"  ✓ {src.name}  →  {dst}", False)
                ok += 1
            except PermissionError:
                try:
                    if self._backup and _path_exists(dst):
                        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                        bak_path = dst.with_suffix(dst.suffix + f".bak_{ts}")
                        bres = subprocess.run(
                            ["sudo", "cp", "-p", str(dst), str(bak_path)],
                            capture_output=True, text=True, timeout=15)
                        if bres.returncode != 0:
                            self.progress.emit(
                                f"  ✗ {src.name}: sudo backup failed: {bres.stderr.strip()}, skipping overwrite", True)
                            err += 1
                            continue
                    r = subprocess.run(
                        ["sudo", "cp", str(src), str(dst)],
                        capture_output=True, text=True, timeout=30)
                    if r.returncode == 0:
                        self.progress.emit(f"  ✓ {src.name}  →  {dst}  (via sudo)", False)
                        ok += 1
                    else:
                        self.progress.emit(
                            f"  ✗ {src.name}: sudo cp failed: {r.stderr.strip()}", True)
                        err += 1
                except subprocess.TimeoutExpired:
                    self.progress.emit(f"  ✗ {src.name}: sudo cp timed out", True)
                    err += 1
                except Exception as exc2:
                    self.progress.emit(f"  ✗ {src.name}: {exc2}", True)
                    err += 1
            except OSError as exc:
                self.progress.emit(f"  ✗ {src.name}: {exc}", True)
                err += 1
        self.finished.emit(ok, err)


class DotfilesManagerDialog(_StandardKeysMixin, QDialog):

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Dotfiles Manager")
        self.setMinimumSize(1500, 1000)
        self._files:  list[dict]     = []
        self._worker: _DeployWorker | None = None
        self._build_ui()
        self._load_files()
        register_style_listener(self._refresh_styles)

    def closeEvent(self, event) -> None:
        if isinstance(self._worker, QThread) and self._worker.isRunning():
            self._worker.quit()
            self._worker.wait(2000)
        super().closeEvent(event)

    def _build_ui(self) -> None:
        t = current_theme()
        bg = t["bg"]
        bg2 = t["bg2"]
        bg3 = t["bg3"]
        sep_col = t["header_sep"]
        acc = t["accent"]
        fg = t["text"]
        dim = t["text_dim"]

        self.setStyleSheet(f"background:{bg};color:{fg};")

        self._header_frame = QFrame()
        self._header_frame.setStyleSheet(f"background:{bg2};border-bottom:1px solid {sep_col};")
        hl = QHBoxLayout(self._header_frame)
        hl.setContentsMargins(14, 10, 14, 10)
        self._title_lbl = QLabel("📄  Dotfiles Manager")
        self._title_lbl.setStyleSheet(f"font-size:{font_sz(5)}px;font-weight:bold;color:{acc};background:transparent;")
        self._sub_lbl = QLabel("Deploy tracked config files from your profile to the live system")
        self._sub_lbl.setStyleSheet(f"font-size:{font_sz(-1)}px;color:{dim};background:transparent;")
        hl.addWidget(self._title_lbl)
        hl.addStretch()
        hl.addWidget(self._sub_lbl)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        self._splitter = splitter

        self._left_widget = QWidget()
        self._left_widget.setStyleSheet(f"background:{bg};")
        ll = QVBoxLayout(self._left_widget)
        ll.setContentsMargins(8, 8, 4, 8)
        ll.setSpacing(6)

        self._list_hdr_lbl = QLabel("Tracked Files")
        self._list_hdr_lbl.setStyleSheet(f"font-size:{font_sz(1)}px;font-weight:bold;color:{acc};")
        ll.addWidget(self._list_hdr_lbl)

        self._list = QListWidget()
        self._list.setStyleSheet(
            f"QListWidget{{background:{bg3};border:1px solid {sep_col};border-radius:4px;"
            f"font-size:{font_sz()}px;color:{fg};outline:none;}}"
            f"QListWidget::item{{padding:6px 8px;border-bottom:1px solid {sep_col};}}"
            f"QListWidget::item:selected{{background:{bg2};color:{acc};border-left:3px solid {acc};}}"
            f"QListWidget::item:hover:!selected{{background:{bg2};}}"
        )
        self._list.currentRowChanged.connect(self._on_select)
        ll.addWidget(self._list)

        self._backup_cb = QCheckBox("Create .bak backup before overwriting")
        self._backup_cb.setChecked(True)
        self._backup_cb.setStyleSheet(f"color:{dim};font-size:{font_sz(-1)}px;")
        ll.addWidget(self._backup_cb)

        self._btn_deploy_sel = QPushButton("⬇  Deploy Selected")
        self._btn_deploy_sel.setMinimumHeight(36)
        self._btn_deploy_sel.setStyleSheet(self._btn_style(t, primary=True))
        self._btn_deploy_sel.clicked.connect(self._deploy_selected)

        self._btn_deploy_all = QPushButton("⬇⬇  Deploy All Changed")
        self._btn_deploy_all.setMinimumHeight(36)
        self._btn_deploy_all.setStyleSheet(self._btn_style(t, primary=False))
        self._btn_deploy_all.clicked.connect(self._deploy_all_changed)

        ll.addWidget(self._btn_deploy_sel)
        ll.addWidget(self._btn_deploy_all)

        self._right_widget = QWidget()
        self._right_widget.setStyleSheet(f"background:{bg};")
        rl = QVBoxLayout(self._right_widget)
        rl.setContentsMargins(4, 8, 8, 8)
        rl.setSpacing(4)

        diff_hdr_row = QHBoxLayout()
        self._diff_title = QLabel("Select a file to see the diff")
        self._diff_title.setStyleSheet(f"font-size:{font_sz(1)}px;font-weight:bold;color:{acc};")
        self._status_lbl = QLabel()
        self._status_lbl.setStyleSheet(f"font-size:{font_sz()}px;")
        diff_hdr_row.addWidget(self._diff_title)
        diff_hdr_row.addStretch()
        diff_hdr_row.addWidget(self._status_lbl)
        rl.addLayout(diff_hdr_row)

        self._diff_view = QTextEdit()
        self._diff_view.setReadOnly(True)
        self._diff_view.setFont(QFont("monospace"))
        self._diff_view.setStyleSheet(
            f"QTextEdit{{background:{bg3};color:{fg};border:1px solid {sep_col};"
            f"border-radius:4px;padding:8px;}}")
        rl.addWidget(self._diff_view, 1)

        splitter.addWidget(self._left_widget)
        splitter.addWidget(self._right_widget)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 2)
        splitter.setStyleSheet(f"QSplitter::handle{{background:{sep_col};width:1px;}}")

        self._bottom_frame = QFrame()
        self._bottom_frame.setStyleSheet(f"background:{bg2};border-top:1px solid {sep_col};")
        bl = QHBoxLayout(self._bottom_frame)
        bl.setContentsMargins(12, 8, 12, 8)
        self._info_lbl = QLabel()
        self._info_lbl.setStyleSheet(f"color:{dim};font-size:{font_sz(-1)}px;background:transparent;")
        bl.addWidget(self._info_lbl)
        bl.addStretch()
        self._close_btn = QPushButton("Close")
        self._close_btn.setMinimumHeight(32)
        self._close_btn.setMinimumWidth(100)
        self._close_btn.setStyleSheet(self._btn_style(t, primary=False))
        self._close_btn.clicked.connect(self.accept)
        bl.addWidget(self._close_btn)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(self._header_frame)
        lay.addWidget(splitter, 1)
        lay.addWidget(self._bottom_frame)

    def done(self, result: int) -> None:
        unregister_style_listener(self._refresh_styles)
        super().done(result)

    def _refresh_styles(self) -> None:
        t = current_theme()
        bg = t["bg"]
        bg2 = t["bg2"]
        bg3 = t["bg3"]
        sep_col = t["header_sep"]
        acc = t["accent"]
        fg = t["text"]
        dim = t["text_dim"]

        self.setStyleSheet(f"background:{bg};color:{fg};")
        self._header_frame.setStyleSheet(f"background:{bg2};border-bottom:1px solid {sep_col};")
        self._title_lbl.setStyleSheet(
            f"font-size:{font_sz(5)}px;font-weight:bold;color:{acc};background:transparent;")
        self._sub_lbl.setStyleSheet(
            f"font-size:{font_sz(-1)}px;color:{dim};background:transparent;")
        self._left_widget.setStyleSheet(f"background:{bg};")
        self._list_hdr_lbl.setStyleSheet(
            f"font-size:{font_sz(1)}px;font-weight:bold;color:{acc};")
        self._list.setStyleSheet(
            f"QListWidget{{background:{bg3};border:1px solid {sep_col};border-radius:4px;"
            f"font-size:{font_sz()}px;color:{fg};outline:none;}}"
            f"QListWidget::item{{padding:6px 8px;border-bottom:1px solid {sep_col};}}"
            f"QListWidget::item:selected{{background:{bg2};color:{acc};border-left:3px solid {acc};}}"
            f"QListWidget::item:hover:!selected{{background:{bg2};}}"
        )
        self._backup_cb.setStyleSheet(f"color:{dim};font-size:{font_sz(-1)}px;")
        self._btn_deploy_sel.setStyleSheet(self._btn_style(t, primary=True))
        self._btn_deploy_all.setStyleSheet(self._btn_style(t, primary=False))
        self._diff_title.setStyleSheet(
            f"font-size:{font_sz(1)}px;font-weight:bold;color:{acc};")
        self._diff_view.setStyleSheet(
            f"QTextEdit{{background:{bg3};color:{fg};border:1px solid {sep_col};"
            f"border-radius:4px;padding:8px;}}")
        self._right_widget.setStyleSheet(f"background:{bg};")
        self._splitter.setStyleSheet(
            f"QSplitter::handle{{background:{sep_col};width:1px;}}")
        self._bottom_frame.setStyleSheet(
            f"background:{bg2};border-top:1px solid {sep_col};")
        self._info_lbl.setStyleSheet(
            f"color:{dim};font-size:{font_sz(-1)}px;background:transparent;")
        self._close_btn.setStyleSheet(self._btn_style(t, primary=False))
        self._load_files()

    @staticmethod
    def _btn_style(t: dict, primary: bool) -> str:
        acc = t["accent"] if primary else t["header_sep"]
        return (f"QPushButton{{background:{t['bg3']};border:1px solid {acc};"
                f"border-radius:4px;color:{t['text']};padding:4px 14px;}}"
                f"QPushButton:hover{{background:{t['bg2']};border-color:{t['accent']};color:{t['highlight']};}}"
                f"QPushButton:focus{{border-color:{t['accent']};color:{t['highlight']};outline:none;}}"
                f"QPushButton:pressed{{background:{t['bg']};border-color:{t['accent2']};color:{t['accent2']};}}")

    def _load_files(self) -> None:
        self._files = active_dotfiles()
        self._list.clear()
        t = current_theme()

        if not self._files:
            item = QListWidgetItem("  No dotfiles configured in profile.")
            item.setForeground(QColor(t["text_dim"]))
            item.setFlags(Qt.ItemFlag.NoItemFlags)
            self._list.addItem(item)
            self._info_lbl.setText("Configure dotfiles in Settings → Dotfiles.")
            return

        identical = changed = missing = 0
        for f in self._files:
            src_raw = f.get("source", "")
            dst_raw = f.get("destination", "")
            src = _expand(first_path(src_raw))
            dst = _expand(first_path(dst_raw))

            title = f.get("title", src.name)
            src_text = _read_safe(src)
            dst_text = _read_safe(dst)

            if src_text is None:
                icon, color_key = "✗ ", "error"
                missing += 1
            elif dst_text is None:
                icon, color_key = "★ ", "warning"
                changed += 1
            elif src_text == dst_text:
                icon, color_key = "✓ ", "success"
                identical += 1
            else:
                icon, color_key = "≠ ", "warning"
                changed += 1

            item = QListWidgetItem(f"{icon}{title}")
            item.setForeground(QColor(t[color_key]))
            item.setData(Qt.ItemDataRole.UserRole, f)
            self._list.addItem(item)

        self._info_lbl.setText(
            f"{len(self._files)} files tracked  —  "
            f"✓ {identical} identical  ·  ≠ {changed} changed/new  ·  ✗ {missing} missing source"
        )

        if self._list.count() > 0:
            self._list.setCurrentRow(0)

    def _on_select(self, row: int) -> None:
        item = self._list.item(row)
        if item is None:
            return
        f = item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(f, dict):
            self._diff_view.clear()
            return

        t       = current_theme()
        src_raw = f.get("source", "")
        dst_raw = f.get("destination", "")
        src = _expand(first_path(src_raw))
        dst = _expand(first_path(dst_raw))

        self._diff_title.setText(f.get("title", src.name))

        src_text = _read_safe(src)
        dst_text = _read_safe(dst)

        if src_text is None:
            err_col = t["error"]
            self._diff_view.setHtml(
                f"<p style='color:{err_col};font-family:monospace;font-size:{font_sz()}px;'>"
                f"✗  Source file not found:<br>{src}</p>")
            self._status_lbl.setText("⚠ Source missing")
            self._status_lbl.setStyleSheet(f"color:{err_col};font-size:{font_sz()}px;")
            return

        if dst_text is None:
            if _path_exists(dst):
                self._status_lbl.setText("🔒 No read access")
                self._status_lbl.setStyleSheet(f"color:{t['warning']};font-size:{font_sz()}px;")
                warn_col = t["warning"]
                self._diff_view.setHtml(
                    f"<p style='color:{warn_col};font-family:monospace;font-size:{font_sz()}px;'>"
                    f"🔒  File exists but cannot be read without elevated permissions.<br>"
                    f"Deploy will use <code>sudo cp</code> automatically.</p>")
            else:
                self._status_lbl.setText("★ Not on system yet")
                self._status_lbl.setStyleSheet(f"color:{t['warning']};font-size:{font_sz()}px;")
                warn_col = t["warning"]
                text_col = t["text"]
                escaped_preview = _html.escape(src_text[:4000])
                self._diff_view.setHtml(
                    f"<p style='color:{warn_col};font-family:monospace;font-size:{font_sz()}px;'>"
                    f"★  Destination does not exist yet — will be created:</p>"
                    f"<pre style='color:{text_col};font-family:monospace;font-size:{font_sz(-1)}px;'>{escaped_preview}</pre>")
            return

        src_lines = src_text.splitlines(keepends=True)
        dst_lines = dst_text.splitlines(keepends=True)
        if src_lines == dst_lines:
            self._status_lbl.setText("✓ Identical")
            self._status_lbl.setStyleSheet(f"color:{t['success']};font-size:{font_sz()}px;")
        else:
            self._status_lbl.setText("≠ Different")
            self._status_lbl.setStyleSheet(f"color:{t['warning']};font-size:{font_sz()}px;")

        self._diff_view.setHtml(_colored_diff_html(src_lines, dst_lines, t))

    def _deploy(self, files: list[dict]) -> None:
        if not files:
            QMessageBox.information(self, "Nothing to deploy", "No files to deploy.")
            return
        if isinstance(self._worker, QThread) and self._worker.isRunning():
            QMessageBox.warning(self, "Busy", "Deployment already running.")
            return

        names = "\n".join(
            f"  • {f.get('title', _expand(first_path(f.get('source', ''))).name) or f.get('source', '?')}"
            for f in files
        )
        ans = QMessageBox.question(
            self, "Confirm Deploy",
            f"Deploy {len(files)} file(s) to the live system?\n\n{names}\n\n"
            f"{'A .bak backup will be created before overwriting.' if self._backup_cb.isChecked() else 'No backup will be created.'}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if ans != QMessageBox.StandardButton.Yes:
            return

        worker = _DeployWorker(files, backup=self._backup_cb.isChecked())
        self._worker = worker
        t = current_theme()

        def _on_progress(msg: str, is_err: bool) -> None:
            color = t["error"] if is_err else t["success"]
            self._diff_view.append(
                f"<p style='color:{color};font-family:monospace;font-size:{font_sz(-1)}px;'>{msg}</p>")

        def _on_done(ok: int, err: int) -> None:
            col = t["error"] if err else t["success"]
            self._diff_view.append(
                f"<p style='color:{col};font-family:monospace;font-size:{font_sz(1)}px;font-weight:bold;'>"
                f"Deploy complete: {ok} succeeded, {err} failed.</p>")
            self._load_files()

        worker.progress.connect(_on_progress)
        worker.finished.connect(_on_done)
        self._diff_view.clear()
        self._diff_view.setHtml(
            f"<p style='color:{t['accent']};font-family:monospace;font-size:{font_sz()}px;'>Deploying…</p>")
        worker.start()

    def _deploy_selected(self) -> None:
        item = self._list.currentItem()
        if not item:
            return
        f = item.data(Qt.ItemDataRole.UserRole)
        if isinstance(f, dict):
            self._deploy([f])

    def _deploy_all_changed(self) -> None:
        changed = []
        for i in range(self._list.count()):
            item = self._list.item(i)
            f = item.data(Qt.ItemDataRole.UserRole) if item else None
            if not isinstance(f, dict):
                continue
            src_raw = f.get("source", "")
            dst_raw = f.get("destination", "")
            src = _expand(first_path(src_raw))
            dst = _expand(first_path(dst_raw))
            src_text = _read_safe(src)
            if src_text is None:
                continue
            dst_text = _read_safe(dst)
            if dst_text != src_text:
                changed.append(f)
        self._deploy(changed)

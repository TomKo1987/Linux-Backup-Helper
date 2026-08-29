import difflib
import html as _html
import os
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QCloseEvent
from PyQt6.QtWidgets import (
    QCheckBox, QDialog, QFrame, QHBoxLayout, QLabel, QListWidget,
    QListWidgetItem, QMessageBox, QPushButton, QSplitter, QTextEdit,
    QVBoxLayout, QWidget,
)

from state import active_dotfiles
from themes import current_theme, font_sz, register_style_listener, unregister_style_listener
from translations import tr
from ui_utils import ask_yes_no, color_style, fit_button_width, header_bar_style, _StandardKeysMixin


def first_path(v) -> str:
    if isinstance(v, list):
        return str(v[0]) if v else ""
    return str(v) if v else ""


def _expand(path: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(path)))


def _read_safe(path: Path) -> str | None:
    try:
        if path.is_dir():
            return None
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


def _is_dir_safe(path: Path) -> bool:
    try:
        return path.is_dir()
    except PermissionError:
        return True
    except OSError:
        return False


def _dir_listing(path: Path) -> dict[str, tuple[int, int]] | None:
    result: dict[str, tuple[int, int]] = {}
    try:
        for root, _dirs, files in os.walk(path):
            for name in files:
                fp = Path(root) / name
                try:
                    st = fp.stat()
                except OSError:
                    continue
                rel = str(fp.relative_to(path))
                result[rel] = (st.st_size, st.st_mtime_ns)
        return result
    except OSError:
        return None


def _make_backup(dst: Path) -> None:
    if not dst.exists():
        return
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    if dst.is_dir():
        bak = dst.with_name(f"{dst.name}.bak_{ts}")
        shutil.copytree(dst, bak, symlinks=True)
    else:
        bak = dst.with_suffix(f"{dst.suffix}.bak_{ts}")
        shutil.copy2(dst, bak)


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
                f"✓  {tr('Files are identical — nothing to deploy.')}</p>")

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
                 f"… {tr('diff truncated (more than 2000 lines)')}</p>")
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
            if not _path_exists(src):
                self.progress.emit(f"  ✗ {tr('Source not found: {src}', src=src)}", True)
                err += 1
                continue

            is_dir = _is_dir_safe(src)
            try:
                if self._backup:
                    _make_backup(dst)
                dst.parent.mkdir(parents=True, exist_ok=True)
                if is_dir:
                    if dst.exists():
                        shutil.copytree(src, dst, symlinks=True, dirs_exist_ok=True)
                    else:
                        shutil.copytree(src, dst, symlinks=True)
                    n = sum(1 for _ in dst.rglob("*") if _.is_file())
                    self.progress.emit(f"  ✓ {src.name}/  →  {dst}  ({tr('{n} file(s)', n=n)})", False)
                else:
                    shutil.copy2(src, dst)
                    self.progress.emit(f"  ✓ {src.name}  →  {dst}", False)
                ok += 1
            except PermissionError:
                try:
                    if self._backup and _path_exists(dst):
                        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                        if dst.is_dir():
                            bak_path = dst.with_name(f"{dst.name}.bak_{ts}")
                            bres = subprocess.run(
                                ["sudo", "cp", "-a", str(dst), str(bak_path)],
                                capture_output=True, text=True, timeout=30)
                        else:
                            bak_path = dst.with_suffix(f"{dst.suffix}.bak_{ts}")
                            bres = subprocess.run(
                                ["sudo", "cp", "-p", str(dst), str(bak_path)],
                                capture_output=True, text=True, timeout=15)
                        if bres.returncode != 0:
                            self.progress.emit(
                                f"  ✗ {src.name}: {tr('sudo backup failed: {err}, skipping overwrite', err=bres.stderr.strip())}", True)
                            err += 1
                            continue

                    subprocess.run(
                        ["sudo", "mkdir", "-p", str(dst.parent)],
                        capture_output=True, text=True, timeout=15)

                    if is_dir:
                        r = subprocess.run(
                            ["sudo", "cp", "-a", "-T", str(src), str(dst)],
                            capture_output=True, text=True, timeout=60)
                    else:
                        r = subprocess.run(
                            ["sudo", "cp", str(src), str(dst)],
                            capture_output=True, text=True, timeout=30)

                    if r.returncode == 0:
                        suffix = "/" if is_dir else ""
                        self.progress.emit(f"  ✓ {src.name}{suffix}  →  {dst}  ({tr('via sudo')})", False)
                        ok += 1
                    else:
                        self.progress.emit(
                            f"  ✗ {src.name}: {tr('sudo cp failed: {err}', err=r.stderr.strip())}", True)
                        err += 1
                except subprocess.TimeoutExpired:
                    self.progress.emit(f"  ✗ {src.name}: {tr('sudo cp timed out')}", True)
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
        self.setWindowTitle(tr("Dotfiles Manager"))
        self.setMinimumSize(1500, 1000)
        self._files:  list[dict]     = []
        self._worker: _DeployWorker | None = None
        self._build_ui()
        self._load_files()
        register_style_listener(self._refresh_styles)
        self.finished.connect(lambda _r: unregister_style_listener(self._refresh_styles))

    def closeEvent(self, a0: QCloseEvent | None) -> None:
        if isinstance(self._worker, QThread) and self._worker.isRunning():
            self._worker.quit()
            self._worker.wait(2000)
        super().closeEvent(a0)

    def _build_ui(self) -> None:
        self._header_frame = QFrame()
        hl = QHBoxLayout(self._header_frame)
        hl.setContentsMargins(14, 10, 14, 10)
        self._title_lbl = QLabel(f"📄  {tr('Dotfiles Manager')}")
        self._sub_lbl = QLabel(tr("Deploy tracked config files from your profile to the live system"))
        hl.addWidget(self._title_lbl)
        hl.addStretch()
        hl.addWidget(self._sub_lbl)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        self._splitter = splitter

        self._left_widget = QWidget()
        ll = QVBoxLayout(self._left_widget)
        ll.setContentsMargins(8, 8, 4, 8)
        ll.setSpacing(6)

        self._list_hdr_lbl = QLabel(tr("Tracked Files"))
        ll.addWidget(self._list_hdr_lbl)

        self._list = QListWidget()
        self._list.currentRowChanged.connect(self._on_select)
        ll.addWidget(self._list)

        self._backup_cb = QCheckBox(tr("Create .bak backup before overwriting"))
        self._backup_cb.setChecked(True)
        ll.addWidget(self._backup_cb)

        self._btn_deploy_sel = QPushButton(f"⬇  {tr('Deploy Selected')}")
        self._btn_deploy_sel.setMinimumHeight(36)
        self._btn_deploy_sel.clicked.connect(self._deploy_selected)

        self._btn_deploy_all = QPushButton(f"⬇⬇  {tr('Deploy All Changed')}")
        self._btn_deploy_all.setMinimumHeight(36)
        self._btn_deploy_all.clicked.connect(self._deploy_all_changed)

        ll.addWidget(self._btn_deploy_sel)
        ll.addWidget(self._btn_deploy_all)

        self._right_widget = QWidget()
        rl = QVBoxLayout(self._right_widget)
        rl.setContentsMargins(4, 8, 8, 8)
        rl.setSpacing(4)

        diff_hdr_row = QHBoxLayout()
        self._diff_title = QLabel(tr("Select a file to see the diff"))
        self._status_lbl = QLabel()
        diff_hdr_row.addWidget(self._diff_title)
        diff_hdr_row.addStretch()
        diff_hdr_row.addWidget(self._status_lbl)
        rl.addLayout(diff_hdr_row)

        self._diff_view = QTextEdit()
        self._diff_view.setReadOnly(True)
        self._diff_view.setFont(QFont("monospace"))
        rl.addWidget(self._diff_view, 1)

        splitter.addWidget(self._left_widget)
        splitter.addWidget(self._right_widget)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 2)

        self._bottom_frame = QFrame()
        bl = QHBoxLayout(self._bottom_frame)
        bl.setContentsMargins(12, 8, 12, 8)
        self._info_lbl = QLabel()
        bl.addWidget(self._info_lbl)
        bl.addStretch()
        self._close_btn = QPushButton(tr("Close"))
        self._close_btn.setMinimumHeight(32)
        fit_button_width(self._close_btn, min_floor=100)
        self._close_btn.clicked.connect(self.accept)
        bl.addWidget(self._close_btn)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(self._header_frame)
        lay.addWidget(splitter, 1)
        lay.addWidget(self._bottom_frame)
        self._apply_styles()

    def _apply_styles(self) -> None:
        t = current_theme()
        bg = t["bg"]
        bg2 = t["bg2"]
        bg3 = t["bg3"]
        sep_col = t["header_sep"]
        acc = t["accent"]
        fg = t["text"]
        dim = t["text_dim"]

        self.setStyleSheet(f"background:{bg};color:{fg};")
        self._header_frame.setStyleSheet(header_bar_style(bg2, sep_col))
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
        self._backup_cb.setStyleSheet(color_style(dim, font_sz(-1)))
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

    def _refresh_styles(self) -> None:
        self._apply_styles()
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
            item = QListWidgetItem(f"  {tr('No dotfiles configured in profile.')}")
            item.setForeground(QColor(t["text_dim"]))
            item.setFlags(Qt.ItemFlag.NoItemFlags)
            self._list.addItem(item)
            self._info_lbl.setText(tr("Configure dotfiles in Settings → Dotfiles."))
            return

        identical = changed = missing = 0
        for f in self._files:
            src_raw = f.get("source", "")
            dst_raw = f.get("destination", "")
            src = _expand(first_path(src_raw))
            dst = _expand(first_path(dst_raw))

            title = f.get("title", src.name)

            if _is_dir_safe(src):
                if not _is_dir_safe(dst):
                    icon, color_key = "★ ", "warning"
                    changed += 1
                else:
                    src_list = _dir_listing(src)
                    dst_list = _dir_listing(dst)
                    if src_list is None:
                        icon, color_key = "✗ ", "error"
                        missing += 1
                    elif src_list == dst_list:
                        icon, color_key = "✓ ", "success"
                        identical += 1
                    else:
                        icon, color_key = "≠ ", "warning"
                        changed += 1
            elif not _path_exists(src):
                icon, color_key = "✗ ", "error"
                missing += 1
            else:
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
            tr("{count} files tracked  —  "
               "✓ {identical} identical  ·  ≠ {changed} changed/new  ·  ✗ {missing} missing source",
               count=len(self._files), identical=identical, changed=changed, missing=missing)
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

        if _is_dir_safe(src):
            self._show_dir_status(src, dst, t)
            return

        if not _path_exists(src):
            err_col = t["error"]
            self._diff_view.setHtml(
                f"<p style='color:{err_col};font-family:monospace;font-size:{font_sz()}px;'>"
                f"✗  {tr('Source not found:')}<br>{_html.escape(str(src))}</p>")
            self._set_status(f"⚠ {tr('Source missing')}", err_col)
            return

        src_text = _read_safe(src)
        dst_text = _read_safe(dst)

        if src_text is None:
            err_col = t["error"]
            self._diff_view.setHtml(
                f"<p style='color:{err_col};font-family:monospace;font-size:{font_sz()}px;'>"
                f"✗  {tr('Source file not found:')}<br>{_html.escape(str(src))}</p>")
            self._set_status(f"⚠ {tr('Source missing')}", err_col)
            return

        if dst_text is None:
            if _path_exists(dst):
                self._set_status(f"🔒 {tr('No read access')}", t['warning'])
                warn_col = t["warning"]
                self._diff_view.setHtml(
                    f"<p style='color:{warn_col};font-family:monospace;font-size:{font_sz()}px;'>"
                    f"🔒  {tr('File exists but cannot be read without elevated permissions.')}<br>"
                    f"{tr('Deploy will use')} <code>sudo cp</code> {tr('automatically.')}</p>")
            else:
                self._set_status(f"★ {tr('Not on system yet')}", t['warning'])
                warn_col = t["warning"]
                text_col = t["text"]
                escaped_preview = _html.escape(src_text[:4000])
                self._diff_view.setHtml(
                    f"<p style='color:{warn_col};font-family:monospace;font-size:{font_sz()}px;'>"
                    f"★  {tr('Destination does not exist yet — will be created:')}</p>"
                    f"<pre style='color:{text_col};font-family:monospace;font-size:{font_sz(-1)}px;'>{escaped_preview}</pre>")
            return

        src_lines = src_text.splitlines(keepends=True)
        dst_lines = dst_text.splitlines(keepends=True)
        if src_lines == dst_lines:
            self._set_status(f"✓ {tr('Identical')}", t['success'])
        else:
            self._set_status(f"≠ {tr('Different')}", t['warning'])

        self._diff_view.setHtml(_colored_diff_html(src_lines, dst_lines, t))

    def _set_status(self, text: str, color: str) -> None:
        self._status_lbl.setText(text)
        self._status_lbl.setStyleSheet(color_style(color, font_sz()))

    def _show_dir_status(self, src: Path, dst: Path, t: dict) -> None:
        src_list = _dir_listing(src)
        if src_list is None:
            err_col = t["error"]
            self._diff_view.setHtml(
                f"<p style='color:{err_col};font-family:monospace;font-size:{font_sz()}px;'>"
                f"✗  {tr('Source directory not readable:')}<br>{_html.escape(str(src))}</p>")
            self._set_status(f"⚠ {tr('Source unreadable')}", err_col)
            return

        if not _is_dir_safe(dst):
            warn_col = t["warning"]
            text_col = t["text"]
            files_preview = "<br>".join(_html.escape(p) for p in sorted(src_list)[:200])
            more = "" if len(src_list) <= 200 else f"<br>… {tr('and {n} more', n=len(src_list) - 200)}"
            self._set_status(f"★ {tr('Not on system yet')}", warn_col)
            self._diff_view.setHtml(
                f"<p style='color:{warn_col};font-family:monospace;font-size:{font_sz()}px;'>"
                f"★  {tr('Destination directory does not exist yet — will be created ({n} file(s)):', n=len(src_list))}</p>"
                f"<pre style='color:{text_col};font-family:monospace;font-size:{font_sz(-1)}px;'>{files_preview}{more}</pre>")
            return

        dst_list = _dir_listing(dst) or {}
        only_src = sorted(set(src_list) - set(dst_list))
        only_dst = sorted(set(dst_list) - set(src_list))
        changed = sorted(k for k in (set(src_list) & set(dst_list)) if src_list[k] != dst_list[k])

        if not only_src and not only_dst and not changed:
            self._set_status(f"✓ {tr('Identical')}", t['success'])
            self._diff_view.setHtml(
                f"<p style='color:{t['success']};font-family:monospace;font-size:{font_sz()}px;'>"
                f"✓  {tr('Directory contents are identical ({n} file(s)) — nothing to deploy.', n=len(src_list))}</p>")
            return

        self._set_status(f"≠ {tr('Different')}", t['warning'])

        rows = []
        add_col = t.get("success", "#4ec994")
        rem_col = t.get("error",   "#f7768e")
        ctx_col = t.get("text_dim", "#565f89")
        for p in only_src:
            rows.append(f"<p style='margin:0;color:{add_col};font-family:monospace;"
                        f"font-size:{font_sz(-1)}px;white-space:pre;'>+ {_html.escape(p)}</p>")
        for p in changed:
            rows.append(f"<p style='margin:0;color:{t['warning']};font-family:monospace;"
                        f"font-size:{font_sz(-1)}px;white-space:pre;'>≠ {_html.escape(p)}</p>")
        for p in only_dst:
            rows.append(f"<p style='margin:0;color:{rem_col};font-family:monospace;"
                        f"font-size:{font_sz(-1)}px;white-space:pre;'>- {_html.escape(p)}</p>")

        legend = (f"<p style='color:{ctx_col};font-family:monospace;font-size:{font_sz(-1)}px;'>"
                 f"+ {tr('new/missing on system')}   ≠ {tr('different')}   - {tr('only on system (not in profile)')}</p>")
        bg = t.get("bg", "#1a1b26")
        html = legend + f"<div style='background:{bg};padding:8px;border-radius:4px;'>" + "\n".join(rows) + "</div>"
        self._diff_view.setHtml(html)

    def _deploy(self, files: list[dict]) -> None:
        if not files:
            QMessageBox.information(self, tr("Nothing to deploy"), tr("No files to deploy."))
            return
        if isinstance(self._worker, QThread) and self._worker.isRunning():
            QMessageBox.warning(self, tr("Busy"), tr("Deployment already running."))
            return

        names = "\n".join(
            f"  • {f.get('title', _expand(first_path(f.get('source', ''))).name) or f.get('source', '?')}"
            for f in files
        )
        ans = ask_yes_no(
            self, tr("Confirm Deploy"),
            tr("Deploy {n} file(s) to the live system?\n\n{names}\n\n{backup_note}",
               n=len(files), names=names,
               backup_note=(tr('A .bak backup will be created before overwriting.') if self._backup_cb.isChecked() else tr('No backup will be created.'))),
        )
        if ans != QMessageBox.StandardButton.Yes:
            return

        worker = _DeployWorker(files, backup=self._backup_cb.isChecked())
        self._worker = worker
        t = current_theme()

        def _on_progress(msg: str, is_err: bool) -> None:
            color = t["error"] if is_err else t["success"]
            self._diff_view.append(
                f"<p style='color:{color};font-family:monospace;font-size:{font_sz(-1)}px;'>{_html.escape(msg)}</p>")

        def _on_done(ok: int, err: int) -> None:
            col = t["error"] if err else t["success"]
            self._diff_view.append(
                f"<p style='color:{col};font-family:monospace;font-size:{font_sz(1)}px;font-weight:bold;'>"
                f"{tr('Deploy complete: {ok} succeeded, {err} failed.', ok=ok, err=err)}</p>")
            self._load_files()

        worker.progress.connect(_on_progress)
        worker.finished.connect(_on_done)
        self._diff_view.clear()
        self._diff_view.setHtml(
            f"<p style='color:{t['accent']};font-family:monospace;font-size:{font_sz()}px;'>{tr('Deploying…')}</p>")
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

            if _is_dir_safe(src):
                src_list = _dir_listing(src)
                if src_list is None:
                    continue
                dst_list = _dir_listing(dst) if _is_dir_safe(dst) else {}
                if src_list != dst_list:
                    changed.append(f)
                continue

            if not _path_exists(src):
                continue
            src_text = _read_safe(src)
            if src_text is None:
                continue
            dst_text = _read_safe(dst)
            if dst_text != src_text:
                changed.append(f)
        self._deploy(changed)

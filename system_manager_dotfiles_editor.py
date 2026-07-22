from pathlib import Path
from typing import TYPE_CHECKING

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QFont, QFontMetrics
from PyQt6.QtWidgets import (
    QApplication, QLabel, QLineEdit, QMessageBox, QPushButton, QFileDialog, QFrame,
    QHBoxLayout, QVBoxLayout, QWidget
)

from dotfiles_manager import first_path
from state import S, _HOME, apply_replacements, save_profile
from themes import font_sz, current_theme, tri_state_legend_html
from ui_utils import browse_field

from system_manager_helpers import (
    _scroll_dlg, _read_import_file, TriCheckBox, _make_tri_cb, _add_select_all_tri,
    _STATE_DELETE, _STATE_DISABLED
)

if TYPE_CHECKING:
    _DotfilesMixinBase = QWidget
else:
    _DotfilesMixinBase = object


class _DotfilesEditorMixin(_DotfilesMixinBase):
    if TYPE_CHECKING:
        def _export_dotfiles(self) -> None: ...
        def _reopen_dotfiles(self) -> None: ...

    def _edit_dotfiles(self) -> None:
        files = sorted(
            [f for f in (S.dotfiles or []) if isinstance(f, dict) and f.get("source") and f.get("destination")],
            key=lambda f: Path(first_path(f["source"])).name.lower()
        )
        checkboxes: list[tuple[TriCheckBox, dict]] = []
        rows: list[tuple[QFrame, TriCheckBox, dict]] = []
        legend = tri_state_legend_html()
        t = current_theme()
        body = QWidget()
        vlay = QVBoxLayout(body)
        vlay.setSpacing(4)

        for idx, f in enumerate(files):
            filename = Path(first_path(f["source"])).name or first_path(f["source"])
            tip = (f"<b>Source:</b><br>{f['source']}<br><br><b>Destination:</b><br>{f['destination']}<br><br>"
                   f"<i>Left-click to change status. Click + to expand &amp; edit.</i><br><br>{legend}")
            cb = _make_tri_cb(filename, f.get("disabled", False), tip)
            checkboxes.append((cb, f))

            outer = QFrame()
            bg = t["bg2"] if idx % 2 == 0 else t["bg3"]
            outer.setStyleSheet(f"QFrame{{background-color:{bg};border-radius:6px;}}")
            outer_vlay = QVBoxLayout(outer)
            outer_vlay.setContentsMargins(6, 2, 6, 2)
            outer_vlay.setSpacing(0)

            header_row = QHBoxLayout()
            header_row.setContentsMargins(0, 0, 0, 0)
            header_row.addWidget(cb, 1)
            hint = QLabel("+")
            hint.setStyleSheet(f"font-size:{font_sz(10)}px")
            hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
            header_row.addWidget(hint)
            outer_vlay.addLayout(header_row)

            detail = QWidget()
            detail.setVisible(False)
            det_lay = QVBoxLayout(detail)
            det_lay.setContentsMargins(4, 6, 4, 6)
            det_lay.setSpacing(6)

            src_ed = QLineEdit(f.get("source", ""))
            dst_ed = QLineEdit(f.get("destination", ""))
            for ed in (src_ed, dst_ed):
                ed.setMinimumHeight(32)

            for lbl_text, ed in [("Source:", src_ed), ("Destination:", dst_ed)]:
                lbl = QLabel(lbl_text)
                lbl.setStyleSheet(f"color:{t['muted']};font-size:{font_sz(-1)}px;")
                det_lay.addWidget(lbl)
                det_lay.addWidget(browse_field(self, ed))

            apply_btn = QPushButton("Apply")
            apply_btn.setMaximumWidth(110)
            apply_btn.setMinimumHeight(30)

            def _make_apply(entry, s_ed, d_ed, _cb):
                def _do():
                    new_src = s_ed.text().strip()
                    new_dst = d_ed.text().strip()
                    if not new_src or not new_dst:
                        QMessageBox.warning(self, "Error", "Source and destination must not be empty.")
                        return
                    entry["source"] = new_src
                    entry["destination"] = new_dst
                    _cb.setText(Path(new_src).name or new_src)
                    save_profile()

                return _do

            apply_btn.clicked.connect(_make_apply(f, src_ed, dst_ed, cb))
            apply_row = QHBoxLayout()
            apply_row.addStretch()
            apply_row.addWidget(apply_btn)
            det_lay.addLayout(apply_row)

            outer_vlay.addWidget(detail)
            vlay.addWidget(outer)
            rows.append((outer, cb, f))

            def _make_toggle(_det=detail, _hint=hint):
                def _toggle():
                    visible = not _det.isVisible()
                    _det.setVisible(visible)
                    _hint.setText("-" if visible else "+")

                return _toggle

            _toggle_fn = _make_toggle()
            hint.mousePressEvent = lambda e, _fn=_toggle_fn: _fn()

        if checkboxes:
            _add_select_all_tri(vlay, [cb for cb, _ in checkboxes])

        def _save(_dlg):
            to_del = [f for _cb_, f in checkboxes if _cb_.checkState() == _STATE_DELETE]
            do_delete = True
            if to_del:
                names = "\n".join(f"  • {apply_replacements(f.get('source', '?'))}" for f in to_del)
                if QMessageBox.question(_dlg, "Confirm Delete",
                                        f"The following dotfile(s) will be permanently removed:\n\n{names}\n\nContinue?",
                                        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
                                        ) != QMessageBox.StandardButton.Yes:
                    do_delete = False

            updated_files = []
            for item in (S.dotfiles or []):
                if item not in files:
                    updated_files.append(item)
                    continue
                cb_match = next((__cb for __cb, _f in checkboxes if _f == item), None)
                if cb_match:
                    if do_delete and cb_match.checkState() == _STATE_DELETE:
                        continue
                    updated_files.append({**item, "disabled": cb_match.checkState() == _STATE_DISABLED})
                else:
                    updated_files.append(item)

            S.dotfiles = sorted(
                updated_files,
                key=lambda x: Path(x.get("source", "")).name.lower() if isinstance(x, dict) else ""
            )
            save_profile()
            _dlg.accept()

        dlg, lay = _scroll_dlg(self, "Dotfiles", body, _save)

        if files:
            fm = QFontMetrics(QFont("monospace"))
            longest = max(max(len(f.get("source", "")), len(f.get("destination", "")))
                          for f in files)
            needed_w = fm.horizontalAdvance("m") * longest + 280
            scr = QApplication.primaryScreen()
            max_w = (scr.availableGeometry().width() - 50) if scr else 1800
            dlg.resize(max(dlg.width(), min(needed_w, max_w)), dlg.height())

        search = QLineEdit()
        search.setPlaceholderText("Filter files...")

        def _apply_search(txt: str) -> None:
            lo = txt.lower()
            for _outer, _cb, _f in rows:
                visible = (lo in _cb.text().lower()
                           or lo in _f.get("source", "").lower()
                           or lo in _f.get("destination", "").lower())
                _outer.setVisible(visible)

        search.textChanged.connect(_apply_search)

        def _on_add_clicked():
            dlg.close()
            QTimer.singleShot(100, self._add_dotfile)

        add_btn = QPushButton("+ Add Dotfile")
        add_btn.clicked.connect(_on_add_clicked)
        add_row = QHBoxLayout()
        add_row.addWidget(add_btn)

        io_row = QHBoxLayout()

        def _open_manager():
            dlg.close()
            from dotfiles_manager import DotfilesManagerDialog
            DotfilesManagerDialog(self).exec()
            QTimer.singleShot(0, self._edit_dotfiles)

        manager_btn = QPushButton("📄 Open Dotfiles Manager")
        manager_btn.setMinimumHeight(34)
        manager_btn.clicked.connect(_open_manager)

        def _on_import_clicked():
            dlg.close()
            QTimer.singleShot(0, self._import_dotfiles)

        for lbl, fn in [("Import (.txt/.csv)", _on_import_clicked), ("Export (.txt)", self._export_dotfiles)]:
            b = QPushButton(lbl)
            b.clicked.connect(fn)
            io_row.addWidget(b)

        lay.insertWidget(1, search)
        lay.insertLayout(2, add_row)
        lay.insertWidget(3, manager_btn)
        lay.insertLayout(4, io_row)
        lay.setStretch(0, 1)
        dlg.exec()

    def _add_dotfile(self) -> None:
        box = QMessageBox(self)
        box.setWindowTitle("Add Dotfile/Folder")
        box.setText("Choose the source type:")
        file_btn = box.addButton("📄 File(s)", QMessageBox.ButtonRole.YesRole)
        box.addButton("📁 Directory", QMessageBox.ButtonRole.NoRole)
        cancel_btn = box.addButton(QMessageBox.StandardButton.Cancel)
        box.exec()
        clicked = box.clickedButton()
        if clicked == cancel_btn:
            return

        sources = (QFileDialog.getOpenFileNames(self, "Select file(s)")[0] if clicked == file_btn
                   else [d] if (d := QFileDialog.getExistingDirectory(self, "Select directory")) else [])
        sources = [s for s in sources if s]
        if not sources:
            return

        dst_dir = QFileDialog.getExistingDirectory(self, "Select Destination Directory (e.g. /etc/...)")
        if not dst_dir:
            return

        S.dotfiles = S.dotfiles or []
        added = []
        for s in sources:
            src_path = Path(s).resolve()
            dst_path = Path(dst_dir) / src_path.name

            if not any(f.get("source") == str(src_path) if isinstance(f, dict) else False for f in S.dotfiles):
                S.dotfiles.append({"source": str(src_path), "destination": str(dst_path), "disabled": False})
                added.append(src_path.name)

        if added:
            S.dotfiles.sort(
                key=lambda x: Path(x.get("source", "")).name.lower() if isinstance(x, dict) else str(x).lower())
            save_profile()
            QMessageBox.information(self, "Success", f"Added {len(added)} item(s).")
        QTimer.singleShot(0, self._edit_dotfiles)

    def _import_dotfiles(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Import Dotfiles", str(_HOME), "Data (*.txt *.csv)")
        if not path:
            self._reopen_dotfiles()
            return
        lines = _read_import_file(self, path)
        if lines is None:
            self._reopen_dotfiles()
            return

        S.dotfiles = S.dotfiles or []
        existing: set[tuple] = {(f["source"], f["destination"]) for f in S.dotfiles if isinstance(f, dict)}
        added = skipped_dup = skipped_inv = 0

        for raw in lines:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            parts = [p.strip() for p in (line.split("\t", 1) if "\t" in line else line.split(",", 1))]
            if len(parts) != 2:
                skipped_inv += 1
                continue
            src, dst = parts
            if not src or not dst:
                skipped_inv += 1
                continue
            if not (src.startswith(("/", "~"))) or not (dst.startswith(("/", "~"))):
                skipped_inv += 1
                continue
            src = str(Path(src).expanduser())
            dst = str(Path(dst).expanduser())
            if (src, dst) in existing:
                skipped_dup += 1
                continue
            S.dotfiles.append({"source": src, "destination": dst, "disabled": False})
            existing.add((src, dst))
            added += 1

        if added:
            S.dotfiles.sort(
                key=lambda x: Path(x.get("source", "")).name.lower() if isinstance(x, dict) else str(x).lower())
            save_profile()

        parts_msg = [f"Imported: {added}"]
        if skipped_dup: parts_msg.append(f"Skipped (duplicate): {skipped_dup}")
        if skipped_inv: parts_msg.append(f"Skipped (invalid format): {skipped_inv}")
        QMessageBox.information(self, "Import Complete", "\n".join(parts_msg))
        self._reopen_dotfiles()

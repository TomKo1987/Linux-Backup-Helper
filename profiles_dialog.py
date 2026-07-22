import json
import shutil
import tarfile
from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QDialog, QDialogButtonBox, QFileDialog, QLabel, QListWidget, QListWidgetItem, QMessageBox,
    QPushButton, QVBoxLayout,
)

from profile_compare import ProfileCompareDialog
from state import (
    S, _HOME, _PROFILES_DIR, _PROFILE_RE, _atomic_write, list_profiles, load_profile, logger, save_profile,
)
from themes import current_theme
from ui_utils import ask_profile_name, btn_row, hdr_label, sep

_ARCHIVE_MAX_PROFILE_BYTES = 1024 * 1024

def _clear_default_flag(profile_name: str, caller: str) -> None:
    if not profile_name:
        return
    path = _PROFILES_DIR / f"{profile_name}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.pop("is_default", None):
            _atomic_write(path, data)
    except (FileNotFoundError, json.JSONDecodeError, OSError) as exc:
        logger.warning("%s: could not clear is_default in '%s': %s", caller, path.name, exc)

class ProfilesDialog(QDialog):

    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle("Profile Manager")
        self.setMinimumSize(700, 520)
        self.was_changed: bool = False
        t      = current_theme()
        layout = QVBoxLayout(self)
        layout.addWidget(hdr_label("Profile Manager"))
        layout.addWidget(sep())
        self._active_lbl = QLabel()
        self._active_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._active_lbl.setStyleSheet(f"color:{t['accent']};font-weight:bold;padding:4px;")
        layout.addWidget(self._active_lbl)
        self.item_list = QListWidget()
        self.item_list.itemDoubleClicked.connect(self._load)
        layout.addWidget(self.item_list, 1)
        layout.addLayout(btn_row([("▶ Load", self._load), ("🆕 New", self._new),
                                   ("⎘ Duplicate", self._copy), ("✕ Delete", self._del)]))
        layout.addLayout(btn_row([("⬆ Import", self._import), ("⬇ Export", self._export)]))
        layout.addLayout(btn_row([("⚖ Compare", self._compare_profiles)]))
        layout.addWidget(sep())
        close_btn = QPushButton("✕ Close")
        close_btn.clicked.connect(self.accept)
        layout.addWidget(close_btn)
        self._refresh()

    def _activate_profile(self, name: str) -> bool:
        if load_profile(_PROFILES_DIR / f"{name}.json"):
            save_profile()
            self.was_changed = True
            self._refresh()
            return True
        return False

    def _refresh(self) -> None:
        t   = current_theme()
        row = self.item_list.currentRow()
        self._active_lbl.setText(f"Active profile:  {S.profile_name or '—'}")
        self.item_list.clear()
        for name in list_profiles():
            active = name == S.profile_name
            item   = QListWidgetItem(f"  {'✓ ' if active else '  '}{name}")
            item.setForeground(QColor(t["accent"] if active else t["text"]))
            item.setData(Qt.ItemDataRole.UserRole, name)
            self.item_list.addItem(item)
        if 0 <= row < self.item_list.count(): self.item_list.setCurrentRow(row)

    def _selected_name(self) -> str | None:
        item = self.item_list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def _compare_profiles(self) -> None:
        if len(list_profiles()) < 2:
            QMessageBox.information(self, "Profile Compare", "You need at least two saved profiles to compare.")
            return
        ProfileCompareDialog(self).exec()

    def _load(self) -> None:
        name = self._selected_name()
        if not name:
            QMessageBox.information(self, "Load Profile", "Please select a profile first.")
            return
        if name == S.profile_name:
            QMessageBox.information(self, "Load Profile", f"'{name}' is already the active profile.")
            return
        _clear_default_flag(S.profile_name, "_load")

        if self._activate_profile(str(name)):
            QMessageBox.information(self, "Profile Loaded", f"Profile '{name}' is now active.")
        else:
            QMessageBox.critical(self, "Error", f"Could not load profile '{name}'.")

    def _new(self) -> None:
        name = ask_profile_name("New Profile", "", self)
        if not name: return
        dest = _PROFILES_DIR / f"{name}.json"
        if dest.exists() and QMessageBox.question(
                self, "Overwrite?", f"Profile '{name}' already exists. Overwrite it?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
            return
        prev_name = S.profile_name
        _clear_default_flag(prev_name, "_new")
        S.reset_to_fresh()
        S.profile_name = name
        if not save_profile():
            QMessageBox.critical(self, "Error", f"Could not save profile '{name}'.")
            if prev_name:
                load_profile(_PROFILES_DIR / f"{prev_name}.json")
            return
        self.was_changed = True
        self._refresh()
        QMessageBox.information(self, "Profile Created", f"Blank profile '{name}' created and is now active.\n\n"
                                                         "Go to Settings → Header Settings to add headers before creating entries.")

    def _copy(self) -> None:
        src_name = self._selected_name() or S.profile_name
        if not src_name:
            QMessageBox.information(self, "Duplicate", "No profile selected.")
            return
        name = ask_profile_name("Duplicate Profile", f"{src_name} copy", self)
        if not name: return
        src_path = _PROFILES_DIR / f"{src_name}.json"
        if not src_path.exists() and src_name == S.profile_name:
            if not save_profile():
                QMessageBox.critical(self, "Error", f"Could not save profile '{src_name}' before duplicating.")
                return
        if not src_path.exists():
            QMessageBox.warning(self, "Duplicate", f"Source file for '{src_name}' not found.")
            return
        dest = _PROFILES_DIR / f"{name}.json"
        if dest.exists() and QMessageBox.question(self, "Overwrite Profile?", f"Profile '{name}' already exists. Overwrite?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
            return
        try:
            shutil.copy2(src_path, dest)
        except OSError as exc:
            QMessageBox.critical(self, "Duplicate Failed", f"Could not copy profile '{src_name}':\n{exc}")
            return
        _clear_default_flag(name, "_copy")
        self.was_changed = True
        self._refresh()
        QMessageBox.information(self, "Duplicated", f"'{src_name}' duplicated as '{name}'.")

    def _del(self) -> None:
        name = self._selected_name()
        if not name: return
        if name == S.profile_name:
            QMessageBox.warning(self, "Delete Profile", "Cannot delete the currently active profile.")
            return
        if QMessageBox.question(self, "Delete Profile", f"Permanently delete profile '{name}'?",
                                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No) \
                == QMessageBox.StandardButton.Yes:
            try:
                (_PROFILES_DIR / f"{name}.json").unlink(missing_ok=True)
                self.was_changed = True
                self._refresh()
            except Exception as exc: QMessageBox.critical(self, "Error", f"Could not delete profile: {exc}")

    def _import(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Import profile(s)", str(_HOME),
                                              "Profile files (*.json *.tar.gz *.tgz);;JSON (*.json);;Archive (*.tar.gz *.tgz)")
        if not path: return
        _PROFILES_DIR.mkdir(parents=True, exist_ok=True)
        if path.endswith((".tar.gz", ".tgz")):
            self._import_archive(path)
            return
        name = ask_profile_name("Import Profile", Path(path).stem, self)
        if not name: return
        dest = _PROFILES_DIR / f"{name}.json"
        if dest.exists() and QMessageBox.question(self, "Overwrite?", f"Profile '{name}' already exists. Overwrite it?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
            return
        try:
            shutil.copy2(path, dest)
        except (OSError, shutil.Error) as exc:
            QMessageBox.critical(self, "Import Failed", f"Could not copy profile:\n{exc}")
            return
        self._refresh()
        if QMessageBox.question(self, "Import Complete", f"'{name}' imported successfully.\nLoad it now?",
                                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes:
            _clear_default_flag(S.profile_name, "_import")
            if not self._activate_profile(name):
                QMessageBox.critical(self, "Error", f"Could not load profile '{name}'.")

    def _import_archive(self, path: str) -> None:
        try:
            with tarfile.open(path, "r:gz") as tar:
                members = [m for m in tar.getmembers() if m.isfile() and m.name.endswith(".json")]
                if not members:
                    QMessageBox.warning(self, "Import", "The archive contains no .json profile files.")
                    return
                imported, skipped = [], []
                for member in members:
                    stem = Path(member.name).stem
                    p = Path(member.name)
                    if ".." in p.parts:
                        skipped.append(f"{stem} (rejected: path traversal)")
                        continue
                    if p.parent != Path("."):
                        skipped.append(f"{stem} (skipped: not a top-level file)")
                        continue
                    if not _PROFILE_RE.match(stem):
                        skipped.append(f"{stem} (invalid name)")
                        continue
                    dest      = _PROFILES_DIR / f"{stem}.json"
                    overwrite = True
                    if dest.exists():
                        ans = QMessageBox.question(self, "Overwrite?", f"Profile '{stem}' already exists. Overwrite?",
                            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No | QMessageBox.StandardButton.Cancel)
                        if ans == QMessageBox.StandardButton.Cancel: break
                        overwrite = ans == QMessageBox.StandardButton.Yes
                    if overwrite:
                        f = tar.extractfile(member)
                        if f:
                            with f:
                                raw = f.read(_ARCHIVE_MAX_PROFILE_BYTES + 1)
                            if len(raw) > _ARCHIVE_MAX_PROFILE_BYTES:
                                skipped.append(f"{stem} (file too large, max 1 MiB)")
                                continue
                            try:
                                json.loads(raw)
                            except json.JSONDecodeError as exc:
                                skipped.append(f"{stem} (invalid JSON: {exc})")
                                continue
                            dest.write_bytes(raw)
                            imported.append(stem)
                        else:
                            skipped.append(f"{stem} (extraction failed)")
                    else: skipped.append(f"{stem} (skipped)")
        except Exception as exc:
            QMessageBox.critical(self, "Import Failed", str(exc))
            return
        self._refresh()
        parts = []
        if imported: parts.append("Imported:\n  " + "\n  ".join(imported))
        if skipped:  parts.append("Skipped:\n  "  + "\n  ".join(skipped))
        QMessageBox.information(self, "Import Complete", "\n\n".join(parts) or "Nothing imported.")
        if len(imported) == 1 and QMessageBox.question(self, "Load Profile", f"Load '{imported[0]}' now?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes:
            _clear_default_flag(S.profile_name, "_import_archive")
            if not self._activate_profile(imported[0]):
                QMessageBox.critical(self, "Error", f"Could not load profile '{imported[0]}'.")

    def _export(self) -> None:
        profiles = list_profiles()
        if not profiles:
            QMessageBox.information(self, "Export", "No profiles to export.")
            return
        selected_name = self._selected_name() or S.profile_name
        export_all    = False

        if len(profiles) > 1:
            from PyQt6.QtWidgets import QButtonGroup, QRadioButton
            choice_dlg = QDialog(self)
            choice_dlg.setWindowTitle("Export — What to export?")
            vl     = QVBoxLayout(choice_dlg)
            vl.addWidget(QLabel("What would you like to export?"))
            rb_sel = QRadioButton(f"Selected profile only  ({selected_name})  →  .json")
            rb_all = QRadioButton(f"All {len(profiles)} profiles  →  .tar.gz archive")
            rb_sel.setChecked(True)
            bg = QButtonGroup(choice_dlg)
            bg.addButton(rb_sel)
            bg.addButton(rb_all)
            vl.addWidget(rb_sel)
            vl.addWidget(rb_all)
            _buttons = QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel  # type: ignore[attr-defined]
            bb = QDialogButtonBox(_buttons)
            bb.accepted.connect(choice_dlg.accept)
            bb.rejected.connect(choice_dlg.reject)
            vl.addWidget(bb)
            if choice_dlg.exec() != QDialog.DialogCode.Accepted: return
            export_all = rb_all.isChecked()

        if export_all:
            ts      = datetime.now().strftime("%Y%m%d_%H%M%S")
            path, _ = QFileDialog.getSaveFileName(self, "Export all profiles",
                                          str(_HOME / f"backup_helper_profiles_{ts}.tar.gz"), "Archive (*.tar.gz)")
            if not path: return
            if not path.endswith(".tar.gz"): path += ".tar.gz"
            try:
                if S.profile_name:
                    save_profile()
                    self.was_changed = True
                with tarfile.open(path, "w:gz") as tar:
                    for name in profiles:
                        src = _PROFILES_DIR / f"{name}.json"
                        if src.exists(): tar.add(src, arcname=f"{name}.json")
                QMessageBox.information(self, "Exported", f"All {len(profiles)} profiles exported to:\n{path}")
            except Exception as exc: QMessageBox.critical(self, "Export Failed", str(exc))
            return

        name = selected_name
        if not name:
            QMessageBox.information(self, "Export", "No profile to export.")
            return
        src = _PROFILES_DIR / f"{name}.json"
        if not src.exists():
            if name == S.profile_name:
                if not save_profile():
                    QMessageBox.critical(self, "Error", "Could not save profile before export.")
                    return
            else:
                QMessageBox.warning(self, "Export", f"Profile file for '{name}' not found.")
                return
        path, _ = QFileDialog.getSaveFileName(self, "Export profile", str(_HOME / f"{name}.json"), "JSON (*.json)")
        if path:
            shutil.copy2(src, path)
            QMessageBox.information(self, "Exported", f"Profile '{name}' exported to:\n{path}")

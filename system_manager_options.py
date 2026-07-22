from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sudo_password import SecureString

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFrame,
    QGridLayout, QHBoxLayout,
    QLabel, QLineEdit, QMessageBox, QProgressDialog, QPushButton, QScrollArea, QSizePolicy,
    QTextEdit, QVBoxLayout, QWidget
)

from firewall_rules import firewall_rules_tooltip
from linux_distro_helper import LinuxDistroHelper, SESSIONS, is_valid_pkg_name
from state import S, _HOME, save_profile, sort_pkg_list, sort_specific_pkg_list
from themes import (
    style_label_info, style_label_mono, style_op_label, apply_tooltip, style_sudo_checkbox,
    current_theme, font_sz
)
from tooltips import sudo_checkbox_tooltip
from ui_utils import ask_text, ok_cancel_buttons

from system_manager_helpers import (
    _is_specific, _commit_pkgs, _pkg_checkboxes, _scroll_dlg, _read_import_file, _pkg_form_dialog,
    _check_aur_helper_installed, _detect_effective_aur_helper, _raw_to_label_html, _raw_to_tips,
    PackageVerifierThread, _add_select_all_tri, _STATE_DELETE, _STATE_DISABLED,
    _detect_boot_info, _compute_op_status, _build_op_text
)
from system_manager_ops_editor import _OpsEditorMixin
from system_manager_dotfiles_editor import _DotfilesEditorMixin


class SystemManagerOptions(_OpsEditorMixin, _DotfilesEditorMixin, QDialog):

    def __init__(self, _parent=None, distro: LinuxDistroHelper | None = None):
        super().__init__(_parent)
        self.setWindowTitle("System Manager Options")
        self.setMinimumSize(1200, 680)
        self._distro = distro or LinuxDistroHelper()
        self._session = self._distro.detect_session()
        self._aur_helper_installed: bool | None = None
        self._verifier_threads: list[PackageVerifierThread] = []
        self._build()

    def _build(self) -> None:
        lay = QVBoxLayout(self)
        if self._distro.has_aur:
            _helper, _ok = _detect_effective_aur_helper(self._distro)
            aur_helper_info = f"   |   AUR Helper: '{_helper}' {'detected' if _ok else 'not detected'}"
        else:
            aur_helper_info = ""
        info = QLabel(
            f"Recognized Linux distribution: {self._distro.distro_pretty_name}   |   Session: {self._session}{aur_helper_info}")
        info.setStyleSheet(style_label_info(bold=True) + f"font-size:{font_sz()}px")
        info.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(info)

        cmd = self._distro.get_pkg_install_cmd("")
        top_text = QLabel(
            f"First you can select 'Dotfiles' in System Manager. These files will be copied using 'sudo', "
            f"for root privilege.\nIf you have 'Dotfiles' selected, System Manager will copy these first. "
            f"This allows you to copy files\nsuch as 'pacman.conf' or 'smb.conf' to '/etc/'.\n\n\n"
            f"Under 'System Manager Operations' you can specify how you would like to proceed. "
            f"Each operation is executed\none after the other. Uncheck operations to disable them.\n\n"
            f"Tips:\n\n"
            f"'Basic Packages' will be installed using '{cmd}'.\n\n"
            f"'AUR Packages' provides access to the Arch User Repository. "
            f"Therefore an AUR helper ({S.aur_helper}) must and will be installed."
            f"\nThis feature is available only on Arch Linux based distributions.\n\n"
            f"You can also define 'Specific Packages'. These packages will be installed only (using '{cmd}')\n"
            f"if the corresponding session has been recognized.\n"
            f"Both full desktop environments and window managers such as 'Hyprland' and others are supported.")
        top_text.setWordWrap(False)
        top_text.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        top_text.setAlignment(Qt.AlignmentFlag.AlignCenter)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(top_text)
        lay.addWidget(scroll)

        for row_specs in [[("System Manager Operations", self._edit_ops), ("Dotfiles", self._edit_dotfiles)],
                          [("Basic Packages", lambda: self._edit_pkgs("basic_packages")),
                           ("AUR Packages", lambda: self._edit_pkgs("aur_packages")),
                           ("Specific Packages", lambda: self._edit_pkgs("specific_packages"))]]:
            row = QHBoxLayout()
            for label, fn in row_specs:
                b = QPushButton(label)
                b.clicked.connect(fn)
                row.addWidget(b)
            lay.addLayout(row)

        close = QPushButton("Close")
        close.clicked.connect(self.close)
        lay.addWidget(close)

    @property
    def aur_helper_installed(self) -> bool:
        if self._aur_helper_installed is None:
            self._aur_helper_installed = _check_aur_helper_installed(self._distro)
        return bool(self._aur_helper_installed)

    def _reopen_pkgs(self, pkg_type: str) -> None:
        QTimer.singleShot(0, lambda: self._edit_pkgs(pkg_type))

    def _reopen_dotfiles(self) -> None:
        QTimer.singleShot(0, self._edit_dotfiles)

    def _edit_pkgs(self, pkg_type: str) -> None:
        is_specific = _is_specific(pkg_type)
        packages = getattr(S, pkg_type, []) or []
        checkboxes = _pkg_checkboxes(packages, is_specific)
        session_headers: dict[str, QLabel] = {}

        body = QWidget()
        grid = QGridLayout(body)
        grid.setSpacing(6)
        cols = 5
        t = current_theme()

        if is_specific:
            from collections import defaultdict
            groups: dict = defaultdict(list)
            for p, cb in zip(packages, checkboxes, strict=True):
                groups[p.get("session", "") if isinstance(p, dict) else ""].append((cb, p))
            row = 0
            for idx, sess in enumerate(sorted(groups)):
                hdr = QLabel(sess or "Unknown")
                border = f"border-top:1px solid {t['header_sep']};" if idx > 0 else ""
                hdr.setStyleSheet(
                    f"font-size:{font_sz(-1)}px;font-weight:bold;color:{t['accent2']};padding:6px 2px 2px;{border}")
                session_headers[sess] = hdr
                grid.addWidget(hdr, row, 0, 1, cols)
                row += 1
                for j, (cb, _) in enumerate(groups[sess]):
                    r_idx = row + j // cols
                    frame = QFrame()
                    bg = t["bg2"] if r_idx % 2 == 0 else t["bg3"]
                    frame.setStyleSheet(f"QFrame{{background-color:{bg};border-radius:4px;}}")
                    flay = QHBoxLayout(frame)
                    flay.setContentsMargins(6, 3, 6, 3)
                    flay.addWidget(cb)
                    grid.addWidget(frame, r_idx, j % cols)
                row += (len(groups[sess]) - 1) // cols + 1
        else:
            for i, cb in enumerate(checkboxes):
                r_idx = i // cols
                frame = QFrame()
                bg = t["bg2"] if r_idx % 2 == 0 else t["bg3"]
                frame.setStyleSheet(f"QFrame{{background-color:{bg};border-radius:4px;}}")
                flay = QHBoxLayout(frame)
                flay.setContentsMargins(6, 3, 6, 3)
                flay.addWidget(cb)
                grid.addWidget(frame, r_idx, i % cols)

        if checkboxes:
            _add_select_all_tri(grid, checkboxes, cols)

        def _save(_dlg):
            to_del = [pkg for _cb, pkg in zip(checkboxes, packages, strict=True) if _cb.checkState() == _STATE_DELETE]
            do_delete = True
            if to_del:
                names = [(f"{pkg.get('package', '')} [{pkg.get('session', '')}]"
                          if is_specific else pkg.get("name", "")) if isinstance(pkg, dict) else str(pkg) for pkg in
                         to_del]
                if (QMessageBox.question(_dlg, "Confirm Delete", "Delete package(s)?\n\n  • " + "\n  • ".join(names),
                                         QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes):
                    do_delete = False
            updated = []
            for _cb, pkg in zip(checkboxes, packages, strict=True):
                if do_delete and _cb.checkState() == _STATE_DELETE:
                    continue
                d = pkg if isinstance(pkg, dict) else {"name": str(pkg)}
                updated.append({**d, "disabled": _cb.checkState() == _STATE_DISABLED})
            _commit_pkgs(pkg_type, updated)
            _dlg.accept()

        raw_title = pkg_type.replace('_', ' ').title()
        formatted_title = raw_title.replace("Aur", "AUR")
        title = f"Edit {formatted_title}"

        dlg, lay = _scroll_dlg(self, title, body, _save)

        search = QLineEdit()
        search.setPlaceholderText("Filter...")

        def _apply_search(txt: str) -> None:
            txt_lower = txt.lower()
            visible_sessions: set[str] = set()
            for _cb, _p in zip(checkboxes, packages, strict=True):
                visible = txt_lower in _cb.text().lower()
                _cb.setVisible(visible)
                parent_widget = _cb.parentWidget()
                if parent_widget is not None:
                    parent_widget.setVisible(visible)
                if visible and is_specific and isinstance(_p, dict):
                    visible_sessions.add(_p.get("session", ""))
            if is_specific:
                for _sess, hdr_lbl in session_headers.items():
                    hdr_lbl.setVisible(not txt_lower or _sess in visible_sessions)

        search.textChanged.connect(_apply_search)
        btn_add_row = QHBoxLayout()

        def make_add_slot(func):
            def slot(*_):
                dlg.close()
                QTimer.singleShot(0, func)

            return slot

        for lbl, fn in [("➕ Add", lambda: self._add_pkg(pkg_type)),
                        ("➕➕ Batch Add", lambda: self._batch_add(pkg_type))]:
            b = QPushButton(lbl)
            b.clicked.connect(make_add_slot(fn))
            btn_add_row.addWidget(b)

        io_row = QHBoxLayout()

        def make_io_slot(func, label):
            def slot(*_):
                if "Import" in label:
                    dlg.close()
                    QTimer.singleShot(0, func)
                else:
                    func()

            return slot

        for lbl, fn in [("📥 Import", lambda: self._import_pkgs(pkg_type)),
                        ("📤 Export", lambda: self._export_pkgs(pkg_type)),
                        ("🔎 Verify Package(s)", lambda: self._verify_pkgs(pkg_type, dlg))]:
            b = QPushButton(lbl)
            b.clicked.connect(make_io_slot(fn, lbl))
            io_row.addWidget(b)

        lay.insertWidget(1, search)
        lay.insertLayout(2, btn_add_row)
        lay.insertLayout(3, io_row)
        lay.setStretch(0, 1)

        def _set_p_ctx(widget, p_data, d):
            widget.contextMenuEvent = lambda _e: self._edit_pkg_entry((widget, p_data), pkg_type, d)

        for cb, p in zip(checkboxes, packages, strict=True):
            _set_p_ctx(cb, p, dlg)
        dlg.exec()

    def _edit_pkg_entry(self, cb_pkg: tuple, pkg_type: str, parent_dlg) -> None:
        _, p = cb_pkg
        if p is None:
            return
        is_specific = _is_specific(pkg_type)
        current_name = p.get("package" if is_specific else "name", "")
        current_sess = p.get("session", "") if is_specific else None
        result = _pkg_form_dialog(
            self, "Edit Package", prefill_name=current_name, prefill_sess=current_sess if is_specific else None)
        if result is None:
            return
        name = result[0]
        if is_specific:
            p["package"], p["session"] = name, result[1]
        else:
            p["name"] = name
        pkg_list = getattr(S, pkg_type, [])
        if is_specific:
            sort_specific_pkg_list(pkg_list)
        else:
            sort_pkg_list(pkg_list)
        save_profile()
        parent_dlg.accept()
        self._reopen_pkgs(pkg_type)

    def _add_pkg(self, pkg_type: str) -> None:
        is_specific = _is_specific(pkg_type)
        if is_specific:
            result = _pkg_form_dialog(self, "Add Specific Package", prefill_sess=SESSIONS[0] if SESSIONS else "")
            if result is None:
                QTimer.singleShot(0, lambda: self._edit_pkgs(pkg_type))
                return None
            name, sess = result
            S.specific_packages = S.specific_packages or []
            if any(isinstance(p, dict) and p.get("package") == name and p.get("session") == sess
                   for p in S.specific_packages):
                QMessageBox.warning(self, "Duplicate", f"'{name}' for '{sess}' already exists.")
            else:
                S.specific_packages.append({"package": name, "session": sess, "disabled": False})
                sort_specific_pkg_list(S.specific_packages)
                save_profile()
                QMessageBox.information(self, "Added", f"Added:\n\n  • {name} [{sess}]")
        else:
            label = pkg_type.replace("_", " ").title().replace("Aur", "AUR").rstrip("s")
            name, ok = ask_text(self, f"Add {label}", "Package name:")
            if ok and name.strip():
                name = name.strip()
                if not is_valid_pkg_name(name):
                    QMessageBox.warning(
                        self, "Error",
                        f"'{name}' is not a valid package name.\n\n"
                        "Allowed: letters, digits, '.', '_', '+', '-' (must not start with a separator).\n"
                        "Gentoo category/package atoms (e.g. 'net-misc/openssh') are also allowed."
                    )
                    QTimer.singleShot(0, lambda: self._edit_pkgs(pkg_type))
                    return None
                current = getattr(S, pkg_type, []) or []
                existing = {p.get("name") if isinstance(p, dict) else str(p) for p in current}
                if name in existing:
                    QMessageBox.warning(self, "Duplicate", f"'{name}' already exists.")
                else:
                    current.append({"name": name, "disabled": False})
                    _commit_pkgs(pkg_type, current)
                    QMessageBox.information(self, "Added", f"Added {label}:\n\n  • {name}")
        QTimer.singleShot(0, lambda: self._edit_pkgs(pkg_type))
        return None

    def _batch_add(self, pkg_type: str) -> None:
        is_specific = _is_specific(pkg_type)
        dlg = QDialog(self)
        label = pkg_type.replace("_", " ").title().replace("Aur", "AUR").rstrip("s")
        dlg.setWindowTitle(f"Batch Add {label}(s)")
        dlg.setWindowModality(Qt.WindowModality.ApplicationModal)
        dlg.setMinimumSize(700, 500)
        lay = QVBoxLayout(dlg)

        batch_sess_cb = None
        if is_specific:
            row = QHBoxLayout()
            row.addWidget(QLabel("Session:"))
            batch_sess_cb = QComboBox()
            batch_sess_cb.addItems(SESSIONS)
            batch_sess_cb.setMinimumHeight(32)
            row.addWidget(batch_sess_cb)
            row.addStretch()
            lay.addLayout(row)

        ed = QTextEdit()
        ed.setPlaceholderText("One package per line (or comma separated)")
        lay.addWidget(ed)

        def _do_add():
            text = ed.toPlainText().strip()
            if not text:
                return

            raw_pkgs = [p.strip() for p in text.replace(",", "\n").split("\n") if p.strip()]
            pkgs = []
            invalid = []
            for p in raw_pkgs:
                if is_valid_pkg_name(p):
                    pkgs.append(p)
                else:
                    invalid.append(p)

            current = getattr(S, pkg_type, []) or []
            added_pkgs = []
            dupes = []

            existing: set
            if is_specific and batch_sess_cb is not None:
                sess = batch_sess_cb.currentText()
                existing = {(p.get("package"), p.get("session")) for p in current if isinstance(p, dict)}
                existing.update({(str(p), sess) for p in current if not isinstance(p, dict)})
                for p in pkgs:
                    if (p, sess) in existing:
                        dupes.append(f"{p} [{sess}]")
                    else:
                        current.append({"package": p, "session": sess, "disabled": False})
                        existing.add((p, sess))
                        added_pkgs.append(f"{p} [{sess}]")
            else:
                existing = {p.get("name") if isinstance(p, dict) else str(p) for p in current}
                for p in pkgs:
                    if p in existing:
                        dupes.append(p)
                    else:
                        current.append({"name": p, "disabled": False})
                        existing.add(p)
                        added_pkgs.append(p)

            if added_pkgs:
                _commit_pkgs(pkg_type, current)

                added_str = "Added package(s):\n\n" + "\n".join(f"  • {n}" for n in added_pkgs)

                extra = []
                if dupes:
                    extra.append("Skipped duplicate(s):\n\n" + "\n".join(f"  • {d}" for d in dupes))
                if invalid:
                    extra.append("Skipped invalid name(s):\n" + "\n".join(f"  • {i}" for i in invalid))

                final_msg = added_str + ("\n\n" + "\n\n".join(extra) if extra else "")
                QMessageBox.information(dlg, "Added", final_msg)

            elif dupes or invalid:
                msg_fail = []
                if dupes: msg_fail.append("Skipped duplicates:\n" + "\n".join(f"  • {d}" for d in dupes))
                if invalid: msg_fail.append("Invalid names:\n" + "\n".join(f"  • {i}" for i in invalid))
                QMessageBox.warning(dlg, "Not Added", "\n\n".join(msg_fail))

            dlg.accept()
            QTimer.singleShot(0, lambda: self._edit_pkgs(pkg_type))

        lay.addWidget(ok_cancel_buttons(dlg, _do_add))
        dlg.exec()

    def _export_data(self, title: str, default_filename: str, items: list, fmt_fn, header: str = "") -> None:
        if not items:
            QMessageBox.information(self, "Export", f"No {title.lower()} to export.")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, f"Export {title}", str(_HOME / default_filename), "Text (*.txt);;CSV (*.csv);;All (*)")
        if not path:
            return

        lines = [header] if header else []
        lines.extend(fmt_fn(item) for item in items if item)

        try:
            Path(path).write_text("\n".join(ln for ln in lines if ln) + "\n", encoding="utf-8")
            QMessageBox.information(self, "Exported", f"Exported {len(items)} entry/entries to:\n{path}")
        except OSError as exc:
            QMessageBox.critical(self, "Export Error", str(exc))

    def _export_dotfiles(self) -> None:
        files = [f for f in (S.dotfiles or []) if isinstance(f, dict) and f.get("source") and f.get("destination")]
        self._export_data("Dotfiles", "dotfiles.txt", files,
                          lambda f: f"{f['source']}\t{f['destination']}", header="# source\tdestination")

    def _export_pkgs(self, pkg_type: str) -> None:
        packages = getattr(S, pkg_type, []) or []
        is_specific = _is_specific(pkg_type)

        def _fmt(p: dict | str) -> str:
            if not isinstance(p, dict):
                return str(p)
            if is_specific:
                line = f"{p.get('package', '')},{p.get('session', '')}"
            else:
                line = p.get("name", "")
            if p.get("disabled"):
                line += ",disabled"
            return line

        label = pkg_type.replace("_", " ").title().replace("Aur", "AUR")
        self._export_data(label, f"{pkg_type}.txt", packages, _fmt)

    def _import_pkgs(self, pkg_type: str) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Import", str(_HOME), "Data (*.txt *.csv)")
        if not path:
            QTimer.singleShot(0, lambda: self._edit_pkgs(pkg_type))
            return

        lines = _read_import_file(self, path)
        if lines is None:
            QTimer.singleShot(0, lambda: self._edit_pkgs(pkg_type))
            return

        is_specific = _is_specific(pkg_type)
        current = getattr(S, pkg_type, []) or []

        existing: set
        if is_specific:
            existing = {(p.get("package"), p.get("session")) for p in current if isinstance(p, dict)}
            existing.update(
                {(str(p), SESSIONS[0] if SESSIONS else "unknown") for p in current if not isinstance(p, dict)})
        else:
            existing = {p.get("name") if isinstance(p, dict) else str(p) for p in current}

        added = 0

        for raw in lines:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue

            parts = [p.strip().strip("\"'") for p in line.split(",")]
            if not parts:
                continue

            name = parts[0]
            if not is_valid_pkg_name(name):
                continue

            if is_specific:
                sess = parts[1] if len(parts) > 1 else (SESSIONS[0] if SESSIONS else "unknown")
                disabled = len(parts) > 2 and parts[2].strip().lower() == "disabled"
                if (name, sess) not in existing:
                    current.append({"package": name, "session": sess, "disabled": disabled})
                    existing.add((name, sess))
                    added += 1
            else:
                disabled = len(parts) > 1 and parts[1].strip().lower() == "disabled"
                if name not in existing:
                    current.append({"name": name, "disabled": disabled})
                    existing.add(name)
                    added += 1

        if added:
            _commit_pkgs(pkg_type, current)
            QMessageBox.information(self, "Import Complete", f"Successfully imported {added} packages.")
        QTimer.singleShot(0, lambda: self._edit_pkgs(pkg_type))

    def _verify_pkgs(self, pkg_type: str, parent_dlg: QDialog) -> None:
        packages = getattr(S, pkg_type, []) or []
        if not packages:
            QMessageBox.information(parent_dlg, "Verify", "There are no packages available for verification.")
            return

        progress = QProgressDialog("Verifying packages...", "Cancel", 0, len(packages), parent_dlg)
        progress.setMinimumSize(500, 150)
        progress.setWindowTitle("Package Verification")
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setAutoClose(True)
        progress.setValue(0)

        verifier_thread = PackageVerifierThread(packages, pkg_type, self._distro.family())
        self._verifier_threads.append(verifier_thread)

        def on_progress(current, total):
            if not progress.wasCanceled():
                progress.setMaximum(total)
                progress.setValue(current)

        def on_result(_valid, invalid):
            if progress.wasCanceled():
                return
            progress.setValue(progress.maximum())

            if not invalid:
                QMessageBox.information(parent_dlg, "Verification Complete",
                                        "All packages are valid and available in the repositories!")
                return

            msg = f"{len(invalid)} invalid or missing package(s) detected:\n\n" + "\n".join(
                f"  • {p}" for p in invalid[:15])
            if len(invalid) > 15:
                msg += f"\n  ... and {len(invalid) - 15} more."
            msg += "\n\nWould you like to permanently remove this/these invalid package(s) from your profile?"

            ans = QMessageBox.question(parent_dlg, "Verification Complete", msg,
                                       QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)

            if ans == QMessageBox.StandardButton.Yes:
                invalid_set = set(invalid)
                is_specific = _is_specific(pkg_type)
                new_pkgs = []
                for p in packages:
                    name = p.get("package" if is_specific else "name", "") if isinstance(p, dict) else str(p)
                    if name not in invalid_set:
                        new_pkgs.append(p)

                _commit_pkgs(pkg_type, new_pkgs)
                parent_dlg.accept()
                QTimer.singleShot(0, lambda: self._edit_pkgs(pkg_type))

        def on_finished():
            if verifier_thread in self._verifier_threads:
                self._verifier_threads.remove(verifier_thread)

        verifier_thread.progress.connect(on_progress)
        verifier_thread.result.connect(on_result)
        verifier_thread.finished.connect(on_finished)
        progress.canceled.connect(verifier_thread.cancel)
        verifier_thread.start()




class SystemManagerLauncher:

    def __init__(self, _parent=None):
        self.parent = _parent
        self.failed_attempts = getattr(_parent, "sm_failed_attempts", 0)
        self._distro = LinuxDistroHelper()
        self._distro_name = self._distro.distro_pretty_name
        self._session = self._distro.detect_session()
        self._sudo_checkbox: QCheckBox | None = None
        self._op_text: dict[str, str] | None = None
        self._op_tips: dict[str, str] | None = None
        self._yay_installed = None
        self._sm_thread = None
        self._sm_dialog = None

    @property
    def aur_helper_installed(self) -> bool:
        if self._yay_installed is None:
            self._yay_installed = _check_aur_helper_installed(self._distro)
        return bool(self._yay_installed)

    def launch(self) -> None:
        if not S.system_manager_ops:
            QMessageBox.information(self.parent, "No Operations Configured",
                                    "System Manager has no operations selected yet.\n\n"
                                    "Please configure what should be executed under 'System Manager Operations' first.")
            SystemManagerOptions(self.parent, distro=self._distro).exec()
            return
        if self.parent:
            self.parent.hide()
        try:
            self._confirm_and_start()
        finally:
            if self.parent:
                self.parent.show()

    def _confirm_and_start(self) -> None:
        ops = S.system_manager_ops
        if self._op_text is None:
            _bootloader, _current_variant, _sys_default = _detect_boot_info()
            _ik = self._distro.detect_installed_kernel_variants()
            _ik.add(_current_variant)
            _op_status = _compute_op_status(self._distro, self.aur_helper_installed, _sys_default,
                                            installed_kernels=_ik)
            _kti_override = [] if "install_kernels" not in ops else None
            _raw = _build_op_text(self._distro, self._session, aur_helper_installed=self.aur_helper_installed,
                                  system_default_variant=_sys_default,
                                  op_status=_op_status, installed_kernels=_ik,
                                  kernels_to_install_override=_kti_override)
            self._op_text = _raw_to_label_html(_raw)
            self._op_tips = _raw_to_tips(_raw)

        if not self._op_text:
            return
        op_text: dict[str, str] = self._op_text
        tips = self._op_tips or {}
        dialog = QDialog(self.parent)
        dialog.setWindowTitle("System Manager")
        outer = QVBoxLayout(dialog)
        outer.setContentsMargins(0, 0, 0, 0)
        aur_helper_info = ""
        if self._distro.has_aur:
            _lnch_helper, _lnch_ok = _detect_effective_aur_helper(self._distro)
            aur_helper_info = f"   |   AUR Helper: '{_lnch_helper}' {'detected' if _lnch_ok else 'not detected'}"

        content_widget = QWidget()
        content_layout = QVBoxLayout(content_widget)
        distro_lbl = QLabel(
            f"Recognized Linux distribution: {self._distro_name}   |   Session: {self._session}{aur_helper_info}")
        distro_lbl.setStyleSheet(style_label_info(bold=True) + f"font-size:{font_sz()}px")
        distro_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        content_layout.addWidget(distro_lbl)
        ops_lbl = QLabel(f"<span style='font-size:{font_sz(2)}px;font-family:monospace;'>"
                         "<br>System Manager will perform the following operations:<br></span>")
        ops_lbl.setTextFormat(Qt.TextFormat.RichText)
        ops_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        content_layout.addWidget(ops_lbl)

        display_num = 0
        for key in ops:
            if key not in op_text:
                continue
            display_num += 1
            tooltip = tips.get(key, "")

            is_firewall_tip = False
            if key == "enable_firewall":
                is_firewall_tip = True
                tooltip = firewall_rules_tooltip()

            has_tip = bool(tooltip)
            colour, decoration = style_op_label(has_tip)
            icon = "󰔨 " if has_tip else ""
            html = f"{icon}   <span style='font-size:{font_sz(2)}px;padding:5px; color:{colour};{decoration}'>{op_text[key]}</span>"
            row = QHBoxLayout()
            num = QLabel(f"{display_num}:")
            num.setStyleSheet(style_label_mono(font_size=font_sz(2)))
            num.setAlignment(Qt.AlignmentFlag.AlignLeft)
            lbl = QLabel(html)
            lbl.setTextFormat(Qt.TextFormat.RichText)
            lbl.setStyleSheet(style_label_mono(font_size=font_sz(2)))
            lbl.setAlignment(Qt.AlignmentFlag.AlignLeft)
            apply_tooltip(lbl, tooltip, wrap=not is_firewall_tip)
            row.addWidget(num)
            row.addWidget(lbl)
            row.addStretch(1)
            content_layout.addLayout(row)

        confirm = QLabel(f"<span style='font-size:{font_sz(2)}px;'>Start System Manager?<br>"
                         "(Check 'Enter sudo password' if privileged commands require a password)<br></span>")
        confirm.setTextFormat(Qt.TextFormat.RichText)
        confirm.setAlignment(Qt.AlignmentFlag.AlignCenter)

        sudo_cb = QCheckBox("Enter sudo password 󰔨")
        sudo_cb.setStyleSheet(style_sudo_checkbox(muted=False))
        if self.failed_attempts:
            sudo_cb.setText("Sudo password must be entered! 󰔨")
            sudo_cb.setChecked(True)
            sudo_cb.setEnabled(False)
            sudo_cb.setStyleSheet(style_sudo_checkbox(muted=True))
        apply_tooltip(sudo_cb, sudo_checkbox_tooltip())
        self._sudo_checkbox = sudo_cb

        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Yes | QDialogButtonBox.StandardButton.No)  # type: ignore
        bb.accepted.connect(dialog.accept)
        bb.rejected.connect(dialog.reject)
        btn_row = QHBoxLayout()
        btn_row.addWidget(self._sudo_checkbox)
        btn_row.addWidget(bb)
        content_layout.addWidget(confirm)
        content_layout.addLayout(btn_row)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        scroll.setWidget(content_widget)
        outer.addWidget(scroll)

        scr = QApplication.primaryScreen()
        if scr:
            sg = scr.availableGeometry()
            sz = content_widget.sizeHint()
            dialog.resize(min(sz.width() + 40, sg.width()), min(sz.height() + 40, sg.height()))
        no_btn = bb.button(QDialogButtonBox.StandardButton.No)
        if no_btn:
            no_btn.setFocus()
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        if self._sudo_checkbox and self._sudo_checkbox.isChecked():
            self._show_sudo_dialog()
        else:
            self._start_thread("")

    def _start_thread(self, pw: "SecureString | str") -> None:
        from system_manager import SystemManagerDialog, SystemManagerThread
        self._sm_thread = SystemManagerThread(pw, distro=self._distro)
        self._sm_dialog = SystemManagerDialog(self.parent)
        t, d = self._sm_thread, self._sm_dialog
        t.thread_started.connect(d.exec, Qt.ConnectionType.QueuedConnection)
        t.outputReceived.connect(d.on_output)
        t.taskListReady.connect(d.on_task_list)
        t.taskStatusChanged.connect(d.on_task_status)
        t.passwordFailed.connect(lambda: self._on_fail(t, d))
        t.passwordSuccess.connect(self._on_ok)
        t.finished.connect(d.mark_done)
        d.cancelRequested.connect(lambda: setattr(t, "terminated", True))
        t.inputRequested.connect(d.on_input_requested, Qt.ConnectionType.QueuedConnection)
        d.inputProvided.connect(t.provide_input)
        t.start()

    def _show_sudo_dialog(self) -> None:
        from sudo_password import SudoPasswordDialog
        dlg = SudoPasswordDialog(self.parent)
        dlg.sudo_password_entered.connect(self._start_thread)
        dlg.update_failed_attempts(self.failed_attempts)
        dlg.exec()

    def _on_fail(self, thread, dialog) -> None:
        t = current_theme()
        self.failed_attempts += 1
        if self.parent:
            self.parent.sm_failed_attempts = self.failed_attempts
        dialog.on_output(f"<p style='color:{t['error']};font-size:17px;font-weight:bold;'>"
                         "Authentication failed. Cancelled to prevent account lockout.<br>"
                         "Possible causes: incorrect password, user not in sudoers.</p>", "info")
        dialog.mark_done(failed_count=self.failed_attempts)
        thread.terminated = True

    def _on_ok(self) -> None:
        self.failed_attempts = 0
        if self.parent:
            self.parent.sm_failed_attempts = 0

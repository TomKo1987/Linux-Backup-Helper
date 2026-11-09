from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtWidgets import (
    QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QMessageBox, QPushButton, QVBoxLayout, QWidget
)

from linux_distro_helper import LinuxDistroHelper
from state import S, active_pkg_names, all_profile_pkg_names, save_profile
from themes import current_theme, font_sz
from translations import tr
from ui_utils import color_style

from scan_verify_helpers import (
    _add_pkgs_to_profile, _family_noise, _get_sm_managed_packages, _get_arch_de_deps, _IGNORE_EXACT,
    _IGNORE_PREFIXES, _specific_applies
)


def _get_package_diff(helper: "LinuxDistroHelper") -> tuple[list[str], list[str], str, list[str]]:
    error = ""
    aur: list[str] = []
    try:
        basic, aur = helper.get_explicitly_installed_packages()
        explicit: set[str] = set(basic) | set(aur)
    except RuntimeError as exc:
        explicit = set()
        error = str(exc)

    profile     = all_profile_pkg_names()
    sm_managed = _get_sm_managed_packages(helper)
    de_deps = _get_arch_de_deps(helper)

    noise = _family_noise(helper, sorted(explicit))

    def _should_ignore(pkg: str) -> bool:
        if pkg in _IGNORE_EXACT or pkg in sm_managed or pkg in de_deps or pkg in noise:
            return True
        return any(pkg.startswith(p) for p in _IGNORE_PREFIXES)

    session = helper.detect_session()
    expected = set(active_pkg_names(S.basic_packages)) | set(active_pkg_names(S.aur_packages))
    expected |= {p["package"] for p in S.specific_packages
                 if isinstance(p, dict) and not p.get("disabled") and p.get("package") and _specific_applies(p, session)}

    not_tracked = sorted(p for p in explicit - profile if not _should_ignore(p))
    missing     = sorted(set(helper.filter_not_installed(sorted(expected))))
    return not_tracked, missing, error, sorted(set(aur))


class _PackageDiffWorker(QThread):
    done = pyqtSignal(tuple)

    def __init__(self, helper: "LinuxDistroHelper", parent=None) -> None:
        super().__init__(parent)
        self._helper = helper

    def run(self) -> None:
        result = _get_package_diff(self._helper)
        self.done.emit(result)


class _PackageDiffTab(QWidget):
    def __init__(self, helper: "LinuxDistroHelper", parent=None) -> None:
        super().__init__(parent)
        self._helper = helper
        self._worker: _PackageDiffWorker | None = None
        self._aur_names: set[str] = set()
        self._build_ui()

    def _build_ui(self) -> None:
        t   = current_theme()
        bg3 = t["bg3"];  _sep = t["header_sep"]
        acc = t["accent"];  fg = t["text"];  dim = t["text_dim"]

        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(8)

        hdr = QHBoxLayout()
        title = QLabel(tr("📦  Package Diff: System vs. Profile"))
        title.setStyleSheet(f"font-size:{font_sz(2)}px;font-weight:bold;color:{acc};")
        self._run_btn = QPushButton(tr("🔄 Check Now"))
        self._run_btn.setMinimumHeight(32)
        self._run_btn.clicked.connect(self._run)
        hdr.addWidget(title)
        hdr.addStretch()
        hdr.addWidget(self._run_btn)
        lay.addLayout(hdr)

        _list_ss = (f"QListWidget{{background:{bg3};border:1px solid {_sep};border-radius:4px;"
                    f"font-size:{font_sz()}px;color:{fg};outline:none;}}"
                    f"QListWidget::item{{padding:4px 8px;border-bottom:1px solid {_sep};}}")

        cols = QHBoxLayout()
        cols.setSpacing(10)

        col_u = QVBoxLayout()
        self._list_untracked_lbl = QLabel(tr("⚠  Installed, not in profile (0)"))
        self._list_untracked_lbl.setStyleSheet(
            f"font-weight:bold;color:{t['warning']};font-size:{font_sz(1)}px;")
        self._list_untracked = QListWidget()
        self._list_untracked.setStyleSheet(_list_ss)
        col_u.addWidget(self._list_untracked_lbl)
        col_u.addWidget(self._list_untracked, 1)
        self._untracked_btns = QWidget()
        ub = QHBoxLayout(self._untracked_btns)
        ub.setContentsMargins(0, 0, 0, 0)
        self._sel_all_btn = QPushButton(tr("Select All New"))
        self._sel_all_btn.clicked.connect(self._select_all_untracked)
        self._add_btn = QPushButton(tr("⬆  Add Selected to Profile"))
        self._add_btn.setStyleSheet(f"font-weight:bold;color:{acc};border-color:{acc};")
        self._add_btn.clicked.connect(self._add_selected)
        ub.addWidget(self._sel_all_btn)
        ub.addStretch()
        ub.addWidget(self._add_btn)
        self._untracked_btns.hide()
        col_u.addWidget(self._untracked_btns)
        cols.addLayout(col_u, 1)

        col_m = QVBoxLayout()
        self._list_missing_lbl = QLabel(tr("✗  In profile, not installed (0)"))
        self._list_missing_lbl.setStyleSheet(
            f"font-weight:bold;color:{t['error']};font-size:{font_sz(1)}px;")
        self._list_missing = QListWidget()
        self._list_missing.setStyleSheet(_list_ss)
        col_m.addWidget(self._list_missing_lbl)
        col_m.addWidget(self._list_missing, 1)
        cols.addLayout(col_m, 1)

        lay.addLayout(cols, 1)

        self._status = QLabel(tr("Click ‘Check Now’ to compare System vs. Profile."))
        self._status.setStyleSheet(color_style(dim, font_sz(-1)))
        lay.addWidget(self._status)

    def _run(self) -> None:
        if self._worker is not None and self._worker.isRunning():
            return
        self._run_btn.setEnabled(False)
        self._status.setText(tr("Analyzing…"))
        worker = _PackageDiffWorker(self._helper, self)
        self._worker = worker
        worker.done.connect(self._on_done)
        worker.start()

    def _select_all_untracked(self) -> None:
        for i in range(self._list_untracked.count()):
            item = self._list_untracked.item(i)
            if item is not None:
                item.setCheckState(Qt.CheckState.Checked)

    def _add_selected(self) -> None:
        rows: list[int] = []
        chosen: list[str] = []
        for i in range(self._list_untracked.count()):
            item = self._list_untracked.item(i)
            if item is not None and item.checkState() == Qt.CheckState.Checked:
                rows.append(i)
                chosen.append(str(item.data(Qt.ItemDataRole.UserRole)))
        if not chosen:
            QMessageBox.information(self.window(), tr("Nothing Selected"), tr("No packages selected."))
            return
        added = _add_pkgs_to_profile([n for n in chosen if n not in self._aur_names],
                                     [n for n in chosen if n in self._aur_names])
        if not save_profile():
            QMessageBox.warning(self.window(), tr("Save Failed"), tr("Could not save profile."))
            return
        for row in reversed(rows):
            self._list_untracked.takeItem(row)
        remaining = self._list_untracked.count()
        self._list_untracked_lbl.setText(tr("⚠  Installed, not in profile ({n})", n=remaining))
        self._untracked_btns.setVisible(remaining > 0)
        QMessageBox.information(self.window(), tr("Done"), tr("{n} package(s) added to profile and saved.", n=added))

    def _on_done(self, result: tuple) -> None:
        not_tracked, missing, error, aur_names = result
        self._aur_names = set(aur_names)

        self._list_untracked.clear()
        for p in not_tracked:
            item = QListWidgetItem(f"{p}  [AUR]" if p in self._aur_names else p)
            item.setData(Qt.ItemDataRole.UserRole, p)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Unchecked)
            self._list_untracked.addItem(item)
        self._untracked_btns.setVisible(bool(not_tracked))
        self._list_untracked_lbl.setText(
            tr("⚠  Installed, not in profile ({n})", n=len(not_tracked)))

        self._list_missing.clear()
        for p in missing:
            self._list_missing.addItem(p)
        self._list_missing_lbl.setText(
            tr("✗  In profile, not installed ({n})", n=len(missing)))

        if error:
            t = current_theme()
            self._status.setStyleSheet(f"color:{t['warning']};font-size:{font_sz(-1)}px;font-weight:bold;")
            self._status.setText(
                tr("⚠  Installed-package detection unavailable ({error}) — "
                   "only 'in profile, not installed' could be checked.", error=error))
        else:
            t = current_theme()
            self._status.setStyleSheet(color_style(t['text_dim'], font_sz(-1)))
            self._status.setText(
                tr("{tracked} not tracked  ·  {missing} missing from the system",
                   tracked=len(not_tracked), missing=len(missing)))
        self._run_btn.setEnabled(True)
        fit = getattr(self.window(), "fit_to_content", None)
        if callable(fit):
            fit()

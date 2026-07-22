from PyQt6.QtWidgets import (
    QApplication, QHBoxLayout, QLabel, QListWidget, QPushButton, QVBoxLayout, QWidget
)

from linux_distro_helper import LinuxDistroHelper
from state import all_profile_pkg_names
from themes import current_theme, font_sz

from scan_verify_helpers import (
    _get_sm_managed_packages, _get_arch_de_deps, _IGNORE_EXACT, _IGNORE_PREFIXES
)


def _get_package_diff(helper: "LinuxDistroHelper") -> tuple[list[str], list[str]]:
    try:
        basic, aur = helper.get_explicitly_installed_packages()
        explicit: set[str] = set(basic) | set(aur)
    except RuntimeError:
        explicit = set()

    profile     = all_profile_pkg_names()
    sm_managed = _get_sm_managed_packages(helper)
    de_deps = _get_arch_de_deps(helper)

    def _should_ignore(pkg: str) -> bool:
        if pkg in _IGNORE_EXACT or pkg in sm_managed or pkg in de_deps:
            return True
        return any(pkg.startswith(p) for p in _IGNORE_PREFIXES)

    not_tracked = sorted(p for p in explicit - profile if not _should_ignore(p))
    missing     = sorted(set(helper.filter_not_installed(sorted(profile))))
    return not_tracked, missing


class _PackageDiffTab(QWidget):
    def __init__(self, helper: "LinuxDistroHelper", parent=None) -> None:
        super().__init__(parent)
        self._helper = helper
        self._build_ui()

    def _build_ui(self) -> None:
        t   = current_theme()
        bg3 = t["bg3"];  _sep = t["header_sep"]
        acc = t["accent"];  fg = t["text"];  dim = t["text_dim"]

        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(8)

        hdr = QHBoxLayout()
        title = QLabel("📦  Package Diff: System vs. Profile")
        title.setStyleSheet(f"font-size:{font_sz(2)}px;font-weight:bold;color:{acc};")
        self._run_btn = QPushButton("🔄 Check Now")
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
        self._list_untracked_lbl = QLabel("⚠  Installed, not in profile (0)")
        self._list_untracked_lbl.setStyleSheet(
            f"font-weight:bold;color:{t['warning']};font-size:{font_sz(1)}px;")
        self._list_untracked = QListWidget()
        self._list_untracked.setStyleSheet(_list_ss)
        col_u.addWidget(self._list_untracked_lbl)
        col_u.addWidget(self._list_untracked, 1)
        cols.addLayout(col_u, 1)

        col_m = QVBoxLayout()
        self._list_missing_lbl = QLabel("✗  In profile, not installed (0)")
        self._list_missing_lbl.setStyleSheet(
            f"font-weight:bold;color:{t['error']};font-size:{font_sz(1)}px;")
        self._list_missing = QListWidget()
        self._list_missing.setStyleSheet(_list_ss)
        col_m.addWidget(self._list_missing_lbl)
        col_m.addWidget(self._list_missing, 1)
        cols.addLayout(col_m, 1)

        lay.addLayout(cols, 1)

        self._status = QLabel("Click ‘Check Now’ to compare System vs. Profile.")
        self._status.setStyleSheet(f"color:{dim};font-size:{font_sz(-1)}px;")
        lay.addWidget(self._status)

    def _run(self) -> None:
        self._run_btn.setEnabled(False)
        self._status.setText("Analyzing…")
        QApplication.processEvents()
        not_tracked, missing = _get_package_diff(self._helper)

        self._list_untracked.clear()
        for p in not_tracked:
            self._list_untracked.addItem(p)
        self._list_untracked_lbl.setText(
            f"⚠  Installed, not in profile ({len(not_tracked)})")

        self._list_missing.clear()
        for p in missing:
            self._list_missing.addItem(p)
        self._list_missing_lbl.setText(
            f"✗  In profile, not installed ({len(missing)})")

        self._status.setText(
            f"{len(not_tracked)} not tracked  ·  {len(missing)} missing from the system")
        self._run_btn.setEnabled(True)

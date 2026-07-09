from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QComboBox, QDialog, QHBoxLayout, QLabel, QListWidget,
    QListWidgetItem, QMessageBox, QPushButton, QTabWidget, QVBoxLayout, QWidget,
)

from state import active_pkg_names, list_profiles, snapshot_profile
from themes import current_theme, font_sz
from ui_utils import _StandardKeysMixin, hdr_label, sep

__all__ = ["ProfileCompareDialog"]


def _pkg_set(fields: dict, key: str, *, is_specific: bool = False) -> set[str]:
    return set(active_pkg_names(fields.get(key, []), is_specific=is_specific))


def _dotfile_label(d: dict) -> str:
    src = d.get("source", "")
    dst = d.get("destination", "")
    src = src[0] if isinstance(src, list) and src else (src if isinstance(src, str) else "")
    dst = dst[0] if isinstance(dst, list) and dst else (dst if isinstance(dst, str) else "")
    return f"{src}  →  {dst}" if (src or dst) else "(unnamed dotfile)"

def _active_dotfile_labels(fields: dict) -> set[str]:
    out = set()
    for d in fields.get("dotfiles", []) or []:
        if not isinstance(d, dict) or d.get("disabled"):
            continue
        out.add(_dotfile_label(d))
    return out

def _entry_label(e: dict) -> str:
    header = e.get("header", "")
    title  = e.get("title", "")
    return f"{header} / {title}" if header else title


class _DiffListWidget(QWidget):

    def __init__(self, name_a: str, name_b: str, only_a: list[str], only_b: list[str],
                 in_both: list[str] | None = None, parent=None) -> None:
        super().__init__(parent)
        t   = current_theme()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(8)

        row = QHBoxLayout()
        row.setSpacing(8)

        row.addWidget(self._column(f"⬅  Only in '{name_a}'", only_a, t["info"]))
        row.addWidget(self._column(f"➡  Only in '{name_b}'", only_b, t["accent"]))
        if in_both is not None:
            row.addWidget(self._column("✓  In both", in_both, t["success"]))

        lay.addLayout(row)

    @staticmethod
    def _column(title: str, items: list[str], color: str) -> QWidget:
        t = current_theme()
        w   = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)

        lbl = QLabel(f"{title}  ({len(items):,})")
        lbl.setStyleSheet(f"color:{color};font-weight:bold;font-size:{font_sz(-1)}px;")
        lay.addWidget(lbl)

        lw = QListWidget()
        lw.setStyleSheet(
            f"QListWidget{{background:{t['bg3']};border:1px solid {t['header_sep']};"
            f"color:{t['text']};font-family:monospace;font-size:{font_sz(-2)}px;}}"
            f"QListWidget::item:hover{{background:{t['bg2']};}}"
        )
        if items:
            lw.addItems(sorted(items))
        else:
            placeholder = QListWidgetItem("— none —")
            placeholder.setForeground(QColor(t["text_dim"]))
            placeholder.setFlags(Qt.ItemFlag.NoItemFlags)
            lw.addItem(placeholder)
        lay.addWidget(lw, 1)
        return w


def _simple_value_diff(name_a: str, name_b: str, label_a: str, label_b: str,
                        same: bool, parent=None) -> QWidget:
    t   = current_theme()
    w   = QWidget(parent)
    lay = QVBoxLayout(w)
    lay.setContentsMargins(8, 8, 8, 8)
    lay.setSpacing(6)

    if same:
        lbl = QLabel(f"✓  Identical in both profiles:  {label_a}")
        lbl.setStyleSheet(f"color:{t['success']};font-size:{font_sz(0)}px;")
        lay.addWidget(lbl)
    else:
        row_a = QLabel(f"'{name_a}':  {label_a or '(none)'}")
        row_a.setStyleSheet(f"color:{t['info']};font-size:{font_sz(0)}px;")
        row_b = QLabel(f"'{name_b}':  {label_b or '(none)'}")
        row_b.setStyleSheet(f"color:{t['accent']};font-size:{font_sz(0)}px;")
        lay.addWidget(row_a)
        lay.addWidget(row_b)
    lay.addStretch()
    return w


# noinspection PyUnresolvedReferences
class ProfileCompareDialog(_StandardKeysMixin, QDialog):

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("⚖  Profile Compare")
        self.setMinimumSize(900, 650)
        self._build()

    def _build(self) -> None:
        t   = current_theme()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(10)

        lay.addWidget(hdr_label("⚖  Profile Compare"))

        info = QLabel("Compare packages, dotfiles, backup entries and system settings between two profiles.")
        info.setStyleSheet(f"color:{t['text_dim']};font-size:{font_sz(-1)}px;")
        lay.addWidget(info)
        lay.addWidget(sep())

        pick_row = QHBoxLayout()
        pick_row.setSpacing(10)

        profiles = list_profiles()

        pick_row.addWidget(QLabel("Profile A:"))
        self._combo_a = QComboBox()
        self._combo_a.addItems(profiles)
        pick_row.addWidget(self._combo_a, 1)

        pick_row.addWidget(QLabel("Profile B:"))
        self._combo_b = QComboBox()
        self._combo_b.addItems(profiles)
        if len(profiles) > 1:
            self._combo_b.setCurrentIndex(1)
        pick_row.addWidget(self._combo_b, 1)

        compare_btn = QPushButton("⚖  Compare")
        compare_btn.clicked.connect(self._compare)
        pick_row.addWidget(compare_btn)

        lay.addLayout(pick_row)
        lay.addWidget(sep())

        self._status_lbl = QLabel(
            "Select two profiles and press Compare." if profiles
            else "No saved profiles found.")
        self._status_lbl.setStyleSheet(f"color:{t['text_dim']};font-size:{font_sz(-1)}px;")
        lay.addWidget(self._status_lbl)

        self._tabs = QTabWidget()
        lay.addWidget(self._tabs, 1)

        close_btn = QPushButton("Close")
        close_btn.setMinimumHeight(32)
        close_btn.clicked.connect(self.accept)
        lay.addWidget(close_btn)

        if len(profiles) >= 2:
            self._compare()

    def _compare(self) -> None:
        name_a = self._combo_a.currentText()
        name_b = self._combo_b.currentText()

        if not name_a or not name_b:
            QMessageBox.information(self, "Profile Compare", "Please select two profiles.")
            return
        if name_a == name_b:
            QMessageBox.information(self, "Profile Compare", "Please select two different profiles.")
            return

        fields_a = snapshot_profile(name_a)
        fields_b = snapshot_profile(name_b)

        if fields_a is None or fields_b is None:
            failed = name_a if fields_a is None else name_b
            QMessageBox.critical(self, "Profile Compare", f"Could not read profile '{failed}'.")
            return

        self._tabs.clear()
        self._tabs.addTab(self._build_packages_tab(name_a, name_b, fields_a, fields_b), "📦  Packages")
        self._tabs.addTab(self._build_dotfiles_tab(name_a, name_b, fields_a, fields_b), "🗂  Dotfiles")
        self._tabs.addTab(self._build_entries_tab(name_a, name_b, fields_a, fields_b), "💾  Backup Entries")
        self._tabs.addTab(self._build_settings_tab(name_a, name_b, fields_a, fields_b), "⚙  Settings")

        self._status_lbl.setText(f"Comparing '{name_a}'  vs  '{name_b}'")

    @staticmethod
    def _build_packages_tab(name_a: str, name_b: str, fa: dict, fb: dict) -> QWidget:
        w   = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(4, 8, 4, 4)
        lay.setSpacing(10)

        sub = QTabWidget()

        basic_a = _pkg_set(fa, "basic_packages")
        basic_b = _pkg_set(fb, "basic_packages")
        sub.addTab(_DiffListWidget(name_a, name_b,
                                    sorted(basic_a - basic_b), sorted(basic_b - basic_a),
                                    sorted(basic_a & basic_b)),
                   f"Official ({len(basic_a)} / {len(basic_b)})")

        aur_a = _pkg_set(fa, "aur_packages")
        aur_b = _pkg_set(fb, "aur_packages")
        sub.addTab(_DiffListWidget(name_a, name_b,
                                    sorted(aur_a - aur_b), sorted(aur_b - aur_a),
                                    sorted(aur_a & aur_b)),
                   f"AUR ({len(aur_a)} / {len(aur_b)})")

        spec_a = _pkg_set(fa, "specific_packages", is_specific=True)
        spec_b = _pkg_set(fb, "specific_packages", is_specific=True)
        sub.addTab(_DiffListWidget(name_a, name_b,
                                    sorted(spec_a - spec_b), sorted(spec_b - spec_a),
                                    sorted(spec_a & spec_b)),
                   f"Session-specific ({len(spec_a)} / {len(spec_b)})")

        lay.addWidget(sub, 1)
        return w

    @staticmethod
    def _build_dotfiles_tab(name_a: str, name_b: str, fa: dict, fb: dict) -> QWidget:
        a = _active_dotfile_labels(fa)
        b = _active_dotfile_labels(fb)
        return _DiffListWidget(name_a, name_b, sorted(a - b), sorted(b - a), sorted(a & b))

    @staticmethod
    def _build_entries_tab(name_a: str, name_b: str, fa: dict, fb: dict) -> QWidget:
        labels_a = {_entry_label(e): e for e in fa.get("entries", []) or [] if isinstance(e, dict)}
        labels_b = {_entry_label(e): e for e in fb.get("entries", []) or [] if isinstance(e, dict)}

        names_a = set(labels_a)
        names_b = set(labels_b)
        only_a  = names_a - names_b
        only_b  = names_b - names_a
        shared  = names_a & names_b

        changed: list[str] = []
        identical: list[str] = []
        for n in shared:
            ea, eb = labels_a[n], labels_b[n]
            if (ea.get("source"), ea.get("destination")) != (eb.get("source"), eb.get("destination")):
                changed.append(f"{n}   [paths differ]")
            else:
                identical.append(n)

        w   = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(4, 8, 4, 4)
        lay.setSpacing(8)
        lay.addWidget(_DiffListWidget(name_a, name_b, sorted(only_a), sorted(only_b), sorted(identical + changed)))

        if changed:
            note = QLabel(f"⚠  {len(changed)} entr{'y' if len(changed) == 1 else 'ies'} exist in both "
                          f"profiles but have different source/destination paths (marked above).")
            t = current_theme()
            note.setStyleSheet(f"color:{t['warning']};font-size:{font_sz(-1)}px;")
            note.setWordWrap(True)
            lay.addWidget(note)

        return w

    @staticmethod
    def _build_settings_tab(name_a: str, name_b: str, fa: dict, fb: dict) -> QWidget:
        w   = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(4, 8, 4, 4)
        lay.setSpacing(10)

        sub = QTabWidget()

        ops_a = set(fa.get("system_manager_ops", []) or [])
        ops_b = set(fb.get("system_manager_ops", []) or [])
        sub.addTab(_DiffListWidget(name_a, name_b,
                                    sorted(ops_a - ops_b), sorted(ops_b - ops_a),
                                    sorted(ops_a & ops_b)),
                   f"System Manager Ops ({len(ops_a)} / {len(ops_b)})")

        mounts_a = {m.get("mount_path", "") for m in (fa.get("mount_options", []) or []) if isinstance(m, dict)}
        mounts_b = {m.get("mount_path", "") for m in (fb.get("mount_options", []) or []) if isinstance(m, dict)}
        mounts_a.discard("")
        mounts_b.discard("")
        sub.addTab(_DiffListWidget(name_a, name_b,
                                    sorted(mounts_a - mounts_b), sorted(mounts_b - mounts_a),
                                    sorted(mounts_a & mounts_b)),
                   f"Mount Paths ({len(mounts_a)} / {len(mounts_b)})")

        shell_same = fa.get("user_shell") == fb.get("user_shell")
        sub.addTab(_simple_value_diff(name_a, name_b, fa.get("user_shell", ""), fb.get("user_shell", ""), shell_same),
                   "Shell")

        kernels_a = set(fa.get("kernels_to_install", []) or [])
        kernels_b = set(fb.get("kernels_to_install", []) or [])
        sub.addTab(_DiffListWidget(name_a, name_b,
                                    sorted(kernels_a - kernels_b), sorted(kernels_b - kernels_a),
                                    sorted(kernels_a & kernels_b)),
                   f"Kernels ({len(kernels_a)} / {len(kernels_b)})")

        lay.addWidget(sub, 1)
        return w

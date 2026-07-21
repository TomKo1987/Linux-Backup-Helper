import copy
import json
import os
import shutil
import subprocess
import tarfile
import threading
from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import Qt, pyqtSignal, QObject, QEvent
from PyQt6.QtGui import QColor, QFont, QFontDatabase, QFontMetrics, QTextCursor
from PyQt6.QtWidgets import (
    QFormLayout, QHBoxLayout, QLabel, QLineEdit, QListWidget, QWidget, QVBoxLayout,
    QListWidgetItem, QMessageBox, QPlainTextEdit, QPushButton, QSplitter, QSizePolicy, QSpinBox,
    QCheckBox, QColorDialog, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFrame, QTextEdit, QApplication
)

from drive_utils import get_mounts, is_mounted
from profile_compare import ProfileCompareDialog
from state import (
    _norm_paths, list_profiles, load_profile, save_profile, logger, S, _HOME, _LOG_FILE, _PROFILES_DIR, _PROFILE_RE,
    _atomic_write, apply_replacements, RESTART_DIALOG,
)
from themes import THEMES, current_theme, apply_style, font_scale, font_sz, apply_tooltip
from ui_utils import sep, hdr_label, ok_cancel_buttons, btn_row, ask_text, ask_profile_name, browse_field, \
    block_set, _StandardKeysMixin

_ARCHIVE_MAX_PROFILE_BYTES = 1024 * 1024

class _UserRoleListMixin:
    item_list: QListWidget

    def _selected_data(self):
        item = self.item_list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None


class _ListDialog(_UserRoleListMixin, QDialog):

    def __init__(self, parent, title: str, size: tuple[int, int], hdr_text: str,
                 btn_specs: list[tuple[str, str]], close_label: str = "✕  Close"):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumSize(*size)

        layout = QVBoxLayout(self)
        layout.addWidget(hdr_label(hdr_text))
        layout.addWidget(sep())
        self.item_list = QListWidget()
        layout.addWidget(self.item_list, 1)
        layout.addLayout(btn_row([(lbl, getattr(self, fn)) for lbl, fn in btn_specs]))
        layout.addWidget(sep())
        close_btn = QPushButton(close_label)
        close_btn.clicked.connect(self.accept)
        layout.addWidget(close_btn)
        self._refresh()

    def _refresh(self) -> None:
        raise NotImplementedError

class _TextViewDialog(QDialog):

    def __init__(self, parent, title: str, min_size: tuple[int, int], font_size: int | None = None,
                 extra_buttons: list[tuple[str, object]] | None = None):

        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumSize(*min_size)
        t  = current_theme()
        fs = font_size if font_size is not None else font_sz()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(5, 5, 5, 5)
        layout.setSpacing(5)

        self.view = QTextEdit()
        self.view.setReadOnly(True)
        self.view.setStyleSheet(f"font-family:monospace;font-size:{fs}px;border:none;border-radius:0;")
        layout.addWidget(self.view, 1)

        bot = QWidget()
        bot.setStyleSheet(f"background:{t['bg2']};border-top:1px solid {t['header_sep']};")
        bl  = QHBoxLayout(bot)
        bl.setContentsMargins(12, 8, 12, 8)
        bl.setSpacing(8)
        for label, fn in [*(extra_buttons or []), ("✕ Close", self.accept)]:
            b = QPushButton(label)
            b.setFixedHeight(36)
            b.clicked.connect(fn)
            bl.addWidget(b)
        layout.addWidget(bot)

class _HintResizer(QObject):
    def __init__(self, watched, hint_label):
        super().__init__(watched)
        self._hint = hint_label

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Type.Resize:
            self._hint.setGeometry(obj.rect())
        return False

class ExcludeDialog(QDialog):
    def __init__(self, parent, src_abs: str, current_excludes: list[str]):
        super().__init__(parent)
        self.src_abs = src_abs
        self.selected_excludes: list[str] = list(current_excludes)
        self.setWindowTitle(f"Exclude items from: {src_abs}")
        self.setMinimumSize(1250, 1000)
        self._current_rel: str = ""
        self._build_ui()
        self._populate()

    def _build_ui(self) -> None:
        from PyQt6.QtWidgets import QScrollArea
        t  = current_theme()
        fs = font_scale()
        layout = QVBoxLayout(self)
        layout.setSpacing(8)
        layout.setContentsMargins(12, 12, 12, 12)

        self._header_lbl = QLabel()
        self._header_lbl.setTextFormat(Qt.TextFormat.RichText)
        self._header_lbl.setWordWrap(True)
        layout.addWidget(self._header_lbl)

        nav_row = QHBoxLayout()
        self._up_btn = QPushButton("↑ Up")
        self._up_btn.setFixedHeight(26)
        self._up_btn.setFixedWidth(70)
        self._up_btn.clicked.connect(self._go_up)
        self._path_lbl = QLabel()
        self._path_lbl.setStyleSheet(f"color:{t['text_dim']};font-size:{fs['sm']}px;")
        nav_row.addWidget(self._up_btn)
        nav_row.addWidget(self._path_lbl, 1)
        layout.addLayout(nav_row)
        layout.addWidget(sep())

        self._scroll_widget = QWidget()
        self._scroll_layout = QVBoxLayout(self._scroll_widget)
        self._scroll_layout.setSpacing(4)
        self._checkboxes: list[tuple[QCheckBox, str]] = []

        scroll = QScrollArea()
        scroll.setWidget(self._scroll_widget)
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet(f"QScrollArea{{border:1px solid {t['bg3']};background:{t['bg2']};}}")
        layout.addWidget(scroll, 1)

        btn_row_layout = QHBoxLayout()
        btn_all  = QPushButton("Select all")
        btn_none = QPushButton("Select none")
        for b in (btn_all, btn_none):
            b.setFixedHeight(28)
            b.setStyleSheet(f"font-size:{fs['sm']}px;")
        def _check_all() -> None:
            for _cb, _ in self._checkboxes:
                _cb.setChecked(True)
        def _check_none() -> None:
            for _cb, _ in self._checkboxes:
                _cb.setChecked(False)
        btn_all.clicked.connect(_check_all)
        btn_none.clicked.connect(_check_none)
        btn_row_layout.addWidget(btn_all)
        btn_row_layout.addWidget(btn_none)
        btn_row_layout.addStretch()
        layout.addLayout(btn_row_layout)

        layout.addWidget(sep())
        layout.addWidget(ok_cancel_buttons(self, self._accept))

    def _populate(self) -> None:
        t  = current_theme()
        fs = font_scale()

        self._header_lbl.setText(
            f"<b style='color:{t['accent']};font-size:{fs['md']}px;'>Select entries to exclude:</b>"
            f"<br><span style='font-size:{fs['sm']}px;color:{t['text_dim']};'>Root: {self.src_abs}</span>"
        )
        rel_display = self._current_rel if self._current_rel else "(root)"
        self._path_lbl.setText(f"Browsing: {rel_display}")
        self._up_btn.setEnabled(bool(self._current_rel))

        while self._scroll_layout.count():
            item = self._scroll_layout.takeAt(0)
            if item:
                w = item.widget()
                if w:
                    w.deleteLater()
        self._checkboxes.clear()

        abs_current = os.path.join(self.src_abs, self._current_rel) if self._current_rel else self.src_abs
        try:
            entries = sorted(os.scandir(abs_current), key=lambda e: (not e.is_dir(), e.name.lower()))
        except (PermissionError, FileNotFoundError, OSError) as exc:
            lbl = QLabel(f"Cannot read directory: {exc}")
            lbl.setStyleSheet(f"color:{t['warning']};")
            self._scroll_layout.addWidget(lbl)
            self._scroll_layout.addStretch()
            return

        for entry in entries:
            is_dir   = entry.is_dir(follow_symlinks=False)
            name: str = str(entry.name)
            rel_path: str = (self._current_rel + "/" + name) if self._current_rel else name

            is_checked = rel_path in self.selected_excludes
            has_sub    = is_dir and any(ex.startswith(rel_path + "/") for ex in self.selected_excludes)

            row_widget = QWidget()
            row_layout = QHBoxLayout(row_widget)
            row_layout.setContentsMargins(0, 0, 0, 0)
            row_layout.setSpacing(4)

            icon  = "📁" if is_dir else "📄"
            label = f"{icon} {name}" + ("  ✦" if has_sub else "")
            cb    = QCheckBox(label)
            cb.setChecked(is_checked)
            cb.setStyleSheet(
                f"QCheckBox{{color:{t['warning'] if is_checked else t['text']};font-size:{fs['md']}px;}}"
                f"QCheckBox::indicator:checked{{background:{t['warning']};border:1px solid {t['warning']};}}"
            )
            if is_dir:
                def _on_check(state: int, rp: str = rel_path) -> None:
                    if state:
                        self.selected_excludes = [ex for ex in self.selected_excludes if not ex.startswith(rp + "/")]
                cb.stateChanged.connect(_on_check)

            row_layout.addWidget(cb, 1)

            if is_dir:
                nav_btn = QPushButton("▶")
                nav_btn.setFixedSize(28, 24)
                nav_btn.setToolTip(f"Browse into {name}")
                nav_btn.setStyleSheet(
                    f"QPushButton{{font-size:{fs['sm']}px;color:{t['accent']};"
                    f"background:{t['bg3']};border:1px solid {t['bg3']};border-radius:3px;}}"
                    f"QPushButton:hover{{background:{t['accent']};color:{t['bg2']};}}"
                )
                nav_btn.clicked.connect(lambda _=False, rp=rel_path: self._enter_dir(rp))
                row_layout.addWidget(nav_btn)

            self._checkboxes.append((cb, rel_path))
            self._scroll_layout.addWidget(row_widget)

        self._scroll_layout.addStretch()

    def _save_current_view(self) -> None:
        for cb, rel_path in self._checkboxes:
            if cb.isChecked():
                if rel_path not in self.selected_excludes:
                    self.selected_excludes.append(rel_path)
            else:
                if rel_path in self.selected_excludes:
                    self.selected_excludes.remove(rel_path)

    def _enter_dir(self, rel_path: str) -> None:
        self._save_current_view()
        self._current_rel = rel_path
        self._populate()

    def _go_up(self) -> None:
        self._save_current_view()
        self._current_rel = self._current_rel.rsplit("/", 1)[0] if "/" in self._current_rel else ""
        self._populate()

    def _accept(self) -> None:
        self._save_current_view()
        self.accept()

class AdvancedOptionsDialog(QDialog):

    def __init__(self, parent, options: dict):
        super().__init__(parent)
        self.setWindowTitle("Advanced Options")
        self.setMinimumWidth(640)
        self._opt: dict = dict(options)
        self._build()

    def _build(self) -> None:
        t  = current_theme()
        fs = font_scale()
        lay = QVBoxLayout(self)
        lay.setSpacing(10)
        lay.setContentsMargins(16, 16, 16, 16)

        lay.addWidget(hdr_label("Advanced Backup / Restore Options"))
        lay.addWidget(sep())

        def _note(text: str) -> QLabel:
            lbl = QLabel(text)
            lbl.setWordWrap(True)
            lbl.setStyleSheet(f"color:{t['text_dim']};font-size:{fs['sm']}px;padding-left:26px;")
            return lbl

        self._mirror_cb = QCheckBox("Delete extraneous destination files (mirror mode)")
        self._mirror_cb.setChecked(bool(self._opt.get("mirror_delete", False)))
        apply_tooltip(
            self._mirror_cb,
            "After copying, any file or folder that exists in the destination but "
            "no longer exists in the source will be <b>deleted</b>.<br><br>"
            "This guarantees that source and destination are <b>exactly identical</b> "
            "once the operation finishes.<br><br>"
            "<i>Supported for local-to-local paths and for SSH destinations (via rsync "
            "<code>--delete</code>). Not supported for SMB destinations.</i>",
        )
        lay.addWidget(self._mirror_cb)
        lay.addWidget(_note("Makes the destination match the source exactly by removing "
                            "anything the source no longer has."))

        self._confirm_cb = QCheckBox("Ask for confirmation before deleting anything")
        self._confirm_cb.setChecked(bool(self._opt.get("confirm_before_delete", True)))
        apply_tooltip(
            self._confirm_cb,
            "Shows a confirmation dialog listing every item that would be deleted "
            "before mirror mode removes anything. Applies to local paths; "
            "recommended to keep enabled.<br><br>"
            "<i>SSH deletions happen as part of the rsync transfer itself and "
            "are not covered by this confirmation.</i>",
        )
        lay.addWidget(self._confirm_cb)

        lay.addWidget(sep())

        self._versioned_cb = QCheckBox("Create versioned archive folders (never overwrite)")
        self._versioned_cb.setChecked(bool(self._opt.get("versioned_archive", False)))
        apply_tooltip(
            self._versioned_cb,
            "Instead of overwriting files at the destination, every run creates a "
            "<b>brand-new subfolder</b> named <code>&lt;number&gt; - &lt;date&gt; &lt;time&gt;</code> "
            "(e.g. <code>001 - 2026-07-21 18-42-05</code>, then <code>002 - …</code>, and so on) "
            "and copies the source into it, so every previous version stays fully intact.<br><br>"
            "<i>Only applies to local destination paths.</i>",
        )
        lay.addWidget(self._versioned_cb)
        lay.addWidget(_note("Every run adds a brand-new, self-contained snapshot — nothing "
                            "is ever overwritten (unless a limit is set below)."))

        max_row = QHBoxLayout()
        max_row.addSpacing(26)
        max_row.addWidget(QLabel("Keep at most:"))
        self._max_spin = QSpinBox()
        self._max_spin.setRange(0, 9999)
        self._max_spin.setSpecialValueText("Unlimited")
        self._max_spin.setValue(int(self._opt.get("max_versions", 0) or 0))
        self._max_spin.setSuffix(" version(s)")
        apply_tooltip(
            self._max_spin,
            "When more versions than this already exist, the oldest ones are "
            "automatically deleted to make room for the new one. "
            "Set to 0 to keep every version forever.",
        )
        max_row.addWidget(self._max_spin)
        max_row.addStretch(1)
        lay.addLayout(max_row)

        lay.addWidget(sep())
        lay.addWidget(ok_cancel_buttons(self, self._accept))

        self._mirror_cb.toggled.connect(self._sync_exclusive)
        self._versioned_cb.toggled.connect(self._sync_exclusive)
        self._sync_exclusive()

    def _sync_exclusive(self) -> None:
        if self._mirror_cb.isChecked() and self._versioned_cb.isChecked():
            if self.sender() is self._versioned_cb:
                block_set(self._mirror_cb, False)
            else:
                block_set(self._versioned_cb, False)
        self._confirm_cb.setEnabled(self._mirror_cb.isChecked())
        self._max_spin.setEnabled(self._versioned_cb.isChecked())

    def _accept(self) -> None:
        self._opt = {
            "mirror_delete":         self._mirror_cb.isChecked(),
            "confirm_before_delete": self._confirm_cb.isChecked(),
            "versioned_archive":     self._versioned_cb.isChecked(),
            "max_versions":          self._max_spin.value(),
        }
        self.accept()

    @property
    def result_options(self) -> dict:
        return self._opt


class EntryDialog(QDialog):
    _COL_CLEAR = QColor(0, 0, 0, 0)

    def __init__(self, parent, entry: dict | None, *, stacked: bool = False, _pairs: list[list[str]] | None = None):
        super().__init__(parent)
        self.result: dict           = {}
        self.pairs: list[list[str]] = list(_pairs) if _pairs is not None else []
        self.stacked: bool          = stacked
        self._suppress_sync: bool   = False
        self._entry_snapshot: dict  = entry or {}
        self._e: dict = entry or {}
        self._show_full_paths: bool = False
        self._pairs_provided: bool  = _pairs is not None
        raw_details: dict = (entry or {}).get("details", {})
        raw_excl: dict = raw_details.get("exclude_paths", {})
        self._pair_excludes: dict[str, list[str]] = {str(k): list(v) for k, v in raw_excl.items() if isinstance(v, list)}
        self._advanced: dict = {
            "mirror_delete":         bool(raw_details.get("mirror_delete", False)),
            "confirm_before_delete": bool(raw_details.get("confirm_before_delete", True)),
            "versioned_archive":     bool(raw_details.get("versioned_archive", False)),
            "max_versions":          int(raw_details.get("max_versions", 0) or 0),
        }
        t = current_theme()
        self._COL_ACTIVE_BG  = QColor(t["info"])
        self._COL_ACTIVE_FG  = QColor(t["bg"])
        self._COL_PARTNER_BG = QColor(t["warning"])
        self._COL_PARTNER_FG = QColor(t["bg"])
        self.setWindowTitle("Edit Entry" if entry else "New Entry")
        self._build(self._entry_snapshot)

    @property
    def snapshot(self) -> dict:
        return self._entry_snapshot

    def _compute_size(self) -> tuple[int, int]:
        fm     = QFontMetrics(QFont("monospace", 15))
        max_px = max((fm.horizontalAdvance(p) for pair in self.pairs for p in pair), default=400)
        scr    = QApplication.primaryScreen()
        screen = scr.availableGeometry() if scr else None
        if screen is None:
            return 1200, 800
        pad = 80
        if self.stacked:
            return (max(1110, min(max_px + 140, screen.width() - pad)),
                    max(900, min(screen.height() - pad, 1100)) + 10)
        return (max(1200, min(max_px * 2 + 150, screen.width() - pad)),
                max(800, min(screen.height() - pad, 950)) + 10)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if not event.spontaneous():
            w, h = self._compute_size()
            self.setMinimumSize(w, h)
            self.resize(w, h)

    def _build(self, e: dict) -> None:
        from PyQt6.QtWidgets import QAbstractItemView
        t  = current_theme()
        fs = font_scale()

        src_paths = _norm_paths(e.get("source", []))
        dst_paths = _norm_paths(e.get("destination", []))
        if not self._pairs_provided and not self.pairs:
            n = max(len(src_paths), len(dst_paths))
            self.pairs = [[src_paths[i] if i < len(src_paths) else "",
                           dst_paths[i] if i < len(dst_paths) else ""] for i in range(n)]

        root = QVBoxLayout(self)
        root.setSpacing(8)
        root.setContentsMargins(12, 12, 12, 12)

        title_row  = QHBoxLayout()
        title_row.addWidget(hdr_label("Edit Entry" if e else "New Entry"))
        title_row.addStretch()
        layout_btn = QPushButton("Side-by-Side View" if self.stacked else "Stacked View")
        layout_btn.setFixedHeight(28)
        layout_btn.setToolTip("Toggle between side-by-side and stacked layout")
        layout_btn.clicked.connect(self._toggle_layout)
        title_row.addWidget(layout_btn)
        root.addLayout(title_row)
        root.addWidget(sep())

        form = QFormLayout()
        form.setSpacing(6)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        self.hdr = QComboBox()
        self.hdr.setEditable(True)
        header_keys = list(S.headers.keys())
        self.hdr.addItems(header_keys)
        saved_hdr = e.get("header", "")
        if saved_hdr:
            self.hdr.setCurrentText(saved_hdr)
        elif header_keys:
            self.hdr.setCurrentIndex(0)

        self.title_edit = QLineEdit(e.get("title", ""))
        self.title_edit.setPlaceholderText("Entry name (use <br> for line break)")
        form.addRow("Header:", self.hdr)
        form.addRow("Title:",  self.title_edit)
        root.addLayout(form)
        root.addWidget(sep())

        lw_style = (f"QListWidget {{ font-family:monospace;font-size:{fs['xl']}px; }}"
                    f"QListWidget::item {{ padding:6px 6px; }}")
        self._src_list = QListWidget()
        self._dst_list = QListWidget()
        for lw in (self._src_list, self._dst_list):
            lw.setStyleSheet(lw_style)
            lw.setAlternatingRowColors(True)
            lw.setMinimumHeight(180)
            lw.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
            lw.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)

        hint_html = (
            f"<div style='color:{t['muted']};font-family:monospace;font-size:{fs['sm']}px;"
            f"line-height:180%;text-align:center;'>"
            f"Click <b style='color:{t['accent']};'>'➕ Add Pair'</b> to add source and destination pairs."
            f"<table cellspacing='0' cellpadding='4' align='center' style='text-align:left;'>"
            f"<tr><td style='white-space:nowrap;padding-right:20px;vertical-align:top;'>Local file:</td>"
            f"    <td>~/Documents/notes.txt<br>"
            f"        <span style='color:{t['muted']};'> or {_HOME}/Documents/notes.txt</span></td></tr>"
            f"<tr><td style='white-space:nowrap;padding-right:20px;vertical-align:top;'>Local folder:</td>"
            f"    <td>~/.config/app/<br>"
            f"        <span style='color:{t['muted']};'>or {_HOME}/.config/app/</span></td></tr>"
            f"<tr><td colspan='2' style='font-size:{fs['xs']}px;padding-top:4px;padding-bottom:8px;'>"
            f"(You can use ~ or the full path {_HOME}/…)<br></td></tr>"
            f"<tr><td style='white-space:nowrap;padding-right:20px;vertical-align:top;'>Samba Shares:</td>"
            f"    <td>smb://192.168.0.53/share/data/</td></tr>"
            f"</table><br><br></div>")

        self._src_hint = QLabel(self._src_list)
        self._src_hint.setText(hint_html)
        self._src_hint.setTextFormat(Qt.TextFormat.RichText)
        self._src_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._src_hint.setStyleSheet("background:transparent;")
        self._src_hint.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._src_hint.setWordWrap(True)

        self._hint_resizer = _HintResizer(self._src_list, self._src_hint)
        self._src_list.installEventFilter(self._hint_resizer)
        self._populate_lists()

        self._src_list.currentRowChanged.connect(lambda r: self._on_selection(self._src_list, self._dst_list, r))
        self._dst_list.currentRowChanged.connect(lambda r: self._on_selection(self._dst_list, self._src_list, r))
        self._src_list.itemDoubleClicked.connect(lambda item: self._edit_pair(self._src_list.row(item)))
        self._dst_list.itemDoubleClicked.connect(lambda item: self._edit_pair(self._dst_list.row(item)))

        def _panel(_label: str, _lw: QListWidget) -> QWidget:
            w  = QWidget()
            vl = QVBoxLayout(w)
            vl.setContentsMargins(0, 0, 0, 0)
            vl.setSpacing(4)
            lbl = QLabel(_label)
            lbl.setStyleSheet(f"font-weight:bold;font-size:{fs['lg']}px;color:{t['accent']};padding:1px 0;")
            vl.addWidget(lbl)
            vl.addWidget(_lw, 1)
            return w

        orientation    = Qt.Orientation.Vertical if self.stacked else Qt.Orientation.Horizontal
        self._splitter = QSplitter(orientation)
        self._splitter.setChildrenCollapsible(False)
        self._splitter.addWidget(_panel("Source",      self._src_list))
        self._splitter.addWidget(_panel("Destination", self._dst_list))
        self._splitter.setSizes([10000, 10000])
        root.addWidget(self._splitter, 1)

        root.addWidget(sep())
        tb = QHBoxLayout()
        tb.setSpacing(6)
        for label, tip, fn in [
            ("➕ Add Pair",  "Add a new source/destination pair",                    self._add_pair),
            ("✏️ Edit",      "Edit selected pair (or double-click)",                  self._edit_selected),
            ("🚫 Exclude",   "Exclude files/subdirs within the selected source dir",  self._exclude_selected),
            ("🛠 Advanced",  "Configure advanced backup/restore behaviour (mirror "
                            "delete, versioned archives) for this entry",           self._open_advanced),
            ("🗑 Remove",    "Remove selected pair",                                  self._remove_selected),
            ("▲ Move Up",   "Move selected pair up",                                 self._move_up),
            ("▼ Move Down", "Move selected pair down",                               self._move_down),
        ]:
            b = QPushButton(label)
            b.setToolTip(tip)
            b.setFixedHeight(32)
            b.setMinimumWidth(110)
            b.clicked.connect(fn)
            tb.addWidget(b)
        tb.addStretch(1)

        self._expand_paths_cb = QCheckBox("Expand paths")
        self._expand_paths_cb.setChecked(self._show_full_paths)
        self._expand_paths_cb.toggled.connect(self._on_full_paths_toggled)
        tb.addWidget(self._expand_paths_cb)
        root.addLayout(tb)

        root.addWidget(sep())
        flags = QHBoxLayout()
        self.no_backup  = QCheckBox("Exclude from backup")
        self.no_restore = QCheckBox("Exclude from restore")
        details = e.get("details") or {}
        self.no_backup.setChecked(details.get("no_backup", False))
        self.no_restore.setChecked(details.get("no_restore", False))
        flags.addWidget(self.no_backup)
        flags.addSpacing(16)
        flags.addWidget(self.no_restore)
        flags.addStretch()
        root.addLayout(flags)

        root.addWidget(sep())
        hooks_btn = QPushButton("🪝 Hooks…")
        hooks_btn.setToolTip("Configure pre/post shell hooks for this entry")
        hooks_btn.clicked.connect(self._edit_hooks)

        bot_row = QHBoxLayout()
        bot_row.addWidget(hooks_btn)
        bot_row.addStretch()
        bot_row.addWidget(ok_cancel_buttons(self, self._accept))
        root.addLayout(bot_row)

    def _edit_hooks(self) -> None:
        from pre_post_hooks import HooksDialog
        if not hasattr(self, "_e") or not isinstance(self._e, dict):
            self._e = {}
        entry = {**self.snapshot, "details": dict(self._e.get("details", {}))}
        dlg = HooksDialog(self, entry)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._e.setdefault("details", {}).update(entry.get("details", {}))

    def _populate_lists(self) -> None:
        self._src_list.clear()
        self._dst_list.clear()
        expand = os.path.expanduser
        fmt    = expand if self._show_full_paths else (lambda p: apply_replacements(expand(p)))
        for src, dst in self.pairs:
            self._src_list.addItem(str(fmt(src)))
            self._dst_list.addItem(str(fmt(dst)))
        self._src_hint.setVisible(self._src_list.count() == 0)
        self._src_hint.setGeometry(self._src_list.rect())

    def _on_full_paths_toggled(self, checked: bool) -> None:
        self._show_full_paths = checked
        self._populate_lists()

    @staticmethod
    def _set_row_colours(lw: QListWidget, row: int, bg: QColor, fg: QColor) -> None:
        if 0 <= row < lw.count():
            item = lw.item(row)
            if item is None:
                return
            item.setData(Qt.ItemDataRole.BackgroundRole, bg)
            item.setData(Qt.ItemDataRole.ForegroundRole, fg)

    def _clear_all_colours(self) -> None:
        for lw in (self._src_list, self._dst_list):
            fg = lw.palette().color(lw.foregroundRole())
            for i in range(lw.count()):
                _item = lw.item(i)
                if _item is None:
                    continue
                _item.setData(Qt.ItemDataRole.BackgroundRole, self._COL_CLEAR)
                _item.setData(Qt.ItemDataRole.ForegroundRole, fg)

    def _on_selection(self, active: QListWidget, partner: QListWidget, row: int) -> None:
        if self._suppress_sync or row < 0:
            return
        self._suppress_sync = True
        try:
            self._clear_all_colours()
            self._set_row_colours(active,  row, self._COL_ACTIVE_BG,  self._COL_ACTIVE_FG)
            self._set_row_colours(partner, row, self._COL_PARTNER_BG, self._COL_PARTNER_FG)
            partner.setCurrentRow(row)
        finally:
            self._suppress_sync = False

    def _pair_dialog(self, src: str = "", dst: str = "", title: str = "Add Entry") -> tuple[str, str] | None:
        scr       = QApplication.primaryScreen()
        screen    = scr.availableGeometry() if scr else None
        dlg_w     = max(700, min((screen.width() - 80) if screen else 900, 1200))
        dlg_max_h = (screen.height() - 80) if screen else 800

        dlg = QDialog(self)
        dlg.setWindowTitle(title)
        dlg.setMinimumWidth(dlg_w)
        if screen:
            dlg.setMaximumSize(screen.width() - 40, dlg_max_h)

        mono   = QFont("monospace", 13)
        fm     = QFontMetrics(mono)
        line_h = fm.height() + 6
        t      = current_theme()

        def _make_editor(prefill: str, placeholder: str) -> QPlainTextEdit:
            ed = QPlainTextEdit(prefill)
            ed.setFont(mono)
            ed.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
            ed.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            ed.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
            ed.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
            ed.setPlaceholderText(placeholder)
            ed.setStyleSheet(
                f"QPlainTextEdit{{background:{t['bg2']};color:{t['text']};"
                f"border:1px solid {t['header_sep']};border-radius:4px;padding:6px}}"
                f"QPlainTextEdit:focus{{border:1px solid {t['accent']};}}"
            )
            ph_lines = placeholder.count("\n") + 1

            def _adjust():
                doc = ed.document()
                doc_h = int(doc.size().height()) if doc is not None else 0
                eff   = max(doc_h, ph_lines if not ed.toPlainText() else 1)
                new_h = max(min(eff * line_h + 12, dlg_max_h // 3), line_h + 12)
                ed.setFixedHeight(new_h)
                dlg.adjustSize()

            _doc = ed.document()
            if _doc is not None:
                _doc.contentsChanged.connect(_adjust)
            _adjust()
            return ed

        vl = QVBoxLayout(dlg)
        vl.setSpacing(10)
        vl.setContentsMargins(16, 16, 16, 16)

        def _path_row(label: str, prefill: str, placeholder: str) -> QPlainTextEdit:
            vl.addWidget(QLabel(label))
            ed = _make_editor(prefill, placeholder)
            vl.addWidget(browse_field(dlg, ed))
            return ed

        src_ed = _path_row("Source path:",      src, "Enter path or use '📄 File' or '📁 Directory'")
        dst_ed = _path_row("Destination path:", dst, "Enter path or use '📄 File' or '📁 Directory'")

        vl.addWidget(sep())
        _buttons = QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel  # type: ignore[attr-defined]
        bb     = QDialogButtonBox(_buttons)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        vl.addWidget(bb)

        dlg.adjustSize()
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return None

        s_raw = src_ed.toPlainText().strip()
        d_raw = dst_ed.toPlainText().strip()

        s_lines = [l for l in s_raw.splitlines() if l.strip()]
        d_lines = [l for l in d_raw.splitlines() if l.strip()]

        if len(s_lines) > 1 or len(d_lines) > 1:
            QMessageBox.warning(
                self,
                "One Path per Field",
                "Each field must contain exactly one path.\n\n"
                "Use '➕ Add Pair' again to add additional source/destination pairs.\n\n"
                "Only the first line has been kept."
            )

        s = s_lines[0] if s_lines else ""
        d = d_lines[0] if d_lines else ""
        return (s, d) if (s or d) else None

    def _add_pair(self) -> None:
        result = self._pair_dialog()
        if result is None:
            return
        self.pairs.append([result[0], result[1]])
        self._populate_lists()
        self._src_list.setCurrentRow(len(self.pairs) - 1)

    def _edit_pair(self, row: int) -> None:
        if not (0 <= row < len(self.pairs)):
            return
        result = self._pair_dialog(*self.pairs[row], title=f"Edit pair #{row + 1}")
        if result is None:
            return
        self.pairs[row] = list(result)
        self._populate_lists()
        self._src_list.setCurrentRow(row)

    def _get_active_row(self) -> int:
        row = self._src_list.currentRow()
        return self._dst_list.currentRow() if row < 0 else row

    def _edit_selected(self) -> None:
        self._edit_pair(self._get_active_row())

    def _remove_selected(self) -> None:
        row = self._get_active_row()
        if 0 <= row < len(self.pairs):
            self.pairs.pop(row)
            self._populate_lists()
            new_row = min(row, len(self.pairs) - 1)
            if new_row >= 0:
                self._src_list.setCurrentRow(new_row)

    def _move_up(self) -> None:
        row = self._get_active_row()
        if row > 0:
            self.pairs[row - 1], self.pairs[row] = self.pairs[row], self.pairs[row - 1]
            self._populate_lists()
            self._src_list.setCurrentRow(row - 1)

    def _move_down(self) -> None:
        row = self._get_active_row()
        if 0 <= row < len(self.pairs) - 1:
            self.pairs[row], self.pairs[row + 1] = self.pairs[row + 1], self.pairs[row]
            self._populate_lists()
            self._src_list.setCurrentRow(row + 1)

    def _exclude_selected(self) -> None:
        row = self._get_active_row()
        if not (0 <= row < len(self.pairs)):
            QMessageBox.information(self, "Exclude", "Please select a pair first.")
            return
        src_raw = self.pairs[row][0]
        src_abs = os.path.abspath(os.path.expanduser(src_raw))
        if not os.path.isdir(src_abs):
            QMessageBox.information(
                self, "Exclude",
                "The source is not a directory.\nExclusions can only be defined for directories."
            )
            return
        current_excludes = self._pair_excludes.get(src_abs, [])
        dlg = ExcludeDialog(self, src_abs, current_excludes)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            if dlg.selected_excludes:
                self._pair_excludes[src_abs] = dlg.selected_excludes
            elif src_abs in self._pair_excludes:
                del self._pair_excludes[src_abs]
            self._populate_lists()

    def _open_advanced(self) -> None:
        dlg = AdvancedOptionsDialog(self, self._advanced)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._advanced = dlg.result_options

    def _toggle_layout(self) -> None:
        _existing_details = self._e.get("details", {}) if isinstance(self._e, dict) else {}
        self._entry_snapshot = {
            **self._entry_snapshot,
            "header":  self.hdr.currentText().strip(),
            "title":   self.title_edit.text().strip(),
            "details": {"no_backup":  self.no_backup.isChecked(), "no_restore": self.no_restore.isChecked(),
                        "exclude_paths": dict(self._pair_excludes),
                        "pre_hooks": _existing_details.get("pre_hooks", []),
                        "post_hooks": _existing_details.get("post_hooks", []),
                        **self._advanced}}
        self.stacked = not self.stacked
        self.done(RESTART_DIALOG)

    def _accept(self) -> None:
        hdr   = self.hdr.currentText().strip()
        title = self.title_edit.text().strip()
        if not hdr or not title:
            QMessageBox.warning(self, "Error", "Header and title are required fields.")
            return

        valid_pairs = [(s.strip(), d.strip()) for s, d in self.pairs if s.strip() and d.strip()]
        if not valid_pairs:
            QMessageBox.warning(self, "Error", "At least one source and one destination path are required.")
            return

        if hdr not in S.headers:
            S.headers[hdr] = {"inactive": False, "color": "#ffffff"}

        valid_srcs = {os.path.abspath(os.path.expanduser(s)) for s, _ in valid_pairs}
        clean_excludes = {k: v for k, v in self._pair_excludes.items() if k in valid_srcs and v}

        _existing_details = self._e.get("details", {})
        self.result = {"header": hdr, "title": title,
                       "source": [s for s, _ in valid_pairs],
                       "destination": [d for _, d in valid_pairs],
                       "details": {"no_backup": self.no_backup.isChecked(),
                                   "no_restore": self.no_restore.isChecked(),
                                   "exclude_paths": clean_excludes,
                                   "pre_hooks": _existing_details.get("pre_hooks", []),
                                   "post_hooks": _existing_details.get("post_hooks", []),
                                   **self._advanced}}
        self.accept()

class MountDialog(QDialog):

    def __init__(self, parent, opt: dict | None):
        super().__init__(parent)
        self.result: dict = {}
        self.setWindowTitle("Edit Drive" if opt else "New Drive")
        self.setMinimumSize(900, 500)
        _opt: dict = opt or {}
        t   = current_theme()
        layout = QVBoxLayout(self)
        layout.setSpacing(15)
        layout.addWidget(hdr_label("Configure Drive"))
        layout.addWidget(sep())
        form = QFormLayout()
        form.setSpacing(15)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapAllRows)

        def _field(key: str, placeholder: str) -> QLineEdit:
            f = QLineEdit(_opt.get(key, "") or "")
            f.setPlaceholderText(placeholder)
            return f

        def _info_label(text: str, tooltip: str) -> QLabel:
            lbl = QLabel(text)
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lbl.setStyleSheet(f"color:{t['accent2']};")
            apply_tooltip(lbl, tooltip)
            return lbl
        self.name = _field("drive_name", "e.g. Backup 1")
        form.addRow(QLabel("Drive name:"))
        form.addRow(self.name)
        self.mount_path = _field("mount_path", "e.g. smb://192.168.0.122/Backup Drive/")
        form.addRow(_info_label("󰔨 Mount path (optional)",
                                "<u>Mount Path (optional)</u><br><br>"
                                "Only needed if this drive cannot be detected automatically.<br><br>"
                                "<i>Leave empty</i> for standard USB/SATA drives — Backup Helper finds them "
                                "automatically under <code>/run/media/&lt;user&gt;/&lt;name&gt;</code>, "
                                "<code>/media/&lt;user&gt;/&lt;name&gt;</code> or <code>/mnt/&lt;name&gt;</code>"
                                " using the name from above.<br><br>"
                                "<i>Fill in</i> when the drive is mounted elsewhere (sshfs, KDE Connect, etc.)."))
        form.addRow(self.mount_path)
        self.mount = _field("mount_command", "udisksctl mount --block-device /dev/sdX1")
        form.addRow(_info_label("󰔨 Mount command:",
                                "<u>Mount Command</u><br><br>"
                                "The command is executed non-interactively — <b>no password prompt will appear</b>."
                                "<br><br><b>sshfs:</b> SSH connections must use key-based authentication.<br>"
                                "Set up a key pair first:<br>"
                                "<code>ssh-keygen -t ed25519 &amp;&amp; ssh-copy-id user@host</code><br><br>"
                                "Example: <code>sshfs user@host:/remote/path ~/local/mountpoint</code><br><br>"
                                "<b>udisksctl / mount:</b> Work as usual for local drives.<br>"
                                "<b>kdeconnect-cli:</b> The device must already be paired and reachable.<br><br>"
                                "<small>Allowed commands: mount, umount, mount.cifs, udisksctl, kdeconnect-cli, "
                                "sshfs, fusermount3, fusermount</small>"))
        form.addRow(self.mount)
        self.unmnt    = _field("unmount_command", "udisksctl unmount --block-device /dev/sdX1")
        lbl_unmnt     = QLabel("Unmount command:")
        lbl_unmnt.setAlignment(Qt.AlignmentFlag.AlignCenter)
        form.addRow(lbl_unmnt)
        form.addRow(self.unmnt)
        layout.addLayout(form)
        layout.addStretch()
        layout.addWidget(sep())
        layout.addWidget(ok_cancel_buttons(self, self._accept))

    def _accept(self) -> None:
        from drive_utils import _valid_drive_name
        name = self.name.text().strip()
        if not name:
            QMessageBox.warning(self, "Error", "Name is a required field.")
            return
        if not _valid_drive_name(name):
            QMessageBox.warning(self, "Invalid Drive Name",
                                "The drive name contains invalid characters or exceeds 128 characters.\n\n"
                                "Allowed: letters, digits, spaces, hyphens, underscores, dots, parentheses, @ and :")
            return
        self.result = {"drive_name": name, "mount_path": self.mount_path.text().strip(),
                       "mount_command": self.mount.text().strip(), "unmount_command": self.unmnt.text().strip()}
        self.accept()

class MountsDialog(_ListDialog):

    def __init__(self, parent):
        self.was_changed: bool = False
        super().__init__(parent, "Mount Options", (700, 460), "Mounted Drives",
                         [("🆕 New", "_new"), ("✎ Edit", "_edit"), ("✕ Remove", "_del")])

    def _refresh(self) -> None:
        self.item_list.clear()
        t   = current_theme()
        out = get_mounts()
        for opt in S.mount_options:
            mounted = is_mounted(opt, out)
            status  = "●" if mounted else "○"
            item    = QListWidgetItem(f"  {status}  {opt.get('drive_name', '?')}")
            item.setForeground(QColor(t["green"] if mounted else t["text_dim"]))
            item.setData(Qt.ItemDataRole.UserRole, opt)
            self.item_list.addItem(item)

    def _new(self) -> None:
        dlg = MountDialog(self, None)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            S.mount_options.append(dlg.result)
            save_profile()
            self.was_changed = True
            self._refresh()

    def _edit(self) -> None:
        opt = self._selected_data()
        if not isinstance(opt, dict): return
        dlg = MountDialog(self, opt)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            idx = next((i for i, o in enumerate(S.mount_options) if o is opt), None)
            if idx is None:
                name = opt.get("drive_name")
                if name:
                    idx = next((i for i, o in enumerate(S.mount_options)
                                if o.get("drive_name") == name), None)
            if idx is not None:
                S.mount_options[idx] = dlg.result
                save_profile()
                self.was_changed = True
                self._refresh()
            else:
                QMessageBox.warning(self, "Edit Failed",
                                    "Could not locate the selected drive in the current profile.\n"
                                    "Please re-select the entry and try again.")

    def _del(self) -> None:
        opt = self._selected_data()
        if not isinstance(opt, dict):
            return
        name: str = opt.get("drive_name", "?")
        if QMessageBox.question(self, "Remove Drive", f"Really remove '{name}' from mount options?",
                                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
            return
        before = len(S.mount_options)
        S.mount_options = [o for o in S.mount_options if o is not opt]
        if len(S.mount_options) == before and name != "?":
            S.mount_options = [o for o in S.mount_options if o.get("drive_name") != name]
        save_profile()
        self.was_changed = True
        self._refresh()

class HeaderSettingsDialog(_UserRoleListMixin, QDialog):
    _selected_name = _UserRoleListMixin._selected_data

    def __init__(self, parent):
        super().__init__(parent)
        self._headers_backup = copy.deepcopy(S.headers)
        self._entries_backup = copy.deepcopy(S.entries)
        self.was_changed: bool = False
        self.setWindowTitle("Header Settings")
        self.setMinimumSize(750, 500)
        layout = QVBoxLayout(self)
        layout.addWidget(hdr_label("Headers"))
        layout.addWidget(sep())
        self.item_list = QListWidget()
        layout.addWidget(self.item_list, 1)
        layout.addLayout(btn_row([("🆕 New", self._new), ("🎨 Color", self._color), ("⏸ Toggle active", self._toggle),
                                   ("✕ Delete", self._delete), ("↑ Up", self._move_up), ("↓ Down", self._move_down)]))
        layout.addWidget(sep())
        layout.addWidget(ok_cancel_buttons(self, self._save_and_close, "Save && Close"))
        self._refresh()

    def _save_and_close(self) -> None:
        save_profile()
        self.accept()

    def reject(self) -> None:
        S.headers = self._headers_backup
        S.entries = self._entries_backup
        super().reject()

    def _refresh(self) -> None:
        t   = current_theme()
        row = self.item_list.currentRow()
        self.item_list.clear()
        for name, d in S.headers.items():
            status = "  [inactive]" if d["inactive"] else ""
            item = QListWidgetItem(f"  {name}{status}")
            item.setForeground(QColor(t["text_dim"] if d.get("inactive", False) else d.get("color", "#ffffff")))
            item.setData(Qt.ItemDataRole.UserRole, name)
            self.item_list.addItem(item)
        if 0 <= row < self.item_list.count():
            self.item_list.setCurrentRow(row)

    def _new(self) -> None:
        name, ok = ask_text(self, "New Header", "Header name:")
        if not ok or not name.strip():
            return
        name = name.strip()
        if name in S.headers:
            QMessageBox.warning(self, "Duplicate", f"Header '{name}' already exists.")
            return
        col = QColorDialog.getColor(QColor(current_theme()["accent"]), self, "Choose header colour")
        S.headers[name] = {"inactive": False, "color": col.name() if col.isValid() else "#ffffff"}
        self.was_changed = True
        self._refresh()
        for i in range(self.item_list.count()):
            item = self.item_list.item(i)
            if item and item.data(Qt.ItemDataRole.UserRole) == name:
                self.item_list.setCurrentRow(i)
                break

    def _color(self) -> None:
        name = self._selected_name()
        if not name: return
        col = QColorDialog.getColor(QColor(S.headers[name]["color"]), self)
        if col.isValid():
            S.headers[name]["color"] = col.name()
            self.was_changed = True
            self._refresh()

    def _toggle(self) -> None:
        name = self._selected_name()
        if name:
            S.headers[name]["inactive"] = not S.headers[name]["inactive"]
            self.was_changed = True
            self._refresh()

    def _delete(self) -> None:
        name = self._selected_name()
        if not name: return
        if QMessageBox.question(self, "Delete", f"Delete header '{name}' and all its entries?",
                                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes:
            del S.headers[name]
            S.entries    = [e for e in S.entries if e["header"] != name]
            self.was_changed = True
            self._refresh()

    def _move_header(self, direction: int) -> bool:
        name = self._selected_name()
        if not name: return False
        keys    = list(S.headers.keys())
        idx     = keys.index(name)
        new_idx = idx + direction
        if not (0 <= new_idx < len(keys)): return False
        keys[idx], keys[new_idx] = keys[new_idx], keys[idx]
        S.headers = {k: S.headers[k] for k in keys}
        self._refresh()
        self.item_list.setCurrentRow(new_idx)
        return True

    def _move_up(self)   -> None:
        if self._move_header(-1): self.was_changed = True

    def _move_down(self) -> None:
        if self._move_header(+1): self.was_changed = True

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

class LogViewer(_TextViewDialog):

    def __init__(self, parent):
        t = current_theme()
        super().__init__(parent, "Log Viewer", (1350, 950),
                         extra_buttons=[("🔄 Refresh", self._load), ("🗑 Clear", self._clear)])
        top = QWidget()
        top.setStyleSheet(f"background:{t['bg2']};border-bottom:1px solid {t['header_sep']};")
        tl  = QHBoxLayout(top)
        tl.setContentsMargins(14, 8, 14, 8)
        tl.addStretch()
        tl.addWidget(hdr_label("📋 Log File"))
        tl.addStretch(1)
        tl.addWidget(QLabel(apply_replacements(str(_LOG_FILE))))
        layout = self.layout()
        if isinstance(layout, QVBoxLayout):
            layout.insertWidget(0, top)
        else:
            logger.error("LogViewer: unexpected layout type %s", type(layout))
        self._load()

    def _load(self) -> None:
        if not _LOG_FILE.exists():
            self.view.setPlainText("No log file found.")
            return
        try:
            size = _LOG_FILE.stat().st_size
            read_bytes = min(size, 512 * 1024)
            with open(_LOG_FILE, "rb") as f:
                if read_bytes < size:
                    f.seek(size - read_bytes)
                raw = f.read(read_bytes)
            text = raw.decode("utf-8", errors="replace")
            if read_bytes < size:
                first_nl = text.find("\n")
                if first_nl != -1:
                    text = text[first_nl + 1:]
            lines = text.splitlines()
            total_lines = len(lines)
            if read_bytes < size or total_lines > 2000:
                count = "many" if read_bytes < size else str(total_lines)
                prefix = f"[… last 2000 of {count} lines …]\n"
            else:
                prefix = ""
            self.view.setPlainText(prefix + "\n".join(lines[-2000:]))
            cursor = self.view.textCursor()
            cursor.movePosition(QTextCursor.MoveOperation.End)
            self.view.setTextCursor(cursor)
        except Exception as e: self.view.setPlainText(f"Error reading log file: {e}")

    def _clear(self) -> None:
        if QMessageBox.question(self, "Clear log", "Permanently delete all log entries?",
                                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No) \
                == QMessageBox.StandardButton.Yes:
            try:
                _LOG_FILE.write_text("", encoding="utf-8")
            except OSError as e:
                QMessageBox.warning(self, "Error", f"Could not clear log: {e}")
                return
            self._load()

class SysInfoDialog(_TextViewDialog):
    done_sig = pyqtSignal(str)

    def __init__(self, parent):
        super().__init__(parent, "System Information", (1400, 875), font_size=font_sz(2))
        self.view.setPlainText("⏳ Loading system information…")
        self.done_sig.connect(self.view.setPlainText)
        self._closed = threading.Event()
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self) -> None:
        try:
            r = subprocess.run(["inxi", "-SMCGAz", "--no-host", "--color", "0"], capture_output=True, text=True,
                               timeout=15, env={**os.environ, "LANG": "C"}, check=False)
            result = r.stdout.strip() or r.stderr.strip() or "No output received from inxi."
        except FileNotFoundError:
            result = ("'inxi' is not installed.\n\n"
                      "Installation:\n"
                      "  Arch / Manjaro:  sudo pacman -S inxi\n"
                      "  Debian / Ubuntu: sudo apt install inxi\n"
                      "  Fedora:          sudo dnf install inxi\n"
                      "  openSUSE:        sudo zypper install inxi\n"
                      "  Void:            sudo xbps-install inxi\n"
                      "  Alpine:          sudo apk add inxi\n"
                      "  Gentoo:          sudo emerge app-misc/inxi\n"
                      "  Solus:           sudo eopkg install inxi\n"
                      "  NixOS:           nix-env -iA nixpkgs.inxi\n")
        except subprocess.TimeoutExpired:
            result = "System information request timed out."
        except Exception as exc:
            result = f"An unexpected error occurred: {exc}"
        try:
            if not self._closed.is_set():
                self.done_sig.emit(result)
        except RuntimeError:
            pass

    def closeEvent(self, event) -> None:
        try:
            self.done_sig.disconnect()
        except (RuntimeError, TypeError):
            pass
        self._closed.set()
        super().closeEvent(event)

class NotesDialog(_StandardKeysMixin, QDialog):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._saved = False
        self._discarded = False

        profile = S.profile_name or "(no profile)"
        self.setWindowTitle(f"Profile Notes — {profile}")
        self.setMinimumSize(1500, 1000)

        t       = current_theme()
        bg      = t["bg"]
        bg2     = t["bg2"]
        bg3     = t["bg3"]
        sep_col = t["header_sep"]
        acc     = t["accent"]
        fg      = t["text"]
        dim     = t["text_dim"]
        hi      = t["highlight"]
        acc2    = t["accent2"]

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        hdr = QFrame()
        hdr.setStyleSheet(f"background:{bg2};border-bottom:1px solid {sep_col};")
        hl = QHBoxLayout(hdr)
        hl.setContentsMargins(14, 10, 14, 10)
        title_lbl = QLabel(f"📝  Notes  ·  {profile}")
        title_lbl.setStyleSheet(
            f"font-size:{font_sz(4)}px;font-weight:bold;"
            f"color:{acc};background:transparent;"
        )
        hint_lbl = QLabel("Auto-saved on close")
        hint_lbl.setStyleSheet(f"font-size:{font_sz(-2)}px;color:{dim};background:transparent;")
        hl.addWidget(title_lbl)
        hl.addStretch()
        hl.addWidget(hint_lbl)

        self._edit = QTextEdit()
        self._edit.setPlaceholderText(
            "Write notes about this profile here…\n\n"
            "E.g.:\n"
            "  • Last restore tested on …\n"
            "  • Excluded paths / known issues\n"
            "  • Destination host / credentials info"
        )
        self._edit.setStyleSheet(
            f"QTextEdit{{background:{bg3};color:{fg};"
            f"border:none;font-size:{font_sz()}px;padding:12px;}}"
        )
        self._edit.setPlainText(S.notes)

        ftr = QFrame()
        ftr.setStyleSheet(f"background:{bg2};border-top:1px solid {sep_col};")
        fl = QHBoxLayout(ftr)
        fl.setContentsMargins(12, 8, 12, 8)
        fl.addStretch()

        def _btn_style(primary: bool = False) -> str:
            border = acc if primary else sep_col
            return (
                f"QPushButton{{background:{bg3};border:1px solid {border};"
                f"border-radius:4px;color:{fg};padding:4px 16px;}}"
                f"QPushButton:hover{{background:{bg2};border-color:{acc};color:{hi};}}"
                f"QPushButton:focus{{border-color:{acc};color:{hi};outline:none;}}"
                f"QPushButton:pressed{{background:{bg};border-color:{acc2};color:{acc2};}}"
            )

        save_btn = QPushButton("💾 Save && Close")
        save_btn.setMinimumHeight(34)
        save_btn.setStyleSheet(_btn_style(primary=True))
        save_btn.clicked.connect(self._save_and_close)

        discard_btn = QPushButton("Discard")
        discard_btn.setMinimumHeight(34)
        discard_btn.setStyleSheet(_btn_style())
        discard_btn.clicked.connect(self._discard)

        fl.addWidget(discard_btn)
        fl.addWidget(save_btn)

        layout.addWidget(hdr)
        layout.addWidget(self._edit, 1)
        layout.addWidget(ftr)
        self.setStyleSheet(f"background:{bg};")

    def _save_and_close(self) -> None:
        S.notes = self._edit.toPlainText()
        save_profile()
        self._saved = True
        self.accept()

    def _discard(self) -> None:
        self._discarded = True
        self.reject()

    def closeEvent(self, event) -> None:
        if not self._saved and not self._discarded:
            S.notes = self._edit.toPlainText()
            save_profile()
            self._saved = True
        event.accept()


class _ThemeDialog(_StandardKeysMixin, QDialog):
    changed = pyqtSignal(int)

    def __init__(self, parent) -> None:
        super().__init__(parent)
        self.setWindowTitle("Theme and Font Settings")
        self.setMinimumSize(480, 380)
        self.setWindowModality(Qt.WindowModality.NonModal)
        self._orig = (
            S.ui.get("theme", "Tokyo Night"), S.ui.get("font_family", ""), S.ui.get("font_size", 14),
            S.ui.get("disable_tray_icon", False),
        )
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        def _combo(label: str, items: list[str], current: str) -> QComboBox:
            layout.addWidget(QLabel(label))
            cb = QComboBox()
            cb.addItems(items)
            cb.setCurrentText(current)
            layout.addWidget(cb)
            return cb

        self._theme_cb = _combo("Select Theme:", list(THEMES.keys()), S.ui.get("theme", "Tokyo Night"))
        self._font_cb = _combo("Select Font:", ["(System Default)"] + sorted(QFontDatabase.families()),
                               S.ui.get("font_family", "") or "(System Default)")
        current_size = str(S.ui.get("font_size", 14))
        size_options = ["10", "11", "12", "13", "14", "15", "16", "17", "18", "20", "22", "24"]
        if current_size not in size_options:
            size_options = sorted(size_options + [current_size], key=int)
        self._size_cb = _combo("Select Font Size:", size_options, current_size)

        prev_btn = QPushButton("Preview")
        prev_btn.clicked.connect(lambda: self._apply(save=False))
        layout.addWidget(prev_btn)

        self._tray_cb = QCheckBox("Disable Tray Icon")
        self._tray_cb.setChecked(bool(S.ui.get("disable_tray_icon", False)))
        layout.addWidget(self._tray_cb)

        layout.addWidget(ok_cancel_buttons(self, self._on_ok, cancel_fn=self.reject))

    def _apply(self, save: bool = False) -> None:
        chosen_font = self._font_cb.currentText()
        if chosen_font == "(System Default)":
            chosen_font = ""
        S.ui.update(
            theme=self._theme_cb.currentText(), font_family=chosen_font, font_size=int(self._size_cb.currentText()))
        apply_style()
        if save:
            S.ui["disable_tray_icon"] = self._tray_cb.isChecked()
            save_profile()

    def _on_ok(self) -> None:
        self._apply(save=True)
        font_display = self._font_cb.currentText()
        msg = f"Theme: {self._theme_cb.currentText()}, Font: {font_display}, Size: {self._size_cb.currentText()} px"
        QMessageBox.information(self, "Theme Saved", msg)
        self.changed.emit(RESTART_DIALOG)
        self.accept()

    def reject(self) -> None:
        orig_theme, orig_font, orig_size, orig_tray = self._orig
        S.ui.update(theme=orig_theme, font_family=orig_font, font_size=orig_size, disable_tray_icon=orig_tray)
        apply_style()
        super().reject()

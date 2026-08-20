import os

from PyQt6.QtCore import QEvent, QObject, QPoint, QPointF, Qt
from PyQt6.QtGui import QColor, QFont, QFontMetrics, QShowEvent
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QFormLayout, QHBoxLayout,
    QLabel, QLineEdit, QListWidget, QListWidgetItem, QMenu, QMessageBox, QPlainTextEdit,
    QPushButton, QSizePolicy, QSpinBox, QSplitter, QVBoxLayout, QWidget,
)

from state import RESTART_DIALOG, S, _HOME, _norm_paths, apply_replacements, logger, save_profile
from themes import apply_tooltip, current_theme, font_scale
from translations import tr
from ui_utils import block_set, browse_field, color_style, hdr_label, ok_cancel_buttons, sep


class _HintResizer(QObject):
    def __init__(self, watched, hint_label):
        super().__init__(watched)
        self._hint = hint_label

    def eventFilter(self, a0: QObject | None, a1: QEvent | None) -> bool:
        if a1 is not None and a1.type() == QEvent.Type.Resize and isinstance(a0, QWidget):
            self._hint.setGeometry(a0.rect())
        return False

class ExcludeDialog(QDialog):
    def __init__(self, parent, src_abs: str, current_excludes: list[str]):
        super().__init__(parent)
        self.src_abs = src_abs
        self.selected_excludes: list[str] = list(current_excludes)
        self.setWindowTitle(tr("Exclude items from: {path}", path=src_abs))
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
        self._up_btn = QPushButton(f"↑ {tr('Up')}")
        self._up_btn.setFixedHeight(26)
        self._up_btn.setMinimumWidth(70)
        self._up_btn.clicked.connect(self._go_up)
        self._path_lbl = QLabel()
        self._path_lbl.setStyleSheet(color_style(t['text_dim'], fs['sm']))
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
        btn_all  = QPushButton(tr("Select all"))
        btn_none = QPushButton(tr("Select none"))
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
            f"<b style='color:{t['accent']};font-size:{fs['md']}px;'>{tr('Select entries to exclude:')}</b>"
            f"<br><span style='font-size:{fs['sm']}px;color:{t['text_dim']};'>{tr('Root:')} {self.src_abs}</span>"
        )
        rel_display = self._current_rel if self._current_rel else tr("(root)")
        self._path_lbl.setText(tr("Browsing: {path}", path=rel_display))
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
            lbl = QLabel(tr("Cannot read directory: {error}", error=exc))
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
                apply_tooltip(nav_btn, tr("Browse into {name}", name=name))
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

    def __init__(self, parent, options: dict, on_save=None):
        super().__init__(parent)
        self.setWindowTitle(tr("Advanced Options"))
        self.setMinimumSize(700, 450)
        self._opt: dict = dict(options)
        self._on_save = on_save
        self._build()

    def _build(self) -> None:
        t  = current_theme()
        fs = font_scale()
        lay = QVBoxLayout(self)
        lay.setSpacing(10)
        lay.setContentsMargins(16, 16, 16, 16)

        lay.addWidget(hdr_label(tr("Advanced Backup / Restore Options")))
        lay.addWidget(sep())

        def _note(text: str) -> QLabel:
            lbl = QLabel(text)
            lbl.setWordWrap(True)
            lbl.setStyleSheet(f"color:{t['text_dim']};font-size:{fs['sm']}px;padding-left:26px;")
            return lbl

        self._mirror_cb = QCheckBox(tr("Delete extraneous destination files (mirror mode)"))
        self._mirror_cb.setChecked(bool(self._opt.get("mirror_delete", False)))
        apply_tooltip(
            self._mirror_cb,
            tr("After copying, any file or folder that exists in the destination but "
              "no longer exists in the source will be <b>deleted</b>.<br><br>"
              "This guarantees that source and destination are <b>exactly identical</b> "
              "once the operation finishes.<br><br>"
              "<i>Supported for local-to-local paths and for pairs involving an SSH "
              "source or destination (via rsync <code>--delete</code>). Not supported "
              "whenever SMB is involved on either side (source or destination).</i>"),
        )
        lay.addWidget(self._mirror_cb)
        lay.addWidget(_note(tr("Makes the destination match the source exactly by removing "
                            "anything the source no longer has.")))

        self._confirm_cb = QCheckBox(tr("Ask for confirmation before deleting anything"))
        self._confirm_cb.setChecked(bool(self._opt.get("confirm_before_delete", True)))
        apply_tooltip(
            self._confirm_cb,
            tr("Shows a confirmation dialog listing every item that would be deleted "
              "before mirror mode removes anything — for local paths as well as SSH "
              "destinations (previewed via a harmless rsync dry-run beforehand). "
              "Recommended to keep enabled.<br><br>"
              "<i>If the SSH preview can't be determined (e.g. the host is unreachable), "
              "remote deletion is skipped entirely for that backup entry, for safety.</i>"),
        )
        lay.addWidget(self._confirm_cb)

        lay.addWidget(sep())

        self._versioned_cb = QCheckBox(tr("Create versioned archive folders (never overwrite)"))
        self._versioned_cb.setChecked(bool(self._opt.get("versioned_archive", False)))
        apply_tooltip(
            self._versioned_cb,
            tr("Instead of overwriting files at the destination, every run creates a "
              "<b>brand-new subfolder</b> named <code>&lt;number&gt; - &lt;date&gt; &lt;time&gt;</code> "
              "(e.g. <code>001 - 2026-07-21 18-42-05</code>, then <code>002 - …</code>, and so on) "
              "and copies the source into it, so every previous version stays fully intact.<br><br>"
              "<i>Only applies to local destination paths, and only for Backup — "
              "Restore always writes directly to the original location and never "
              "creates a versioned subfolder there.</i>"),
        )
        lay.addWidget(self._versioned_cb)
        lay.addWidget(_note(tr("Every run adds a brand-new, self-contained snapshot — nothing "
                            "is ever overwritten (unless a limit is set below).")))

        max_row = QHBoxLayout()
        max_row.addSpacing(26)
        max_row.addWidget(QLabel(tr("Keep at most:")))
        self._max_spin = QSpinBox()
        self._max_spin.setRange(0, 9999)
        self._max_spin.setSpecialValueText(tr("Unlimited"))
        self._max_spin.setValue(int(self._opt.get("max_versions", 0) or 0))
        self._max_spin.setSuffix(f" {tr('version(s)')}")
        apply_tooltip(
            self._max_spin,
            tr("When more versions than this already exist, the oldest ones are "
              "automatically deleted to make room for the new one. "
              "Set to 0 to keep every version forever."),
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
        if self._on_save is not None:
            try:
                self._on_save(dict(self._opt))
            except Exception as exc:
                logger.error("AdvancedOptionsDialog: on_save callback failed: %s", exc)
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
        self._src_fm_btn: QPushButton | None = None
        self._dst_fm_btn: QPushButton | None = None
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
        self.setWindowTitle(tr("Edit Entry") if entry else tr("New Entry"))
        self._build(self._entry_snapshot)

    @property
    def snapshot(self) -> dict:
        return self._entry_snapshot

    def _toolbar_min_width(self) -> int:
        """Actual width the toolbar row needs (button sizeHints + spacing +
        the 'Expand paths' checkbox), so the dialog never shrinks below what
        the current language's translated button labels require."""
        btns = getattr(self, "_toolbar_btns", None)
        if not btns:
            return 0
        spacing = 6
        total = sum(max(b.sizeHint().width(), b.minimumWidth()) for b in btns)
        total += spacing * (len(btns) - 1)
        cb = getattr(self, "_expand_paths_cb", None)
        if cb is not None:
            total += spacing + cb.sizeHint().width()
        margins = self.layout().contentsMargins() if self.layout() else None
        if margins is not None:
            total += margins.left() + margins.right()
        return total

    def _compute_size(self) -> tuple[int, int]:
        fm     = QFontMetrics(QFont("monospace", 15))
        max_px = max((fm.horizontalAdvance(p) for pair in self.pairs for p in pair), default=400)
        scr    = QApplication.primaryScreen()
        screen = scr.availableGeometry() if scr else None
        if screen is None:
            return 1200, 800
        pad = 80
        tb_min = self._toolbar_min_width()
        if self.stacked:
            w = max(1110, tb_min, min(max_px + 140, screen.width() - pad))
            return (min(w, screen.width() - pad),
                    max(900, min(screen.height() - pad, 1100)) + 10)
        w = max(1200, tb_min, min(max_px * 2 + 150, screen.width() - pad))
        return (min(w, screen.width() - pad),
                max(800, min(screen.height() - pad, 950)) + 10)

    def showEvent(self, a0: QShowEvent | None) -> None:
        super().showEvent(a0)
        if a0 is not None and not a0.spontaneous():
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
        title_row.addWidget(hdr_label(tr("Edit Entry") if e else tr("New Entry")))
        title_row.addStretch()
        layout_btn = QPushButton(tr("Side-by-Side View") if self.stacked else tr("Stacked View"))
        layout_btn.setFixedHeight(28)
        apply_tooltip(layout_btn, tr("Toggle between side-by-side and stacked layout"))
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
        self.title_edit.setPlaceholderText(tr("Entry name (use <br> for line break)"))
        form.addRow(tr("Header:"), self.hdr)
        form.addRow(tr("Title:"),  self.title_edit)
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
            f"{tr('Click')} <b style='color:{t['accent']};'>'➕ {tr('Add Pair')}'</b> {tr('to add source and destination pairs.')}"
            f"<table cellspacing='0' cellpadding='4' align='center' style='text-align:left;'>"
            f"<tr><td style='white-space:nowrap;padding-right:20px;vertical-align:top;'>{tr('Local file:')}</td>"
            f"    <td>~/Documents/notes.txt<br>"
            f"        <span style='color:{t['muted']};'> {tr('or')} {_HOME}/Documents/notes.txt</span></td></tr>"
            f"<tr><td style='white-space:nowrap;padding-right:20px;vertical-align:top;'>{tr('Local folder:')}</td>"
            f"    <td>~/.config/app/<br>"
            f"        <span style='color:{t['muted']};'>{tr('or')} {_HOME}/.config/app/</span></td></tr>"
            f"<tr><td colspan='2' style='font-size:{fs['xs']}px;padding-top:4px;padding-bottom:8px;'>"
            f"{tr('(You can use ~ or the full path {home}/…)', home=str(_HOME))}<br></td></tr>"
            f"<tr><td style='white-space:nowrap;padding-right:20px;vertical-align:top;'>{tr('Samba Shares:')}</td>"
            f"    <td>smb://192.168.0.53/share/data/</td></tr>"
            f"<tr><td style='white-space:nowrap;padding-right:20px;vertical-align:top;'>SSH / rsync:</td>"
            f"    <td>user@192.168.0.53:/data/backup/<br>"
            f"        <span style='color:{t['muted']};'>{tr('or')} 192.168.0.53:/data/backup/</span></td></tr>"
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

        self._src_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._dst_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._src_list.customContextMenuRequested.connect(
            lambda pos: self._show_list_context_menu(self._src_list, True, pos))
        self._dst_list.customContextMenuRequested.connect(
            lambda pos: self._show_list_context_menu(self._dst_list, False, pos))

        def _panel(_label: str, _lw: QListWidget, _is_source: bool) -> QWidget:
            w = QWidget()
            vl = QVBoxLayout(w)
            vl.setContentsMargins(0, 0, 0, 0)
            vl.setSpacing(4)
            hl = QHBoxLayout()
            hl.setContentsMargins(0, 0, 0, 0)
            hl.setSpacing(6)
            lbl = QLabel(_label)
            lbl.setStyleSheet(f"font-weight:bold;font-size:{fs['lg']}px;color:{t['accent']};padding:1px 0;")
            hl.addWidget(lbl)
            hl.addStretch(1)
            fm_btn = QPushButton(f"📂 {tr('Open in file manager')}")
            apply_tooltip(fm_btn, tr("Open the selected {label} path in the default file manager",
                                     label=_label.lower()), wrap=False)
            fm_btn.setStyleSheet(f"font-size:{fs['md']}px;")
            fm_btn.clicked.connect(lambda _=False, is_src=_is_source: self._open_in_file_manager(is_src))
            if _is_source:
                self._src_fm_btn = fm_btn
            else:
                self._dst_fm_btn = fm_btn
            hl.addWidget(fm_btn)
            vl.addLayout(hl)
            vl.addWidget(_lw, 1)
            return w

        orientation    = Qt.Orientation.Vertical if self.stacked else Qt.Orientation.Horizontal
        self._splitter = QSplitter(orientation)
        self._splitter.setChildrenCollapsible(False)
        self._splitter.addWidget(_panel(tr("Source"),      self._src_list, True))
        self._splitter.addWidget(_panel(tr("Destination"), self._dst_list, False))
        self._splitter.setSizes([10000, 10000])
        root.addWidget(self._splitter, 1)
        self._update_fm_buttons()

        root.addWidget(sep())
        tb = QHBoxLayout()
        tb.setSpacing(6)
        self._toolbar_btns: list[QPushButton] = []
        for label, tip, fn in [
            (f"➕ {tr('Add Pair')}", tr("Add a new source/destination pair"),                     self._add_pair),
            (f"✏️ {tr('Edit')}",     tr("Edit selected pair (or double-click)"),                  self._edit_selected),
            (f"🚫 {tr('Exclude')}",  tr("Exclude files/subdirs within the selected source dir"),  self._exclude_selected),
            (f"🛠 {tr('Advanced')}", tr("Configure advanced backup/restore behaviour (mirror "
                            "delete, versioned archives) for this entry"),            self._open_advanced),
            (f"🗑 {tr('Remove')}",    tr("Remove selected pair"),                                  self._remove_selected),
            (f"▲ {tr('Move Up')}",   tr("Move selected pair up"),                                 self._move_up),
            (f"▼ {tr('Move Down')}", tr("Move selected pair down"),                               self._move_down),
        ]:
            b = QPushButton(label)
            apply_tooltip(b, tip)
            b.setFixedHeight(32)
            b.setMinimumWidth(110)
            b.clicked.connect(fn)
            tb.addWidget(b)
            self._toolbar_btns.append(b)
        tb.addStretch(1)

        self._expand_paths_cb = QCheckBox(tr("Expand paths"))
        self._expand_paths_cb.setChecked(self._show_full_paths)
        self._expand_paths_cb.toggled.connect(self._on_full_paths_toggled)
        tb.addWidget(self._expand_paths_cb)
        root.addLayout(tb)

        root.addWidget(sep())
        flags = QHBoxLayout()
        self.no_backup  = QCheckBox(tr("Exclude from backup"))
        self.no_restore = QCheckBox(tr("Exclude from restore"))
        details = e.get("details") or {}
        self.no_backup.setChecked(details.get("no_backup", False))
        self.no_restore.setChecked(details.get("no_restore", False))
        flags.addWidget(self.no_backup)
        flags.addSpacing(16)
        flags.addWidget(self.no_restore)
        flags.addStretch()
        root.addLayout(flags)

        root.addWidget(sep())
        hooks_btn = QPushButton(f"🪝 {tr('Hooks…')}")
        apply_tooltip(hooks_btn, tr("Configure pre/post shell hooks for this entry"))
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
        original = self._entry_snapshot if isinstance(self._entry_snapshot, dict) else None
        persist_target = original if (original and any(e is original for e in S.entries)) else None
        dlg = HooksDialog(self, entry, persist_target=persist_target)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._e.setdefault("details", {}).update(entry.get("details", {}))
            if persist_target is not None:
                self._entry_snapshot.setdefault("details", {}).update(entry.get("details", {}))

    def _populate_lists(self) -> None:
        self._src_list.clear()
        self._dst_list.clear()
        expand = os.path.expanduser
        fmt = expand if self._show_full_paths else (lambda p: apply_replacements(expand(p)))
        for src, dst in self.pairs:
            src_item = QListWidgetItem(str(fmt(src)))
            self._src_list.addItem(src_item)
            dst_item = QListWidgetItem(str(fmt(dst)))
            self._dst_list.addItem(dst_item)

        self._src_hint.setVisible(self._src_list.count() == 0)
        self._src_hint.setGeometry(self._src_list.rect())
        self._update_fm_buttons()

    def _on_full_paths_toggled(self, checked: bool) -> None:
        self._show_full_paths = checked
        self._populate_lists()

    def _update_fm_buttons(self) -> None:
        has_pairs = bool(self.pairs)
        if self._src_fm_btn is not None:
            self._src_fm_btn.setEnabled(has_pairs)
        if self._dst_fm_btn is not None:
            self._dst_fm_btn.setEnabled(has_pairs)

    def _show_list_context_menu(self, lw: QListWidget, is_source: bool, pos: QPoint | QPointF) -> None:
        point = pos.toPoint() if isinstance(pos, QPointF) else pos
        item = lw.itemAt(point)
        if item is None:
            return
        row = lw.row(item)
        if not (0 <= row < len(self.pairs)):
            return
        lw.setCurrentRow(row)

        menu     = QMenu(self)
        act_fm   = menu.addAction(f"📂  {tr('Open in File Manager')}")
        act_copy = menu.addAction(f"📋  {tr('Copy full path')}")
        chosen   = menu.exec(lw.mapToGlobal(point))
        if chosen == act_fm:
            self._open_in_file_manager(is_source)
        elif chosen == act_copy:
            raw = self.pairs[row][0 if is_source else 1]
            clipboard = QApplication.clipboard()
            if clipboard is not None:
                clipboard.setText(os.path.expanduser(raw.strip()))

    def _open_in_file_manager(self, is_source: bool) -> None:
        row = self._src_list.currentRow()
        if row < 0:
            row = self._dst_list.currentRow()
        if not (0 <= row < len(self.pairs)):
            QMessageBox.information(self, tr("Open in File Manager"), tr("Please select a pair first."))
            return
        raw = self.pairs[row][0 if is_source else 1].strip()
        if not raw:
            side = tr("source") if is_source else tr("destination")
            QMessageBox.information(self, tr("Open in File Manager"), tr("This pair has no {side} path set.", side=side))
            return
        from drive_utils import open_in_file_manager
        ok, err = open_in_file_manager(raw)
        if not ok:
            logger.warning("EntryDialog open_in_file_manager: %s", err)
            QMessageBox.warning(self, tr("Open in File Manager"), tr("Could not open file manager:") + f"\n{err}")

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

    def _pair_dialog(self, src: str = "", dst: str = "", title: str | None = None) -> tuple[str, str] | None:
        if title is None:
            title = tr("Add Entry")
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

        src_ed = _path_row(tr("Source path:"),      src, tr("Enter path or use '📄 File' or '📁 Directory'"))
        dst_ed = _path_row(tr("Destination path:"), dst, tr("Enter path or use '📄 File' or '📁 Directory'"))

        vl.addWidget(sep())
        vl.addWidget(ok_cancel_buttons(dlg, dlg.accept, ok_label=tr("Ok")))

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
                tr("One Path per Field"),
                tr("Each field must contain exactly one path.") + "\n\n" +
                tr("Use '➕ Add Pair' again to add additional source/destination pairs.") + "\n\n" +
                tr("Only the first line has been kept.")
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
        result = self._pair_dialog(*self.pairs[row], title=tr("Edit pair #{n}", n=row + 1))
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
            QMessageBox.information(self, tr("Exclude"), tr("Please select a pair first."))
            return
        from drive_utils import is_smb, is_ssh
        src_raw = self.pairs[row][0]
        if is_smb(src_raw) or is_ssh(src_raw):
            QMessageBox.information(
                self, tr("Exclude"),
                tr("Exclusions can't be browsed for SMB or SSH sources — this dialog only "
                  "browses the local filesystem.") + "\n\n" +
                tr("Swap source/destination first if you "
                  "want to exclude items from the local side of this pair instead.")
            )
            return
        src_abs = os.path.abspath(os.path.expanduser(src_raw))
        if not os.path.isdir(src_abs):
            QMessageBox.information(
                self, tr("Exclude"),
                tr("The source is not a directory.") + "\n" + tr("Exclusions can only be defined for directories.")
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
        def _persist(new_advanced: dict) -> None:
            self._advanced = new_advanced
            original = self._entry_snapshot if isinstance(self._entry_snapshot, dict) else None
            if not original:
                return
            idx = next((i for i, e in enumerate(S.entries) if e is original), None)
            if idx is None:
                return
            details = dict(S.entries[idx].get("details", {}))
            details.update(new_advanced)
            S.entries[idx]["details"] = details
            self._e["details"] = details
            self._entry_snapshot["details"] = details
            if not save_profile():
                QMessageBox.warning(
                    self, tr("Save Failed"),
                    tr("The advanced options could not be saved to the profile. "
                      "Please check the log for details."))

        dlg = AdvancedOptionsDialog(self, self._advanced, on_save=_persist)
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
            QMessageBox.warning(self, tr("Error"), tr("Header and title are required fields."))
            return

        valid_pairs = [(s.strip(), d.strip()) for s, d in self.pairs if s.strip() and d.strip()]
        if not valid_pairs:
            QMessageBox.warning(self, tr("Error"), tr("At least one source and one destination path are required."))
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

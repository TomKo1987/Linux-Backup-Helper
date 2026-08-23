from pathlib import Path
from typing import Callable, TYPE_CHECKING

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QTextCursor, QKeyEvent
from PyQt6.QtWidgets import (
    QAbstractButton, QApplication, QCheckBox, QDialog, QDialogButtonBox, QFileDialog, QFrame,
    QHBoxLayout, QLabel, QLayout, QLineEdit, QMessageBox, QPushButton,
    QScrollArea, QVBoxLayout, QWidget, QPlainTextEdit
)

from state import _HOME, _PROFILE_RE
from translations import tr

if TYPE_CHECKING:
    _MixinBase = QWidget
else:
    _MixinBase = object


def fit_button_width(btn: QAbstractButton, h_pad: int = 28, min_floor: int = 0) -> None:
    needed = btn.fontMetrics().horizontalAdvance(btn.text()) + h_pad
    btn.setMinimumWidth(max(needed, min_floor))


def ask_yes_no(
    parent,
    title: str,
    text: str,
    *,
    cancel: bool = False,
    default_yes: bool = True,
    icon: "QMessageBox.Icon" = QMessageBox.Icon.Question,
) -> int:

    box = QMessageBox(parent)
    box.setIcon(icon)
    box.setWindowTitle(title)
    box.setText(text)
    buttons = QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
    if cancel:
        buttons |= QMessageBox.StandardButton.Cancel
    box.setStandardButtons(buttons)
    yes_btn = box.button(QMessageBox.StandardButton.Yes)
    no_btn = box.button(QMessageBox.StandardButton.No)
    if yes_btn:
        yes_btn.setText(tr("Yes"))
        fit_button_width(yes_btn)
    if no_btn:
        no_btn.setText(tr("No"))
        fit_button_width(no_btn)
    if cancel:
        cancel_btn = box.button(QMessageBox.StandardButton.Cancel)
        if cancel_btn:
            cancel_btn.setText(tr("Cancel"))
            fit_button_width(cancel_btn)
    box.setDefaultButton(
        QMessageBox.StandardButton.Yes if default_yes else QMessageBox.StandardButton.No
    )
    return box.exec()


def size_to_screen(
    widget: QWidget,
    max_w: int,
    max_h: int,
    *,
    fraction: float = 0.85,
    fallback_w: int = 1200,
    fallback_h: int = 700,
) -> None:

    screen = QApplication.primaryScreen()
    geo = screen.availableGeometry() if screen else None
    if geo:
        widget.setMinimumSize(
            min(max_w, int(geo.width() * fraction)),
            min(max_h, int(geo.height() * fraction)),
        )
    else:
        widget.setMinimumSize(fallback_w, fallback_h)


def build_dialog_shell(
    dialog: QDialog,
    theme: dict,
    font_sz_fn,
    title: str,
    icon: str = "",
    *,
    header_extra: list[QWidget] | None = None,
    footer_extra: list[QWidget] | None = None,
    close_text: str | None = None,
) -> tuple[QVBoxLayout, QVBoxLayout, QPushButton]:

    if close_text is None:
        close_text = f"\u2715 {tr('Close')}"
    t = theme
    lay = QVBoxLayout(dialog)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(0)

    hdr = QFrame()
    hdr.setStyleSheet(header_bar_style(t['bg2'], t['header_sep']))
    hl = QHBoxLayout(hdr)
    hl.setContentsMargins(16, 10, 16, 10)
    title_lbl = QLabel(f"{icon}  {title}" if icon else title)
    title_lbl.setStyleSheet(
        f"font-size:{font_sz_fn(4)}px;font-weight:bold;color:{t['accent']};"
        f"background:transparent;border:none;"
    )
    hl.addWidget(title_lbl)
    hl.addStretch()
    for w in header_extra or ():
        hl.addWidget(w)
    lay.addWidget(hdr)

    body = QWidget()
    body.setStyleSheet(f"background:{t['bg']};")
    body_lay = QVBoxLayout(body)
    body_lay.setContentsMargins(16, 16, 16, 16)
    body_lay.setSpacing(10)

    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(QFrame.Shape.NoFrame)
    scroll.setWidget(body)
    lay.addWidget(scroll, 1)

    ftr = QFrame()
    ftr.setStyleSheet(footer_bar_style(t['bg2'], t['header_sep']))
    fl = QHBoxLayout(ftr)
    fl.setContentsMargins(12, 8, 12, 8)
    for w in footer_extra or ():
        fl.addWidget(w)
    fl.addStretch()
    close_btn = QPushButton(close_text)
    close_btn.setFixedHeight(34)
    fit_button_width(close_btn)
    close_btn.clicked.connect(dialog.accept)
    fl.addWidget(close_btn)
    lay.addWidget(ftr)

    return lay, body_lay, close_btn


def clear_layout(layout: QLayout) -> None:
    while layout.count():
        item = layout.takeAt(0)
        w = item.widget() if item else None
        if w:
            w.deleteLater()


def block_set(cb: QCheckBox, checked: bool) -> None:
    cb.blockSignals(True)
    cb.setChecked(checked)
    cb.blockSignals(False)


def sep() -> QFrame:
    from themes import current_theme
    line = QFrame()
    line.setFrameShape(QFrame.Shape.HLine)
    line.setStyleSheet(f"color:{current_theme()['header_sep']};margin:4px 0;")
    return line


def hdr_label(text: str, color: str = "", size: int | None = None) -> QLabel:
    from themes import current_theme, font_sz
    lbl = QLabel(text)
    sz  = size if size is not None else font_sz(3)
    lbl.setStyleSheet(f"font-size:{sz}px;font-weight:bold;"
                      f"color:{color or current_theme()['accent']};padding:4px 0;")
    lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
    return lbl


def color_style(color: str, size: int) -> str:
    return f"color:{color};font-size:{size}px;"


def header_bar_style(bg: str, _sep: str) -> str:
    return f"background:{bg};border-bottom:1px solid {_sep};"


def footer_bar_style(bg: str, _sep: str) -> str:
    return f"background:{bg};border-top:1px solid {_sep};"


def card_frame_style(bg: str, radius: int = 8) -> str:
    return f"QFrame{{background:{bg}; border-radius:{radius}px;}}"


def checkbox_row_frame_style(bg: str, radius: int = 4) -> str:
    return f"QFrame{{background-color:{bg};border-radius:{radius}px;}}"


def ok_cancel_buttons(dialog: QDialog, ok_fn, ok_label: str | None = None, cancel_label: str | None = None, cancel_fn=None) -> QDialogButtonBox:
    if ok_label is None:
        ok_label = tr("Save")
    if cancel_label is None:
        cancel_label = tr("Cancel")
    bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel) # type: ignore
    ok_btn     = bb.button(QDialogButtonBox.StandardButton.Ok)
    cancel_btn = bb.button(QDialogButtonBox.StandardButton.Cancel)
    if ok_btn:
        ok_btn.setText(ok_label)
        fit_button_width(ok_btn)
    if cancel_btn:
        cancel_btn.setText(cancel_label)
        fit_button_width(cancel_btn)
    bb.accepted.connect(ok_fn)
    bb.rejected.connect(cancel_fn if cancel_fn else dialog.reject)
    return bb


def btn_row(buttons: list[tuple[str, Callable]]) -> QHBoxLayout:
    row = QHBoxLayout()
    for label, fn in buttons:
        b = QPushButton(label)
        fit_button_width(b)
        b.clicked.connect(fn)
        row.addWidget(b)
    return row


def do_browse(parent: QWidget, editor, mode: str, home: Path = _HOME) -> None:
    path = (QFileDialog.getExistingDirectory(parent, tr("Select directory"), str(home))
            if mode == "dir" else QFileDialog.getOpenFileName(parent, tr("Select file"), str(home))[0])
    if not path:
        return
    if hasattr(editor, "setPlainText"):
        editor.setPlainText(path)
        cur = editor.textCursor()
        cur.movePosition(QTextCursor.MoveOperation.End)
        editor.setTextCursor(cur)
    else:
        editor.setText(path)


def browse_field(parent: QWidget, editor: QLineEdit | QPlainTextEdit, btn_height: int = 36) -> QWidget:
    row  = QWidget()
    hlay = QHBoxLayout(row)
    hlay.setContentsMargins(0, 0, 0, 0)
    hlay.setSpacing(6)
    hlay.addWidget(editor)
    for lbl, mode in [(f"📄 {tr('File')}", "file"), (f"📁 {tr('Directory')}", "dir")]:
        b = QPushButton(lbl)
        b.setMinimumHeight(btn_height)
        fit_button_width(b, min_floor=70)
        b.clicked.connect(lambda _c=False, _e=editor, _m=mode: do_browse(parent, _e, _m))
        hlay.addWidget(b)
    return row


def ask_text(parent, title: str, label: str, default: str = "", min_width: int = 440) -> tuple[str, bool]:
    dlg = QDialog(parent)
    dlg.setWindowTitle(title)
    dlg.setMinimumWidth(min_width)
    layout = QVBoxLayout(dlg)
    layout.setSpacing(10)
    layout.setContentsMargins(16, 16, 16, 16)
    layout.addWidget(QLabel(label))
    edit = QLineEdit(default)
    edit.selectAll()
    layout.addWidget(edit)
    bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel) # type: ignore
    ok_btn = bb.button(QDialogButtonBox.StandardButton.Ok)
    cancel_btn = bb.button(QDialogButtonBox.StandardButton.Cancel)
    if ok_btn:
        ok_btn.setText(tr("Save"))
        fit_button_width(ok_btn)
    if cancel_btn:
        cancel_btn.setText(tr("Cancel"))
        fit_button_width(cancel_btn)
    bb.accepted.connect(dlg.accept)
    bb.rejected.connect(dlg.reject)
    layout.addWidget(bb)
    edit.setFocus()
    accepted = dlg.exec() == QDialog.DialogCode.Accepted
    return edit.text(), accepted


def ask_profile_name(title: str, default: str, parent=None) -> str | None:
    while True:
        name, ok = ask_text(parent, title, tr("Profile name:"), default=default)
        if not ok:
            return None
        name = name.strip()
        if not name:
            QMessageBox.warning(parent, tr("Invalid Name"), tr("Name must not be empty."))
            continue
        if not _PROFILE_RE.match(name):
            QMessageBox.warning(parent, tr("Invalid Name"),
                                tr("Only letters, digits, spaces, hyphens, underscores and dots are allowed."))
            continue
        return name


class _StandardKeysMixin(_MixinBase):
    def _handle_return(self, widget) -> bool:
        if isinstance(widget, QPushButton):
            widget.click()
            return True
        return False

    def keyPressEvent(self, a0: QKeyEvent | None) -> None:
        if a0 is None:
            return
        k = a0.key()
        if k in (Qt.Key.Key_Enter, Qt.Key.Key_Return):
            if not self._handle_return(self.focusWidget()):
                super().keyPressEvent(a0)
        elif k == Qt.Key.Key_Escape:
            self.close()
        else:
            super().keyPressEvent(a0)

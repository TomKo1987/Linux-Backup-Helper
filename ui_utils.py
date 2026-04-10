from pathlib import Path
from typing import Optional, TYPE_CHECKING

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QTextCursor
from PyQt6.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QFileDialog, QFrame,
    QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton,
    QVBoxLayout, QWidget, QPlainTextEdit
)

from state import _HOME, _PROFILE_RE

if TYPE_CHECKING:
    from PyQt6.QtWidgets import QWidget as _QWidgetBase
    _MixinBase = _QWidgetBase
else:
    _MixinBase = object


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


def hdr_label(text: str, color: str = "", size: Optional[int] = None) -> QLabel:
    from themes import current_theme, font_sz
    lbl = QLabel(text)
    sz  = size if size is not None else font_sz(3)
    lbl.setStyleSheet(f"font-size:{sz}px;font-weight:bold;"
                      f"color:{color or current_theme()['accent']};padding:4px 0;")
    lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
    return lbl


def ok_cancel_buttons(dialog: QDialog, ok_fn, ok_label: str = "Save", cancel_label: str = "Cancel", cancel_fn=None) -> QDialogButtonBox:
    bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel) # type: ignore
    ok_btn     = bb.button(QDialogButtonBox.StandardButton.Ok)
    cancel_btn = bb.button(QDialogButtonBox.StandardButton.Cancel)
    if ok_btn:
        ok_btn.setText(ok_label)
    if cancel_btn:
        cancel_btn.setText(cancel_label)
    bb.accepted.connect(ok_fn)
    bb.rejected.connect(cancel_fn if cancel_fn else dialog.reject)
    return bb


def btn_row(buttons: list[tuple[str, object]]) -> QHBoxLayout:
    row = QHBoxLayout()
    for label, fn in buttons:
        b = QPushButton(label)
        b.clicked.connect(fn)
        row.addWidget(b)
    return row


def do_browse(parent: QWidget, editor, mode: str, home: Path = _HOME) -> None:
    path = (QFileDialog.getExistingDirectory(parent, "Select directory", str(home))
            if mode == "dir" else QFileDialog.getOpenFileName(parent, "Select file", str(home))[0])
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
    for lbl, mode in [("📄 File", "file"), ("📁 Directory", "dir")]:
        b = QPushButton(lbl)
        b.setMinimumHeight(btn_height)
        b.setMinimumWidth(70)
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
    bb.accepted.connect(dlg.accept)
    bb.rejected.connect(dlg.reject)
    layout.addWidget(bb)
    edit.setFocus()
    accepted = dlg.exec() == QDialog.DialogCode.Accepted
    return edit.text(), accepted


def ask_profile_name(title: str, default: str, parent=None) -> Optional[str]:
    while True:
        name, ok = ask_text(parent, title, "Profile name:", default=default)
        if not ok:
            return None
        name = name.strip()
        if not name:
            QMessageBox.warning(parent, "Invalid Name", "Name must not be empty.")
            continue
        if not _PROFILE_RE.match(name):
            QMessageBox.warning(parent, "Invalid Name",
                                "Only letters, digits, spaces, hyphens, underscores and dots are allowed.")
            continue
        return name


class _StandardKeysMixin(_MixinBase):
    def keyPressEvent(self, event) -> None:
        k = event.key()
        if k in (Qt.Key.Key_Enter, Qt.Key.Key_Return):
            widget = self.focusWidget()
            if isinstance(widget, QPushButton):
                widget.click()
        elif k == Qt.Key.Key_Escape:
            self.close()
        else:
            super().keyPressEvent(event)

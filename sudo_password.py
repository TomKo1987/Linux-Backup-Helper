from typing import Optional

from PyQt6.QtCore import pyqtSignal, Qt
from PyQt6.QtWidgets import QDialog, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton, QVBoxLayout

__all__ = ["SudoPasswordDialog", "SecureString"]

_NOTE_NORMAL  = "Note: Only one authentication attempt will be made."
_NOTE_WARNING = "⚠  Third attempt!<br>Your password may be temporarily blocked if entered incorrectly again."


class SudoPasswordDialog(QDialog):
    sudo_password_entered = pyqtSignal(object)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Sudo Authentication")
        self.failed_attempts = 0
        self._build_ui()

    def _build_ui(self) -> None:
        from themes import current_theme
        t      = current_theme()
        layout = QVBoxLayout(self)

        intro = QLabel("Enter your sudo password to run System Manager.\nIt will be used for all privileged commands during this session.")
        intro.setAlignment(Qt.AlignmentFlag.AlignCenter)
        intro.setWordWrap(False)
        layout.addWidget(intro)

        pw_row = QHBoxLayout()
        pw_row.addWidget(QLabel("Password:"))
        self._pw_input = QLineEdit()
        self._pw_input.setEchoMode(QLineEdit.EchoMode.Password)
        self._pw_input.setPlaceholderText("Enter your sudo password")
        pw_row.addWidget(self._pw_input)
        layout.addLayout(pw_row)

        self._note = QLabel(_NOTE_NORMAL)
        self._note.setTextFormat(Qt.TextFormat.RichText)
        self._note.setWordWrap(True)
        self._note.setStyleSheet(f"color:{t['muted']};font-style:italic;")
        self._note.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self._note)
        layout.addSpacing(10)

        btn_row   = QHBoxLayout()
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.reject)
        self.auth_btn = QPushButton("Authenticate")
        self.auth_btn.setDefault(True)
        self.auth_btn.clicked.connect(self._on_authenticate)
        btn_row.addWidget(close_btn)
        btn_row.addWidget(self.auth_btn)
        layout.addLayout(btn_row)

        self._pw_input.setFocus()

    def _on_authenticate(self) -> None:
        raw_password = self._pw_input.text()
        if not raw_password.strip():
            QMessageBox.warning(self, "Empty Password", "Please enter your sudo password or click Close.")
            self._pw_input.setFocus()
            return

        secure_pw = SecureString(raw_password)
        self._pw_input.clear()
        self.sudo_password_entered.emit(secure_pw)
        self.accept()

    def update_failed_attempts(self, count: int) -> None:
        from themes import current_theme
        t = current_theme()
        self.failed_attempts = count

        if count >= 2:
            self._note.setText(_NOTE_WARNING)
            self._note.setStyleSheet(f"color:{t['error']};font-style:italic;font-weight:bold;")
        else:
            text = _NOTE_NORMAL + (f"<br>Failed attempts: {count}" if count > 0 else "")
            self._note.setText(text)
            self._note.setStyleSheet(f"color:{t['muted']};font-style:italic;")

        self._pw_input.setFocus()
        self.adjustSize()


class SecureString:
    __slots__ = ("_buf",)

    def __init__(self, value: Optional[str] = None) -> None: self._buf = bytearray(value.encode("utf-8")) if value else bytearray()

    def get(self) -> str: return self._buf.decode("utf-8") if self._buf else ""

    def get_bytes(self) -> bytearray: return bytearray(self._buf)

    def clear(self) -> None:
        if self._buf:
            for i in range(len(self._buf)):
                self._buf[i] = 0
            self._buf = bytearray()

    def __bool__(self) -> bool: return len(self._buf) > 0

    def __len__(self) -> int: return len(self._buf)

    def __repr__(self) -> str: return "SecureString(<secured>)"

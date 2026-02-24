from typing import Optional
from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (QDialog, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton, QVBoxLayout)

MAX_ATTEMPTS = 3


class SudoPasswordDialog(QDialog):

    sudo_password_entered = pyqtSignal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Sudo Authentication")
        self.failed_attempts = 0
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        intro = QLabel(
            "Enter your sudo password to run Package Installer.\n"
            "It will be used for all privileged commands during this session."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        pw_row = QHBoxLayout()
        pw_row.addWidget(QLabel("Password:"))
        self._pw_input = QLineEdit()
        self._pw_input.setEchoMode(QLineEdit.EchoMode.Password)
        self._pw_input.setPlaceholderText("Enter your sudo password")
        pw_row.addWidget(self._pw_input)
        layout.addLayout(pw_row)

        self._note_label = QLabel("Note: Only one authentication attempt will be made.")
        self._note_label.setStyleSheet("color: #666; font-style: italic;")
        layout.addWidget(self._note_label)
        layout.addSpacing(10)

        btn_row = QHBoxLayout()
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.reject)
        btn_row.addWidget(close_btn)

        auth_btn = QPushButton("Authenticate")
        auth_btn.setDefault(True)
        auth_btn.clicked.connect(self._on_authenticate)
        btn_row.addWidget(auth_btn)
        layout.addLayout(btn_row)

        self._pw_input.setFocus()
        self._pw_input.returnPressed.connect(auth_btn.click)

    def _on_authenticate(self) -> None:
        password = self._pw_input.text()
        if not password:
            QMessageBox.warning(self, "Empty Password",
                                "Please enter your sudo password or click Close.")
            return
        self.sudo_password_entered.emit(password)
        self._pw_input.clear()
        self.accept()

    def update_failed_attempts(self, count: int) -> None:
        self.failed_attempts = count
        if count >= MAX_ATTEMPTS:
            msg = (
                "⚠  Third attempt!\n"
                "Your password may be temporarily blocked if entered incorrectly again."
            )
            self._note_label.setStyleSheet("color: red; font-style: italic; font-weight: bold;")
        else:
            msg = "Note: Only one authentication attempt will be made."
            if count:
                msg += f"\nFailed attempts: {count}"
            self._note_label.setStyleSheet("color: #666; font-style: italic;")
        self._note_label.setText(msg)
        self.adjustSize()


class SecureString:    

    __slots__ = ("_buf",)

    def __init__(self, value: Optional[str] = None) -> None:
        self._buf = bytearray(value.encode("utf-8")) if value else bytearray()

    def get(self) -> str:
        return self._buf.decode("utf-8") if self._buf else ""

    get_value = get

    def clear(self) -> None:
        if self._buf:
            memoryview(self._buf)[:] = bytes(len(self._buf))
            self._buf.clear()

    def __bool__(self) -> bool:
        return bool(self._buf)

    def __repr__(self) -> str:
        return f"SecureString(len={len(self._buf)})"

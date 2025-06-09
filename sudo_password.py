import secrets
from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import QDialog, QLabel, QLineEdit, QPushButton, QVBoxLayout, QHBoxLayout, QMessageBox


# noinspection PyUnresolvedReferences
class SudoPasswordDialog(QDialog):
    sudo_password_entered = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Sudo-Authentication")
        self.layout = QVBoxLayout(self)
        self.failed_attempts = 0
        self.label = QLabel("Please enter your sudo password to run Package Installer.\nThis will be used for all sudo commands during execution.")
        self.label.setWordWrap(True)
        self.layout.addWidget(self.label)
        self.password_layout = QHBoxLayout()
        self.password_label = QLabel("Password:")
        self.password_layout.addWidget(self.password_label)
        self.password_input = QLineEdit(self)
        self.password_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.password_input.setPlaceholderText("Enter your sudo password")
        self.password_layout.addWidget(self.password_input)
        self.layout.addLayout(self.password_layout)
        self.info_label = QLabel("Note: For security, only one authentication attempt will be made.")
        self.info_label.setStyleSheet("color: #666; font-style: italic;")
        self.layout.addWidget(self.info_label)
        self.layout.addSpacing(10)
        self.button_layout = QHBoxLayout()
        self.close_button = QPushButton("Close", self)
        self.close_button.clicked.connect(self.reject)
        self.button_layout.addWidget(self.close_button)
        self.ok_button = QPushButton("Authenticate", self)
        self.ok_button.clicked.connect(self.on_ok_clicked)
        self.ok_button.setDefault(True)
        self.button_layout.addWidget(self.ok_button)
        self.layout.addLayout(self.button_layout)
        self.password_input.setFocus()
        self.password_input.returnPressed.connect(self.ok_button.click)

    def on_ok_clicked(self):
        sudo_password = self.password_input.text()
        if sudo_password:
            self.sudo_password_entered.emit(sudo_password)
            self.password_input.clear()
            self.accept()
        else:
            QMessageBox.warning(self, "Empty Password", "Please enter your sudo password or click Close.")

    def update_failed_attempts(self, failed_attempts):
        self.failed_attempts = failed_attempts
        if self.failed_attempts == 2:
            msg = "Attention! Third attempt!\nPassword could be blocked temporarily if entered incorrectly."
            self.info_label.setStyleSheet("color: red; font-style: italic; font-weight: bold;")
        else:
            msg = f"Note: For security, only one authentication attempt will be made.\nFailed attempts: {self.failed_attempts}"
            self.info_label.setStyleSheet("color: #666; font-style: italic; font-weight: normal;")
        self.info_label.setText(msg)
        self.adjustSize()


class SecureString:
    def __init__(self, initial_value=None):
        self._value = bytearray(initial_value.encode('utf-8')) if initial_value else bytearray()

    def get_value(self):
        return self._value.decode('utf-8') if self._value else ''

    def clear(self):
        if self._value:
            try:
                for i in range(len(self._value)):
                    self._value[i] = secrets.randbelow(256)
            finally:
                del self._value[:]
                self._value = bytearray()

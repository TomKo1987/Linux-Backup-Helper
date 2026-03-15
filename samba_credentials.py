from typing import Optional, Tuple
import getpass, json, os, subprocess, keyring, keyring.errors, pwd, threading
from keyring.backends import SecretService

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QLineEdit, QMessageBox, QPushButton, QVBoxLayout,
    QCheckBox, QDialog, QErrorMessage, QHBoxLayout, QLabel
)

from state import logger
from themes import current_theme

__all__ = ["SambaPasswordManager", "SambaPasswordDialog"]

_KWALLET_TIMEOUT  = 2
_KWALLET_WALLET   = "kdewallet"
_KEYRING_SERVICE  = "backup-helper-samba"

_KEYRING_INITIALIZED = False
_KEYRING_LOCK = threading.Lock()

def _init_keyring() -> None:
    global _KEYRING_INITIALIZED
    if _KEYRING_INITIALIZED:
        return
    with _KEYRING_LOCK:
        if _KEYRING_INITIALIZED:
            return
        try:
            keyring.set_keyring(SecretService.Keyring())
            _KEYRING_INITIALIZED = True
        except Exception as exc:
            logger.debug("Could not set SecretService keyring backend: %s", exc)


def _current_username() -> str:
    for fn in (os.getlogin, getpass.getuser):
        try:
            return fn()
        except OSError:
            pass
    try:
        return pwd.getpwuid(os.getuid()).pw_name
    except OSError:
        return "user"


class SambaPasswordDialog(QDialog):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._username_field = None
        self._password_field = None
        self.setWindowTitle("Samba Credentials")
        self.setMinimumSize(750, 350)

        self._manager      = SambaPasswordManager()
        self._error_dialog = QErrorMessage(self)

        username, password, from_kwallet = self._manager.get_credentials()
        self._build_ui(username or _current_username(), password, bool(password), from_kwallet)

    def _build_ui(self, username: str, password: Optional[str],
                  has_credentials: bool, from_kwallet: bool) -> None:
        layout = QVBoxLayout(self)
        t = current_theme()

        if has_credentials:
            origin = "KWallet" if from_kwallet else "system keyring"
            banner = QLabel(f"Credentials are already stored.\n(Retrieved from {origin})")
            banner.setStyleSheet(f"color:{t['success']}; font-weight:bold;")
            banner.setAlignment(Qt.AlignmentFlag.AlignCenter)
            layout.addWidget(banner)

        layout.addStretch()

        for label_text, attr, placeholder, echo, prefill in (("Username:", "_username_field", "", QLineEdit.EchoMode.Normal, username),
            ("Password:", "_password_field", "", QLineEdit.EchoMode.Password, password or "")):
            lbl = QLabel(label_text)
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            layout.addWidget(lbl)
            field = QLineEdit(prefill)
            field.setEchoMode(echo)
            field.setAlignment(Qt.AlignmentFlag.AlignCenter)
            setattr(self, attr, field)
            layout.addWidget(field)
            layout.addStretch()

        show_pw_cb = QCheckBox("Show password")
        show_pw_cb.setStyleSheet(f"color:{t['accent']};")
        show_pw_cb.toggled.connect(
            lambda checked: self._password_field.setEchoMode(QLineEdit.EchoMode.Normal if checked else QLineEdit.EchoMode.Password))
        layout.addWidget(show_pw_cb)

        btn_row = QHBoxLayout()
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.reject)
        btn_row.addWidget(close_btn)

        if has_credentials and not from_kwallet:
            del_btn = QPushButton("Delete Credentials")
            del_btn.setStyleSheet(f"border: 1px solid {t['error']}; color: {t['error']};")
            del_btn.clicked.connect(self._delete_credentials)
            btn_row.addWidget(del_btn)

        save_btn = QPushButton("Update Credentials" if has_credentials else "Save")
        save_btn.clicked.connect(self._save_credentials)
        btn_row.addWidget(save_btn)

        layout.addLayout(btn_row)

    def _save_credentials(self) -> None:
        username = self._username_field.text().strip()
        password = self._password_field.text()
        if not username or not password:
            QMessageBox.warning(self, "Input Error", "Username and password must not be empty.")
            return
        try:
            self._manager.save_credentials(username, password)
            QMessageBox.information(self, "Success", "Samba credentials saved successfully!")
            self.accept()
        except Exception as exc:
            self._error_dialog.showMessage(f"Failed to save credentials:\n{exc}")

    def _delete_credentials(self) -> None:
        username = self._username_field.text().strip()
        try:
            if self._manager.delete_credentials(username):
                QMessageBox.information(self, "Success", "Samba credentials deleted successfully!")
                self.accept()
            else:
                QMessageBox.warning(self, "Failed", "Could not delete credentials. They might not exist.")
        except Exception as exc:
            self._error_dialog.showMessage(f"Failed to delete credentials:\n{exc}")


class SambaPasswordManager:
    def __init__(self) -> None:
        self._cached_kwallet_entry: Optional[str] = None
        _init_keyring()

    @staticmethod
    def _run_kwallet(args: list[str], input_data: Optional[str] = None) -> Optional[str]:
        try:
            result = subprocess.run(
                ["kwallet-query"] + args,
                input=input_data,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=_KWALLET_TIMEOUT,
                check=False,
            )
            return result.stdout if result.returncode == 0 else None
        except FileNotFoundError:
            logger.debug("kwallet-query not found. KWallet integration disabled.")
            return None
        except Exception as exc:
            logger.debug("kwallet-query error: %s", exc)
            return None

    def _find_kwallet_entry(self) -> Optional[str]:
        if self._cached_kwallet_entry:
            return self._cached_kwallet_entry
        out = self._run_kwallet(["--list-entries", _KWALLET_WALLET])
        if out:
            for line in out.strip().splitlines():
                if line.startswith("smb-"):
                    self._cached_kwallet_entry = line
                    return line
        return None

    def _read_from_kwallet(self) -> Tuple[Optional[str], Optional[str]]:
        entry = self._find_kwallet_entry()
        if not entry:
            return None, None
        out = self._run_kwallet(["--read-password", entry, _KWALLET_WALLET])
        if not out:
            return None, None
        raw = out.strip()
        try:
            data = json.loads(raw)
            return data.get("login"), data.get("password")
        except json.JSONDecodeError:
            return _current_username(), raw

    def _write_to_kwallet(self, entry: str, username: str, password: str) -> None:
        payload = json.dumps({"login": username, "password": password})
        self._run_kwallet(["--write-password", entry, _KWALLET_WALLET], input_data=payload)
        logger.info("Updated Samba credentials in KWallet entry: %s", entry)

    def get_credentials(self) -> Tuple[Optional[str], Optional[str], bool]:
        username, password = self._read_from_kwallet()
        if password:
            logger.info("Retrieved Samba credentials from KWallet")
            return username, password, True
        try:
            user = _current_username()
            pw   = keyring.get_password(_KEYRING_SERVICE, user)
            if pw:
                logger.info("Retrieved Samba credentials from system keyring")
                return user, pw, False
        except Exception as exc:
            logger.exception("Failed to retrieve from system keyring: %s", exc)
        return None, None, False

    def save_credentials(self, username: str, password: str) -> None:
        kwallet_entry = self._find_kwallet_entry()
        if kwallet_entry:
            self._write_to_kwallet(kwallet_entry, username, password)
        else:
            try:
                keyring.set_password(_KEYRING_SERVICE, username, password)
                logger.info("Saved Samba credentials to system keyring.")
            except Exception as exc:
                logger.exception("Failed to save to system keyring: %s", exc)

    @staticmethod
    def delete_credentials(username: str) -> bool:
        try:
            keyring.delete_password(_KEYRING_SERVICE, username)
            logger.info("Deleted Samba credentials for '%s' from system keyring.", username)
            return True
        except keyring.errors.PasswordDeleteError:
            logger.warning("Credentials for '%s' not found in keyring.", username)
            return False
        except Exception as exc:
            logger.error("Error deleting from keyring: %s", exc)
            return False
import hmac
import json
import shutil
import subprocess
import threading
from functools import lru_cache
from typing import Optional

import keyring
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QCheckBox, QDialog, QErrorMessage, QHBoxLayout, QLabel,
    QLineEdit, QMessageBox, QPushButton, QVBoxLayout,
)
from keyring.errors import PasswordDeleteError, KeyringError

from state import logger, _USER
from sudo_password import SecureString
from themes import current_theme
from ui_utils import _StandardKeysMixin

__all__ = ["SambaPasswordManager", "SambaPasswordDialog"]

_KWALLET_TIMEOUT  = 2
_KWALLET_WALLET   = "kdewallet"
_KEYRING_SERVICE  = "backup-helper-samba"
_KEYRING_USER_KEY = f"{_USER}__samba_username"

_keyring_init_lock = threading.Lock()
_keyring_ready     = False


@lru_cache(maxsize=1)
def _kwallet_available() -> bool:
    return shutil.which("kwallet-query") is not None


def _init_keyring() -> None:
    global _keyring_ready
    if _keyring_ready:
        return
    with _keyring_init_lock:
        if _keyring_ready:
            return
        try:
            from keyring.backends import SecretService
            keyring.set_keyring(SecretService.Keyring())
        except Exception as exc:
            logger.debug("Could not set SecretService keyring backend: %s", exc)
        finally:
            _keyring_ready = True


class _VerifyPasswordDialog(QDialog):
    _MAX_ATTEMPTS = 3

    def __init__(self, parent, username: str, stored_pw: SecureString) -> None:
        super().__init__(parent)
        self.setWindowTitle("Samba — Verify Password")
        self.setMinimumWidth(550)
        self._stored_pw: Optional[SecureString] = stored_pw
        self._attempts  = 0

        t      = current_theme()
        layout = QVBoxLayout(self)

        info = QLabel(f"Samba credentials are stored for <b>{username}</b>.<br>Please enter your current Samba password to continue.")
        info.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(info)
        layout.addSpacing(8)

        self._pw_input = QLineEdit()
        self._pw_input.setEchoMode(QLineEdit.EchoMode.Password)
        self._pw_input.setPlaceholderText("Current Samba password")
        self._pw_input.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self._pw_input)

        self._err_lbl = QLabel()
        self._err_lbl.setStyleSheet(f"color:{t['error']};font-style:italic;")
        self._err_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self._err_lbl)
        layout.addSpacing(8)

        btn_row    = QHBoxLayout()
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        ok_btn = QPushButton("Verify")
        ok_btn.setDefault(True)
        ok_btn.clicked.connect(self._verify)
        btn_row.addWidget(cancel_btn)
        btn_row.addWidget(ok_btn)
        layout.addLayout(btn_row)

        self._pw_input.setFocus()

    def _verify(self) -> None:
        if self._stored_pw is None:
            self.reject()
            return
        entered_secure = SecureString(self._pw_input.text())
        self._pw_input.clear()
        temp_buf = self._stored_pw.get_bytes()
        entered_buf = entered_secure.get_bytes()
        try:
            matched = hmac.compare_digest(entered_buf, temp_buf)
        finally:
            for i in range(len(entered_buf)):
                entered_buf[i] = 0
            for i in range(len(temp_buf)):
                temp_buf[i] = 0
            entered_secure.clear()

        if matched:
            self._stored_pw.clear()
            self._stored_pw = None
            self.accept()
            return

        self._attempts += 1
        if self._attempts >= self._MAX_ATTEMPTS:
            self._stored_pw.clear()
            self._stored_pw = None
            QMessageBox.critical(self, "Access Denied", "Too many failed attempts.")
            self.reject()
            return

        remaining = self._MAX_ATTEMPTS - self._attempts
        self._err_lbl.setText(f"Incorrect password. Remaining attempts: {remaining}")
        self._pw_input.setFocus()

    def closeEvent(self, event) -> None:
        self._pw_input.clear()
        if self._stored_pw is not None:
            self._stored_pw.clear()
            self._stored_pw = None
        super().closeEvent(event)


class SambaPasswordManager:

    def __init__(self) -> None:
        self._cached_kwallet_entry: Optional[str] = None
        _init_keyring()

    @staticmethod
    def _run_kwallet(args: list[str], input_data: Optional[str] = None) -> Optional[str]:
        if not _kwallet_available():
            return None
        try:
            result = subprocess.run(
                ["kwallet-query"] + args, input=input_data, capture_output=True, text=True, timeout=_KWALLET_TIMEOUT, check=False)

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

    def _read_from_kwallet(self) -> tuple[Optional[str], Optional[str]]:
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
            logger.warning("KWallet entry is not JSON — treating raw value as password (legacy format)")
            return _USER, raw

    def _write_to_kwallet(self, entry: str, username: str, password: str) -> None:
        payload = json.dumps({"login": username, "password": password})
        result = self._run_kwallet(["--write-password", entry, _KWALLET_WALLET], input_data=payload)
        del payload
        if result is None:
            self._cached_kwallet_entry = None
            raise RuntimeError("Failed to write credentials to KWallet")
        logger.info("Updated Samba credentials in KWallet entry: %s", entry)

    def get_credentials(self) -> tuple[Optional[str], Optional[SecureString], bool]:
        username, password = self._read_from_kwallet()
        if password:
            logger.info("Retrieved Samba credentials from KWallet")
            secure = SecureString(password)
            del password
            return (username or _USER), secure, True
        try:
            stored_user = keyring.get_password(_KEYRING_SERVICE, _KEYRING_USER_KEY) or _USER
            pw = keyring.get_password(_KEYRING_SERVICE, stored_user)
            if pw:
                secure = SecureString(pw)
                del pw
                return stored_user, secure, False
        except Exception as exc:
            logger.exception("Failed to retrieve from system keyring: %s", exc)
        return None, None, False

    def save_credentials(self, username: str, password: str) -> None:
        target = "kwallet" if self._find_kwallet_entry() else "keyring"
        self.save_credentials_to(username, password, target)

    def save_credentials_to(self, username: str, password: str, target: str) -> None:
        if target == "kwallet":
            entry = self._find_kwallet_entry() or f"smb-{username}"
            self._write_to_kwallet(entry, username, password)
        else:
            try:
                keyring.set_password(_KEYRING_SERVICE, username, password)
                keyring.set_password(_KEYRING_SERVICE, _KEYRING_USER_KEY, username)
                logger.info("Saved Samba credentials to system keyring for user '%s'.", username)
            except Exception as exc:
                logger.exception("Failed to save to system keyring: %s", exc)
                raise

    def delete_credentials(self, username: str) -> bool:
        self._cached_kwallet_entry = None
        try:
            keyring.delete_password(_KEYRING_SERVICE, username)
            try:
                keyring.delete_password(_KEYRING_SERVICE, _KEYRING_USER_KEY)
            except (PasswordDeleteError, KeyringError):
                pass
            logger.info("Deleted Samba credentials for '%s' from system keyring.", username)
            return True
        except PasswordDeleteError:
            logger.warning("Credentials for '%s' not found in keyring.", username)
            return False
        except Exception as exc:
            logger.error("Error deleting from keyring: %s", exc)
            return False


# noinspection PyUnresolvedReferences
class SambaPasswordDialog(_StandardKeysMixin, QDialog):

    @classmethod
    def open(cls, parent=None) -> None:
        manager                      = SambaPasswordManager()
        username, stored_pw, from_kw = manager.get_credentials()
        if stored_pw:
            accepted = (_VerifyPasswordDialog(parent, username or "", stored_pw).exec() == QDialog.DialogCode.Accepted)
            if not accepted:
                return
            cls(parent, manager, username or "", from_kw, has_credentials=True).exec()
        else:
            has_kw = _kwallet_available()
            msg = ("No Samba credentials are stored yet.\nWould you like to set up credentials now? "
                   "You can choose to store them in KWallet or the system keyring." if has_kw else
                   "No Samba credentials are stored yet.\n\n"
                   "Would you like to store credentials in the system keyring?")
            ans = QMessageBox.question(
                parent, "Samba Credentials", msg, QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if ans != QMessageBox.StandardButton.Yes:
                return
            cls(parent, manager, _USER, False, first_setup=True, kwallet_available=has_kw).exec()

    def __init__(self, parent=None, manager: Optional[SambaPasswordManager] = None, username: str = "",
                 from_kwallet: bool = False, *, has_credentials: bool = False, first_setup: bool = False,
                 kwallet_available: bool = False) -> None:

        super().__init__(parent)
        self._username_field: Optional[QLineEdit] = None
        self._password_field: Optional[QLineEdit] = None
        self._confirm_password_field: Optional[QLineEdit] = None
        self._store_in_kwallet: Optional[QCheckBox] = None
        self.setWindowTitle("Samba Credentials")
        self.setMinimumSize(750, 400)

        self._manager = manager or SambaPasswordManager()
        self._error_dialog = QErrorMessage(self)
        self._first_setup = first_setup
        self._kwallet_avail = kwallet_available
        self._from_kwallet = from_kwallet
        self._has_credentials = has_credentials

        self._build_ui(username, from_kwallet)

    def _build_ui(self, username: str, from_kwallet: bool) -> None:
        layout = QVBoxLayout(self)
        t = current_theme()

        if self._has_credentials:
            origin = "KWallet" if from_kwallet else "system keyring"
            banner = QLabel(f"Samba credentials have already been defined.\nYou can edit your password below.\n"
                            f"(Stored in {origin})")
            banner.setStyleSheet(f"color:{t['success']};font-weight:bold;")
            banner.setAlignment(Qt.AlignmentFlag.AlignCenter)
            layout.addWidget(banner)

        layout.addStretch()

        for label_text, attr, echo, prefill in (("Username:", "_username_field", QLineEdit.EchoMode.Normal, username),
                                                ("Password:", "_password_field", QLineEdit.EchoMode.Password, ""),
                                                ("Confirm Password:", "_confirm_password_field", QLineEdit.EchoMode.Password, "")):
            lbl = QLabel(label_text)
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            layout.addWidget(lbl)

            field = QLineEdit(prefill)
            field.setEchoMode(echo)
            field.setAlignment(Qt.AlignmentFlag.AlignCenter)
            setattr(self, attr, field)
            layout.addWidget(field)

        self._username_field.returnPressed.connect(self._save_credentials)
        self._password_field.returnPressed.connect(self._save_credentials)
        self._confirm_password_field.returnPressed.connect(self._save_credentials)

        show_pw_cb = QCheckBox("Show password")
        show_pw_cb.setStyleSheet(f"color:{t['accent']};")

        def _toggle_echo(checked: bool) -> None:
            mode = QLineEdit.EchoMode.Normal if checked else QLineEdit.EchoMode.Password
            self._password_field.setEchoMode(mode)
            self._confirm_password_field.setEchoMode(mode)

        show_pw_cb.toggled.connect(_toggle_echo)
        layout.addStretch()
        layout.addWidget(show_pw_cb)
        layout.addStretch()

        if self._first_setup and self._kwallet_avail:
            self._store_in_kwallet = QCheckBox("Store in KWallet (recommended)")
            self._store_in_kwallet.setChecked(True)
            self._store_in_kwallet.setStyleSheet(f"color:{t['accent']};")
            layout.addWidget(self._store_in_kwallet)

        btn_row = QHBoxLayout()
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.reject)
        btn_row.addWidget(close_btn)

        if self._has_credentials and not from_kwallet:
            del_btn = QPushButton("Delete Credentials")
            del_btn.setStyleSheet(f"border:1px solid {t['error']};color:{t['error']};")
            del_btn.clicked.connect(self._delete_credentials)
            btn_row.addWidget(del_btn)

        save_btn = QPushButton("Update Credentials" if self._has_credentials else "Save")
        save_btn.clicked.connect(self._save_credentials)
        save_btn.setDefault(True)
        btn_row.addWidget(save_btn)

        layout.addLayout(btn_row)
        self._password_field.setFocus()

    def _save_credentials(self) -> None:
        if self._username_field is None or self._password_field is None or self._confirm_password_field is None:
            return
        username = self._username_field.text().strip()
        pw_secure = SecureString(self._password_field.text())
        cf_secure = SecureString(self._confirm_password_field.text())
        self._password_field.clear()
        self._confirm_password_field.clear()

        if not username or not pw_secure:
            pw_secure.clear()
            cf_secure.clear()
            QMessageBox.warning(self, "Input Error", "Username and password must not be empty.")
            return

        if not hmac.compare_digest(pw_secure.get_bytes(), cf_secure.get_bytes()):
            cf_secure.clear()
            pw_secure.clear()
            QMessageBox.warning(self, "Input Error", "Passwords do not match. Please try again.")
            self._password_field.setFocus()
            return

        cf_secure.clear()
        pw_buf = pw_secure.get_bytes()
        pw_secure.clear()
        try:
            pw_str = pw_buf.decode("utf-8")
            try:
                if self._first_setup and self._store_in_kwallet is not None:
                    target = "kwallet" if self._store_in_kwallet.isChecked() else "keyring"
                    self._manager.save_credentials_to(username, pw_str, target)
                else:
                    self._manager.save_credentials(username, pw_str)
            finally:
                del pw_str
            QMessageBox.information(self, "Success", "Samba credentials successfully saved!")
            self.accept()
        except Exception as exc:
            self._error_dialog.showMessage(f"Failed to save credentials:\n{exc}")
        finally:
            for i in range(len(pw_buf)):
                pw_buf[i] = 0

    def _delete_credentials(self) -> None:
        username = self._username_field.text().strip()
        try:
            if self._manager.delete_credentials(username):
                QMessageBox.information(self, "Success", "Samba credentials successfully deleted!")
                self.accept()
            else:
                QMessageBox.warning(self, "Failed", "Could not delete credentials. They might not exist.")
        except Exception as exc:
            self._error_dialog.showMessage(f"Failed to delete credentials:\n{exc}")

    def _cleanup(self) -> None:
        self._pw_input.clear()
        if self._stored_pw is not None:
            self._stored_pw.clear()
            self._stored_pw = None

    def reject(self) -> None:
        self._cleanup()
        super().reject()

    def closeEvent(self, event) -> None:
        self._cleanup()
        super().closeEvent(event)

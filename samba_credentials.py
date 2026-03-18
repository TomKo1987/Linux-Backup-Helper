from typing import Optional
from keyring.backends import SecretService
import getpass, hmac, json, os, shutil, subprocess, keyring, keyring.errors, pwd, threading

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QCheckBox, QDialog, QErrorMessage, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton, QVBoxLayout

from state import logger
from themes import current_theme

__all__ = ["SambaPasswordManager", "SambaPasswordDialog"]

_KWALLET_TIMEOUT = 2
_KWALLET_WALLET  = "kdewallet"
_KEYRING_SERVICE = "backup-helper-samba"

_KEYRING_INITIALIZED = False
_KEYRING_LOCK        = threading.Lock()


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
    for fn in (getpass.getuser, os.getlogin):
        try:
            return fn()
        except OSError:
            pass
    try:
        return pwd.getpwuid(os.getuid()).pw_name
    except OSError:
        return "user"


def _kwallet_available() -> bool:
    return shutil.which("kwallet-query") is not None


class _VerifyPasswordDialog(QDialog):
    _MAX_ATTEMPTS = 3

    def __init__(self, parent, username: str, stored_pw: str) -> None:
        super().__init__(parent)
        self.setWindowTitle("Samba — Verify Password")
        self.setMinimumWidth(550)
        self._stored_pw = stored_pw
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
        entered = self._pw_input.text()
        self._pw_input.clear()
        if hmac.compare_digest(entered, self._stored_pw):
            self._stored_pw = ""
            self.accept()
            return

        self._attempts += 1

        if self._attempts >= self._MAX_ATTEMPTS:
            QMessageBox.critical(self, "Access Denied", "Too many failed attempts.")
            self._stored_pw = ""
            self.reject()
            return

        self._err_lbl.setText(f"Incorrect password. Attempt {self._attempts}/{self._MAX_ATTEMPTS}.")
        self._pw_input.setFocus()


# noinspection PyUnresolvedReferences
class SambaPasswordDialog(QDialog):

    @classmethod
    def open(cls, parent=None) -> None:
        manager                      = SambaPasswordManager()
        username, stored_pw, from_kw = manager.get_credentials()

        if stored_pw:
            if _VerifyPasswordDialog(parent, username, stored_pw).exec() != QDialog.DialogCode.Accepted:
                return
            cls(parent, manager, username or "", stored_pw, from_kw).exec()
        else:
            has_kw = _kwallet_available()
            if has_kw:
                msg = ("No Samba credentials are stored yet.\nWould you like to set up credentials now? "
                       "You can choose to store them in KWallet or the system keyring.")
            else:
                msg = "No Samba credentials are stored yet.\n\nWould you like to store credentials in the system keyring?"

            ans = QMessageBox.question(parent, "Samba Credentials", msg, QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if ans != QMessageBox.StandardButton.Yes:
                return
            cls(parent, manager, _current_username(), None, False, first_setup=True, kwallet_available=has_kw).exec()


    def __init__(self, parent=None, manager: Optional[SambaPasswordManager] = None,  username: str = "", password: Optional[str] = None,
                 from_kwallet: bool = False, *, first_setup: bool = False, kwallet_available: bool = False, ) -> None:

        super().__init__(parent)
        self._username_field:   Optional[QLineEdit]  = None
        self._password_field:   Optional[QLineEdit]  = None
        self._store_in_kwallet: Optional[QCheckBox]  = None
        self.setWindowTitle("Samba Credentials")
        self.setMinimumSize(750, 350)

        self._manager       = manager or SambaPasswordManager()
        self._error_dialog  = QErrorMessage(self)
        self._first_setup   = first_setup
        self._kwallet_avail = kwallet_available
        self._from_kwallet  = from_kwallet

        self._build_ui(username, password, bool(password), from_kwallet)

    def _build_ui(self, username: str, password: Optional[str], has_credentials: bool, from_kwallet: bool) -> None:
        layout = QVBoxLayout(self)
        t = current_theme()

        if has_credentials:
            origin = "KWallet" if from_kwallet else "system keyring"
            banner = QLabel(f"Credentials are already stored.\n(Retrieved from {origin})")
            banner.setStyleSheet(f"color:{t['success']};font-weight:bold;")
            banner.setAlignment(Qt.AlignmentFlag.AlignCenter)
            layout.addWidget(banner)

        layout.addStretch()

        for label_text, attr, echo, prefill in (("Username:", "_username_field", QLineEdit.EchoMode.Normal, username),
            ("Password:", "_password_field", QLineEdit.EchoMode.Password, password or "")):
            lbl = QLabel(label_text)
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            layout.addWidget(lbl)

            field = QLineEdit(prefill)
            field.setEchoMode(echo)
            field.setAlignment(Qt.AlignmentFlag.AlignCenter)

            setattr(self, attr, field)
            layout.addWidget(field)
            layout.addStretch()

        self._username_field.returnPressed.connect(self._save_credentials)
        self._password_field.returnPressed.connect(self._save_credentials)

        show_pw_cb = QCheckBox("Show password")
        show_pw_cb.setStyleSheet(f"color:{t['accent']};")
        show_pw_cb.toggled.connect(
            lambda checked: self._password_field.setEchoMode(QLineEdit.EchoMode.Normal if checked else QLineEdit.EchoMode.Password ))
        layout.addWidget(show_pw_cb)

        if self._first_setup and self._kwallet_avail:
            self._store_in_kwallet = QCheckBox("Store in KWallet (recommended)")
            self._store_in_kwallet.setChecked(True)
            self._store_in_kwallet.setStyleSheet(f"color:{t['accent']};")
            layout.addWidget(self._store_in_kwallet)

        btn_row = QHBoxLayout()

        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.reject)
        btn_row.addWidget(close_btn)

        if has_credentials and not from_kwallet:
            del_btn = QPushButton("Delete Credentials")
            del_btn.setStyleSheet(f"border:1px solid {t['error']};color:{t['error']};")
            del_btn.clicked.connect(self._delete_credentials)
            btn_row.addWidget(del_btn)

        save_btn = QPushButton("Update Credentials" if has_credentials else "Save")

        save_btn.clicked.connect(self._save_credentials)
        save_btn.setDefault(True)

        btn_row.addWidget(save_btn)

        layout.addLayout(btn_row)

    def _save_credentials(self) -> None:
        username = self._username_field.text().strip()
        password = self._password_field.text()
        if not username or not password:
            QMessageBox.warning(self, "Input Error", "Username and password must not be empty.")
            return
        try:
            if self._first_setup and self._store_in_kwallet is not None:
                target = "kwallet" if self._store_in_kwallet.isChecked() else "keyring"
                self._manager.save_credentials_to(username, password, target)
            else:
                self._manager.save_credentials(username, password)
            self._password_field.clear()
            QMessageBox.information(self, "Success", "Samba credentials successfully saved!")
            self.accept()
        except Exception as exc:
            self._error_dialog.showMessage(f"Failed to save credentials:\n{exc}")

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


class SambaPasswordManager:

    def __init__(self) -> None:
        self._cached_kwallet_entry: Optional[str] = None
        _init_keyring()

    @staticmethod
    def _run_kwallet(args: list[str], input_data: Optional[str] = None) -> Optional[str]:
        try:
            result = subprocess.run(["kwallet-query"] + args, input=input_data, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    text=True, timeout=_KWALLET_TIMEOUT, check=False)
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
            return _current_username(), raw

    def _write_to_kwallet(self, entry: str, username: str, password: str) -> None:
        payload = json.dumps({"login": username, "password": password})
        result = self._run_kwallet(["--write-password", entry, _KWALLET_WALLET], input_data=payload)
        if result is None:
            raise RuntimeError("Failed to write credentials to KWallet")
        logger.info("Updated Samba credentials in KWallet entry: %s", entry)

    def get_credentials(self) -> tuple[Optional[str], Optional[str], bool]:
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
        entry = self._find_kwallet_entry()
        if entry:
            self._write_to_kwallet(entry, username, password)
        else:
            try:
                keyring.set_password(_KEYRING_SERVICE, username, password)
                logger.info("Saved Samba credentials to system keyring.")
            except Exception as exc:
                logger.exception("Failed to save to system keyring: %s", exc)

    def save_credentials_to(self, username: str, password: str, target: str) -> None:
        if target == "kwallet":
            entry = self._find_kwallet_entry() or f"smb-{username}"
            self._write_to_kwallet(entry, username, password)
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
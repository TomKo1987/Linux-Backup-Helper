from __future__ import annotations
from typing import Optional
import getpass, json, os, subprocess, keyring
from keyring.backends import SecretService
from keyring import errors as keyring_errors
from PyQt6.QtWidgets import (QCheckBox, QDialog, QErrorMessage, QHBoxLayout, QLabel, QLineEdit, QMessageBox,
                             QPushButton, QVBoxLayout)

from logging_config import setup_logger
logger = setup_logger(__name__)

__all__ = ["SambaPasswordManager", "SambaPasswordDialog"]

_KWALLET_TIMEOUT = 2
_KEYRING_SERVICE = "backup-helper-samba"
_KWALLET_WALLET  = "kdewallet"


def _current_username() -> str:
    try:
        return os.getlogin()
    except OSError:
        return getpass.getuser()


class SambaPasswordManager:

    def __init__(self) -> None:
        self._kwallet_entry: Optional[str] = None
        try:
            keyring.set_keyring(SecretService.Keyring())
        except (keyring_errors.KeyringError, Exception) as exc:
            logger.debug("Could not set SecretService keyring backend: %s", exc)

    @staticmethod
    def _run_kwallet(args: list[str], input_data: bytes | None = None) -> Optional[str]:
        try:
            if input_data is not None:
                with subprocess.Popen(
                    ["kwallet-query"] + args,
                    stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                ) as proc:
                    proc.communicate(input=input_data, timeout=_KWALLET_TIMEOUT)
                return ""
            result = subprocess.run(
                ["kwallet-query"] + args,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, timeout=_KWALLET_TIMEOUT,
            )
            return result.stdout
        except Exception as exc:
            logger.exception("kwallet-query %s failed: %s", args[0] if args else "", exc)
            return None

    def _find_kwallet_entry(self) -> Optional[str]:
        if self._kwallet_entry:
            return self._kwallet_entry
        out = self._run_kwallet(["--list-entries", _KWALLET_WALLET])
        if out:
            for line in out.strip().splitlines():
                if line.startswith("smb-"):
                    self._kwallet_entry = line
                    logger.info("Found KWallet entry: %s", line)
                    return line
        return None

    def _read_from_kwallet(self) -> tuple[Optional[str], Optional[str]]:
        entry = self._find_kwallet_entry()
        if not entry:
            return None, None
        out = self._run_kwallet(["--read-password", entry, _KWALLET_WALLET])
        if not out or not out.strip():
            return None, None
        try:
            data = json.loads(out.strip())
            return data.get("login"), data.get("password")
        except Exception as exc:
            logger.exception("Failed to parse KWallet response: %s", exc)
            return None, None

    def _write_to_kwallet(self, username: str, password: str) -> None:
        entry = self._find_kwallet_entry() or f"smb-{username}-backup"
        self._kwallet_entry = entry
        payload = json.dumps({"login": username, "password": password}).encode()
        self._run_kwallet(["--write-password", entry, _KWALLET_WALLET], input_data=payload)
        logger.info("Saved Samba credentials to KWallet entry: %s", entry)

    def _delete_from_kwallet(self) -> bool:
        entry = self._find_kwallet_entry()
        if not entry:
            return True
        self._run_kwallet(["--delete-entry", entry, _KWALLET_WALLET])
        logger.info("Deleted KWallet entry: %s", entry)
        self._kwallet_entry = None
        return True

    @property
    def kwallet_entry(self) -> Optional[str]:
        return self._kwallet_entry

    def get_credentials(self) -> tuple[Optional[str], Optional[str]]:
        username, password = self._read_from_kwallet()
        if password:
            logger.info("Retrieved Samba credentials from KWallet.")
            return username, password
        try:
            username = _current_username()
            password = keyring.get_password(_KEYRING_SERVICE, username)
            if password:
                logger.info("Retrieved Samba credentials from system keyring.")
                return username, password
        except Exception as exc:
            logger.exception("Failed to retrieve credentials from system keyring: %s", exc)
        return None, None

    get_samba_credentials = get_credentials

    def save_credentials(self, username: str, password: str) -> None:
        if self._find_kwallet_entry():
            self._write_to_kwallet(username, password)
        else:
            try:
                keyring.set_password(_KEYRING_SERVICE, username, password)
                logger.info("Saved Samba credentials to system keyring.")
            except Exception as exc:
                logger.exception("Failed to save credentials to system keyring: %s", exc)

    save_samba_credentials = save_credentials

    def delete_credentials(self, username: str) -> bool:
        kwallet_ok  = self._delete_from_kwallet()
        keyring_ok  = True
        try:
            keyring.delete_password(_KEYRING_SERVICE, username)
            logger.info("Deleted Samba credentials for '%s' from system keyring.", username)
        except keyring_errors.PasswordDeleteError:
            logger.warning("No keyring entry found for '%s' — nothing to delete.", username)
        except Exception as exc:
            logger.exception("Failed to delete keyring entry for '%s': %s", username, exc)
            keyring_ok = False
        return kwallet_ok and keyring_ok

    delete_samba_credentials = delete_credentials


class SambaPasswordDialog(QDialog):

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Samba Credentials")
        self.setMinimumSize(950, 500)

        self._manager      = SambaPasswordManager()
        self._error_dialog = QErrorMessage(self)

        username, password   = self._fetch_credentials()
        has_credentials      = bool(password)
        from_kwallet         = bool(self._manager.kwallet_entry)
        self._build_ui(username, password, has_credentials, from_kwallet)

    def _build_ui(self, username, password, has_credentials, from_kwallet) -> None:
        layout = QVBoxLayout(self)

        if has_credentials and from_kwallet:
            lbl = QLabel("Credentials are already stored.\n"
                         "You only need to update them if the password has changed.")
            lbl.setStyleSheet("color: lightgreen;")
            layout.addWidget(lbl)

        note = (
            "(Password loaded from KWallet.)" if from_kwallet else
            "(Password loaded from system keyring.)" if has_credentials else
            "(No saved password found. Fill in the fields to create a new entry.)"
        )

        layout.addWidget(QLabel("Username:"))
        self._username_field = QLineEdit(username or _current_username())
        layout.addWidget(self._username_field)

        layout.addWidget(QLabel("Password:"))
        self._password_field = QLineEdit()
        self._password_field.setEchoMode(QLineEdit.EchoMode.Password)
        if password:
            self._password_field.setText(password)
        layout.addWidget(self._password_field)

        layout.addWidget(QLabel(note))

        show_pw = QCheckBox("Show password")
        show_pw.setStyleSheet("color: lightgreen;")
        show_pw.toggled.connect(
            lambda v: self._password_field.setEchoMode(
                QLineEdit.EchoMode.Normal if v else QLineEdit.EchoMode.Password
            )
        )
        layout.addWidget(show_pw)

        btn_row = QHBoxLayout()
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.reject)
        btn_row.addWidget(close_btn)

        if has_credentials:
            del_btn = QPushButton("Delete Credentials")
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
            self.accept()
            QMessageBox.information(self, "Success", "Samba credentials saved successfully!")
        except Exception as exc:
            self._error_dialog.showMessage(f"Failed to save credentials:\n{exc}")

    def _delete_credentials(self) -> None:
        username = self._username_field.text().strip()
        try:
            self._manager.delete_credentials(username)
            self.accept()
            QMessageBox.information(self, "Success", "Samba credentials deleted successfully!")
        except Exception as exc:
            self._error_dialog.showMessage(f"Failed to delete credentials:\n{exc}")

    def _fetch_credentials(self) -> tuple[Optional[str], Optional[str]]:
        try:
            return self._manager.get_credentials()
        except (OSError, RuntimeError, KeyError) as exc:
            logger.exception("Error fetching credentials: %s", exc)
            return _current_username(), None

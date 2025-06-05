from keyring import errors
from keyring.backends import SecretService
import os, json, subprocess, getpass, keyring
from PyQt6.QtWidgets import QDialog, QLabel, QLineEdit, QPushButton, QVBoxLayout, QHBoxLayout, QMessageBox, QCheckBox, QErrorMessage


# noinspection PyUnresolvedReferences
class SambaPasswordDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Samba Credentials")
        self.samba_password_manager = SambaPasswordManager()
        self.has_existing_credentials = False
        self.password_from_keyring = False
        self.error_dialog = QErrorMessage(self)
        layout = QVBoxLayout()
        try:
            current_user = os.getlogin()
        except OSError:
            current_user = getpass.getuser() if hasattr(__builtins__, 'getpass') else "user"
        try:
            username, password = self.samba_password_manager.get_samba_credentials()
            if password:
                keyring_source = "KWallet" if self.samba_password_manager.kwallet_entry else "system keyring"
                self.password_from_keyring = self.samba_password_manager.kwallet_entry is None
                info_text = f"(Password extracted from {keyring_source}.)"
                self.has_existing_credentials = True
            else:
                info_text = "(No password found. Create new entry in system keyring.)"
        except Exception as e:
            username = current_user
            password = None
            info_text = f"(Error retrieving credentials: {type(e).__name__})"
        self.info_label = QLabel(info_text)
        self.username_label = QLabel("Username:")
        self.username_field = QLineEdit()
        self.username_field.setText(username or current_user)
        self.password_label = QLabel("Password:")
        self.password_field = QLineEdit()
        self.password_field.setEchoMode(QLineEdit.EchoMode.Password)
        if password:
            self.password_field.setText(password)
        self.show_password = QCheckBox("Show password")
        self.show_password.setStyleSheet("color: lightgreen;")
        self.show_password.clicked.connect(self.toggle_password_visibility)
        button_box = QHBoxLayout()
        self.save_button = QPushButton()
        self.close_button = QPushButton("Close")
        self.close_button.clicked.connect(self.reject)
        button_box.addWidget(self.close_button)
        if self.password_from_keyring:
            credentials_available_label = QLabel("Credentials already available.\nNo need to save again unless you want to change them.")
            credentials_available_label.setStyleSheet("color: lightgreen;")
            layout.addWidget(credentials_available_label)
            self.delete_button = QPushButton("Delete credentials")
            self.delete_button.clicked.connect(self.del_samba_credentials)
            button_box.addWidget(self.delete_button)
        self.save_button.setText("Update credentials") if self.has_existing_credentials else self.save_button.setText("Save")
        button_box.addWidget(self.save_button)
        self.setMinimumSize(475, 325)
        layout.addWidget(self.username_label)
        layout.addWidget(self.username_field)
        layout.addWidget(self.password_label)
        layout.addWidget(self.password_field)
        layout.addWidget(self.info_label)
        layout.addWidget(self.show_password)
        self.save_button.clicked.connect(self.save_password)
        layout.addLayout(button_box)
        self.setLayout(layout)

    def toggle_password_visibility(self):
        if self.show_password.isChecked():
            self.password_field.setEchoMode(QLineEdit.EchoMode.Normal)
        else:
            self.password_field.setEchoMode(QLineEdit.EchoMode.Password)

    def save_password(self):
        username = self.username_field.text()
        password = self.password_field.text()
        if not username or not password:
            QMessageBox.warning(self, "Input Error", "Username and password cannot be empty.")
            return
        try:
            self.samba_password_manager.save_samba_credentials(username, password)
            self.accept()
            QMessageBox.information(self, "Success", "Samba credentials successfully saved!")
        except Exception as e:
            self.error_dialog.showMessage(f"Failed to save credentials.\n{e}")

    def del_samba_credentials(self):
        username = self.username_field.text()
        try:
            self.samba_password_manager.delete_samba_credentials(username)
            self.accept()
            QMessageBox.information(self, "Success", "Samba credentials successfully deleted!")
        except Exception as e:
            self.error_dialog.showMessage(f"Failed to delete credentials: {str(e)}")


class SambaPasswordManager:
    def __init__(self):
        self.keyring_service = "backup-helper-samba"
        self.kwallet_wallet = "kdewallet"
        self.kwallet_entry = None
        self._set_system_keyring()

    @staticmethod
    def _set_system_keyring():
        try:
            keyring.set_keyring(SecretService.Keyring())
        except keyring.errors.KeyringError:
            pass

    def _find_kwallet_entry(self):
        if self.kwallet_entry:
            return self.kwallet_entry
        try:
            result = subprocess.run(["kwallet-query", "--list-entries", self.kwallet_wallet], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=2)
            entries = result.stdout.strip().splitlines()
            for entry in entries:
                if entry.startswith("smb-"):
                    self.kwallet_entry = entry
                    print(f"Found KWallet entry: {entry}")
                    return entry
        except Exception as e:
            print(f"Failed to list KWallet entries: {e}")
        return None

    def _get_password_from_kwallet(self):
        entry = self._find_kwallet_entry()
        if not entry:
            return None, None
        try:
            result = subprocess.run(["kwallet-query", "--read-password", entry, self.kwallet_wallet], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=2)
            raw = result.stdout.strip()
            if not raw:
                return None, None
            credentials = json.loads(raw)
            return credentials.get("login"), credentials.get("password")
        except Exception as e:
            print(f"Failed to retrieve password from KWallet: {e}")
            return None, None

    def _save_password_to_kwallet(self, username, password):
        entry = self._find_kwallet_entry()
        if not entry:
            entry = f"smb-{username}-backup"
            self.kwallet_entry = entry
        try:
            data = json.dumps({"login": username, "password": password})
            with subprocess.Popen(["kwallet-query", "--write-password", entry, self.kwallet_wallet], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) as proc:
                proc.communicate(input=data.encode(), timeout=2)
            print(f"Saved samba credentials to KWallet entry: {entry}")
        except Exception as e:
            print(f"Failed to save credentials to KWallet: {type(e).__name__}")

    def get_samba_credentials(self):
        print("get_samba_credentials was called")
        username, password = self._get_password_from_kwallet()
        if password:
            print("Retrieved samba password from KWallet")
            return username, password
        try:
            username = os.getlogin()
            password = keyring.get_password(self.keyring_service, username)
            if password:
                print("Retrieved samba password from system keyring")
                return username, password
        except Exception as e:
            print(f"Failed to retrieve from keyring: {e}")
        return None, None

    def save_samba_credentials(self, username, password):
        kwallet_username, kwallet_password = self._get_password_from_kwallet()
        if kwallet_password:
            self._save_password_to_kwallet(username, password)
        else:
            try:
                keyring.set_password(self.keyring_service, username, password)
                print("Saved samba password to system keyring")
            except Exception as e:
                print(f"Failed to save to keyring: {e}")

    def delete_samba_credentials(self, username):
        success = True
        try:
            keyring.delete_password(self.keyring_service, username)
            print(f"Deleted samba password for {username} from keyring")
        except Exception as e:
            print(f"Failed to delete from keyring for {username}: {e}")
            success = False
        return success
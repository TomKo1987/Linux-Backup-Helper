from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import QDialog, QFormLayout, QLabel, QLineEdit, QListWidgetItem, QMessageBox, QVBoxLayout

from dialog_base import _ListDialog
from drive_utils import get_mounts, is_mounted
from state import S, save_profile
from themes import apply_tooltip, current_theme
from ui_utils import hdr_label, ok_cancel_buttons, sep

class MountDialog(QDialog):

    def __init__(self, parent, opt: dict | None):
        super().__init__(parent)
        self.result: dict = {}
        self.setWindowTitle("Edit Drive" if opt else "New Drive")
        self.setMinimumSize(900, 500)
        _opt: dict = opt or {}
        t   = current_theme()
        layout = QVBoxLayout(self)
        layout.setSpacing(15)
        layout.addWidget(hdr_label("Configure Drive"))
        layout.addWidget(sep())
        form = QFormLayout()
        form.setSpacing(15)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapAllRows)

        def _field(key: str, placeholder: str) -> QLineEdit:
            f = QLineEdit(_opt.get(key, "") or "")
            f.setPlaceholderText(placeholder)
            return f

        def _info_label(text: str, tooltip: str) -> QLabel:
            lbl = QLabel(text)
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lbl.setStyleSheet(f"color:{t['accent2']};")
            apply_tooltip(lbl, tooltip)
            return lbl
        self.name = _field("drive_name", "e.g. Backup 1")
        form.addRow(QLabel("Drive name:"))
        form.addRow(self.name)
        self.mount_path = _field("mount_path", "e.g. smb://192.168.0.122/Backup Drive/")
        form.addRow(_info_label("󰔨 Mount path (optional)",
                                "<u>Mount Path (optional)</u><br><br>"
                                "Only needed if this drive cannot be detected automatically.<br><br>"
                                "<i>Leave empty</i> for standard USB/SATA drives — Backup Helper finds them "
                                "automatically under <code>/run/media/&lt;user&gt;/&lt;name&gt;</code>, "
                                "<code>/media/&lt;user&gt;/&lt;name&gt;</code> or <code>/mnt/&lt;name&gt;</code>"
                                " using the name from above.<br><br>"
                                "<i>Fill in</i> when the drive is mounted elsewhere (sshfs, KDE Connect, etc.)."))
        form.addRow(self.mount_path)
        self.mount = _field("mount_command", "udisksctl mount --block-device /dev/sdX1")
        form.addRow(_info_label("󰔨 Mount command:",
                                "<u>Mount Command</u><br><br>"
                                "The command is executed non-interactively — <b>no password prompt will appear</b>."
                                "<br><br><b>sshfs:</b> SSH connections must use key-based authentication.<br>"
                                "Set up a key pair first:<br>"
                                "<code>ssh-keygen -t ed25519 &amp;&amp; ssh-copy-id user@host</code><br><br>"
                                "Example: <code>sshfs user@host:/remote/path ~/local/mountpoint</code><br><br>"
                                "<b>udisksctl / mount:</b> Work as usual for local drives.<br>"
                                "<b>kdeconnect-cli:</b> The device must already be paired and reachable.<br><br>"
                                "<small>Allowed commands: mount, umount, mount.cifs, udisksctl, kdeconnect-cli, "
                                "sshfs, fusermount3, fusermount</small>"))
        form.addRow(self.mount)
        self.unmnt    = _field("unmount_command", "udisksctl unmount --block-device /dev/sdX1")
        lbl_unmnt     = QLabel("Unmount command:")
        lbl_unmnt.setAlignment(Qt.AlignmentFlag.AlignCenter)
        form.addRow(lbl_unmnt)
        form.addRow(self.unmnt)
        layout.addLayout(form)
        layout.addStretch()
        layout.addWidget(sep())
        layout.addWidget(ok_cancel_buttons(self, self._accept))

    def _accept(self) -> None:
        from drive_utils import _valid_drive_name
        name = self.name.text().strip()
        if not name:
            QMessageBox.warning(self, "Error", "Name is a required field.")
            return
        if not _valid_drive_name(name):
            QMessageBox.warning(self, "Invalid Drive Name",
                                "The drive name contains invalid characters or exceeds 128 characters.\n\n"
                                "Allowed: letters, digits, spaces, hyphens, underscores, dots, parentheses, @ and :")
            return
        self.result = {"drive_name": name, "mount_path": self.mount_path.text().strip(),
                       "mount_command": self.mount.text().strip(), "unmount_command": self.unmnt.text().strip()}
        self.accept()

class MountsDialog(_ListDialog):

    def __init__(self, parent):
        self.was_changed: bool = False
        super().__init__(parent, "Mount Options", (700, 460), "Mounted Drives",
                         [("🆕 New", "_new"), ("✎ Edit", "_edit"), ("✕ Remove", "_del")])

    def _refresh(self) -> None:
        self.item_list.clear()
        t   = current_theme()
        out = get_mounts()
        for opt in S.mount_options:
            mounted = is_mounted(opt, out)
            status  = "●" if mounted else "○"
            item    = QListWidgetItem(f"  {status}  {opt.get('drive_name', '?')}")
            item.setForeground(QColor(t["green"] if mounted else t["text_dim"]))
            item.setData(Qt.ItemDataRole.UserRole, opt)
            self.item_list.addItem(item)

    def _new(self) -> None:
        dlg = MountDialog(self, None)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            S.mount_options.append(dlg.result)
            save_profile()
            self.was_changed = True
            self._refresh()

    def _edit(self) -> None:
        opt = self._selected_data()
        if not isinstance(opt, dict): return
        dlg = MountDialog(self, opt)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            idx = next((i for i, o in enumerate(S.mount_options) if o is opt), None)
            if idx is None:
                name = opt.get("drive_name")
                if name:
                    idx = next((i for i, o in enumerate(S.mount_options)
                                if o.get("drive_name") == name), None)
            if idx is not None:
                S.mount_options[idx] = dlg.result
                save_profile()
                self.was_changed = True
                self._refresh()
            else:
                QMessageBox.warning(self, "Edit Failed",
                                    "Could not locate the selected drive in the current profile.\n"
                                    "Please re-select the entry and try again.")

    def _del(self) -> None:
        opt = self._selected_data()
        if not isinstance(opt, dict):
            return
        name: str = opt.get("drive_name", "?")
        if QMessageBox.question(self, "Remove Drive", f"Really remove '{name}' from mount options?",
                                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
            return
        before = len(S.mount_options)
        S.mount_options = [o for o in S.mount_options if o is not opt]
        if len(S.mount_options) == before and name != "?":
            S.mount_options = [o for o in S.mount_options if o.get("drive_name") != name]
        save_profile()
        self.was_changed = True
        self._refresh()

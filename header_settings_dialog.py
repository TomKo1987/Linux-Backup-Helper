import copy

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import QColorDialog, QDialog, QListWidget, QListWidgetItem, QMessageBox, QVBoxLayout

from dialog_base import _UserRoleListMixin
from state import S, save_profile
from themes import current_theme
from ui_utils import ask_text, btn_row, hdr_label, ok_cancel_buttons, sep

class HeaderSettingsDialog(_UserRoleListMixin, QDialog):
    _selected_name = _UserRoleListMixin._selected_data

    def __init__(self, parent):
        super().__init__(parent)
        self._headers_backup = copy.deepcopy(S.headers)
        self._entries_backup = copy.deepcopy(S.entries)
        self.was_changed: bool = False
        self.setWindowTitle("Header Settings")
        self.setMinimumSize(750, 500)
        layout = QVBoxLayout(self)
        layout.addWidget(hdr_label("Headers"))
        layout.addWidget(sep())
        self.item_list = QListWidget()
        layout.addWidget(self.item_list, 1)
        layout.addLayout(btn_row([("🆕 New", self._new), ("🎨 Color", self._color), ("⏸ Toggle active", self._toggle),
                                   ("✕ Delete", self._delete), ("↑ Up", self._move_up), ("↓ Down", self._move_down)]))
        layout.addWidget(sep())
        layout.addWidget(ok_cancel_buttons(self, self._save_and_close, "Save && Close"))
        self._refresh()

    def _save_and_close(self) -> None:
        save_profile()
        self.accept()

    def reject(self) -> None:
        S.headers = self._headers_backup
        S.entries = self._entries_backup
        super().reject()

    def _refresh(self) -> None:
        t   = current_theme()
        row = self.item_list.currentRow()
        self.item_list.clear()
        for name, d in S.headers.items():
            status = "  [inactive]" if d["inactive"] else ""
            item = QListWidgetItem(f"  {name}{status}")
            item.setForeground(QColor(t["text_dim"] if d.get("inactive", False) else d.get("color", "#ffffff")))
            item.setData(Qt.ItemDataRole.UserRole, name)
            self.item_list.addItem(item)
        if 0 <= row < self.item_list.count():
            self.item_list.setCurrentRow(row)

    def _new(self) -> None:
        name, ok = ask_text(self, "New Header", "Header name:")
        if not ok or not name.strip():
            return
        name = name.strip()
        if name in S.headers:
            QMessageBox.warning(self, "Duplicate", f"Header '{name}' already exists.")
            return
        col = QColorDialog.getColor(QColor(current_theme()["accent"]), self, "Choose header colour")
        S.headers[name] = {"inactive": False, "color": col.name() if col.isValid() else "#ffffff"}
        self.was_changed = True
        self._refresh()
        for i in range(self.item_list.count()):
            item = self.item_list.item(i)
            if item and item.data(Qt.ItemDataRole.UserRole) == name:
                self.item_list.setCurrentRow(i)
                break

    def _color(self) -> None:
        name = self._selected_name()
        if not name: return
        col = QColorDialog.getColor(QColor(S.headers[name]["color"]), self)
        if col.isValid():
            S.headers[name]["color"] = col.name()
            self.was_changed = True
            self._refresh()

    def _toggle(self) -> None:
        name = self._selected_name()
        if name:
            S.headers[name]["inactive"] = not S.headers[name]["inactive"]
            self.was_changed = True
            self._refresh()

    def _delete(self) -> None:
        name = self._selected_name()
        if not name: return
        if QMessageBox.question(self, "Delete", f"Delete header '{name}' and all its entries?",
                                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes:
            del S.headers[name]
            S.entries    = [e for e in S.entries if e["header"] != name]
            self.was_changed = True
            self._refresh()

    def _move_header(self, direction: int) -> bool:
        name = self._selected_name()
        if not name: return False
        keys    = list(S.headers.keys())
        idx     = keys.index(name)
        new_idx = idx + direction
        if not (0 <= new_idx < len(keys)): return False
        keys[idx], keys[new_idx] = keys[new_idx], keys[idx]
        S.headers = {k: S.headers[k] for k in keys}
        self._refresh()
        self.item_list.setCurrentRow(new_idx)
        return True

    def _move_up(self)   -> None:
        if self._move_header(-1): self.was_changed = True

    def _move_down(self) -> None:
        if self._move_header(+1): self.was_changed = True

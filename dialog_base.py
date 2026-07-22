from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QDialog, QHBoxLayout, QListWidget, QPushButton, QTextEdit, QVBoxLayout, QWidget

from themes import current_theme, font_sz
from ui_utils import btn_row, hdr_label, sep


class _UserRoleListMixin:
    item_list: QListWidget

    def _selected_data(self):
        item = self.item_list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None


class _ListDialog(_UserRoleListMixin, QDialog):

    def __init__(self, parent, title: str, size: tuple[int, int], hdr_text: str,
                 btn_specs: list[tuple[str, str]], close_label: str = "✕  Close"):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumSize(*size)

        layout = QVBoxLayout(self)
        layout.addWidget(hdr_label(hdr_text))
        layout.addWidget(sep())
        self.item_list = QListWidget()
        layout.addWidget(self.item_list, 1)
        layout.addLayout(btn_row([(lbl, getattr(self, fn)) for lbl, fn in btn_specs]))
        layout.addWidget(sep())
        close_btn = QPushButton(close_label)
        close_btn.clicked.connect(self.accept)
        layout.addWidget(close_btn)
        self._refresh()

    def _refresh(self) -> None:
        raise NotImplementedError


class _TextViewDialog(QDialog):

    def __init__(self, parent, title: str, min_size: tuple[int, int], font_size: int | None = None,
                 extra_buttons: list[tuple[str, object]] | None = None):

        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumSize(*min_size)
        t  = current_theme()
        fs = font_size if font_size is not None else font_sz()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(5, 5, 5, 5)
        layout.setSpacing(5)

        self.view = QTextEdit()
        self.view.setReadOnly(True)
        self.view.setStyleSheet(f"font-family:monospace;font-size:{fs}px;border:none;border-radius:0;")
        layout.addWidget(self.view, 1)

        bot = QWidget()
        bot.setStyleSheet(f"background:{t['bg2']};border-top:1px solid {t['header_sep']};")
        bl  = QHBoxLayout(bot)
        bl.setContentsMargins(12, 8, 12, 8)
        bl.setSpacing(8)
        for label, fn in [*(extra_buttons or []), ("✕ Close", self.accept)]:
            b = QPushButton(label)
            b.setFixedHeight(36)
            b.clicked.connect(fn)
            bl.addWidget(b)
        layout.addWidget(bot)

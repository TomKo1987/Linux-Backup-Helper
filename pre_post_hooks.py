import shlex
import subprocess

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QAbstractItemView, QApplication, QDialog, QHBoxLayout, QInputDialog,
    QLabel, QListWidget, QListWidgetItem, QMessageBox,
    QPushButton, QVBoxLayout, QWidget,
)

from state import logger
from themes import current_theme, font_sz
from ui_utils import _StandardKeysMixin, hdr_label, sep

__all__ = ["HooksDialog", "run_hooks"]

_MAX_HOOKS = 20
_HOOK_TIMEOUT = 120


def run_hooks(hooks: list[str], *, abort_on_error: bool = True, label: str = "") -> tuple[bool, list[str]]:
    errors: list[str] = []
    for raw_cmd in hooks:
        cmd = raw_cmd.strip()
        if not cmd or cmd.startswith("#"):
            continue
        try:
            tokens = shlex.split(cmd)
        except ValueError as exc:
            msg = f"Hook parse error ({label!r}): {exc} — command: {cmd!r}"
            logger.error("run_hooks: %s", msg)
            errors.append(msg)
            if abort_on_error:
                return False, errors
            continue

        logger.info("run_hooks: [%s] %s", label or "hook", cmd)
        try:
            result = subprocess.run(
                tokens,
                capture_output=True,
                text=True,
                timeout=_HOOK_TIMEOUT,
            )
            if result.returncode != 0:
                stderr = (result.stderr or result.stdout or "").strip()
                msg = (
                    f"Hook failed (exit {result.returncode})"
                    f"{' — ' + label if label else ''}: {cmd!r}"
                    + (f"\n  → {stderr}" if stderr else "")
                )
                logger.error("run_hooks: %s", msg)
                errors.append(msg)
                if abort_on_error:
                    return False, errors
        except subprocess.TimeoutExpired:
            msg = f"Hook timed out after {_HOOK_TIMEOUT}s ({label}): {cmd!r}"
            logger.error("run_hooks: %s", msg)
            errors.append(msg)
            if abort_on_error:
                return False, errors
        except Exception as exc:
            msg = f"Hook exception ({label}): {exc} — {cmd!r}"
            logger.error("run_hooks: %s", msg)
            errors.append(msg)
            if abort_on_error:
                return False, errors

    return len(errors) == 0, errors


class _HookList(QWidget):
    def __init__(self, title: str, hooks: list[str], parent=None) -> None:
        super().__init__(parent)
        self._hooks = list(hooks)
        t = current_theme()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)

        hdr = QLabel(title)
        hdr.setStyleSheet(
            f"font-size:{font_sz(1)}px;font-weight:bold;"
            f"color:{t['accent']};background:transparent;border:none;"
        )
        lay.addWidget(hdr)

        self._lw = QListWidget()
        self._lw.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self._lw.setAlternatingRowColors(True)
        self._lw.setMinimumHeight(120)
        self._lw.setStyleSheet(
            f"QListWidget{{background:{t['bg3']};border:1px solid {t['bg2']};"
            f"color:{t['text']};font-family:monospace;font-size:{font_sz(-1)}px;}}"
            f"QListWidget::item:selected{{background:{t['highlight']};color:{t['bg']};}}"
        )
        lay.addWidget(self._lw, 1)

        btn_row = QHBoxLayout()
        for label, slot in [
            ("➕ Add",    self._add),
            ("✏ Edit",   self._edit),
            ("🗑 Remove", self._remove),
            ("▲",        self._move_up),
            ("▼",        self._move_down),
        ]:
            b = QPushButton(label)
            b.setFixedHeight(28)
            b.clicked.connect(slot)
            btn_row.addWidget(b)
        btn_row.addStretch()
        lay.addLayout(btn_row)

        self._populate()

    def _populate(self) -> None:
        self._lw.clear()
        for cmd in self._hooks:
            it = QListWidgetItem(cmd)
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsEditable)
            self._lw.addItem(it)

    def _add(self) -> None:
        if len(self._hooks) >= _MAX_HOOKS:
            QMessageBox.warning(self, "Limit Reached", f"Maximum {_MAX_HOOKS} hooks per phase.")
            return
        cmd, ok = QInputDialog.getText(
            self, "New Hook", "Shell command:",
            text="",
        )
        if ok and cmd.strip():
            self._hooks.append(cmd.strip())
            self._populate()

    def _edit(self) -> None:
        row = self._lw.currentRow()
        if row < 0:
            return
        cmd, ok = QInputDialog.getText(
            self, "Edit Hook", "Shell command:",
            text=self._hooks[row],
        )
        if ok and cmd.strip():
            self._hooks[row] = cmd.strip()
            self._populate()

    def _remove(self) -> None:
        row = self._lw.currentRow()
        if row < 0:
            return
        ans = QMessageBox.question(
            self, "Remove Hook",
            f"Remove hook:\n{self._hooks[row]}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if ans == QMessageBox.StandardButton.Yes:
            self._hooks.pop(row)
            self._populate()

    def _move_up(self) -> None:
        row = self._lw.currentRow()
        if row > 0:
            self._hooks[row - 1], self._hooks[row] = self._hooks[row], self._hooks[row - 1]
            self._populate()
            self._lw.setCurrentRow(row - 1)

    def _move_down(self) -> None:
        row = self._lw.currentRow()
        if 0 <= row < len(self._hooks) - 1:
            self._hooks[row], self._hooks[row + 1] = self._hooks[row + 1], self._hooks[row]
            self._populate()
            self._lw.setCurrentRow(row + 1)

    def get_hooks(self) -> list[str]:
        result: list[str] = []
        for i in range(self._lw.count()):
            it = self._lw.item(i)
            if it is not None:
                result.append(it.text())
        return result


class HooksDialog(_StandardKeysMixin, QDialog):
    def __init__(self, parent, entry: dict) -> None:
        super().__init__(parent)
        self._entry = entry
        self.setWindowTitle(f"Hooks — {entry.get('title', '?')}")
        screen = QApplication.primaryScreen()
        geo    = screen.availableGeometry() if screen else None
        if geo:
            self.setMinimumSize(
                min(1500, int(geo.width()  * 0.85)),
                min(1000, int(geo.height() * 0.85)),
            )
        else:
            self.setMinimumSize(1200, 700)
        self._build()

    def _build(self) -> None:
        t   = current_theme()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 16, 16, 16)
        lay.setSpacing(14)

        lay.addWidget(hdr_label(f"🪝  Hooks for: {self._entry.get('title', '?')}"))

        info = QLabel(
            "Pre-hooks run <b>before</b> the backup/restore copy starts.<br>"
            "Post-hooks run <b>after</b> the copy finishes (even on cancel).<br>"
            "A failing pre-hook will abort the operation for this entry. "
            "Post-hook failures are logged but do not affect the result."
        )
        info.setTextFormat(Qt.TextFormat.RichText)
        info.setWordWrap(True)
        info.setStyleSheet(
            f"color:{t['text_dim']};font-size:{font_sz(-1)}px;"
            f"background:transparent;border:none;"
        )
        lay.addWidget(info)
        lay.addWidget(sep())

        details = self._entry.setdefault("details", {})
        self._pre  = _HookList("⚡ Pre-hooks  (run before copy)",
                               details.get("pre_hooks", []))
        self._post = _HookList("🏁 Post-hooks  (run after copy)",
                               details.get("post_hooks", []))

        lay.addWidget(self._pre,  1)
        lay.addWidget(sep())
        lay.addWidget(self._post, 1)
        lay.addWidget(sep())

        bot = QHBoxLayout()
        bot.addStretch()
        cancel_btn = QPushButton("Cancel")
        cancel_btn.setFixedHeight(34)
        cancel_btn.clicked.connect(self.reject)
        save_btn = QPushButton("💾 Save")
        save_btn.setFixedHeight(34)
        save_btn.setDefault(True)
        save_btn.clicked.connect(self._save)
        bot.addWidget(cancel_btn)
        bot.addWidget(save_btn)
        lay.addLayout(bot)

    def _save(self) -> None:
        details = self._entry.setdefault("details", {})
        details["pre_hooks"]  = self._pre.get_hooks()
        details["post_hooks"] = self._post.get_hooks()
        self.accept()
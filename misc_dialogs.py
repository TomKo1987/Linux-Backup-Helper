import os
import subprocess
import threading

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QFontDatabase, QTextCursor
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QFrame, QHBoxLayout, QLabel, QMessageBox, QPushButton,
    QTextEdit, QVBoxLayout, QWidget,
)

from dialog_base import _TextViewDialog
from state import RESTART_DIALOG, S, _LOG_FILE, apply_replacements, logger, save_profile
from themes import THEMES, apply_style, current_theme, font_sz
from ui_utils import _StandardKeysMixin, hdr_label, ok_cancel_buttons

class LogViewer(_TextViewDialog):

    def __init__(self, parent):
        t = current_theme()
        super().__init__(parent, "Log Viewer", (1350, 950),
                         extra_buttons=[("🔄 Refresh", self._load), ("🗑 Clear", self._clear)])
        top = QWidget()
        top.setStyleSheet(f"background:{t['bg2']};border-bottom:1px solid {t['header_sep']};")
        tl  = QHBoxLayout(top)
        tl.setContentsMargins(14, 8, 14, 8)
        tl.addStretch()
        tl.addWidget(hdr_label("📋 Log File"))
        tl.addStretch(1)
        tl.addWidget(QLabel(apply_replacements(str(_LOG_FILE))))
        layout = self.layout()
        if isinstance(layout, QVBoxLayout):
            layout.insertWidget(0, top)
        else:
            logger.error("LogViewer: unexpected layout type %s", type(layout))
        self._load()

    def _load(self) -> None:
        if not _LOG_FILE.exists():
            self.view.setPlainText("No log file found.")
            return
        try:
            size = _LOG_FILE.stat().st_size
            read_bytes = min(size, 512 * 1024)
            with open(_LOG_FILE, "rb") as f:
                if read_bytes < size:
                    f.seek(size - read_bytes)
                raw = f.read(read_bytes)
            text = raw.decode("utf-8", errors="replace")
            if read_bytes < size:
                first_nl = text.find("\n")
                if first_nl != -1:
                    text = text[first_nl + 1:]
            lines = text.splitlines()
            total_lines = len(lines)
            if read_bytes < size or total_lines > 2000:
                count = "many" if read_bytes < size else str(total_lines)
                prefix = f"[… last 2000 of {count} lines …]\n"
            else:
                prefix = ""
            self.view.setPlainText(prefix + "\n".join(lines[-2000:]))
            cursor = self.view.textCursor()
            cursor.movePosition(QTextCursor.MoveOperation.End)
            self.view.setTextCursor(cursor)
        except Exception as e: self.view.setPlainText(f"Error reading log file: {e}")

    def _clear(self) -> None:
        if QMessageBox.question(self, "Clear log", "Permanently delete all log entries?",
                                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No) \
                == QMessageBox.StandardButton.Yes:
            try:
                _LOG_FILE.write_text("", encoding="utf-8")
            except OSError as e:
                QMessageBox.warning(self, "Error", f"Could not clear log: {e}")
                return
            self._load()

class SysInfoDialog(_TextViewDialog):
    done_sig = pyqtSignal(str)

    def __init__(self, parent):
        super().__init__(parent, "System Information", (1400, 875), font_size=font_sz(2))
        self.view.setPlainText("⏳ Loading system information…")
        self.done_sig.connect(self.view.setPlainText)
        self._closed = threading.Event()
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self) -> None:
        try:
            r = subprocess.run(["inxi", "-SMCGAz", "--no-host", "--color", "0"], capture_output=True, text=True,
                               timeout=15, env={**os.environ, "LANG": "C"}, check=False)
            result = r.stdout.strip() or r.stderr.strip() or "No output received from inxi."
        except FileNotFoundError:
            result = ("'inxi' is not installed.\n\n"
                      "Installation:\n"
                      "  Arch / Manjaro:  sudo pacman -S inxi\n"
                      "  Debian / Ubuntu: sudo apt install inxi\n"
                      "  Fedora:          sudo dnf install inxi\n"
                      "  openSUSE:        sudo zypper install inxi\n"
                      "  Void:            sudo xbps-install inxi\n"
                      "  Alpine:          sudo apk add inxi\n"
                      "  Gentoo:          sudo emerge app-misc/inxi\n"
                      "  Solus:           sudo eopkg install inxi\n"
                      "  NixOS:           nix-env -iA nixpkgs.inxi\n")
        except subprocess.TimeoutExpired:
            result = "System information request timed out."
        except Exception as exc:
            result = f"An unexpected error occurred: {exc}"
        try:
            if not self._closed.is_set():
                self.done_sig.emit(result)
        except RuntimeError:
            pass

    def closeEvent(self, event) -> None:
        try:
            self.done_sig.disconnect()
        except (RuntimeError, TypeError):
            pass
        self._closed.set()
        super().closeEvent(event)

class NotesDialog(_StandardKeysMixin, QDialog):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._saved = False
        self._discarded = False

        profile = S.profile_name or "(no profile)"
        self.setWindowTitle(f"Profile Notes — {profile}")
        self.setMinimumSize(1500, 1000)

        t       = current_theme()
        bg      = t["bg"]
        bg2     = t["bg2"]
        bg3     = t["bg3"]
        sep_col = t["header_sep"]
        acc     = t["accent"]
        fg      = t["text"]
        dim     = t["text_dim"]
        hi      = t["highlight"]
        acc2    = t["accent2"]

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        hdr = QFrame()
        hdr.setStyleSheet(f"background:{bg2};border-bottom:1px solid {sep_col};")
        hl = QHBoxLayout(hdr)
        hl.setContentsMargins(14, 10, 14, 10)
        title_lbl = QLabel(f"📝  Notes  ·  {profile}")
        title_lbl.setStyleSheet(
            f"font-size:{font_sz(4)}px;font-weight:bold;"
            f"color:{acc};background:transparent;"
        )
        hint_lbl = QLabel("Auto-saved on close")
        hint_lbl.setStyleSheet(f"font-size:{font_sz(-2)}px;color:{dim};background:transparent;")
        hl.addWidget(title_lbl)
        hl.addStretch()
        hl.addWidget(hint_lbl)

        self._edit = QTextEdit()
        self._edit.setPlaceholderText(
            "Write notes about this profile here…\n\n"
            "E.g.:\n"
            "  • Last restore tested on …\n"
            "  • Excluded paths / known issues\n"
            "  • Destination host / credentials info"
        )
        self._edit.setStyleSheet(
            f"QTextEdit{{background:{bg3};color:{fg};"
            f"border:none;font-size:{font_sz()}px;padding:12px;}}"
        )
        self._edit.setPlainText(S.notes)

        ftr = QFrame()
        ftr.setStyleSheet(f"background:{bg2};border-top:1px solid {sep_col};")
        fl = QHBoxLayout(ftr)
        fl.setContentsMargins(12, 8, 12, 8)
        fl.addStretch()

        def _btn_style(primary: bool = False) -> str:
            border = acc if primary else sep_col
            return (
                f"QPushButton{{background:{bg3};border:1px solid {border};"
                f"border-radius:4px;color:{fg};padding:4px 16px;}}"
                f"QPushButton:hover{{background:{bg2};border-color:{acc};color:{hi};}}"
                f"QPushButton:focus{{border-color:{acc};color:{hi};outline:none;}}"
                f"QPushButton:pressed{{background:{bg};border-color:{acc2};color:{acc2};}}"
            )

        save_btn = QPushButton("💾 Save && Close")
        save_btn.setMinimumHeight(34)
        save_btn.setStyleSheet(_btn_style(primary=True))
        save_btn.clicked.connect(self._save_and_close)

        discard_btn = QPushButton("Discard")
        discard_btn.setMinimumHeight(34)
        discard_btn.setStyleSheet(_btn_style())
        discard_btn.clicked.connect(self._discard)

        fl.addWidget(discard_btn)
        fl.addWidget(save_btn)

        layout.addWidget(hdr)
        layout.addWidget(self._edit, 1)
        layout.addWidget(ftr)
        self.setStyleSheet(f"background:{bg};")

    def _save_and_close(self) -> None:
        S.notes = self._edit.toPlainText()
        save_profile()
        self._saved = True
        self.accept()

    def _discard(self) -> None:
        self._discarded = True
        self.reject()

    def closeEvent(self, event) -> None:
        if not self._saved and not self._discarded:
            S.notes = self._edit.toPlainText()
            save_profile()
            self._saved = True
        event.accept()


class _ThemeDialog(_StandardKeysMixin, QDialog):
    changed = pyqtSignal(int)

    def __init__(self, parent) -> None:
        super().__init__(parent)
        self.setWindowTitle("Theme and Font Settings")
        self.setMinimumSize(480, 380)
        self.setWindowModality(Qt.WindowModality.NonModal)
        self._orig = (
            S.ui.get("theme", "Tokyo Night"), S.ui.get("font_family", ""), S.ui.get("font_size", 14),
            S.ui.get("disable_tray_icon", False),
        )
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        def _combo(label: str, items: list[str], current: str) -> QComboBox:
            layout.addWidget(QLabel(label))
            cb = QComboBox()
            cb.addItems(items)
            cb.setCurrentText(current)
            layout.addWidget(cb)
            return cb

        self._theme_cb = _combo("Select Theme:", list(THEMES.keys()), S.ui.get("theme", "Tokyo Night"))
        self._font_cb = _combo("Select Font:", ["(System Default)"] + sorted(QFontDatabase.families()),
                               S.ui.get("font_family", "") or "(System Default)")
        current_size = str(S.ui.get("font_size", 14))
        size_options = ["10", "11", "12", "13", "14", "15", "16", "17", "18", "20", "22", "24"]
        if current_size not in size_options:
            size_options = sorted(size_options + [current_size], key=int)
        self._size_cb = _combo("Select Font Size:", size_options, current_size)

        prev_btn = QPushButton("Preview")
        prev_btn.clicked.connect(lambda: self._apply(save=False))
        layout.addWidget(prev_btn)

        self._tray_cb = QCheckBox("Disable Tray Icon")
        self._tray_cb.setChecked(bool(S.ui.get("disable_tray_icon", False)))
        layout.addWidget(self._tray_cb)

        layout.addWidget(ok_cancel_buttons(self, self._on_ok, cancel_fn=self.reject))

    def _apply(self, save: bool = False) -> None:
        chosen_font = self._font_cb.currentText()
        if chosen_font == "(System Default)":
            chosen_font = ""
        S.ui.update(
            theme=self._theme_cb.currentText(), font_family=chosen_font, font_size=int(self._size_cb.currentText()))
        apply_style()
        if save:
            S.ui["disable_tray_icon"] = self._tray_cb.isChecked()
            save_profile()

    def _on_ok(self) -> None:
        self._apply(save=True)
        font_display = self._font_cb.currentText()
        msg = f"Theme: {self._theme_cb.currentText()}, Font: {font_display}, Size: {self._size_cb.currentText()} px"
        QMessageBox.information(self, "Theme Saved", msg)
        self.changed.emit(RESTART_DIALOG)
        self.accept()

    def reject(self) -> None:
        orig_theme, orig_font, orig_size, orig_tray = self._orig
        S.ui.update(theme=orig_theme, font_family=orig_font, font_size=orig_size, disable_tray_icon=orig_tray)
        apply_style()
        super().reject()

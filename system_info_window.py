from __future__ import annotations
import subprocess
from global_style import get_current_style
from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtGui import QFont, QFontMetrics, QTextOption
from PyQt6.QtWidgets import QDialog, QPushButton, QTextEdit, QVBoxLayout

from logging_config import setup_logger
logger = setup_logger(__name__)

_MAX_WIDTH_RATIO = 0.90
_WORKER_WAIT_MS  = 3000
_INXI_ARGS       = ["inxi", "-SMCGAz", "--no-host", "--color", "0"]
_INXI_TIMEOUT    = 15


class _InxiWorker(QThread):

    finished = pyqtSignal(str)

    def run(self) -> None:
        try:
            result = subprocess.run(
                _INXI_ARGS,
                capture_output=True,
                text=True,
                timeout=_INXI_TIMEOUT,
            )
            if result.returncode == 0 and result.stdout.strip():
                self.finished.emit(result.stdout.strip())
            else:
                err = result.stderr.strip() or "Unknown error while running inxi."
                self.finished.emit(f"Error: {err}")
        except FileNotFoundError:
            self.finished.emit(
                "The tool 'inxi' is not installed on your system.\n\n"
                "Install it with one of:\n"
                "  Ubuntu/Debian:  sudo apt install inxi\n"
                "  Arch/Manjaro:   sudo pacman -S inxi\n"
                "  Fedora:         sudo dnf install inxi"
            )
        except subprocess.TimeoutExpired:
            self.finished.emit("Error: inxi timed out while collecting system information.")
        except Exception as exc:
            self.finished.emit(f"Unexpected error: {exc}")


# noinspection PyUnresolvedReferences
class SystemInfoWindow(QDialog):

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("System Information")
        self.setMinimumSize(1250, 950)
        self._worker: _InxiWorker | None = None
        self._text_edit = QTextEdit()
        self._close_btn = QPushButton("Close")
        self._build_ui()
        self._start_loading()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(5, 5, 5, 5)

        self._text_edit.setReadOnly(True)
        self._text_edit.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        self._text_edit.setWordWrapMode(QTextOption.WrapMode.NoWrap)
        self._text_edit.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._text_edit.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)

        font = QFont("Monospace")
        font.setStyleHint(QFont.StyleHint.Monospace)
        font.setFixedPitch(True)
        self._text_edit.setFont(font)
        self._text_edit.setTabStopDistance(4 * QFontMetrics(font).horizontalAdvance(" "))
        self._text_edit.setStyleSheet(get_current_style())
        layout.addWidget(self._text_edit)

        self._close_btn.setStyleSheet(get_current_style())
        self._close_btn.clicked.connect(self.close)
        layout.addWidget(self._close_btn)

    def _start_loading(self) -> None:
        self._text_edit.setPlainText("Loading system information…")
        self._worker = _InxiWorker()
        self._worker.finished.connect(self._on_info_loaded)
        self._worker.start()

    def _on_info_loaded(self, info: str) -> None:
        self._text_edit.setPlainText(info)
        self._close_btn.setFocus()
        self._fit_width_to_content()

    def _fit_width_to_content(self) -> None:
        try:
            metrics = QFontMetrics(self._text_edit.font())
            lines   = self._text_edit.toPlainText().split("\n")
            if not lines:
                return
            max_px  = max((metrics.horizontalAdvance(l) for l in lines), default=0)
            screen  = self.screen()
            if screen is None:
                return
            capped = min(max_px + 100, int(screen.availableGeometry().width() * _MAX_WIDTH_RATIO))
            if capped > self.width():
                self.resize(capped, self.height())
        except Exception as exc:
            logger.debug("Could not auto-fit window width: %s", exc)

    def keyPressEvent(self, event) -> None:
        if event.key() in (Qt.Key.Key_Enter, Qt.Key.Key_Return, Qt.Key.Key_Escape):
            self.close()
        else:
            super().keyPressEvent(event)

    def closeEvent(self, event) -> None:
        if self._worker and self._worker.isRunning():
            self._worker.requestInterruption()
            if not self._worker.wait(_WORKER_WAIT_MS):
                logger.warning("SystemInfoWindow: worker thread did not stop in time — terminating.")
                self._worker.terminate()
                self._worker.wait(500)
        parent = self.parent()
        try:
            if parent:
                parent.show()
        except RuntimeError:
            pass
        super().closeEvent(event)

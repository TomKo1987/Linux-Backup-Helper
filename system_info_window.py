import subprocess
from global_style import get_current_style
from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtGui import QFont, QTextOption, QFontMetrics
from PyQt6.QtWidgets import QDialog, QVBoxLayout, QTextEdit, QPushButton, QLabel

from logging_config import setup_logger
logger = setup_logger(__name__)


# noinspection PyUnresolvedReferences
class InxiWorker(QThread):
    finished = pyqtSignal(str)
    default_args = ['inxi', '-SMCGAz', '--no-host', '--color', '0']

    def run(self):
        try:
            result = subprocess.run(
                self.default_args,
                capture_output=True,
                text=True,
                timeout=10
            )

            if result.returncode == 0 and result.stdout.strip():
                self.finished.emit(result.stdout.strip())
            else:
                error_msg = result.stderr.strip() or "Unknown error while running inxi."
                self.finished.emit(f"Error: {error_msg}")

        except FileNotFoundError:
            self.finished.emit(
                "The tool 'inxi' is not installed on your system.\n\n"
                "To install:\n"
                "Ubuntu/Debian:   sudo apt install inxi\n"
                "Arch/Manjaro:    sudo pacman -S inxi\n"
                "Fedora:          sudo dnf install inxi"
            )
        except subprocess.TimeoutExpired:
            self.finished.emit("Error: Timed out while collecting system information with inxi.")
        except Exception as e:
            self.finished.emit(f"Unexpected error: {e}")


# noinspection PyUnresolvedReferences
class SystemInfoWindow(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.text_edit = QTextEdit()
        self.close_btn = QPushButton("Close")
        self.setWindowTitle("System Information")
        self.setMinimumSize(1250, 950)

        self.worker = None
        self.init_ui()
        self.load_system_info()

    def init_ui(self):
        layout = QVBoxLayout()
        layout.setContentsMargins(5, 5, 5, 5)

        header = QLabel("System Information")
        header_font = QFont()
        header_font.setPointSize(14)
        header_font.setBold(True)
        header.setFont(header_font)
        header.setAlignment(Qt.AlignmentFlag.AlignCenter)
        header.setStyleSheet(get_current_style())
        layout.addWidget(header)

        self.text_edit.setReadOnly(True)
        self.text_edit.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        self.text_edit.setWordWrapMode(QTextOption.WrapMode.NoWrap)

        font = QFont("Monospace")
        font.setStyleHint(QFont.StyleHint.Monospace)
        font.setFixedPitch(True)
        self.text_edit.setFont(font)

        self.text_edit.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.text_edit.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)

        font_metrics = QFontMetrics(font)
        tab_width = 4 * font_metrics.horizontalAdvance(' ')
        self.text_edit.setTabStopDistance(tab_width)

        self.text_edit.setStyleSheet(get_current_style())
        layout.addWidget(self.text_edit)

        self.close_btn.setStyleSheet(get_current_style())
        self.close_btn.clicked.connect(self.close)
        layout.addWidget(self.close_btn)

        self.setLayout(layout)

    def on_info_loaded(self, info):
        self.text_edit.setPlainText(info)
        self.close_btn.setFocus()
        self.adjust_window_width()

    def adjust_window_width(self):
        try:
            font_metrics = QFontMetrics(self.text_edit.font())
            text = self.text_edit.toPlainText()
            lines = text.split('\n')

            if lines:
                max_line_width = max(font_metrics.horizontalAdvance(line) for line in lines)
                optimal_width = max_line_width + 100

                screen = self.screen().availableGeometry()
                max_width = int(screen.width() * 0.9)

                new_width = min(optimal_width, max_width)
                if new_width > self.width():
                    self.resize(new_width, self.height())
        except Exception as e:
            logger.debug(f"Could not adjust window width: {e}")

    def load_system_info(self):
        self.text_edit.setPlainText("Loading system information...")

        self.worker = InxiWorker()
        self.worker.finished.connect(self.on_info_loaded)
        self.worker.start()

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Enter, Qt.Key.Key_Return, Qt.Key.Key_Escape):
            self.close()
        else:
            super().keyPressEvent(event)

    def closeEvent(self, event):
        if self.worker and self.worker.isRunning():
            self.worker.terminate()
            self.worker.wait()

        if self.parent():
            self.parent().show()
        event.accept()

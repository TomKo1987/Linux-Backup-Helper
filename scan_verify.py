from PyQt6.QtCore import Qt, QThread, QTimer
from PyQt6.QtGui import QCloseEvent
from PyQt6.QtWidgets import (
    QApplication, QDialog, QHBoxLayout, QLabel, QPushButton, QTabWidget, QVBoxLayout
)

from linux_distro_helper import LinuxDistroHelper
from state import S, logger
from themes import current_theme, font_sz
from translations import tr
from ui_utils import color_style, _StandardKeysMixin

from scan_verify_capture import _CaptureTab
from scan_verify_verify import _VerifyTab
from scan_verify_packagediff import _PackageDiffTab


class ScanVerifyDialog(_StandardKeysMixin, QDialog):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(tr("Scan & Verify"))
        self._helper = LinuxDistroHelper()
        self._build_ui()
        self._size_to_screen()

    def closeEvent(self, a0: QCloseEvent | None) -> None:
        for tab in (getattr(self, "_capture_tab", None), getattr(self, "_verify_tab", None),
                   getattr(self, "_packagediff_tab", None)):
            if tab is not None:
                worker = getattr(tab, "_worker", None)
                if isinstance(worker, QThread) and worker.isRunning():
                    if not worker.wait(5000):
                        logger.warning("ScanVerifyDialog: worker did not finish within timeout on close "
                                      "— detaching its signals so a late result cannot reach destroyed widgets")
                        self._detach_worker_signals(worker)
        super().closeEvent(a0)

    @staticmethod
    def _detach_worker_signals(worker: QThread) -> None:
        for sig_name in ("progress", "done", "result_ready", "all_done", "finished"):
            sig = getattr(worker, sig_name, None)
            if sig is None:
                continue
            try:
                sig.disconnect()  # type: ignore[union-attr]
            except TypeError:
                pass

    _MIN_W, _MIN_H = 1250, 850

    def _clamped_size(self, want_w: int, want_h: int) -> "tuple[int, int] | None":
        scr = QApplication.primaryScreen()
        if not scr:
            return None
        sg = scr.availableGeometry()
        w = min(max(want_w, self._MIN_W), sg.width() - 60)
        h = min(max(want_h, self._MIN_H), sg.height() - 60)
        return w, h

    def _size_to_screen(self) -> None:
        scr = QApplication.primaryScreen()
        size = self._clamped_size(scr.availableGeometry().width() * 2 // 3,
                                  scr.availableGeometry().height() * 3 // 4) if scr else None
        self.resize(*size) if size else self.resize(950, 640)

    def fit_to_content(self) -> None:
        def _do_resize() -> None:
            if not self.isVisible():
                return
            size = self._clamped_size(self.width(), self.height())
            if size:
                self.resize(*size)

        QTimer.singleShot(60, _do_resize)

    def _build_ui(self) -> None:
        t = current_theme()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.setSpacing(8)

        info = QLabel(f"<b>{self._helper.distro_pretty_name}</b>  •  "
                      f"{tr('Package manager:')} <b>{self._helper.pkg_manager_name()}</b>  •  "
                      f"{tr('Profile:')} <b>{S.profile_name or tr('none')}</b>")
        info.setTextFormat(Qt.TextFormat.RichText)
        info.setStyleSheet(color_style(t['muted'], font_sz(-1)))
        lay.addWidget(info)

        tabs = QTabWidget()
        self._capture_tab = _CaptureTab(self._helper)
        self._verify_tab = _VerifyTab(self._helper)
        self._packagediff_tab = _PackageDiffTab(self._helper)
        tabs.addTab(self._capture_tab, f"🔍  {tr('System Scan')}")
        tabs.addTab(self._verify_tab, f"✅  {tr('Verify Profile')}")
        tabs.addTab(self._packagediff_tab, f"📦  {tr('Package Diff')}")
        lay.addWidget(tabs, 1)

        close_btn = QPushButton(tr("Close"))
        close_btn.clicked.connect(self.accept)
        row = QHBoxLayout()
        row.addStretch()
        row.addWidget(close_btn)
        lay.addLayout(row)

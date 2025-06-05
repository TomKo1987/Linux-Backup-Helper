global_style = """
QDialog, QWidget {
    background-color: #232534;
    border-radius: 16px;
    border: 1px solid #2d2d44;
}
QWidget {
    color: #c0caf5;
    font-family: "Noto Sans Mono", "DejaVu Sans Mono", "Courier New", monospace;
    font-size: 16px;
}
QPushButton, QCheckBox {
    border-radius: 10px;
    outline: none;
    text-align: center;
    padding: 5px;
    font-size: 15px;
    font: bold;
    color: #ffffff;
}
QPushButton {
    border: 2px solid #7dcfff;
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #24283b, stop:1 #414868);
}
QPushButton:enabled:hover, QPushButton:enabled:focus {
    border: 2px solid #ffff00;
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #3a425f, stop:1 #606d9c);
    color: #55ffff;
}
QPushButton:disabled {
    border: 2px solid #444;
    color: #616161;
    background-color: #282828;
}
QCheckBox {
    background-color: transparent;
}
QCheckBox:enabled, QCheckBox:disabled {
    border: 2px solid transparent;
}
QCheckBox:enabled:hover, QCheckBox:enabled:focus {
    border: 2px solid #ffff00;
    color: #55ffff;
}
QCheckBox:disabled {
    color: #616161;
}
QCheckBox:indicator {
    width: 8px;
    height: 8px;
    border-radius: 4px;
}
QCheckBox:indicator:checked {
    border: 1px solid #55ff00;
    background-color: #55ff00;
}
QCheckBox:indicator:unchecked {
    border: 1px solid #f7768e;
    background-color: #f7768e;
}
QCheckBox:indicator:indeterminate {
    border: 1px solid #7aa2f7;
    background-color: transparent;
}
QLabel {
    color: #c0caf5;
    border: none;
    border-radius: 2px;
    padding: 5px;
    font-size: 17px;
    qproperty-alignment: 'AlignCenter';
    font-family: "CaskaydiaCove Nerd Font", "Noto Sans", "DejaVu Sans", "Arial", sans-serif;
}
QWidget[class="container"] {
    background-color: #222;
    border-radius: 5px;
}
QLineEdit {
    background-color: #555582;
    color: #aaff00;
    padding: 5px 5px;
    border-radius: 8px;
    font-size: 16px;
}
QTextEdit, QListWidget {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #293147, stop:1 #4d5a83);
    color: #aaff00;
    border: none;
    padding: 5px 5px;
    border-radius: 8px;
    font-size: 16px;
    font-family: "FiraCode Nerd Font Mono", "DejaVu Sans Mono", "Courier New", monospace;
}
QListWidget::item {
    padding: 5px;
    border-radius: 5px;
    border: 1px solid transparent;
}
QScrollBar:vertical {
    border: none;
    background: #2d2d44;
    width: 14px;
    margin: 15px 0;
}
QScrollBar::handle:vertical {
    background-color: #4f4f78;
    min-height: 30px;
    border-radius: 7px;
}
QScrollBar::handle:vertical:hover,
QScrollBar::handle:vertical:pressed {
    background-color: #9090dc;
}
QScrollBar::sub-line:vertical,
QScrollBar::add-line:vertical {
    border: none;
    background-color: #3b3b5a;
    height: 15px;
    border-radius: 7px;
    subcontrol-origin: margin;
}
QScrollBar::sub-line:vertical {
    border-top-left-radius: 7px;
    border-top-right-radius: 7px;
    subcontrol-position: top;
}
QScrollBar::add-line:vertical {
    border-bottom-left-radius: 7px;
    border-bottom-right-radius: 7px;
    subcontrol-position: bottom;
}
QScrollBar::sub-line:vertical:hover,
QScrollBar::sub-line:vertical:pressed,
QScrollBar::add-line:vertical:hover {
    background-color: #00ddff;
}
QScrollBar::add-line:vertical:pressed {
    background-color: #b9005c;
}
QScrollBar::up-arrow:vertical,
QScrollBar::down-arrow:vertical,
QScrollBar::add-page:vertical,
QScrollBar::sub-page:vertical {
    background: none;
}
QProgressBar {
    background-color: #6d7582;
    border-radius: 8px;
    border: 3px solid #7aa2f7;
    height: 25px;
    text-align: center;
    margin: 10px 0;
    font-weight: bold;
    font-size: 18px;
    color: #000000;
}
QProgressBar::chunk {
    background-color: qlineargradient(spread:pad, x1:0, y1:0, x2:1, y2:0, stop:0 #7dcfff, stop:1 #6689cf);
    border-radius: 7px;
}
QTabBar::tab {
    background-color: #24283b;
    font-size: 16px;
    padding: 8px 12px;
    border: 1px solid #414868;
    border-bottom: none;
    border-top-left-radius: 6px;
    border-top-right-radius: 6px;
    font-family: "FiraCode Nerd Font Mono", "DejaVu Sans Mono", "Courier New", monospace;
}
QTabBar::tab:selected {
    background-color: #1e1e2e;
    border-bottom: none;
}
"""

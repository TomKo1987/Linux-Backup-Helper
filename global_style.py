from PyQt6.QtWidgets import QApplication, QDialog, QWidget

THEMES = {
    "Tokyo Night": """
QDialog, QWidget {
    border-radius: 16px;
    background-color: #232534;
    border: 1px solid #2d2d44;
}
QWidget {
    color: #c0caf5;
}
QPushButton, QCheckBox {
    border-radius: 10px;
    outline: none;
    text-align: center;
    padding: 3px;
    font-weight: bold;
    color: #ffffff;
}
QPushButton {
    border: 2px solid #7dcfff;
    height: 23px;
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #24283b, stop:1 #414868);
}
QPushButton:enabled:hover, QPushButton:enabled:focus {
    border: 2px solid #ffff00;
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #3a425f, stop:1 #606d9c);
    color: #55ffff;
    outline: none;
}
QPushButton:disabled {
    border: 2px solid #444;
    color: #616161;
    background-color: #282828;
}
QCheckBox {
    background-color: transparent;
    border: 2px solid transparent;
}
QCheckBox:enabled:hover, QCheckBox:enabled:focus {
    border: 2px solid #ffff00;
    color: #55ffff;
    outline: none;
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
    background-color: #00ffbf;
    border: 1px solid #98971a;
}
QCheckBox:indicator:unchecked {
    background-color: #cc241d;
    border: 1px solid #9d0006;
}
QCheckBox:indicator:indeterminate {
    background-color: transparent;
    border: 1px solid #00ffbf;
}
QLabel {
    color: #c0caf5;
    border: none;
    border-radius: 2px;
    padding: 5px;
    qproperty-alignment: 'AlignCenter';
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
}
QTextEdit, QListWidget {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #293147, stop:1 #4d5a83);
    color: #aaff00;
    border: none;
    padding: 2px 2px;
    border-radius: 8px;
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
    height: 22.5px;
    text-align: center;
    margin: 10px 0;
    font-weight: bold;
    color: #000000;
}
QProgressBar::chunk {
    background-color: qlineargradient(spread:pad, x1:0, y1:0, x2:1, y2:0, stop:0 #7dcfff, stop:1 #6689cf);
    border-radius: 7px;
}
QTabBar::tab {
    background-color: #24283b;
    padding: 8px 12px;
    border: 1px solid #414868;
    border-bottom: none;
    border-top-left-radius: 6px;
    border-top-right-radius: 6px;
}
QTabBar::tab:selected {
    background-color: #1e1e2e;
    border-bottom: none;
}
""",

    "Dark Mode": """
QDialog, QWidget {
    border-radius: 16px;
    background-color: #1a1a1a;
    border: 1px solid #333;
}
QWidget {
    color: #e0e0e0;
}
QPushButton, QCheckBox {
    border-radius: 10px;
    outline: none;
    text-align: center;
    padding: 3px;
    font-weight: bold;
    color: #ffffff;
}
QPushButton {
    border: 2px solid #666;
    height: 23px;
    background-color: #2b2b2b;
}
QPushButton:enabled:hover, QPushButton:enabled:focus {
    border: 2px solid #888;
    background-color: #505050;
    color: #ffffff;
    outline: none;
}
QPushButton:disabled {
    border: 2px solid #333;
    color: #666;
    background-color: #1a1a1a;
}
QCheckBox {
    background-color: transparent;
    border: 2px solid transparent;
}
QCheckBox:enabled:hover, QCheckBox:enabled:focus {
    border: 2px solid #888;
    color: #ffffff;
    outline: none;
}
QCheckBox:disabled {
    color: #666;
}
QCheckBox:indicator {
    width: 8px;
    height: 8px;
    border-radius: 4px;
}
QCheckBox:indicator:checked {
    background-color: #00ffbf;
    border: 1px solid #98971a;
}
QCheckBox:indicator:unchecked {
    background-color: #cc241d;
    border: 1px solid #9d0006;
}
QCheckBox:indicator:indeterminate {
    background-color: transparent;
    border: 1px solid #00ffbf;
}
QLabel {
    color: #e0e0e0;
    border: none;
    border-radius: 2px;
    padding: 5px;
    qproperty-alignment: 'AlignCenter';
}
QLineEdit {
    background-color: #2b2b2b;
    color: #e0e0e0;
    padding: 5px 5px;
    border-radius: 4px;
    border: 1px solid #444;
}
QTextEdit, QListWidget {
    background-color: #2b2b2b;
    color: #e0e0e0;
    border: none;
    padding: 2px 2px;
    border-radius: 8px;
}
QScrollBar:vertical {
    border: none;
    background: #2b2b2b;
    width: 12px;
}
QScrollBar::handle:vertical {
    background-color: #555;
    min-height: 30px;
    border-radius: 6px;
}
QScrollBar::handle:vertical:hover {
    background-color: #666;
}
QProgressBar {
    background-color: #2b2b2b;
    border-radius: 4px;
    border: 1px solid #444;
    height: 22.5px;
    text-align: center;
    margin: 10px 0;
    font-weight: bold;
    color: #000000;
}
QProgressBar::chunk {
    background-color: #4a9eff;
    border-radius: 3px;
}
""",

    "Light Mode": """
QDialog, QWidget {
    border-radius: 16px;
    background-color: #f5f5f5;
    border: 1px solid #d0d0d0;
}
QWidget {
    color: #333333;
}
QPushButton, QCheckBox {
    border-radius: 10px;
    outline: none;
    text-align: center;
    padding: 3px;
    font-weight: bold;
    color: #333333;
}
QPushButton {
    border: 2px solid #b0b0b0;
    height: 23px;
    background-color: #ffffff;
}
QPushButton:enabled:hover, QPushButton:enabled:focus {
    border: 2px solid #0078d4;
    background-color: #e1f5fe;
    color: #0078d4;
    outline: none;
}
QPushButton:disabled {
    border: 2px solid #e0e0e0;
    color: #999999;
    background-color: #f0f0f0;
}
QCheckBox {
    background-color: transparent;
    border: 2px solid transparent;
}
QCheckBox:enabled:hover, QCheckBox:enabled:focus {
    border: 2px solid #0078d4;
    color: #0078d4;
    outline: none;
}
QCheckBox:disabled {
    color: #999999;
}
QCheckBox:indicator {
    width: 8px;
    height: 8px;
    border-radius: 4px;
}
QCheckBox:indicator:checked {
    background-color: #00ffbf;
    border: 1px solid #98971a;
}
QCheckBox:indicator:unchecked {
    background-color: #cc241d;
    border: 1px solid #9d0006;
}
QCheckBox:indicator:indeterminate {
    background-color: transparent;
    border: 1px solid #00ffbf;
}
QLabel {
    color: #333333;
    border: none;
    border-radius: 2px;
    padding: 5px;
    qproperty-alignment: 'AlignCenter';
}
QLineEdit {
    background-color: #ffffff;
    color: #333333;
    padding: 5px 5px;
    border-radius: 4px;
    border: 1px solid #d0d0d0;
}
QTextEdit, QListWidget {
    background-color: #ffffff;
    color: #333333;
    border: none;
    padding: 2px 2px;
    border-radius: 8px;
}
QScrollBar:vertical {
    border: none;
    background: #f0f0f0;
    width: 12px;
}
QScrollBar::handle:vertical {
    background-color: #c0c0c0;
    min-height: 30px;
    border-radius: 6px;
}
QScrollBar::handle:vertical:hover {
    background-color: #a0a0a0;
}
QProgressBar {
    background-color: #e0e0e0;
    border-radius: 4px;
    border: 1px solid #d0d0d0;
    height: 22.5px;
    text-align: center;
    margin: 10px 0;
    font-weight: bold;
    color: #000000;
}
QProgressBar::chunk {
    background-color: #0078d4;
    border-radius: 3px;
}
""",

    "Solarized Dark": """
QDialog, QWidget {
    border-radius: 16px;
    background-color: #002b36;
    border: 1px solid #073642;
}
QWidget {
    color: #839496;
}
QPushButton, QCheckBox {
    border-radius: 10px;
    outline: none;
    text-align: center;
    padding: 3px;
    font-weight: bold;
    color: #fdf6e3;
}
QPushButton {
    border: 2px solid #268bd2;
    height: 23px;
    background-color: #073642;
}
QPushButton:enabled:hover, QPushButton:enabled:focus {
    border: 2px solid #b58900;
    background-color: #586e75;
    color: #fdf6e3;
    outline: none;
}
QPushButton:disabled {
    border: 2px solid #333;
    color: #657b83;
    background-color: #002b36;
}
QCheckBox {
    background-color: transparent;
    border: 2px solid transparent;
}
QCheckBox:enabled:hover, QCheckBox:enabled:focus {
    border: 2px solid #b58900;
    color: #fdf6e3;
    outline: none;
}
QCheckBox:disabled {
    color: #586e75;
}
QCheckBox:indicator {
    width: 8px;
    height: 8px;
    border-radius: 4px;
}
QCheckBox:indicator:checked {
    background-color: #00ffbf;
    border: 1px solid #98971a;
}
QCheckBox:indicator:unchecked {
    background-color: #cc241d;
    border: 1px solid #9d0006;
}
QCheckBox:indicator:indeterminate {
    background-color: transparent;
    border: 1px solid #00ffbf;
}
QLabel {
    color: #93a1a1;
    border: none;
    border-radius: 2px;
    padding: 5px;
    qproperty-alignment: 'AlignCenter';
}
QLineEdit {
    background-color: #073642;
    color: #eee8d5;
    padding: 5px;
    border-radius: 6px;
}
QTextEdit, QListWidget {
    background-color: #073642;
    color: #93a1a1;
    border: none;
    padding: 2px 2px;
    border-radius: 8px;
}
QScrollBar:vertical {
    background: #002b36;
    width: 12px;
}
QScrollBar::handle:vertical {
    background-color: #586e75;
    border-radius: 6px;
}
QScrollBar::handle:vertical:hover {
    background-color: #93a1a1;
}
QProgressBar {
    background-color: #073642;
    border: 1px solid #268bd2;
    border-radius: 6px;
    height: 22.5px;
    text-align: center;
    margin: 10px 0;
    font-weight: bold;
    color: #000000;
}
QProgressBar::chunk {
    background-color: #268bd2;
    border-radius: 4px;
}
""",

    "Dracula": """
QDialog, QWidget {
    border-radius: 16px;
    background-color: #282a36;
    border: 1px solid #44475a;
}
QWidget {
    color: #f8f8f2;
}
QPushButton, QCheckBox {
    border-radius: 10px;
    outline: none;
    text-align: center;
    padding: 3px;
    font-weight: bold;
    color: #f8f8f2;
}
QPushButton {
    border: 2px solid #bd93f9;
    height: 23px;
    background-color: #44475a;
}
QPushButton:enabled:hover, QPushButton:enabled:focus {
    border: 2px solid #ff79c6;
    background-color: #6272a4;
    color: #50fa7b;
    outline: none;
}
QPushButton:disabled {
    border: 2px solid #444;
    color: #6272a4;
    background-color: #1e1f29;
}
QCheckBox {
    background-color: transparent;
    border: 2px solid transparent;
}
QCheckBox:enabled:hover, QCheckBox:enabled:focus {
    border: 2px solid #ff79c6;
    color: #50fa7b;
    outline: none;
}
QCheckBox:disabled {
    color: #6272a4;
}
QCheckBox:indicator {
    width: 8px;
    height: 8px;
    border-radius: 4px;
}
QCheckBox:indicator:checked {
    background-color: #00ffbf;
    border: 1px solid #98971a;
}
QCheckBox:indicator:unchecked {
    background-color: #cc241d;
    border: 1px solid #9d0006;
}
QCheckBox:indicator:indeterminate {
    background-color: transparent;
    border: 1px solid #00ffbf;
}
QLabel {
    color: #f8f8f2;
    border: none;
    border-radius: 2px;
    padding: 5px;
    qproperty-alignment: 'AlignCenter';
}
QLineEdit {
    background-color: #44475a;
    color: #f8f8f2;
    padding: 5px;
    border-radius: 6px;
}
QTextEdit, QListWidget {
    background-color: #44475a;
    color: #f8f8f2;
    border: none;
    padding: 2px 2px;
    border-radius: 8px;
}
QScrollBar:vertical {
    background: #282a36;
    width: 12px;
}
QScrollBar::handle:vertical {
    background-color: #6272a4;
    border-radius: 6px;
}
QScrollBar::handle:vertical:hover {
    background-color: #bd93f9;
}
QProgressBar {
    background-color: #44475a;
    border: 1px solid #bd93f9;
    border-radius: 6px;
    height: 22.5px;
    text-align: center;
    margin: 10px 0;
    font-weight: bold;
    color: #000000;
}
QProgressBar::chunk {
    background-color: #50fa7b;
    border-radius: 4px;
}
""",

    "Gruvbox Dark": """
QDialog, QWidget {
    border-radius: 16px;
    background-color: #282828;
    border: 1px solid #3c3836;
}
QWidget {
    color: #ebdbb2;
}
QPushButton, QCheckBox {
    border-radius: 10px;
    outline: none;
    text-align: center;
    padding: 3px;
    font-weight: bold;
    color: #fbf1c7;
}
QPushButton {
    border: 2px solid #7c7e1a;
    height: 23px;
    background-color: #3c3836;
}
QPushButton:enabled:hover, QPushButton:enabled:focus {
    background-color: #504945;
    color: #fabd2f;
    border: 2px solid #fcff34;
    outline: none;
}
QPushButton:disabled {
    color: #928374;
    background-color: #282828;
    border: 2px solid #444;
}
QCheckBox {
    background-color: transparent;
    border: 2px solid transparent;
}
QCheckBox:enabled:hover, QCheckBox:enabled:focus {
    color: #fabd2f;
    border: 2px solid #fcff34;
    outline: none;
}
QCheckBox:disabled {
    color: #928374;
}
QCheckBox:indicator {
    width: 8px;
    height: 8px;
    border-radius: 4px;
}
QCheckBox:indicator:checked {
    background-color: #00ffbf;
    border: 1px solid #98971a;
}
QCheckBox:indicator:unchecked {
    background-color: #cc241d;
    border: 1px solid #9d0006;
}
QCheckBox:indicator:indeterminate {
    background-color: transparent;
    border: 1px solid #00ffbf;
}
QLabel {
    color: #ebdbb2;
    border: none;
    border-radius: 2px;
    padding: 5px;
    qproperty-alignment: 'AlignCenter';
}
QLineEdit, QTextEdit, QListWidget {
    background-color: #3c3836;
    color: #ebdbb2;
    border: none;
    padding: 2px 2px;
    border-radius: 8px;
}
QScrollBar:vertical {
    background: #3c3836;
    width: 12px;
}
QScrollBar::handle:vertical {
    background-color: #665c54;
    border-radius: 6px;
}
QScrollBar::handle:vertical:hover {
    background-color: #fabd2f;
}
QProgressBar {
    background-color: #504945;
    border-radius: 6px;
    border: 1px solid #b8bb26;
    height: 22.5px;
    text-align: center;
    margin: 10px 0;
    font-weight: bold;
    color: #000000;
}
QProgressBar::chunk {
    background-color: #b8bb26;
    border-radius: 4px;
}
""",

    "Nord": """
QDialog, QWidget {
    border-radius: 16px;
    background-color: #2e3440;
    border: 1px solid #4c566a;
}
QWidget {
    color: #d8dee9;
}
QPushButton, QCheckBox {
    border-radius: 10px;
    outline: none;
    text-align: center;
    padding: 3px;
    font-weight: bold;
    color: #eceff4;
}
QPushButton {
    border: 2px solid #88c0d0;
    height: 23px;
    background-color: #3b4252;
}
QPushButton:enabled:hover, QPushButton:enabled:focus {
    background-color: #5a657d;
    border: 2px solid #85c199;
    color: #aaff00;
    outline: none;
}
QPushButton:disabled {
    color: #616e88;
    background-color: #2e3440;
    border: 2px solid #4c566a;
}
QCheckBox {
    background-color: transparent;
    border: 2px solid transparent;
}
QCheckBox:enabled:hover, QCheckBox:enabled:focus {
    color: #aaff00;
    border: 2px solid #85c199;
    outline: none;
}
QCheckBox:disabled {
    color: #616e88;
}
QCheckBox:indicator {
    width: 8px;
    height: 8px;
    border-radius: 4px;
}
QCheckBox:indicator:checked {
    background-color: #00ffbf;
    border: 1px solid #98971a;
}
QCheckBox:indicator:unchecked {
    background-color: #cc241d;
    border: 1px solid #9d0006;
}
QCheckBox:indicator:indeterminate {
    background-color: transparent;
    border: 1px solid #00ffbf;
}
QLabel {
    color: #d8dee9;
    border: none;
    border-radius: 2px;
    padding: 5px;
    qproperty-alignment: 'AlignCenter';
}
QLineEdit, QTextEdit, QListWidget {
    background-color: #3b4252;
    color: #eceff4;
    border: none;
    padding: 2px 2px;
    border-radius: 8px;
}
QScrollBar:vertical {
    background: #2e3440;
    width: 12px;
}
QScrollBar::handle:vertical {
    background-color: #4c566a;
    border-radius: 6px;
}
QScrollBar::handle:vertical:hover {
    background-color: #88c0d0;
}
QProgressBar {
    background-color: #3b4252;
    border-radius: 6px;
    border: 1px solid #88c0d0;
    height: 22.5px;
    text-align: center;
    margin: 10px 0;
    font-weight: bold;
    color: #000000;
}
QProgressBar::chunk {
    background-color: #81a1c1;
    border-radius: 4px;
}
""",

    "Catppuccin Mocha": """
QDialog, QWidget {
    border-radius: 16px;
    background-color: #1e1e2e;
    border: 1px solid #313244;
}
QWidget {
    color: #cdd6f4;
}
QPushButton, QCheckBox {
    border-radius: 10px;
    outline: none;
    text-align: center;
    padding: 3px;
    font-weight: bold;
    color: #cdd6f4;
}
QPushButton {
    border: 2px solid #89b4fa;
    height: 23px;
    background-color: #313244;
}
QPushButton:enabled:hover, QPushButton:enabled:focus {
    background-color: #45475a;
    border-color: #f5c2e7;
    color: #00ffc8;
    outline: none;
}
QPushButton:disabled {
    color: #6c7086;
    background-color: #1e1e2e;
    border: 2px solid #313244;
}
QCheckBox {
    background-color: transparent;
    border: 2px solid transparent;
}
QCheckBox:enabled:hover, QCheckBox:enabled:focus {
    color: #00ffc8;
    border: 2px solid #f5c2e7;
    outline: none;
}
QCheckBox:disabled {
    color: #6c7086;
}
QCheckBox:indicator {
    width: 8px;
    height: 8px;
    border-radius: 4px;
}
QCheckBox:indicator:checked {
    background-color: #00ffbf;
    border: 1px solid #98971a;
}
QCheckBox:indicator:unchecked {
    background-color: #cc241d;
    border: 1px solid #9d0006;
}
QCheckBox:indicator:indeterminate {
    background-color: transparent;
    border: 1px solid #00ffbf;
}
QLabel {
    color: #cdd6f4;
    border: none;
    border-radius: 2px;
    padding: 5px;
    qproperty-alignment: 'AlignCenter';
}
QLineEdit, QTextEdit, QListWidget {
    background-color: #313244;
    color: #cdd6f4;
    border: none;
    padding: 2px 2px;
    border-radius: 8px;
}
QScrollBar:vertical {
    background: #1e1e2e;
    width: 12px;
}
QScrollBar::handle:vertical {
    background-color: #6c7086;
    border-radius: 6px;
}
QScrollBar::handle:vertical:hover {
    background-color: #89b4fa;
}
QProgressBar {
    background-color: #313244;
    border-radius: 6px;
    border: 1px solid #89b4fa;
    height: 22.5px;
    text-align: center;
    margin: 10px 0;
    font-weight: bold;
    color: #000000;
}
QProgressBar::chunk {
    background-color: #89b4fa;
    border-radius: 4px;
}
""",

    "Monokai Pro": """
QDialog, QWidget {
    border-radius: 16px;
    background-color: #2d2a2e;
    border: 1px solid #403e41;
}
QWidget {
    color: #f8f8f2;
}
QPushButton, QCheckBox {
    border-radius: 10px;
    outline: none;
    text-align: center;
    padding: 3px;
    font-weight: bold;
    color: #f8f8f2;
}
QPushButton {
    border: 2px solid #66d9ef;
    height: 23px;
    background-color: #403e41;
}
QPushButton:enabled:hover, QPushButton:enabled:focus {
    background-color: #413f34;
    border-color: #a6e22e;
    color: #a6e22e;
    outline: none;
}
QPushButton:disabled {
    color: #75715e;
    background-color: #2d2a2e;
    border: 2px solid #403e41;
}
QCheckBox {
    background-color: transparent;
    border: 2px solid transparent;
}
QCheckBox:enabled:hover, QCheckBox:enabled:focus {
    color: #a6e22e;
    border: 2px solid #a6e22e;
    outline: none;
}
QCheckBox:disabled {
    color: #75715e;
}
QCheckBox:indicator {
    width: 8px;
    height: 8px;
    border-radius: 4px;
}
QCheckBox:indicator:checked {
    background-color: #00ffbf;
    border: 1px solid #98971a;
}
QCheckBox:indicator:unchecked {
    background-color: #cc241d;
    border: 1px solid #9d0006;
}
QCheckBox:indicator:indeterminate {
    background-color: transparent;
    border: 1px solid #00ffbf;
}
QLabel {
    color: #f8f8f2;
    border: none;
    border-radius: 2px;
    padding: 5px;
    qproperty-alignment: 'AlignCenter';
}
QLineEdit, QTextEdit, QListWidget {
    background-color: #403e41;
    color: #f8f8f2;
    border: none;
    padding: 2px 2px;
    border-radius: 8px;
}
QScrollBar:vertical {
    background: #2d2a2e;
    width: 12px;
}
QScrollBar::handle:vertical {
    background-color: #75715e;
    border-radius: 6px;
}
QScrollBar::handle:vertical:hover {
    background-color: #a6e22e;
}
QProgressBar {
    background-color: #403e41;
    border-radius: 6px;
    border: 1px solid #66d9ef;
    height: 22.5px;
    text-align: center;
    margin: 10px 0;
    font-weight: bold;
    color: #000000;
}
QProgressBar::chunk {
    background-color: #66d9ef;
    border-radius: 4px;
}
"""
}

current_theme = "Tokyo Night"

def get_current_style():
    base_style = THEMES.get(current_theme, THEMES["Tokyo Night"])
    try:
        from options import Options
        font_family = Options.ui_settings.get("font_family", "DejaVu Sans")
        font_size = Options.ui_settings.get("font_size", 14)
        font_style = f"""
* {{
    font-family: "{font_family}";
    font-size: {font_size}px;
}}
QPushButton, QCheckBox, QLabel, QProgressBar, QTabBar::tab {{
    font-size: {font_size}px;
}}
"""
        return base_style + font_style
    except ImportError:
        return base_style

def apply_theme_to_widget(widget, theme_name=None):
    if theme_name is None:
        theme_name = current_theme
    if theme_name in THEMES:
        style = get_current_style()
        widget.setStyleSheet(style)
        for child in widget.findChildren(QWidget):
            child.style().unpolish(child)
            child.style().polish(child)

def refresh_all_windows():
    current_style = get_current_style()
    for widget in QApplication.topLevelWidgets():
        if isinstance(widget, (QDialog, QWidget)) and widget.isVisible():
            widget.setStyleSheet(current_style)
            widget.update()

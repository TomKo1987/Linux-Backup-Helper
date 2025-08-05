THEMES = {
    "Tokyo Night": """
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
    font-size: 16px;
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
""",

    "Dark Mode": """
QDialog, QWidget {
    background-color: #1a1a1a;
    border-radius: 8px;
    border: 1px solid #333;
}
QWidget {
    color: #e0e0e0;
    font-family: "Noto Sans Mono", "DejaVu Sans Mono", "Courier New", monospace;
    font-size: 16px;
}
QPushButton, QCheckBox {
    border-radius: 6px;
    outline: none;
    text-align: center;
    padding: 5px;
    font-size: 16px;
    font: bold;
    color: #ffffff;
}
QPushButton {
    border: 2px solid #666;
    background-color: #2b2b2b;
}
QPushButton:enabled:hover, QPushButton:enabled:focus {
    border: 2px solid #888;
    background-color: #3a3a3a;
    color: #ffffff;
}
QPushButton:disabled {
    border: 2px solid #333;
    color: #666;
    background-color: #1a1a1a;
}
QCheckBox {
    background-color: transparent;
}
QCheckBox:enabled:hover, QCheckBox:enabled:focus {
    border: 2px solid #888;
    color: #ffffff;
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
    border: 1px solid #4a9eff;
    background-color: #4a9eff;
}
QCheckBox:indicator:unchecked {
    border: 1px solid #666;
    background-color: #333;
}
QLabel {
    color: #e0e0e0;
    border: none;
    padding: 5px;
    font-size: 17px;
    qproperty-alignment: 'AlignCenter';
}
QLineEdit {
    background-color: #2b2b2b;
    color: #e0e0e0;
    padding: 5px 5px;
    border-radius: 4px;
    border: 1px solid #444;
    font-size: 16px;
}
QTextEdit, QListWidget {
    background-color: #2b2b2b;
    color: #e0e0e0;
    border: 1px solid #444;
    padding: 5px 5px;
    border-radius: 4px;
    font-size: 16px;
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
    height: 25px;
    text-align: center;
}
QProgressBar::chunk {
    background-color: #4a9eff;
    border-radius: 3px;
}
""",

    "Light Mode": """
QDialog, QWidget {
    background-color: #f5f5f5;
    border-radius: 8px;
    border: 1px solid #d0d0d0;
}
QWidget {
    color: #333333;
    font-family: "Noto Sans Mono", "DejaVu Sans Mono", "Courier New", monospace;
    font-size: 16px;
}
QPushButton, QCheckBox {
    border-radius: 6px;
    outline: none;
    text-align: center;
    padding: 5px;
    font-size: 16px;
    font: bold;
    color: #333333;
}
QPushButton {
    border: 2px solid #b0b0b0;
    background-color: #ffffff;
}
QPushButton:enabled:hover, QPushButton:enabled:focus {
    border: 2px solid #0078d4;
    background-color: #e1f5fe;
    color: #0078d4;
}
QPushButton:disabled {
    border: 2px solid #e0e0e0;
    color: #999999;
    background-color: #f0f0f0;
}
QCheckBox {
    background-color: transparent;
}
QCheckBox:enabled:hover, QCheckBox:enabled:focus {
    border: 2px solid #0078d4;
    color: #0078d4;
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
    border: 1px solid #0078d4;
    background-color: #0078d4;
}
QCheckBox:indicator:unchecked {
    border: 1px solid #999999;
    background-color: #ffffff;
}
QLabel {
    color: #333333;
    border: none;
    padding: 5px;
    font-size: 17px;
    qproperty-alignment: 'AlignCenter';
}
QLineEdit {
    background-color: #ffffff;
    color: #333333;
    padding: 5px 5px;
    border-radius: 4px;
    border: 1px solid #d0d0d0;
    font-size: 16px;
}
QTextEdit, QListWidget {
    background-color: #ffffff;
    color: #333333;
    border: 1px solid #d0d0d0;
    padding: 5px 5px;
    border-radius: 4px;
    font-size: 16px;
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
    height: 25px;
    text-align: center;
}
QProgressBar::chunk {
    background-color: #0078d4;
    border-radius: 3px;
}
""",
    "Solarized Dark": """
QDialog, QWidget {
    background-color: #002b36;
    border-radius: 10px;
    border: 1px solid #073642;
}
QWidget {
    color: #839496;
    font-family: "Noto Sans Mono", "DejaVu Sans Mono", "Courier New", monospace;
    font-size: 16px;
}
QPushButton, QCheckBox {
    border-radius: 8px;
    padding: 5px;
    font-size: 16px;
    font: bold;
    color: #fdf6e3;
}
QPushButton {
    border: 2px solid #268bd2;
    background-color: #073642;
}
QPushButton:enabled:hover, QPushButton:enabled:focus {
    border: 2px solid #b58900;
    background-color: #586e75;
    color: #fdf6e3;
}
QPushButton:disabled {
    border: 2px solid #333;
    color: #657b83;
    background-color: #002b36;
}
QCheckBox:enabled:hover, QCheckBox:enabled:focus {
    border: 2px solid #b58900;
    color: #fdf6e3;
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
    border: 1px solid #859900;
    background-color: #859900;
}
QCheckBox:indicator:unchecked {
    border: 1px solid #dc322f;
    background-color: #dc322f;
}
QLabel {
    color: #93a1a1;
    padding: 5px;
    font-size: 17px;
    qproperty-alignment: 'AlignCenter';
}
QLineEdit {
    background-color: #073642;
    color: #eee8d5;
    padding: 5px;
    border-radius: 6px;
    font-size: 16px;
}
QTextEdit, QListWidget {
    background-color: #073642;
    color: #93a1a1;
    border: 1px solid #586e75;
    padding: 5px;
    border-radius: 6px;
    font-size: 16px;
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
    height: 25px;
    text-align: center;
    font-size: 16px;
}
QProgressBar::chunk {
    background-color: #268bd2;
    border-radius: 4px;
}
""",

    "Dracula": """
QDialog, QWidget {
    background-color: #282a36;
    border-radius: 10px;
    border: 1px solid #44475a;
}
QWidget {
    color: #f8f8f2;
    font-family: "JetBrains Mono", "FiraCode Nerd Font", "Courier New", monospace;
    font-size: 16px;
}
QPushButton, QCheckBox {
    border-radius: 6px;
    padding: 5px;
    font-size: 16px;
    font: bold;
    color: #f8f8f2;
}
QPushButton {
    border: 2px solid #bd93f9;
    background-color: #44475a;
}
QPushButton:enabled:hover, QPushButton:enabled:focus {
    border: 2px solid #ff79c6;
    background-color: #6272a4;
    color: #50fa7b;
}
QPushButton:disabled {
    border: 2px solid #444;
    color: #6272a4;
    background-color: #1e1f29;
}
QCheckBox:enabled:hover, QCheckBox:enabled:focus {
    border: 2px solid #ff79c6;
    color: #50fa7b;
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
    border: 1px solid #50fa7b;
    background-color: #50fa7b;
}
QCheckBox:indicator:unchecked {
    border: 1px solid #ff5555;
    background-color: #ff5555;
}
QLabel {
    color: #f8f8f2;
    padding: 5px;
    font-size: 17px;
    qproperty-alignment: 'AlignCenter';
}
QLineEdit {
    background-color: #44475a;
    color: #f8f8f2;
    padding: 5px;
    border-radius: 6px;
    font-size: 16px;
}
QTextEdit, QListWidget {
    background-color: #44475a;
    color: #f8f8f2;
    border: 1px solid #6272a4;
    padding: 5px;
    border-radius: 6px;
    font-size: 16px;
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
    height: 25px;
    text-align: center;
    font-size: 16px;
}
QProgressBar::chunk {
    background-color: #50fa7b;
    border-radius: 4px;
}
""",
    "Gruvbox Dark": """
QDialog, QWidget {
    background-color: #282828;
    border-radius: 10px;
    border: 1px solid #3c3836;
}
QWidget {
    color: #ebdbb2;
    font-family: "Fira Code", "Noto Sans Mono", monospace;
    font-size: 16px;
}
QPushButton, QCheckBox {
    border-radius: 6px;
    padding: 5px;
    font-size: 16px;
    font: bold;
    color: #fbf1c7;
}
QPushButton {
    border: 2px solid #b8bb26;
    background-color: #3c3836;
}
QPushButton:enabled:hover {
    background-color: #504945;
    color: #fabd2f;
    border: 2px solid #fabd2f;
}
QPushButton:disabled {
    color: #928374;
    background-color: #282828;
    border: 2px solid #444;
}
QCheckBox:enabled:hover {
    color: #fabd2f;
    border: 2px solid #fabd2f;
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
    background-color: #b8bb26;
}
QCheckBox:indicator:unchecked {
    background-color: #cc241d;
}
QLabel {
    color: #ebdbb2;
    padding: 5px;
    font-size: 17px;
    qproperty-alignment: 'AlignCenter';
}
QLineEdit, QTextEdit, QListWidget {
    background-color: #3c3836;
    color: #ebdbb2;
    border-radius: 5px;
    padding: 5px;
    font-size: 16px;
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
    height: 25px;
    text-align: center;
}
QProgressBar::chunk {
    background-color: #b8bb26;
    border-radius: 4px;
}
""",

    "Nord": """
QDialog, QWidget {
    background-color: #2e3440;
    border-radius: 10px;
    border: 1px solid #4c566a;
}
QWidget {
    color: #d8dee9;
    font-family: "JetBrains Mono", "Fira Code", monospace;
    font-size: 16px;
}
QPushButton, QCheckBox {
    border-radius: 6px;
    padding: 5px;
    font: bold;
    color: #eceff4;
}
QPushButton {
    border: 2px solid #88c0d0;
    background-color: #3b4252;
}
QPushButton:enabled:hover {
    background-color: #434c5e;
    border-color: #81a1c1;
    color: #8fbcbb;
}
QPushButton:disabled {
    color: #616e88;
    background-color: #2e3440;
    border: 2px solid #4c566a;
}
QCheckBox:enabled:hover {
    color: #8fbcbb;
    border: 2px solid #81a1c1;
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
    background-color: #a3be8c;
}
QCheckBox:indicator:unchecked {
    background-color: #bf616a;
}
QLabel {
    color: #d8dee9;
    padding: 5px;
    font-size: 17px;
    qproperty-alignment: 'AlignCenter';
}
QLineEdit, QTextEdit, QListWidget {
    background-color: #3b4252;
    color: #eceff4;
    border-radius: 6px;
    padding: 5px;
    font-size: 16px;
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
    height: 25px;
    text-align: center;
}
QProgressBar::chunk {
    background-color: #81a1c1;
    border-radius: 4px;
}
""",

    "Catppuccin Mocha": """
QDialog, QWidget {
    background-color: #1e1e2e;
    border-radius: 10px;
    border: 1px solid #313244;
}
QWidget {
    color: #cdd6f4;
    font-family: "Iosevka", "Fira Code", monospace;
    font-size: 16px;
}
QPushButton, QCheckBox {
    border-radius: 6px;
    padding: 5px;
    font: bold;
    color: #cdd6f4;
}
QPushButton {
    border: 2px solid #89b4fa;
    background-color: #313244;
}
QPushButton:enabled:hover {
    background-color: #45475a;
    border-color: #f5c2e7;
    color: #f5c2e7;
}
QPushButton:disabled {
    color: #6c7086;
    background-color: #1e1e2e;
    border: 2px solid #313244;
}
QCheckBox:enabled:hover {
    color: #f5c2e7;
    border: 2px solid #f5c2e7;
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
    background-color: #a6e3a1;
}
QCheckBox:indicator:unchecked {
    background-color: #f38ba8;
}
QLabel {
    color: #cdd6f4;
    padding: 5px;
    font-size: 17px;
    qproperty-alignment: 'AlignCenter';
}
QLineEdit, QTextEdit, QListWidget {
    background-color: #313244;
    color: #cdd6f4;
    border-radius: 6px;
    padding: 5px;
    font-size: 16px;
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
    height: 25px;
    text-align: center;
}
QProgressBar::chunk {
    background-color: #89b4fa;
    border-radius: 4px;
}
""",

    "Monokai Pro": """
QDialog, QWidget {
    background-color: #2d2a2e;
    border-radius: 10px;
    border: 1px solid #403e41;
}
QWidget {
    color: #f8f8f2;
    font-family: "Source Code Pro", "Fira Code", monospace;
    font-size: 16px;
}
QPushButton, QCheckBox {
    border-radius: 6px;
    padding: 5px;
    font: bold;
    color: #f8f8f2;
}
QPushButton {
    border: 2px solid #66d9ef;
    background-color: #403e41;
}
QPushButton:enabled:hover {
    background-color: #75715e;
    border-color: #a6e22e;
    color: #a6e22e;
}
QPushButton:disabled {
    color: #75715e;
    background-color: #2d2a2e;
    border: 2px solid #403e41;
}
QCheckBox:enabled:hover {
    color: #a6e22e;
    border: 2px solid #a6e22e;
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
    background-color: #a6e22e;
}
QCheckBox:indicator:unchecked {
    background-color: #f92672;
}
QLabel {
    color: #f8f8f2;
    padding: 5px;
    font-size: 17px;
    qproperty-alignment: 'AlignCenter';
}
QLineEdit, QTextEdit, QListWidget {
    background-color: #403e41;
    color: #f8f8f2;
    border-radius: 6px;
    padding: 5px;
    font-size: 16px;
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
    height: 25px;
    text-align: center;
}
QProgressBar::chunk {
    background-color: #66d9ef;
    border-radius: 4px;
}
"""
}

# Default theme selection
current_theme = "Tokyo Night"

def get_current_style():
    return THEMES.get(current_theme, THEMES["Tokyo Night"])

# For backward compatibility
global_style = get_current_style()

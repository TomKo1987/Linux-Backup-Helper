import base64
from functools import lru_cache

from PyQt6.QtWidgets import QApplication

from state import S, logger, invalidate_tooltip_cache

THEMES: dict[str, dict[str, str]] = {
    "Ayu Dark": {
        "bg": "#0b0e14", "bg2": "#13161d", "bg3": "#1c2028",
        "accent": "#ff8f40", "accent2": "#e6b450",
        "highlight": "#f07178", "text": "#908e89", "text_dim": "#8a8f99",
        "green": "#aad94c", "red": "#f07178", "cyan": "#39bae6",
        "header_sep": "#273040", "info": "#39bae6", "muted": "#9aa0aa",
        "success": "#7dd474", "warning": "#fae079", "error": "#f31d27",
        "pb_bg": "#1e2530", "pb_text": "#ffffff", "pb_chunk": "#995526", "pb_chunk2": "#8a6c30",
    },
    "Catppuccin": {
        "bg": "#161622", "bg2": "#1e1e2e", "bg3": "#313244",
        "accent": "#89b4fa", "accent2": "#cba6f7",
        "highlight": "#fab387", "text": "#cdd6f4", "text_dim": "#a6adc8",
        "green": "#a6e3a1", "red": "#f38ba8", "cyan": "#89dceb",
        "header_sep": "#45475a", "info": "#89b4fa", "muted": "#9399b2",
        "success": "#7dd474", "warning": "#fae079", "error": "#f31d27",
        "pb_bg": "#4e5068", "pb_text": "#ffffff", "pb_chunk": "#526c95", "pb_chunk2": "#796394",
    },
    "Dracula": {
        "bg": "#1e1f29", "bg2": "#282a36", "bg3": "#373a4d",
        "accent": "#bd93f9", "accent2": "#ff79c6",
        "highlight": "#f1fa8c", "text": "#f8f8f2", "text_dim": "#c8c8d0",
        "green": "#50fa7b", "red": "#ff5555", "cyan": "#8be9fd",
        "header_sep": "#44475a", "info": "#8be9fd", "muted": "#9d9dbd",
        "success": "#7dd474", "warning": "#fae079", "error": "#f31d27",
        "pb_bg": "#44475a", "pb_text": "#ffffff", "pb_chunk": "#8466ae", "pb_chunk2": "#b2548a",
    },
    "Everforest": {
        "bg": "#1e2326", "bg2": "#272e33", "bg3": "#333d41",
        "accent": "#83c092", "accent2": "#a7c080",
        "highlight": "#60dbdb", "text": "#d3c6aa", "text_dim": "#a0a89a",
        "green": "#a7c080", "red": "#e67e80", "cyan": "#7fbbb3",
        "header_sep": "#3d484d", "info": "#7fbbb3", "muted": "#9aa090",
        "success": "#7dd474", "warning": "#fae079", "error": "#f31d27",
        "pb_bg": "#374145", "pb_text": "#ffffff", "pb_chunk": "#4e7357", "pb_chunk2": "#64734c",
    },
    "Gruvbox": {
        "bg": "#1d2021", "bg2": "#282828", "bg3": "#3c3836",
        "accent": "#fabd2f", "accent2": "#fe8019",
        "highlight": "#12bbb0", "text": "#ebdbb2", "text_dim": "#bdae93",
        "green": "#1abb65", "red": "#fb4934", "cyan": "#83a598",
        "header_sep": "#504945", "info": "#83a598", "muted": "#a89984",
        "success": "#7dd474", "warning": "#fae079", "error": "#f31d27",
        "pb_bg": "#5a5248", "pb_text": "#ffffff", "pb_chunk": "#95711c", "pb_chunk2": "#984c0f",
    },
    "High Contrast": {
        "bg": "#000000", "bg2": "#0d0d0d", "bg3": "#1e1e1e",
        "accent": "#00d7ff", "accent2": "#ffaf00",
        "highlight": "#ffff00", "text": "#ffffff", "text_dim": "#d8d8d8",
        "green": "#00ff5f", "red": "#ff3333", "cyan": "#00d7ff",
        "header_sep": "#505050", "info": "#00d7ff", "muted": "#bbbbbb",
        "success": "#00ff5f", "warning": "#ffaf00", "error": "#ff3333",
        "pb_bg": "#2a2a2a", "pb_text": "#ffffff", "pb_chunk": "#008199", "pb_chunk2": "#996900",
    },
    "Monokai": {
        "bg": "#1c1c1c", "bg2": "#272822", "bg3": "#3a3830",
        "accent": "#a6e22e", "accent2": "#66d9e8",
        "highlight": "#f916d3", "text": "#f8f8f2", "text_dim": "#c0bfb8",
        "green": "#a6e22e", "red": "#f92672", "cyan": "#66d9e8",
        "header_sep": "#49483e", "info": "#66d9e8", "muted": "#90908a",
        "success": "#7dd474", "warning": "#fae079", "error": "#f31d27",
        "pb_bg": "#565650", "pb_text": "#ffffff", "pb_chunk": "#537117", "pb_chunk2": "#336c74",
    },
    "Nord": {
        "bg": "#2e3440", "bg2": "#3b4252", "bg3": "#434c5e",
        "accent": "#88c0d0", "accent2": "#81a1c1",
        "highlight": "#ebcb8b", "text": "#eceff4", "text_dim": "#d8dee9",
        "green": "#a3be8c", "red": "#bf616a", "cyan": "#8fbcbb",
        "header_sep": "#4c566a", "info": "#88c0d0", "muted": "#616e88",
        "success": "#a3be8c", "warning": "#ebcb8b", "error": "#bf616a",
        "pb_bg": "#3b4252", "pb_text": "#eceff4", "pb_chunk": "#5e81ac", "pb_chunk2": "#88c0d0",
    },
    "One Dark": {
        "bg": "#1e2127", "bg2": "#21252b", "bg3": "#282c34",
        "accent": "#61afef", "accent2": "#c678dd",
        "highlight": "#e5c07b", "text": "#abb2bf", "text_dim": "#828997",
        "green": "#98c379", "red": "#e06c75", "cyan": "#56b6c2",
        "header_sep": "#3e4451", "info": "#61afef", "muted": "#636d83",
        "success": "#98c379", "warning": "#e5c07b", "error": "#e06c75",
        "pb_bg": "#3e4451", "pb_text": "#abb2bf", "pb_chunk": "#3a7db7", "pb_chunk2": "#8e4fb5",
    },
    "Rosé Pine": {
        "bg": "#191724", "bg2": "#1f1d2e", "bg3": "#2d2a40",
        "accent": "#c4a7e7", "accent2": "#ebbcba",
        "highlight": "#f6c177", "text": "#e0def4", "text_dim": "#b8b4d0",
        "green": "#9ccfd8", "red": "#eb6f92", "cyan": "#9ccfd8",
        "header_sep": "#403d52", "info": "#c4a7e7", "muted": "#8a8aaa",
        "success": "#7dd474", "warning": "#fae079", "error": "#f31d27",
        "pb_bg": "#393552", "pb_text": "#ffffff", "pb_chunk": "#75648a", "pb_chunk2": "#8c706f",
    },
    "Solarized Dark": {
        "bg": "#001b22", "bg2": "#002b36", "bg3": "#073d4a",
        "accent": "#268bd2", "accent2": "#2aa198",
        "highlight": "#b58900", "text": "#eee8d5", "text_dim": "#b0bab5",
        "green": "#859900", "red": "#dc322f", "cyan": "#2aa198",
        "header_sep": "#0a4a5a", "info": "#268bd2", "muted": "#8fa8ad",
        "success": "#7dd474", "warning": "#fae079", "error": "#f31d27",
        "pb_bg": "#0a3d4d", "pb_text": "#ffffff", "pb_chunk": "#1e6fa8", "pb_chunk2": "#218079",
    },
    "Tokyo Night": {
        "bg": "#1a1b2e", "bg2": "#24283b", "bg3": "#2d2d44",
        "accent": "#b26fff", "accent2": "#77f7aa",
        "highlight": "#f7c948", "text": "#c0caf5", "text_dim": "#8897d9",
        "green": "#00ffbf", "red": "#ff5370", "cyan": "#55ffff",
        "header_sep": "#414868", "info": "#b26fff", "muted": "#9a9a9a",
        "success": "#7dd474", "warning": "#fae079", "error": "#f31d27",
        "pb_bg": "#4a4a6a", "pb_text": "#ffffff", "pb_chunk": "#4a7c99", "pb_chunk2": "#705c94",
    },
    "Zenburn": {
        "bg": "#1f1f1f", "bg2": "#2d2d2d", "bg3": "#3a3830",
        "accent": "#7f9f7f", "accent2": "#dfaf8f",
        "highlight": "#7beef0", "text": "#dcdccc", "text_dim": "#9f9f8f",
        "green": "#7f9f7f", "red": "#cc9393", "cyan": "#93e0e3",
        "header_sep": "#4a4a4a", "info": "#93e0e3", "muted": "#9a9a8a",
        "success": "#7dd474", "warning": "#fae079", "error": "#f31d27",
        "pb_bg": "#4a4a3a", "pb_text": "#ffffff", "pb_chunk": "#4c5f4c", "pb_chunk2": "#856955",
    },
}

DEFAULT_THEME = "Tokyo Night"
_current_theme_name = DEFAULT_THEME

_style_listeners: list = []


def register_style_listener(fn) -> None:
    if fn not in _style_listeners:
        _style_listeners.append(fn)


def unregister_style_listener(fn) -> None:
    try:
        _style_listeners.remove(fn)
    except ValueError:
        pass


def current_theme() -> dict[str, str]:
    return THEMES.get(_current_theme_name, THEMES[DEFAULT_THEME])


def _base_font_size() -> int:
    try:
        return int(S.ui.get("font_size", 14))
    except (ValueError, TypeError):
        return 14


def _font_sizes(base: int) -> dict[str, int]:
    return {
        "xs":          max(9,  base - 3),
        "sm":          max(11, base - 2),
        "md":          base,
        "lg":          base + 2,
        "xl":          base + 3,
        "xxl":         base + 6,
        "card_title":  base + 4,
        "card_val":    base + 18,
        "card_val_sm": base + 10,
    }


def font_scale() -> dict[str, int]:
    return _font_sizes(_base_font_size())


def font_sz(delta: int = 0) -> int:
    return max(9, _base_font_size() + delta)


@lru_cache(maxsize=32)
def _build_indeterminate_svg(colour: str) -> str:
    svg = (f"<svg xmlns='http://www.w3.org/2000/svg' width='14' height='14'>"
           f"<rect x='3' y='3' width='8' height='8' fill='{colour}' /></svg>")
    return base64.b64encode(svg.encode()).decode()


def tri_styles() -> tuple[str, str, str]:
    t   = current_theme()
    b64 = _build_indeterminate_svg(t["highlight"])

    ind = ("QCheckBox::indicator{"
           "width:8px;height:8px;border-radius:4px;"
           "background:transparent;border:1px solid transparent;image:none;}")

    checked   = f"QCheckBox::indicator:checked{{background:{t['green']};border:1px solid {t['green']};}}"
    unchecked = f"QCheckBox::indicator:unchecked{{background:{t['bg3']};border:1px solid {t['text_dim']};}}"
    indet_std = f"QCheckBox::indicator:indeterminate{{background:{t['bg3']};border:1px solid {t['text_dim']};}}"
    indet_svg = (f"QCheckBox::indicator:indeterminate{{background:{t['bg3']};"
                 f"border:1px solid {t['highlight']};"
                 f"image:url('data:image/svg+xml;base64,{b64}');}}")

    active = (f"QCheckBox{{color:{t['text']};font-weight:bold;spacing:8px;}}"
              f"{ind}{checked}{unchecked}{indet_std}")

    disabled = (f"QCheckBox{{color:{t['muted']};text-decoration:line-through;spacing:8px;}}{ind}"
                f"QCheckBox::indicator:checked{{background:{t['bg3']};border:1px solid {t['muted']};}}"
                f"QCheckBox::indicator:unchecked{{background:{t['bg3']};border:1px solid {t['muted']};}}{indet_svg}")

    delete = (f"QCheckBox{{color:{t['red']};text-decoration:line-through;font-style:italic;spacing:8px;}}{ind}"
              f"QCheckBox::indicator:checked{{background:{t['bg3']};border:1px solid {t['red']};}}"
              f"QCheckBox::indicator:unchecked{{background:{t['bg3']};border:1px solid {t['red']};}}"
              f"QCheckBox::indicator:indeterminate{{background:{t['bg3']};border:1px solid {t['red']};}}")

    return active, disabled, delete


def style_label_info(font_size: int = 0) -> str:
    fs = font_size or font_scale()["xxl"]
    return f"font-size:{fs}px;color:{current_theme()['success']};font-family:monospace;"


def style_label_info_bold(font_size: int = 0) -> str:
    fs = font_size or font_scale()["lg"]
    t  = current_theme()
    return f"font-size:{fs}px;color:{t['success']};font-weight:bold;padding:4px;font-family:monospace;"


def style_label_mono(font_size: int = 0) -> str:
    fs = font_size or font_scale()["lg"]
    return f"font-size:{fs}px;padding:5px;qproperty-alignment:AlignLeft;font-family:monospace;"


def style_checkbox_select_all() -> str:
    return f"QCheckBox{{color:{current_theme()['cyan']};}}"


def style_checkbox_muted() -> str:
    return f"QCheckBox{{color:{current_theme()['muted']};}}"


def style_sudo_checkbox(muted: bool = False) -> str:
    t = current_theme()
    if muted:
        return f"color:{t['muted']};"
    return f"font-size:{font_scale()['lg']}px;color:{t['cyan']};font-family:monospace;"


def style_op_label(has_tip: bool) -> tuple[str, str]:
    t     = current_theme()
    color = t["accent2"] if has_tip else t["accent"]
    deco  = "text-decoration:underline dotted;" if has_tip else ""
    return color, deco


def _build_stylesheet(t: dict[str, str], font_family: str, font_size: int) -> str:
    b64       = _build_indeterminate_svg(t["highlight"])
    pb_bg     = t.get("pb_bg",    t["bg3"])
    pb_text   = t.get("pb_text",  "#ffffff")
    pb_chunk  = t.get("pb_chunk",  t["accent"])
    pb_chunk2 = t.get("pb_chunk2", t["accent2"])
    font_rule = f'font-family: "{font_family}"; ' if font_family else ""
    fs        = _font_sizes(font_size)

    return f"""
* {{ {font_rule}font-size: {font_size}px; }}

QMainWindow, QDialog, QWidget {{
    background-color: {t['bg']};
    color: {t['text']};
    border: none;
}}

QScrollArea {{ border: none; background: transparent; }}
QScrollArea > QWidget > QWidget {{ background: transparent; }}

QPushButton {{
    background: qlineargradient(x1:0,y1:0,x2:0,y2:1,stop:0 {t['bg3']},stop:1 {t['bg2']});
    color: {t['text']};
    border: 1px solid {t['accent']};
    border-radius: 6px;
    padding: 4px 12px;
    font-weight: bold;
    min-height: 26px;
}}
QPushButton:hover {{
    background: qlineargradient(x1:0,y1:0,x2:0,y2:1,stop:0 {t['bg3']},stop:1 {t['header_sep']});
    border: 1px solid {t['highlight']};
    color: {t['highlight']};
}}
QPushButton:focus {{ border: 1px solid {t['highlight']}; color: {t['highlight']}; outline: none; }}
QPushButton:pressed {{ background: {t['bg']}; border: 1px solid {t['accent2']}; color: {t['accent2']}; }}
QPushButton:disabled {{ color: {t['muted']}; border: 1px solid {t['bg3']}; background: {t['bg2']}; }}

QPushButton#mainMenuBtn {{
    font-size: {font_size + 4}px;
    font-weight: bold;
    min-height: {max(52, font_size * 3)}px;
}}

QCheckBox {{
    color: {t['text']};
    background: transparent;
    border: 1px solid transparent;
    border-radius: 4px;
    padding: 2px 4px;
    font-weight: bold;
    spacing: 6px;
}}
QCheckBox:hover {{ color: {t['highlight']}; border: 1px solid {t['highlight']}; }}
QCheckBox:focus {{ border: 1px solid {t['highlight']}; outline: none; }}
QCheckBox::indicator {{ width: 8px; height: 8px; border-radius: 4px; }}
QCheckBox::indicator:checked {{ background-color: {t['green']}; border: 1px solid {t['green']}; image: none; }}
QCheckBox::indicator:unchecked {{ background-color: {t['bg3']}; border: 1px solid {t['red']}; image: none; }}
QCheckBox::indicator:indeterminate {{
    background-color: {t['bg3']};
    border: 1px solid {t['highlight']};
    image: url("data:image/svg+xml;base64,{b64}");
}}

QLabel {{ color: {t['text']}; background: transparent; border: none; padding: 1px; }}

QLineEdit {{
    background: {t['bg3']}; color: {t['text']};
    border: 1px solid {t['header_sep']}; border-radius: 5px;
    padding: 4px 8px; selection-background-color: {t['accent']}; selection-color: {t['bg']};
}}
QLineEdit:focus {{ border: 1px solid {t['accent']}; color: {t['text']}; }}

QTextEdit {{
    background: {t['bg2']}; color: {t['text']};
    border: 1px solid {t['header_sep']}; border-radius: 5px;
    padding: 4px; selection-background-color: {t['accent']}; selection-color: {t['bg']};
}}

QListWidget {{
    background: {t['bg2']}; color: {t['text']};
    border: 1px solid {t['header_sep']}; border-radius: 5px; padding: 2px;
}}
QListWidget::item {{ padding: 4px 8px; border-radius: 3px; }}
QListWidget::item:selected {{ background: {t['accent']}; color: {t['bg']}; font-weight: bold; }}
QListWidget::item:hover {{ background: {t['bg3']}; color: {t['text']}; }}

QScrollBar:vertical {{ background: {t['bg2']}; width: 10px; border-radius: 5px; margin: 0; }}
QScrollBar::handle:vertical {{ background: {t['bg3']}; border-radius: 5px; min-height: 24px; }}
QScrollBar::handle:vertical:hover {{ background: {t['accent']}; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; border: none; }}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: none; }}
QScrollBar:horizontal {{ background: {t['bg2']}; height: 10px; border-radius: 5px; }}
QScrollBar::handle:horizontal {{ background: {t['bg3']}; border-radius: 5px; min-width: 24px; }}
QScrollBar::handle:horizontal:hover {{ background: {t['accent']}; }}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0; border: none; }}

QProgressBar {{
    background: {pb_bg};
    border: 1px solid {t['accent']};
    border-radius: 6px;
    text-align: center;
    color: {pb_text};
    font-size: {fs['xxl']}px;
    font-weight: bold;
    min-height: 35px;
}}
QProgressBar::chunk {{
    background: qlineargradient(x1:0,y1:0,x2:1,y2:0,stop:0 {pb_chunk},stop:1 {pb_chunk2});
    border-radius: 5px;
}}

QTabWidget::pane {{ border: 1px solid {t['header_sep']}; border-radius: 6px; background: {t['bg2']}; }}
QTabBar::tab {{
    background: {t['bg3']}; color: {t['text_dim']};
    border: 1px solid {t['header_sep']}; border-bottom: none;
    padding: 6px 14px; border-top-left-radius: 5px; border-top-right-radius: 5px;
    margin-right: 2px; font-weight: bold;
}}
QTabBar::tab:selected {{ background: {t['bg2']}; color: {t['accent']}; border-bottom: 2px solid {t['accent']}; }}
QTabBar::tab:hover:!selected {{ color: {t['text']}; background: {t['bg']}; }}

QComboBox {{
    background: {t['bg3']}; color: {t['text']};
    border: 1px solid {t['header_sep']}; border-radius: 5px;
    padding: 4px 8px; min-height: 24px;
}}
QComboBox:hover {{ border: 1px solid {t['highlight']}; color: {t['text']}; }}
QComboBox:focus {{ border: 1px solid {t['accent']}; color: {t['text']}; }}
QComboBox::drop-down {{ border: none; width: 20px; }}
QComboBox::down-arrow {{
    width: 8px; height: 8px; image: none;
    border-left: 4px solid transparent;
    border-right: 4px solid transparent;
    border-top: 6px solid {t['accent']};
}}
QComboBox QAbstractItemView {{
    background: {t['bg2']}; color: {t['text']};
    border: 1px solid {t['header_sep']};
    selection-background-color: {t['accent']}; selection-color: {t['bg']};
    outline: none;
}}

QMenu {{
    background: {t['bg2']}; color: {t['text']};
    border: 1px solid {t['header_sep']}; border-radius: 5px; padding: 4px;
}}
QMenu::item {{ padding: 5px 20px; border-radius: 3px; }}
QMenu::item:selected {{ background: {t['accent']}; color: {t['bg']}; }}

QMessageBox {{ background: {t['bg2']}; }}

QToolTip {{
    background-color: {t['bg2']};
    color: {t['text']};
    border: 1px solid {t['accent']};
    border-radius: 4px;
    padding: 4px 8px;
}}

QSplitter::handle {{
    background: {t['header_sep']};
    width: 4px;
    height: 4px;
}}
QSplitter::handle:hover {{
    background: {t['accent']};
}}

QPlainTextEdit {{
    background: {t['bg2']};
    color: {t['text']};
    border: 1px solid {t['header_sep']};
    border-radius: 5px;
    padding: 4px;
    selection-background-color: {t['accent']};
    selection-color: {t['bg']};
}}
QPlainTextEdit:focus {{
    border: 1px solid {t['accent']};
}}

QFrame[frameShape="4"], QFrame[frameShape="5"] {{
    color: {t['header_sep']}; background: {t['header_sep']};
}}
"""


_style_cache: tuple = ()


def apply_tooltip(widget, text: str) -> None:
    if not text:
        return
    from PyQt6.QtCore import Qt
    widget.setToolTip(text)
    widget.setToolTipDuration(600_000)
    widget.setCursor(Qt.CursorShape.WhatsThisCursor)


def tri_state_legend_html() -> str:
    t = current_theme()
    c_act = t['green']
    c_dis = t['highlight']
    c_del = t['red']
    return (f"<span style='color:{c_act};'>●</span> Active &nbsp;&nbsp; "
            f"<span style='color:{c_dis};'>○</span> "
            f"<span style='color:{t['muted']};text-decoration:line-through;'>Disabled</span> &nbsp;&nbsp; "
            f"<span style='color:{c_del};'>○</span> "
            f"<span style='color:{c_del};text-decoration:line-through;font-style:italic;'>Delete</span>")


def get_style() -> str:
    global _style_cache
    font = S.ui.get("font_family", "") or ""
    size = _base_font_size()
    theme = current_theme()
    key = (_current_theme_name, font, size)
    if _style_cache and _style_cache[0] == key:
        return _style_cache[1]
    css = _build_stylesheet(theme, font, size)
    _style_cache = (key, css)
    return css


def apply_style() -> None:
    global _current_theme_name
    _current_theme_name = S.ui.get("theme", DEFAULT_THEME)
    invalidate_tooltip_cache()
    app = QApplication.instance()
    if isinstance(app, QApplication):
        app.setStyleSheet(get_style())
    for fn in list(_style_listeners):
        try:
            fn()
        except Exception as e:
            logger.error("Style listener error (%s): %s", fn, e)

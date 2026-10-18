"""Light and dark themes: one set of color tokens drives the Qt palette, the stylesheet and the icons."""

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication, QStyleFactory

THEMES = {
    "dark": {
        "bg": "#14161b", "surface": "#1d2027", "surface2": "#262a33", "surface3": "#30353f", "border": "#343a46",
        "text": "#e7e9ef", "muted": "#9aa2b1", "accent": "#7b93ff", "accent_text": "#0f1220",
        "accent_soft": "#7b93ff2e", "mark": "#ffc23d", "mark_soft": "#ffc23d55", "good": "#41d39a",
        "warn": "#ffb454", "bad": "#ff6b6b", "canvas": "#0d0f13", "shadow": "#00000080",
    },
    "light": {
        "bg": "#f3f4f8", "surface": "#ffffff", "surface2": "#eef0f5", "surface3": "#e3e6ee", "border": "#d9dde6",
        "text": "#1b2030", "muted": "#646d80", "accent": "#4a63e7", "accent_text": "#ffffff",
        "accent_soft": "#4a63e722", "mark": "#f5a400", "mark_soft": "#ffb40050", "good": "#13945f",
        "warn": "#c26a00", "bad": "#d33a3a", "canvas": "#dfe2ea", "shadow": "#1b203033",
    },
}

current: dict[str, str] = dict(THEMES["dark"])
current_name = "dark"


def color(token: str) -> QColor:
    v = current[token]
    if len(v) == 9:  # #rrggbbaa
        c = QColor(v[:7])
        c.setAlpha(int(v[7:], 16))
        return c
    return QColor(v)


def _css(v: str) -> str:
    """Qt stylesheets read 8-digit hex as #AARRGGBB, so translucent tokens become rgba()."""
    if len(v) == 9:
        r, g, b, a = (int(v[i:i + 2], 16) for i in (1, 3, 5, 7))
        return f"rgba({r},{g},{b},{a})"
    return v


def _qss(tokens: dict[str, str]) -> str:
    t = {k: _css(v) for k, v in tokens.items()}
    return f"""
    QWidget {{ color: {t['text']}; }}
    QMainWindow, #Central {{ background: {t['bg']}; }}
    QToolTip {{ background: {t['surface2']}; color: {t['text']}; border: 1px solid {t['border']};
               padding: 5px 8px; border-radius: 6px; }}
    QMenuBar {{ background: {t['bg']}; border: none; padding: 2px 6px; }}
    QMenuBar::item {{ padding: 5px 10px; border-radius: 6px; background: transparent; }}
    QMenuBar::item:selected {{ background: {t['surface2']}; }}
    QMenu {{ background: {t['surface']}; border: 1px solid {t['border']}; border-radius: 10px; padding: 6px; }}
    QMenu::item {{ padding: 7px 26px 7px 14px; border-radius: 6px; }}
    QMenu::item:selected {{ background: {t['accent_soft']}; }}
    QMenu::item:checked {{ color: {t['accent']}; font-weight: 600; }}
    QMenu::separator {{ height: 1px; background: {t['border']}; margin: 5px 8px; }}

    #TopBar {{ background: {t['surface']}; border-bottom: 1px solid {t['border']}; }}
    #TopBar QToolButton, #PlayerBar QToolButton, #PanelHeader QToolButton {{
        background: transparent; border: none; border-radius: 8px; padding: 6px; }}
    #TopBar QToolButton:hover, #PlayerBar QToolButton:hover, #PanelHeader QToolButton:hover {{
        background: {t['surface2']}; }}
    #TopBar QToolButton:checked, #PlayerBar QToolButton:checked, #PanelHeader QToolButton:checked {{
        background: {t['accent_soft']}; color: {t['accent']}; }}
    #DocTitle {{ font-size: 11pt; font-weight: 600; }}
    #DocSub {{ color: {t['muted']}; }}
    #Badge {{ border-radius: 11px; padding: 3px 11px; font-weight: 600; }}
    #ZoomLabel {{ color: {t['muted']}; min-width: 44px; }}

    #PlayerBar {{ background: {t['surface']}; border-top: 1px solid {t['border']}; }}
    #Caption {{ font-size: 11pt; color: {t['text']}; }}
    #CaptionHint {{ color: {t['muted']}; }}
    #Meta {{ color: {t['muted']}; }}
    #Chip, QComboBox#Chip {{ background: {t['surface2']}; border: 1px solid {t['border']}; border-radius: 15px;
             padding: 5px 12px; min-height: 18px; }}
    #Chip:hover, QComboBox#Chip:hover {{ border-color: {t['accent']}; }}
    #Chip:checked {{ background: {t['accent_soft']}; border-color: {t['accent']}; color: {t['accent']}; }}
    QComboBox#Chip::drop-down {{ border: none; width: 18px; }}
    QComboBox QAbstractItemView {{ background: {t['surface']}; border: 1px solid {t['border']};
             selection-background-color: {t['accent_soft']}; selection-color: {t['text']}; outline: none;
             padding: 4px; }}

    #SidePanel, #Rail {{ background: {t['surface']}; }}
    #SidePanel {{ border-right: 1px solid {t['border']}; }}
    #Rail {{ border-right: 1px solid {t['border']}; }}
    #PanelHeader {{ background: {t['surface']}; }}
    #PanelTitle {{ font-size: 11pt; font-weight: 700; }}
    #PanelCount {{ color: {t['muted']}; }}
    QLineEdit {{ background: {t['surface2']}; border: 1px solid {t['border']}; border-radius: 9px;
                padding: 6px 10px; selection-background-color: {t['accent']}; }}
    QLineEdit:focus {{ border-color: {t['accent']}; }}
    #Transcript {{ background: {t['surface']}; border: none; selection-background-color: {t['accent_soft']}; }}
    #Pill {{ background: {t['accent']}; color: {t['accent_text']}; border: none; border-radius: 15px;
             padding: 6px 14px; font-weight: 600; }}
    #Pill:hover {{ background: {t['accent']}; }}

    #Toast {{ background: {t['surface3']}; color: {t['text']}; border: 1px solid {t['border']};
              border-radius: 12px; padding: 9px 16px; }}
    #Banner {{ background: {t['bad']}; color: white; padding: 8px 14px; font-weight: 600; }}

    QSplitter::handle {{ background: {t['border']}; }}
    QDockWidget {{ titlebar-close-icon: none; }}
    QDockWidget::title {{ background: {t['surface']}; padding: 8px; font-weight: 700; }}
    QListWidget {{ background: {t['surface']}; border: 1px solid {t['border']}; border-radius: 8px; padding: 4px; }}
    QListWidget::item {{ padding: 6px; border-radius: 6px; }}
    QListWidget::item:selected {{ background: {t['accent_soft']}; color: {t['text']}; }}
    QPushButton {{ background: {t['surface2']}; border: 1px solid {t['border']}; border-radius: 8px;
                   padding: 6px 12px; }}
    QPushButton:hover {{ border-color: {t['accent']}; }}
    QPushButton:default {{ background: {t['accent']}; color: {t['accent_text']}; border: none; }}
    QComboBox {{ background: {t['surface2']}; border: 1px solid {t['border']}; border-radius: 8px; padding: 5px 9px; }}
    QSpinBox {{ background: {t['surface2']}; border: 1px solid {t['border']}; border-radius: 6px; padding: 4px; }}
    QGroupBox {{ border: 1px solid {t['border']}; border-radius: 10px; margin-top: 14px; padding: 10px; }}
    QGroupBox::title {{ subcontrol-origin: margin; left: 10px; padding: 0 4px; color: {t['muted']}; }}
    QProgressBar {{ background: {t['surface2']}; border: none; border-radius: 6px; height: 12px; text-align: center; }}
    QProgressBar::chunk {{ background: {t['accent']}; border-radius: 6px; }}
    QSlider::groove:horizontal {{ height: 4px; background: {t['surface3']}; border-radius: 2px; }}
    QSlider::sub-page:horizontal {{ background: {t['accent']}; border-radius: 2px; }}
    QSlider::handle:horizontal {{ background: {t['accent']}; width: 14px; margin: -6px 0; border-radius: 7px; }}
    QScrollBar:vertical {{ background: transparent; width: 11px; margin: 2px; }}
    QScrollBar::handle:vertical {{ background: {t['surface3']}; border-radius: 4px; min-height: 30px; }}
    QScrollBar::handle:vertical:hover {{ background: {t['muted']}; }}
    QScrollBar:horizontal {{ background: transparent; height: 11px; margin: 2px; }}
    QScrollBar::handle:horizontal {{ background: {t['surface3']}; border-radius: 4px; min-width: 30px; }}
    QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
    QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
    QCheckBox::indicator {{ width: 16px; height: 16px; }}
    """


def apply(app: QApplication, name: str) -> None:
    global current_name
    current_name = name if name in THEMES else "dark"
    current.clear()
    current.update(THEMES[current_name])
    app.setStyle(QStyleFactory.create("Fusion"))
    t = current
    pal = QPalette()
    for role, token in [(QPalette.ColorRole.Window, "bg"), (QPalette.ColorRole.Base, "surface"),
                        (QPalette.ColorRole.AlternateBase, "surface2"), (QPalette.ColorRole.Button, "surface2"),
                        (QPalette.ColorRole.Text, "text"), (QPalette.ColorRole.WindowText, "text"),
                        (QPalette.ColorRole.ButtonText, "text"), (QPalette.ColorRole.ToolTipBase, "surface2"),
                        (QPalette.ColorRole.ToolTipText, "text"), (QPalette.ColorRole.PlaceholderText, "muted"),
                        (QPalette.ColorRole.Highlight, "accent"), (QPalette.ColorRole.HighlightedText, "accent_text"),
                        (QPalette.ColorRole.Link, "accent")]:
        pal.setColor(role, color(token))
    pal.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, color("muted"))
    pal.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, color("muted"))
    pal.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.WindowText, color("muted"))
    app.setPalette(pal)
    app.setStyleSheet(_qss(t))

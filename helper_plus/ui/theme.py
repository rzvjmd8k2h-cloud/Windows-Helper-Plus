"""Qt 原生风格初始化与轻量样式。

优先使用 Qt 6.12 的 ``windows11`` 原生样式，退化到 ``windowsvista``。
样式表只做轻量间距/卡片/强调色，不替换按钮、滚动条等原生控件绘制，
避免破坏 Windows 原生观感和输入法行为。
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import QApplication, QStyleFactory


ACCENT = "#0067c0"
ACCENT_HOVER = "#005a9e"
ACCENT_PRESSED = "#004b87"


APP_QSS = f"""
QFrame#HeaderCard {{
    background-color: palette(base);
    border: 1px solid palette(mid);
    border-radius: 8px;
}}
QLabel#AppTitle {{
    font-size: 18pt;
    font-weight: 600;
    color: palette(text);
}}
QLabel#Subtitle {{
    color: palette(dark);
    font-size: 9pt;
}}
QLabel#StageBadge {{
    background-color: #e8f1fb;
    color: #005a9e;
    border: 1px solid #c5ddf5;
    border-radius: 10px;
    padding: 2px 10px;
    font-size: 9pt;
}}
QLabel#ModelBadge {{
    color: palette(dark);
    background: transparent;
    border: none;
    padding: 2px 2px;
    font-size: 8.5pt;
}}
QLabel#ElapsedLabel {{
    color: palette(dark);
    font-size: 8.5pt;
}}
QLabel#Hint {{
    color: palette(dark);
    font-size: 8.5pt;
}}
QPushButton#PrimaryButton {{
    background-color: {ACCENT};
    color: white;
    border: 1px solid {ACCENT_HOVER};
    border-radius: 4px;
    padding: 5px 18px;
    font-weight: 600;
}}
QPushButton#PrimaryButton:hover {{ background-color: {ACCENT_HOVER}; }}
QPushButton#PrimaryButton:pressed {{ background-color: {ACCENT_PRESSED}; }}
QPushButton#PrimaryButton:disabled {{ background-color: #9bbdd9; border-color: #9bbdd9; color: #f4f8fc; }}
QGroupBox {{
    border: 1px solid palette(mid);
    border-radius: 6px;
    margin-top: 9px;
    padding-top: 9px;
    background-color: palette(base);
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: 10px;
    padding: 0 4px;
    color: palette(text);
}}
QPlainTextEdit#StreamPane {{
    background-color: palette(base);
    border: 1px solid palette(mid);
    border-radius: 4px;
    padding: 4px;
    selection-background-color: #cfe7ff;
}}
QTextBrowser#ConclusionPane {{
    background-color: palette(base);
    border: 1px solid palette(mid);
    border-radius: 4px;
}}
QTreeWidget {{
    alternate-background-color: palette(alternate-base);
    border: 1px solid palette(mid);
    border-radius: 4px;
}}
QTreeWidget::item {{ min-height: 22px; }}
QTabWidget::pane {{ border: 1px solid palette(mid); border-radius: 4px; top: -1px; }}
QTabBar::tab {{ padding: 5px 14px; }}
QStatusBar {{ border-top: 1px solid palette(mid); }}
QLineEdit {{ padding: 4px 6px; border: 1px solid palette(mid); border-radius: 4px; background: palette(base); }}
QLineEdit:focus {{ border: 1px solid {ACCENT}; }}
"""


def create_application(argv: list[str]) -> QApplication:
    existing = QApplication.instance()
    if existing is not None:
        return existing  # type: ignore[return-value]
    if hasattr(Qt.ApplicationAttribute, "AA_UseHighDpiPixmaps"):
        QApplication.setAttribute(Qt.ApplicationAttribute.AA_UseHighDpiPixmaps, True)
    app = QApplication(argv)
    app.setApplicationName("Windows Helper Plus")
    app.setApplicationDisplayName("Windows Helper Plus")
    app.setOrganizationName("Windows Helper Plus")
    app.setOrganizationDomain("local.windowshelperplus")
    app.setApplicationVersion("0.1.0")

    available = QStyleFactory.keys()
    style = "windows11" if "windows11" in available else ("windowsvista" if "windowsvista" in available else None)
    if style:
        app.setStyle(QStyleFactory.create(style))

    families = set(QFontDatabase.families())
    family = "Segoe UI Variable Text" if "Segoe UI Variable Text" in families else "Segoe UI"
    font = QFont(family, 9)
    font.setStyleStrategy(QFont.StyleStrategy.PreferAntialias)
    app.setFont(font)
    app.setStyleSheet(APP_QSS)
    return app

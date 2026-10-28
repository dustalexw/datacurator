"""Peppermint themes: cream and barber red in light mode, deep burgundy in dark mode."""
from __future__ import annotations

import os

from PySide6.QtCore import QRectF, QSize, Qt, Signal
from PySide6.QtGui import (QColor, QFont, QIcon, QPainter, QPainterPath, QPalette, QPen, QPixmap)
from PySide6.QtWidgets import (QApplication, QFrame, QLabel, QSizePolicy, QStyle, QStyledItemDelegate,
                               QVBoxLayout, QWidget)

RES = os.path.join(os.path.dirname(__file__), "resources")
# Semantic accents resolve at paint time, so existing tiles and badges follow theme changes.
ACCENT, INFO, DUPLICATE, QUARANTINE = "accent", "info", "duplicate", "quarantine"
WARNING, SUCCESS, ERROR, MUTED, TEXT = "warning", "success", "error", "muted", "text"

THEMES = {
    "dark": {
        "bg": "#260F18", "sidebar": "#351421", "panel": "#421C2C", "input": "#2E1420",
        "line": "#754454", "text": "#FFF4EB", "muted": "#D3AFBD", "title": "#FFF1E5",
        "button": "#542137", "hover": "#653049", "primary": "#8A2445", "primary_hover": "#A53050",
        "selection": "#842442", "on_primary": "#FFF9F2", "disabled_bg": "#38202B", "disabled": "#AC8998",
        "accent": "#FF9AAF", "info": "#96DCC7", "duplicate": "#E4ADBF", "quarantine": "#F5B69B",
        "warning": "#F4C780", "success": "#A1DBAE", "error": "#FFA3A3", "mint": "#96DCC7",
        "stripe": "#C83A54", "cream": "#FFF1E5", "focus": "#96DCC7",
    },
    "light": {
        "bg": "#FFF9F2", "sidebar": "#F5E8E1", "panel": "#FFFCF8", "input": "#FFFFFF",
        "line": "#D6B8BF", "text": "#451B29", "muted": "#755260", "title": "#75223F",
        "button": "#F5E6E4", "hover": "#EED7DB", "primary": "#8A2445", "primary_hover": "#A53050",
        "selection": "#842442", "on_primary": "#FFF9F2", "disabled_bg": "#EFE4E0", "disabled": "#836D76",
        "accent": "#8A2445", "info": "#256C5C", "duplicate": "#853C56", "quarantine": "#994634",
        "warning": "#805714", "success": "#346646", "error": "#B4233E", "mint": "#256C5C",
        "stripe": "#BA2F4C", "cream": "#FFF9F2", "focus": "#8A2445",
    },
}


def normalize_name(name):
    return name if name in ("light", "dark", "system") else "dark"  # migrates the old Nebula setting


def current_name():
    app = QApplication.instance()
    return normalize_name(app.property("curatorTheme") if app else "dark")


def colors(name=None):
    return THEMES.get(name or current_name(), THEMES["light"])


def color(role):
    return colors().get(role, role)


def is_custom():
    return current_name() != "system"


def app_icon() -> QIcon:
    icon = QIcon()
    for size in (16, 32, 48, 64, 128, 256, 512):
        path = os.path.join(RES, f"icon-{size}.png")
        if os.path.exists(path):
            icon.addFile(path, QSize(size, size))
    return icon


def logo_pixmap(size: int) -> QPixmap:
    pm = QPixmap(os.path.join(RES, "AppIcon.png"))
    return pm.scaled(size, size, Qt.KeepAspectRatio, Qt.SmoothTransformation) if not pm.isNull() else QPixmap()


def stylesheet(c):
    check = os.path.join(RES, "check.png").replace("\\", "/")
    return f"""
* {{ font-size: 10pt; }}
QMainWindow, QDialog, QMessageBox, QWidget#Root {{ background: {c['bg']}; }}
QWidget#Sidebar {{ background: {c['sidebar']}; border-right: 1px solid {c['line']}; }}
QWidget#Page, QLabel {{ background: transparent; }}
QLabel, QCheckBox {{ color: {c['text']}; }}
QLabel[role="hint"], QLabel[role="section"] {{ color: {c['muted']}; }}
QLabel[role="title"] {{ font-size: 19pt; font-weight: 700; }}
QLabel[role="section"] {{ font-size: 8.5pt; font-weight: 700; letter-spacing: 1px; }}
QLabel[role="example"] {{ font-family: "SF Mono", "Menlo", "Consolas", monospace;
  color: {c['info']}; background: {c['input']}; border: 1px solid {c['line']}; border-radius: 8px; padding: 8px 10px; }}
QLabel#AboutVersion {{ color: {c['accent']}; background: {c['panel']}; border: 1px solid {c['line']};
  border-radius: 12px; padding: 4px 12px; font-size: 9pt; }}
QFrame#AboutFeatures {{ background: {c['panel']}; border: 1px solid {c['line']}; border-radius: 12px; }}
QLabel#AboutFeatureTitle {{ color: {c['text']}; font-weight: 700; }}
QLabel#AboutCredits {{ font-size: 8.5pt; }}
QListWidget#steps {{ background: transparent; border: none; outline: 0; color: {c['muted']}; font-size: 11pt; }}
QListWidget#steps::item {{ padding: 11px 14px; margin: 2px 0; border-radius: 10px; }}
QListWidget#steps::item:hover {{ background: {c['hover']}; color: {c['text']}; }}
QListWidget#steps::item:selected {{ color: {c['on_primary']}; font-weight: 600; background: {c['selection']}; }}
QGroupBox {{ color: {c['text']}; font-weight: 700; background: {c['panel']}; border: 1px solid {c['line']};
  border-radius: 14px; margin-top: 16px; padding: 14px 12px 10px; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 14px; padding: 0 6px; color: {c['muted']}; }}
QLineEdit, QComboBox, QTextBrowser, QPlainTextEdit, QTextEdit, QListWidget, QTableView, QTreeView, QTableWidget {{
  background: {c['input']}; color: {c['text']}; border: 1px solid {c['line']}; border-radius: 8px;
  selection-background-color: {c['selection']}; selection-color: {c['on_primary']}; }}
QLineEdit, QComboBox {{ padding: 6px 10px; min-height: 20px; }}
QLineEdit:focus, QComboBox:focus, QTableView:focus, QTreeView:focus, QListWidget:focus {{ border: 1px solid {c['focus']}; }}
QLineEdit:disabled, QComboBox:disabled {{ background: {c['disabled_bg']}; color: {c['disabled']}; }}
QComboBox::drop-down {{ border: none; width: 22px; }}
QComboBox QAbstractItemView {{ background: {c['panel']}; color: {c['text']}; border: 1px solid {c['line']};
  selection-background-color: {c['selection']}; selection-color: {c['on_primary']}; outline: 0; }}
QPushButton {{ color: {c['text']}; background: {c['button']}; border: 1px solid {c['line']};
  border-radius: 9px; padding: 7px 14px; }}
QPushButton:hover {{ background: {c['hover']}; border-color: {c['focus']}; }}
QPushButton:pressed {{ background: {c['selection']}; color: {c['on_primary']}; }}
QPushButton:focus {{ border: 1px solid {c['focus']}; }}
QPushButton:disabled, QPushButton[primary="true"]:disabled {{ color: {c['disabled']}; background: {c['disabled_bg']}; border: 1px solid {c['line']}; }}
QPushButton[primary="true"] {{ color: {c['on_primary']}; font-weight: 700; padding: 9px 22px;
  border: 1px solid {c['primary']}; background: {c['primary']}; }}
QPushButton[primary="true"]:hover {{ background: {c['primary_hover']}; border-color: {c['primary_hover']}; }}
QTabWidget::pane {{ border: 1px solid {c['line']}; border-radius: 12px; background: {c['panel']}; top: -1px; }}
QTabBar::tab {{ color: {c['muted']}; background: transparent; padding: 8px 16px; margin-right: 4px;
  border-top-left-radius: 9px; border-top-right-radius: 9px; }}
QTabBar::tab:hover {{ color: {c['text']}; background: {c['hover']}; }}
QTabBar::tab:selected {{ color: {c['on_primary']}; font-weight: 600; background: {c['selection']}; border-bottom: 2px solid {c['mint']}; }}
QHeaderView::section {{ background: {c['panel']}; color: {c['muted']}; border: none; border-bottom: 1px solid {c['line']}; padding: 7px 8px; font-weight: 700; }}
QTableView, QTableWidget, QTreeView {{ gridline-color: transparent; alternate-background-color: {c['panel']}; }}
QTableView::item, QTableWidget::item, QTreeView::item {{ padding: 3px 6px; }}
QTableView::item:hover, QTreeView::item:hover {{ background: {c['hover']}; }}
QTableView::item:selected, QTableWidget::item:selected, QTreeView::item:selected {{ background: {c['selection']}; color: {c['on_primary']}; }}
QTableCornerButton::section {{ background: {c['panel']}; border: none; }}
QCheckBox {{ spacing: 8px; }}
QCheckBox::indicator, QGroupBox::indicator {{ width: 16px; height: 16px; border-radius: 5px;
  border: 1px solid {c['muted']}; background: {c['input']}; }}
QCheckBox::indicator:hover, QGroupBox::indicator:hover {{ border: 1px solid {c['focus']}; }}
QCheckBox::indicator:checked, QGroupBox::indicator:checked {{ border: 1px solid {c['primary']}; image: url({check}); background: {c['primary']}; }}
QProgressBar {{ background: {c['panel']}; border: none; border-radius: 6px; height: 12px; color: transparent; max-height: 12px; }}
QProgressBar::chunk {{ border-radius: 6px; background: {c['primary_hover']}; }}
QStatusBar {{ background: {c['bg']}; color: {c['muted']}; border-top: 1px solid {c['line']}; }}
QStatusBar QLabel {{ color: {c['muted']}; }}
QMenuBar {{ background: {c['bg']}; color: {c['text']}; }}
QMenuBar::item:selected {{ background: {c['selection']}; color: {c['on_primary']}; border-radius: 6px; }}
QMenu {{ background: {c['panel']}; color: {c['text']}; border: 1px solid {c['line']}; border-radius: 8px; padding: 4px; }}
QMenu::item {{ padding: 6px 18px; border-radius: 6px; }}
QMenu::item:selected {{ background: {c['selection']}; color: {c['on_primary']}; }}
QSplitter::handle {{ background: transparent; width: 8px; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {c['line']}; border-radius: 4px; min-height: 30px; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
QScrollBar::handle:horizontal {{ background: {c['line']}; border-radius: 4px; min-width: 30px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QToolTip {{ background: {c['panel']}; color: {c['text']}; border: 1px solid {c['line']}; padding: 6px; border-radius: 6px; }}
QFrame#Divider {{ background: {c['line']}; max-height: 1px; }}
"""


SYSTEM_QSS = """
QLabel#AboutVersion { border: 1px solid palette(mid); border-radius: 12px; padding: 4px 12px; font-size: 9pt; }
QFrame#AboutFeatures { border: 1px solid palette(mid); border-radius: 12px; }
QLabel#AboutFeatureTitle { font-weight: 700; }
QLabel#AboutCredits { font-size: 8.5pt; }
QLabel[role="hint"], QLabel[role="section"] { color: palette(placeholder-text); }
QLabel[role="section"] { font-size: 8.5pt; font-weight: 700; }
QLabel[role="example"] { font-family: monospace; padding: 6px; border: 1px solid palette(mid); border-radius: 6px; }
QListWidget#steps { border: none; background: transparent; outline: 0; }
QListWidget#steps::item { padding: 10px 14px; margin: 2px 0; border-radius: 8px; }
QListWidget#steps::item:selected { background: palette(highlight); color: palette(highlighted-text); }
QPushButton[primary="true"] { font-weight: 700; padding: 6px 18px; }
"""


def palette(name):
    c = colors(name)
    p = QPalette()
    roles = {
        QPalette.Window: "bg", QPalette.WindowText: "text", QPalette.Base: "input", QPalette.AlternateBase: "panel",
        QPalette.Text: "text", QPalette.Button: "button", QPalette.ButtonText: "text", QPalette.Highlight: "selection",
        QPalette.HighlightedText: "on_primary", QPalette.ToolTipBase: "panel", QPalette.ToolTipText: "text",
        QPalette.PlaceholderText: "muted", QPalette.Link: "info", QPalette.LinkVisited: "duplicate",
        QPalette.Mid: "line", QPalette.Dark: "bg", QPalette.Light: "panel",
    }
    for role, key in roles.items():
        p.setColor(role, QColor(c[key]))
    for role in (QPalette.Text, QPalette.ButtonText, QPalette.WindowText):
        p.setColor(QPalette.Disabled, role, QColor(c["disabled"]))
    return p


def apply(app: QApplication, name: str = "dark") -> str:
    name = normalize_name(name)
    app.setStyle("Fusion")
    app.setProperty("curatorTheme", name)
    if name == "system":
        app.setPalette(app.style().standardPalette())
        app.setStyleSheet(SYSTEM_QSS)
    else:
        app.setPalette(palette(name))
        app.setStyleSheet(stylesheet(colors(name)))
    # Refresh local styles and painted widgets, including an already-open About dialog.
    for widget in app.allWidgets():
        if isinstance(widget, StatTile):
            widget.set_active(widget.active)
        widget.update()
    return name


class PeppermintStripe(QWidget):
    """A quiet barber-pole stripe for the brand, kept outside content and text."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(6)
        self.setAccessibleName("Peppermint stripe")

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        clip = QPainterPath(); clip.addRoundedRect(QRectF(self.rect()), 3, 3)
        p.setClipPath(clip)
        c = colors(); p.fillRect(self.rect(), QColor(c["cream"]))
        p.setPen(QPen(QColor(c["stripe"]), 9))
        for x in range(-12, self.width() + 12, 20):
            p.drawLine(x, self.height(), x + 8, 0)
        p.end()


class BrandTitle(QWidget):
    """Crisp burgundy or cream brand lettering, depending on the theme."""
    def __init__(self, text: str, point_size: float = 20, parent=None):
        super().__init__(parent)
        self.text = text
        self.font_ = QFont(self.font()); self.font_.setPointSizeF(point_size); self.font_.setWeight(QFont.Bold)
        self.setAccessibleName(text)
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        from PySide6.QtGui import QFontMetrics
        fm = QFontMetrics(self.font_)
        self.setMinimumHeight(fm.height() + 6)
        self.setMinimumWidth(fm.horizontalAdvance(text) + 8)

    def paintEvent(self, event):
        p = QPainter(self); p.setRenderHint(QPainter.Antialiasing)
        from PySide6.QtGui import QFontMetrics
        path = QPainterPath(); path.addText(2, QFontMetrics(self.font_).ascent() + 2, self.font_, self.text)
        p.fillPath(path, QColor(color("title")) if is_custom() else self.palette().windowText())
        p.end()


class StatTile(QFrame):
    clicked = Signal(str)

    def __init__(self, key: str, label: str, accent: str, parent=None):
        super().__init__(parent)
        self.key, self.accent, self.active = key, accent, False
        self.setCursor(Qt.PointingHandCursor); self.setFocusPolicy(Qt.StrongFocus)
        self.setAccessibleName(label); self.setMinimumWidth(118)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        lay = QVBoxLayout(self); lay.setContentsMargins(14, 10, 14, 10); lay.setSpacing(0)
        self.value = QLabel("—"); self.value.setObjectName("TileValue")
        self.caption = QLabel(label); self.caption.setObjectName("TileCaption")
        lay.addWidget(self.value); lay.addWidget(self.caption)
        self.set_active(False)

    def set_value(self, text: str):
        self.value.setText(text)
        self.setAccessibleDescription(f"{text} {self.caption.text()}")

    def set_active(self, on: bool):
        self.active = on
        if is_custom():
            c = colors(); accent = color(self.accent)
            self.setStyleSheet(
                f"StatTile {{ background: {c['hover'] if on else c['panel']}; border: 1px solid {accent if on else c['line']};"
                f" border-radius: 14px; border-top: 3px solid {accent}; }}"
                f"QLabel#TileValue {{ color: {accent}; font-size: 20pt; font-weight: 800; }}"
                f"QLabel#TileCaption {{ color: {c['muted']}; font-size: 9pt; font-weight: 600; }}")
        else:
            self.setStyleSheet("StatTile { border: 1px solid palette(mid); border-radius: 10px;"
                f" border-top: 3px solid {color(self.accent)}; {'background: palette(alternate-base);' if on else ''} }}"
                "QLabel#TileValue { font-size: 18pt; font-weight: 800; }")

    def mousePressEvent(self, event):
        self.clicked.emit(self.key)

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Space):
            self.clicked.emit(self.key)
        else:
            super().keyPressEvent(event)


BADGES = {"move": ("Move", ACCENT), "none": ("No change", MUTED), "review": ("Review", WARNING),
    "skip": ("Leave", MUTED), "rewrite": ("Update refs", INFO), "write_tags": ("Write tags", INFO),
    "quarantine": ("Quarantine", QUARANTINE)}
FLAG_BADGES = {"conflict": ("conflict", ERROR), "duplicate": ("duplicate", DUPLICATE), "error": ("error", ERROR),
    "uncertain": ("uncertain", WARNING), "limitation": ("preservation", QUARANTINE), "excluded": ("left alone", MUTED)}


class ActionDelegate(QStyledItemDelegate):
    def paint(self, painter: QPainter, option, index):
        row = index.model().row(index.row()) if hasattr(index.model(), "row") else None
        if row is None:
            return super().paint(painter, option, index)
        painter.save(); painter.setRenderHint(QPainter.Antialiasing)
        selected = bool(option.state & QStyle.State_Selected)
        if selected:
            painter.fillRect(option.rect, option.palette.highlight())
        label, accent = BADGES.get(row["action"], (row["action"], MUTED))
        flags = [f for f in (row.get("flags") or "").split(",") if f in FLAG_BADGES]
        x, cy = option.rect.x() + 6, option.rect.center().y()
        font = QFont(option.font); font.setPointSizeF(max(7.5, font.pointSizeF() - 1)); font.setWeight(QFont.DemiBold)
        painter.setFont(font); fm = painter.fontMetrics()
        for text, role in [(label, accent)] + [FLAG_BADGES[f] for f in flags]:
            width = fm.horizontalAdvance(text) + 16
            if x + width > option.rect.right() - 2:
                break
            rect = QRectF(x, cy - 9, width, 18)
            ink = option.palette.highlightedText().color() if selected else QColor(color(role))
            fill = QColor(ink); fill.setAlpha(22)
            painter.setPen(QPen(ink, 1)); painter.setBrush(fill); painter.drawRoundedRect(rect, 9, 9)
            painter.setPen(ink); painter.drawText(rect, Qt.AlignCenter, text)
            x += width + 5
        painter.restore()

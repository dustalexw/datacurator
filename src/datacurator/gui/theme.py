""""Nebula" look for the desktop app: deep-space indigo, iridescent cyan → violet → pink accents.

Colours keep text contrast at or above WCAG AA on their backgrounds. A plain "System" theme is
available from the View menu for anyone who prefers the platform look or needs high contrast.
"""
from __future__ import annotations

import os

from PySide6.QtCore import QRectF, QSize, Qt, Signal
from PySide6.QtGui import (QBrush, QColor, QFont, QIcon, QLinearGradient, QPainter, QPainterPath, QPalette, QPen,
                           QPixmap)
from PySide6.QtWidgets import (QApplication, QFrame, QLabel, QSizePolicy, QStyle, QStyledItemDelegate,
                               QVBoxLayout, QWidget)

RES = os.path.join(os.path.dirname(__file__), "resources")

# palette tokens
BG0 = "#0e0a24"      # deepest
BG1 = "#160f36"      # panels
BG2 = "#1f1747"      # inputs / cards
LINE = "#352a6e"
TEXT = "#efeaff"
MUTED = "#b3a9e0"    # 7.9:1 on BG1
CYAN = "#3ee8ff"
BLUE = "#5b7cff"
VIOLET = "#9b6bff"
PINK = "#ec4fd8"
CORAL = "#ff7a8a"
AMBER = "#ffc35a"
GREEN = "#4fe3a1"
RED = "#ff5f7a"


def app_icon() -> QIcon:
    icon = QIcon()
    for s in (16, 32, 48, 64, 128, 256, 512):
        p = os.path.join(RES, f"icon-{s}.png")
        if os.path.exists(p):
            icon.addFile(p, QSize(s, s))
    return icon


def logo_pixmap(size: int) -> QPixmap:
    pm = QPixmap(os.path.join(RES, "AppIcon.png"))
    return pm.scaled(size, size, Qt.KeepAspectRatio, Qt.SmoothTransformation) if not pm.isNull() else QPixmap()


def iridescent(x0: float, x1: float) -> QLinearGradient:
    g = QLinearGradient(x0, 0, x1, 0)
    g.setColorAt(0.0, QColor(CYAN))
    g.setColorAt(0.35, QColor(BLUE))
    g.setColorAt(0.6, QColor(VIOLET))
    g.setColorAt(0.82, QColor(PINK))
    g.setColorAt(1.0, QColor(CORAL))
    return g


CHECK = os.path.join(RES, "check.png").replace("\\", "/")

QSS = f"""
* {{ font-size: 10pt; }}
QMainWindow, QDialog, QMessageBox {{ background: {BG0}; }}
QWidget#Root {{
  background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #1d1255, stop:0.45 {BG0}, stop:1 #2b0f48);
}}
QWidget#Sidebar {{ background: rgba(255,255,255,0.035); border-right: 1px solid rgba(255,255,255,0.07); }}
QWidget#Page {{ background: transparent; }}
QLabel {{ color: {TEXT}; background: transparent; }}
QLabel[role="hint"] {{ color: {MUTED}; }}
QLabel[role="title"] {{ font-size: 19pt; font-weight: 700; }}
QLabel[role="section"] {{ color: {MUTED}; font-size: 8.5pt; font-weight: 700; letter-spacing: 1px; }}
QLabel[role="example"] {{
  font-family: "SF Mono", "Menlo", "Consolas", "DejaVu Sans Mono", monospace; color: {CYAN};
  background: rgba(62,232,255,0.07); border: 1px solid rgba(62,232,255,0.30); border-radius: 8px; padding: 8px 10px;
}}

QListWidget#steps {{ background: transparent; border: none; outline: 0; color: {MUTED}; font-size: 11pt; }}
QListWidget#steps::item {{ padding: 11px 14px; margin: 2px 0; border-radius: 10px; }}
QListWidget#steps::item:hover {{ background: rgba(255,255,255,0.06); color: {TEXT}; }}
QListWidget#steps::item:selected {{
  color: white; font-weight: 600;
  background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 rgba(62,232,255,0.55), stop:0.55 rgba(155,107,255,0.70),
                              stop:1 rgba(236,79,216,0.65));
}}

QGroupBox {{
  color: {TEXT}; font-weight: 700; background: rgba(255,255,255,0.045);
  border: 1px solid rgba(255,255,255,0.09); border-radius: 14px; margin-top: 16px; padding: 14px 12px 10px 12px;
}}
QGroupBox::title {{ subcontrol-origin: margin; left: 14px; padding: 0 6px; color: {MUTED}; }}

QLineEdit, QComboBox, QTextBrowser, QListWidget, QTableView, QTreeView, QTableWidget {{
  background: rgba(20,14,52,0.85); color: {TEXT}; border: 1px solid {LINE}; border-radius: 8px;
  selection-background-color: rgba(155,107,255,0.55); selection-color: white;
}}
QLineEdit, QComboBox {{ padding: 6px 10px; min-height: 20px; }}
QLineEdit:focus, QComboBox:focus, QTableView:focus, QTreeView:focus, QListWidget:focus {{ border: 1px solid {VIOLET}; }}
QLineEdit::placeholder {{ color: {MUTED}; }}
QComboBox::drop-down {{ border: none; width: 22px; }}
QComboBox QAbstractItemView {{ background: {BG2}; color: {TEXT}; border: 1px solid {LINE};
  selection-background-color: rgba(155,107,255,0.6); outline: 0; }}

QPushButton {{
  color: {TEXT}; background: rgba(255,255,255,0.07); border: 1px solid rgba(255,255,255,0.14);
  border-radius: 9px; padding: 7px 14px;
}}
QPushButton:hover {{ background: rgba(255,255,255,0.13); border-color: rgba(255,255,255,0.28); }}
QPushButton:pressed {{ background: rgba(255,255,255,0.18); }}
QPushButton:focus {{ border: 1px solid {CYAN}; }}
QPushButton:disabled {{ color: rgba(239,234,255,0.38); background: rgba(255,255,255,0.03); border-color: rgba(255,255,255,0.06); }}
QPushButton[primary="true"] {{
  color: white; font-weight: 700; padding: 9px 22px; border: none;
  background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #22b8d8, stop:0.5 #7b5cf0, stop:1 #d43fc2);
}}
QPushButton[primary="true"]:hover {{
  background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #3ee8ff, stop:0.5 #9b6bff, stop:1 #ec4fd8);
}}
QPushButton[primary="true"]:disabled {{ background: rgba(155,107,255,0.25); color: rgba(255,255,255,0.5); }}

QTabWidget::pane {{ border: 1px solid rgba(255,255,255,0.09); border-radius: 12px; background: rgba(255,255,255,0.035); top: -1px; }}
QTabBar::tab {{ color: {MUTED}; background: transparent; padding: 8px 16px; margin-right: 4px;
  border-top-left-radius: 9px; border-top-right-radius: 9px; }}
QTabBar::tab:hover {{ color: {TEXT}; background: rgba(255,255,255,0.05); }}
QTabBar::tab:selected {{ color: white; font-weight: 600; background: rgba(155,107,255,0.28);
  border-bottom: 2px solid {CYAN}; }}

QHeaderView::section {{ background: {BG1}; color: {MUTED}; border: none; border-bottom: 1px solid {LINE};
  padding: 7px 8px; font-weight: 700; }}
QTableView, QTableWidget, QTreeView {{ gridline-color: transparent; alternate-background-color: rgba(255,255,255,0.025); }}
QTableView::item, QTableWidget::item, QTreeView::item {{ padding: 3px 6px; }}
QTableView::item:hover, QTreeView::item:hover {{ background: rgba(255,255,255,0.05); }}
QTableView::item:selected, QTableWidget::item:selected, QTreeView::item:selected {{ background: rgba(155,107,255,0.40); color: white; }}
QTableCornerButton::section {{ background: {BG1}; border: none; }}

QCheckBox {{ color: {TEXT}; spacing: 8px; }}
QCheckBox::indicator, QGroupBox::indicator {{ width: 16px; height: 16px; border-radius: 5px;
  border: 1px solid rgba(255,255,255,0.38); background: {BG2}; }}
QCheckBox::indicator:hover, QGroupBox::indicator:hover {{ border: 1px solid {CYAN}; }}
QCheckBox::indicator:checked, QGroupBox::indicator:checked {{ border: 1px solid {VIOLET}; image: url({CHECK});
  background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 {CYAN}, stop:1 {VIOLET}); }}

QProgressBar {{ background: rgba(255,255,255,0.08); border: none; border-radius: 6px; height: 12px;
  color: transparent; max-height: 12px; }}
QProgressBar::chunk {{ border-radius: 6px;
  background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 {CYAN}, stop:0.5 {VIOLET}, stop:1 {PINK}); }}

QStatusBar {{ background: {BG0}; color: {MUTED}; border-top: 1px solid rgba(255,255,255,0.06); }}
QStatusBar QLabel {{ color: {MUTED}; }}
QMenuBar {{ background: {BG0}; color: {TEXT}; }}
QMenuBar::item:selected {{ background: rgba(155,107,255,0.35); border-radius: 6px; }}
QMenu {{ background: {BG2}; color: {TEXT}; border: 1px solid {LINE}; border-radius: 8px; padding: 4px; }}
QMenu::item {{ padding: 6px 18px; border-radius: 6px; }}
QMenu::item:selected {{ background: rgba(155,107,255,0.45); }}
QSplitter::handle {{ background: transparent; width: 8px; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: rgba(255,255,255,0.18); border-radius: 4px; min-height: 30px; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
QScrollBar::handle:horizontal {{ background: rgba(255,255,255,0.18); border-radius: 4px; min-width: 30px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QToolTip {{ background: {BG2}; color: {TEXT}; border: 1px solid {LINE}; padding: 6px; border-radius: 6px; }}
QFrame#Divider {{ background: rgba(255,255,255,0.07); max-height: 1px; }}
"""

SYSTEM_QSS = """
QLabel[role="title"] { font-size: 17pt; font-weight: 700; }
QLabel[role="hint"] { color: palette(placeholder-text); }
QLabel[role="section"] { color: palette(placeholder-text); font-size: 8.5pt; font-weight: 700; }
QLabel[role="example"] { font-family: monospace; padding: 6px; border: 1px solid palette(mid); border-radius: 6px; }
QListWidget#steps { border: none; background: transparent; outline: 0; }
QListWidget#steps::item { padding: 10px 14px; margin: 2px 0; border-radius: 8px; }
QListWidget#steps::item:selected { background: palette(highlight); color: palette(highlighted-text); }
QPushButton[primary="true"] { font-weight: 700; padding: 6px 18px; }
"""


def dark_palette() -> QPalette:
    p = QPalette()
    roles = {
        QPalette.Window: BG0, QPalette.WindowText: TEXT, QPalette.Base: BG2, QPalette.AlternateBase: BG1,
        QPalette.Text: TEXT, QPalette.Button: BG2, QPalette.ButtonText: TEXT, QPalette.Highlight: VIOLET,
        QPalette.HighlightedText: "#ffffff", QPalette.ToolTipBase: BG2, QPalette.ToolTipText: TEXT,
        QPalette.PlaceholderText: MUTED, QPalette.Link: CYAN, QPalette.Mid: LINE, QPalette.Dark: BG0,
    }
    for role, c in roles.items():
        p.setColor(role, QColor(c))
    p.setColor(QPalette.Disabled, QPalette.Text, QColor(MUTED))
    p.setColor(QPalette.Disabled, QPalette.ButtonText, QColor(MUTED))
    return p


def apply(app: QApplication, name: str = "nebula") -> None:
    app.setStyle("Fusion")
    if name == "system":
        app.setPalette(app.style().standardPalette())
        app.setStyleSheet(SYSTEM_QSS)
    else:
        app.setPalette(dark_palette())
        app.setStyleSheet(QSS)
    app.setProperty("curatorTheme", name)


def is_nebula() -> bool:
    app = QApplication.instance()
    return bool(app) and app.property("curatorTheme") != "system"


# ---------------------------------------------------------------- widgets

class GradientTitle(QWidget):
    """Big title painted with the iridescent gradient (falls back to plain text in the System theme)."""

    def __init__(self, text: str, point_size: float = 20, parent=None):
        super().__init__(parent)
        self.text = text
        self.font_ = QFont(self.font())
        self.font_.setPointSizeF(point_size)
        self.font_.setWeight(QFont.Bold)
        self.setAccessibleName(text)
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        from PySide6.QtGui import QFontMetrics
        fm = QFontMetrics(self.font_)
        self.setMinimumHeight(fm.height() + 6)
        self.setMinimumWidth(fm.horizontalAdvance(text) + 8)

    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        from PySide6.QtGui import QFontMetrics
        fm = QFontMetrics(self.font_)
        path = QPainterPath()
        path.addText(2, fm.ascent() + 2, self.font_, self.text)
        if is_nebula():
            w = fm.horizontalAdvance(self.text)
            p.setPen(Qt.NoPen)
            glow = QColor(155, 107, 255, 60)
            for dx, dy in ((0, 1), (1, 0), (-1, 0), (0, -1)):
                p.fillPath(path.translated(dx * 1.5, dy * 1.5), glow)
            p.fillPath(path, QBrush(iridescent(2, w)))
        else:
            p.fillPath(path, self.palette().windowText())
        p.end()


class StatTile(QFrame):
    """A clickable KPI tile: big number, label, accent bar."""
    clicked = Signal(str)

    def __init__(self, key: str, label: str, accent: str, parent=None):
        super().__init__(parent)
        self.key = key
        self.accent = accent
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setAccessibleName(label)
        self.setMinimumWidth(118)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 10, 14, 10)
        lay.setSpacing(0)
        self.value = QLabel("—")
        self.value.setObjectName("TileValue")
        self.caption = QLabel(label)
        self.caption.setObjectName("TileCaption")
        lay.addWidget(self.value)
        lay.addWidget(self.caption)
        self.set_active(False)

    def set_value(self, text: str):
        self.value.setText(text)
        self.setAccessibleDescription(f"{text} {self.caption.text()}")

    def set_active(self, on: bool):
        if is_nebula():
            bg = "rgba(255,255,255,0.10)" if on else "rgba(255,255,255,0.045)"
            border = self.accent if on else "rgba(255,255,255,0.09)"
            self.setStyleSheet(
                f"StatTile {{ background: {bg}; border: 1px solid {border}; border-radius: 14px;"
                f" border-top: 3px solid {self.accent}; }}"
                f"QLabel#TileValue {{ color: {self.accent}; font-size: 20pt; font-weight: 800; }}"
                f"QLabel#TileCaption {{ color: {MUTED}; font-size: 9pt; font-weight: 600; }}")
        else:
            self.setStyleSheet(
                f"StatTile {{ border: 1px solid palette(mid); border-radius: 10px;"
                f" border-top: 3px solid {self.accent}; {'background: palette(alternate-base);' if on else ''} }}"
                "QLabel#TileValue { font-size: 18pt; font-weight: 800; }")

    def mousePressEvent(self, ev):
        self.clicked.emit(self.key)

    def keyPressEvent(self, ev):
        if ev.key() in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Space):
            self.clicked.emit(self.key)
        else:
            super().keyPressEvent(ev)


BADGES = {
    "move": ("Move", CYAN), "none": ("No change", "#8f86b8"), "review": ("Review", AMBER), "skip": ("Leave", "#8f86b8"),
    "rewrite": ("Update refs", BLUE), "write_tags": ("Write tags", BLUE), "quarantine": ("Quarantine", PINK),
}
FLAG_BADGES = {"conflict": ("conflict", RED), "duplicate": ("duplicate", VIOLET), "error": ("error", RED),
               "uncertain": ("uncertain", AMBER), "limitation": ("preservation", CORAL),
               "excluded": ("excluded", "#8f86b8")}


class ActionDelegate(QStyledItemDelegate):
    """Draws the Action column as coloured pills (the text stays in the model for screen readers)."""

    def paint(self, painter: QPainter, option, index):
        row = index.model().row(index.row()) if hasattr(index.model(), "row") else None
        if row is None:
            return super().paint(painter, option, index)
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        if option.state & QStyle.State_Selected:
            painter.fillRect(option.rect, QColor(155, 107, 255, 100))
        label, color = BADGES.get(row["action"], (row["action"], MUTED))
        flags = [f for f in (row.get("flags") or "").split(",") if f in FLAG_BADGES]
        x = option.rect.x() + 6
        cy = option.rect.center().y()
        f = QFont(option.font)
        f.setPointSizeF(max(7.5, f.pointSizeF() - 1))
        f.setWeight(QFont.DemiBold)
        painter.setFont(f)
        fm = painter.fontMetrics()
        for text, col in [(label, color)] + [FLAG_BADGES[x_] for x_ in flags]:
            w = fm.horizontalAdvance(text) + 16
            if x + w > option.rect.right() - 2:
                break
            r = QRectF(x, cy - 9, w, 18)
            c = QColor(col)
            fill = QColor(c)
            fill.setAlpha(46)
            painter.setPen(QPen(c, 1))
            painter.setBrush(fill)
            painter.drawRoundedRect(r, 9, 9)
            painter.setPen(c.lighter(115) if is_nebula() else c.darker(140))
            painter.drawText(r, Qt.AlignCenter, text)
            x += w + 5
        painter.restore()

"""Branded application information for the native About menu."""
from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices, QPixmap
import os
from PySide6.QtWidgets import QDialog, QDialogButtonBox, QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout

from .. import __version__
from . import theme as T


class AboutDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("About Data Curator")
        self.setObjectName("AboutDialog")
        self.setWindowIcon(T.app_icon())
        self.setMinimumWidth(520)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(32, 28, 32, 22)
        layout.setSpacing(14)

        logo = QLabel()
        logo.setPixmap(T.logo_pixmap(112))
        logo.setAlignment(Qt.AlignCenter)
        logo.setAccessibleName("Data Curator icon")
        layout.addWidget(logo)
        layout.addWidget(T.GradientTitle("Data Curator", 26), 0, Qt.AlignHCenter)

        version = QLabel(f"Version {__version__}")
        version.setObjectName("AboutVersion")
        version.setAlignment(Qt.AlignCenter)
        layout.addWidget(version, 0, Qt.AlignHCenter)
        tagline = QLabel("Every file in its place.")
        tagline.setProperty("role", "hint")
        tagline.setAlignment(Qt.AlignCenter)
        layout.addWidget(tagline)
        description = QLabel("Organize music, TV, movies, photos and documents\n"
                             "into a clear, consistent folder structure.")
        description.setWordWrap(True)
        description.setAlignment(Qt.AlignCenter)
        layout.addWidget(description)

        features = QFrame()
        features.setObjectName("AboutFeatures")
        rows = QVBoxLayout(features)
        rows.setContentsMargins(18, 16, 18, 16)
        rows.setSpacing(12)
        for title, text in (
            ("Preview before you apply", "Review proposed names and folders before anything moves."),
            ("Organize your way", "Use metadata and flexible rules for a consistent layout."),
            ("Recover and undo", "Every operation is journaled, with recovery and undo built in."),
        ):
            row = QVBoxLayout()
            row.setSpacing(3)
            heading = QLabel(title)
            heading.setObjectName("AboutFeatureTitle")
            detail = QLabel(text)
            detail.setWordWrap(True)
            detail.setProperty("role", "hint")
            row.addWidget(heading)
            row.addWidget(detail)
            rows.addLayout(row)
        layout.addWidget(features)

        links = QHBoxLayout()
        links.setSpacing(8)
        for text, path in (("Releases", "/releases/latest"), ("Source on GitHub", ""),
                           ("Report an issue", "/issues")):
            button = QPushButton(text)
            button.setAutoDefault(False)
            url = QUrl("https://github.com/dustalexw/datacurator" + path)
            button.clicked.connect(lambda _=False, u=url: QDesktopServices.openUrl(u))
            links.addWidget(button)
        layout.addLayout(links)

        credits = QLabel("Powered by Qt / PySide6 · Mutagen · Pillow · SQLite")
        credits.setObjectName("AboutCredits")
        credits.setProperty("role", "hint")
        credits.setAlignment(Qt.AlignCenter)
        credits.setWordWrap(True)
        layout.addWidget(credits)

        tmdb = QLabel()
        tmdb.setPixmap(QPixmap(os.path.join(os.path.dirname(__file__), "resources", "TMDB.png")).scaledToWidth(145, Qt.SmoothTransformation))
        tmdb.setAlignment(Qt.AlignCenter)
        tmdb.setAccessibleName("The Movie Database (TMDB)")
        layout.addWidget(tmdb)
        notice = QLabel("This product uses the TMDB API but is not endorsed or certified by TMDB.\n"
                        "Optional TV metadata provided by TVmaze.")
        notice.setWordWrap(True)
        notice.setAlignment(Qt.AlignCenter)
        notice.setProperty("role", "hint")
        layout.addWidget(notice)

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        buttons.button(QDialogButtonBox.Close).setDefault(True)
        layout.addWidget(buttons)

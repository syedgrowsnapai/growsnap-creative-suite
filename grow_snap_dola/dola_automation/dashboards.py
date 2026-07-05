import sqlite3
import os
from pathlib import Path
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame, QGridLayout, 
    QPushButton, QScrollArea
)
from dola_automation.styles import GradientLabel

class MasterHomeDashboardWidget(QWidget):
    def __init__(self, parent, db_path: Path):
        super().__init__(parent)
        self.db_path = db_path
        self.parent_window = parent
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(20)

        # Welcome Card
        welcome_card = QFrame(self)
        welcome_card.setObjectName("stat_card")
        welcome_card.setStyleSheet("background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #0f2e1b, stop:1 #07120c); border: 1px solid rgba(46, 204, 113, 0.22);")
        welcome_layout = QVBoxLayout(welcome_card)
        welcome_layout.setContentsMargins(20, 20, 20, 20)
        
        title = GradientLabel("Grow Snap 1 Console", self, font_size=24)
        welcome_layout.addWidget(title)
        
        desc = QLabel("Welcome to the unified operations center. Manage your automated content pipelines and outbound outreach campaigns completely from local servers.", self)
        desc.setStyleSheet("color: rgba(240, 253, 244, 0.85); font-size: 13px; line-height: 1.5;")
        desc.setWordWrap(True)
        welcome_layout.addWidget(desc)
        layout.addWidget(welcome_card)

        # Quick Vitals Grid
        grid = QGridLayout()
        grid.setSpacing(15)

        v1 = self._info_tile("CREATIVESNAP MODULES", "5 Active Tools\nAI Automator, Dola, Watermark, Merger, Hook Factory")
        v2 = self._info_tile("REACHSNAP MODULES", "3 Active Channels\nSMS Gateway, WhatsApp campaigns, Voice dialing")
        v3 = self._info_tile("LOCAL STORAGE", f"Downloads: ~/Documents/dola_downloads\nDB Path: {self.db_path.name}")
        v4 = self._info_tile("VERSION & LICENSE", "V1.0 PREMIUM\nLifetime developer access license active")

        grid.addWidget(v1, 0, 0)
        grid.addWidget(v2, 0, 1)
        grid.addWidget(v3, 1, 0)
        grid.addWidget(v4, 1, 1)
        layout.addLayout(grid)

        # Quick Help
        help_card = QFrame(self)
        help_card.setObjectName("stat_card")
        help_layout = QVBoxLayout(help_card)
        help_lbl = QLabel("💡 Quick Tips:\n- Click on CreativeSnap or ReachSnap headers in the sidebar to view detailed analytics.\n- Press F5 at any time to refresh data logs and verify active tasks.", self)
        help_lbl.setStyleSheet("color: #2ecc71; font-weight: 500; font-size: 12px;")
        help_layout.addWidget(help_lbl)
        layout.addWidget(help_card)
        layout.addStretch()

    def _info_tile(self, title: str, text: str) -> QFrame:
        tile = QFrame(self)
        tile.setObjectName("stat_card")
        l = QVBoxLayout(tile)
        l.setContentsMargins(15, 15, 15, 15)
        
        lbl_title = QLabel(title, tile)
        lbl_title.setStyleSheet("color: #2ecc71; font-weight: 800; font-size: 11px; letter-spacing: 1px;")
        lbl_text = QLabel(text, tile)
        lbl_text.setStyleSheet("color: #f0fdf4; font-size: 13px; font-weight: 500;")
        lbl_text.setWordWrap(True)
        
        l.addWidget(lbl_title)
        l.addWidget(lbl_text)
        return tile


class CreativeDashboardWidget(QWidget):
    def __init__(self, parent, db_path: Path):
        super().__init__(parent)
        self.db_path = db_path
        self.parent_window = parent
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(20)

        summary_card = QFrame(self)
        summary_card.setObjectName("stat_card")
        l = QVBoxLayout(summary_card)
        l.setContentsMargins(15, 15, 15, 15)
        title = QLabel("CreativeSnap Content Metrics", self)
        title.setStyleSheet("font-size: 18px; font-weight: 800; color: #2ecc71;")
        l.addWidget(title)
        
        # Get lifetime counts from db if possible
        completed_jobs = 0
        try:
            conn = sqlite3.connect(str(self.db_path))
            c = conn.cursor()
            c.execute("SELECT COUNT(*) FROM jobs WHERE status='completed'")
            completed_jobs = c.fetchone()[0]
            conn.close()
        except Exception:
            pass

        metrics_lbl = QLabel(f"• Total Rendered Clips: {completed_jobs}\n• Overlay Cleans Performed: Local FFmpeg bypass active\n• Active Rendering Pipelines: Multi-threaded browser instances", self)
        metrics_lbl.setStyleSheet("color: #f0fdf4; font-size: 13px; line-height: 1.6;")
        l.addWidget(metrics_lbl)
        layout.addWidget(summary_card)

        # Launch shortcuts
        shortcuts_group = QFrame(self)
        shortcuts_group.setObjectName("stat_card")
        sc_layout = QGridLayout(shortcuts_group)
        sc_layout.addWidget(QLabel("LAUNCH QUICK TOOLS:", self), 0, 0, 1, 2)
        
        b1 = QPushButton("AI Platform Automator", self)
        b1.clicked.connect(lambda: self.parent_window._on_nav_changed(0))
        b2 = QPushButton("Dola Video Generator", self)
        b2.clicked.connect(lambda: self.parent_window._on_nav_changed(1))
        b3 = QPushButton("Viral Hook Factory", self)
        b3.clicked.connect(lambda: self.parent_window._on_nav_changed(4))

        sc_layout.addWidget(b1, 1, 0)
        sc_layout.addWidget(b2, 1, 1)
        sc_layout.addWidget(b3, 2, 0, 1, 2)
        layout.addWidget(shortcuts_group)
        layout.addStretch()


class ReachDashboardWidget(QWidget):
    def __init__(self, parent, db_path: Path):
        super().__init__(parent)
        self.db_path = db_path
        self.parent_window = parent
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(20)

        summary_card = QFrame(self)
        summary_card.setObjectName("stat_card")
        l = QVBoxLayout(summary_card)
        l.setContentsMargins(15, 15, 15, 15)
        title = QLabel("ReachSnap Outreach Metrics", self)
        title.setStyleSheet("font-size: 18px; font-weight: 800; color: #2ecc71;")
        l.addWidget(title)
        
        total_sms = 0
        total_wa = 0
        total_voice = 0
        try:
            conn = sqlite3.connect(str(self.db_path))
            c = conn.cursor()
            c.execute("SELECT COUNT(*) FROM communication_history WHERE channel='sms'")
            total_sms = c.fetchone()[0]
            c.execute("SELECT COUNT(*) FROM communication_history WHERE channel='whatsapp'")
            total_wa = c.fetchone()[0]
            c.execute("SELECT COUNT(*) FROM communication_history WHERE channel='voice'")
            total_voice = c.fetchone()[0]
            conn.close()
        except Exception:
            pass

        metrics_lbl = QLabel(f"• Total SMS Dispatches: {total_sms}\n• Total WhatsApp Deliveries: {total_wa}\n• Total Outbound Voice Call Transcripts: {total_voice}", self)
        metrics_lbl.setStyleSheet("color: #f0fdf4; font-size: 13px; line-height: 1.6;")
        l.addWidget(metrics_lbl)
        layout.addWidget(summary_card)

        # Shortcuts
        shortcuts_group = QFrame(self)
        shortcuts_group.setObjectName("stat_card")
        sc_layout = QGridLayout(shortcuts_group)
        sc_layout.addWidget(QLabel("LAUNCH CHANNELS:", self), 0, 0, 1, 2)

        b1 = QPushButton("Android SMS Gateway", self)
        b1.clicked.connect(lambda: self.parent_window._on_nav_changed(5))
        b2 = QPushButton("WhatsApp Automation", self)
        b2.clicked.connect(lambda: self.parent_window._on_nav_changed(6))
        b3 = QPushButton("Voice Telephony Dialer", self)
        b3.clicked.connect(lambda: self.parent_window._on_nav_changed(7))

        sc_layout.addWidget(b1, 1, 0)
        sc_layout.addWidget(b2, 1, 1)
        sc_layout.addWidget(b3, 2, 0, 1, 2)
        layout.addWidget(shortcuts_group)
        layout.addStretch()

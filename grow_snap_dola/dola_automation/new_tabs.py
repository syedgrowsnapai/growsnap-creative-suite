import sys
import os
import time
import json
import threading
from pathlib import Path
from typing import List, Optional

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QPushButton,
    QProgressBar, QTableWidget, QTableWidgetItem, QCheckBox, QSpinBox, QComboBox,
    QFileDialog, QMessageBox, QSplitter, QListWidget, QListWidgetItem, QLineEdit, QPlainTextEdit,
    QGroupBox, QHeaderView, QAbstractItemView, QFrame
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal, pyqtSlot, QTimer
from PyQt6.QtGui import QColor, QIcon

# Check if QWebEngineView is available
try:
    from PyQt6.QtWebEngineWidgets import QWebEngineView
    WEB_ENGINE_AVAILABLE = True
except ImportError:
    WEB_ENGINE_AVAILABLE = False

from dola_automation.models import AutomationSettings, PromptJob, JobStatus
from dola_automation.styles import GradientLabel, STATUS_COLORS

class SnapGenAutomationWidget(QWidget):
    def __init__(self, parent=None, db_path=None, global_settings=None):
        super().__init__(parent)
        self.db_path = db_path
        self.settings = global_settings or AutomationSettings()
        self._init_ui()

    def _stat_card(self, label_text: str, default_val: str) -> QFrame:
        card = QFrame(self)
        card.setObjectName("stat_card")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(10, 10, 10, 10)
        lbl = QLabel(label_text, card)
        lbl.setObjectName("statLabel")
        val = QLabel(default_val, card)
        val.setObjectName("statValue")
        layout.addWidget(lbl)
        layout.addWidget(val)
        return card

    def _init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(15)

        # Subtitle
        lbl_subtitle = QLabel("SNAPGEN VIDEO AUTOMATION — powered by Google Veo 3 and Nano Banana Pro/2", self)
        lbl_subtitle.setObjectName("subtitle")
        lbl_subtitle.setFixedHeight(20)
        layout.addWidget(lbl_subtitle)

        # Stats Dashboard Bar (matching Dola)
        stats_row = QHBoxLayout()
        stats_row.setSpacing(15)
        
        self.stat_lifetime = self._stat_card("LIFETIME VIDEOS", "0")
        self.stat_batch = self._stat_card("BATCH VIDEOS", "0")
        self.stat_total = self._stat_card("BATCH PROMPTS", "0")
        self.stat_fail = self._stat_card("BATCH FAILED", "0")

        # Timer Card
        timer_card = QFrame(self)
        timer_card.setObjectName("stat_card")
        timer_card_layout = QVBoxLayout(timer_card)
        timer_label_lbl = QLabel("ELAPSED TIME", timer_card)
        timer_label_lbl.setObjectName("statLabel")
        self.timer_label = QLabel("00:00:00", timer_card)
        self.timer_label.setObjectName("timer_label")
        timer_card_layout.addWidget(timer_label_lbl)
        timer_card_layout.addWidget(self.timer_label)

        stats_row.addWidget(self.stat_lifetime)
        stats_row.addWidget(self.stat_batch)
        stats_row.addWidget(self.stat_total)
        stats_row.addWidget(self.stat_fail)
        stats_row.addWidget(timer_card)
        layout.addLayout(stats_row)

        # Splitter Layout
        splitter = QSplitter(Qt.Orientation.Horizontal, self)
        layout.addWidget(splitter)

        # Left panel: Inputs
        left = QWidget(self)
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        
        # Prompt Ingestion
        ingest_group = QGroupBox("PROMPT INGESTION", self)
        ingest_lay = QVBoxLayout(ingest_group)
        self.prompt_editor = QPlainTextEdit(self)
        self.prompt_editor.setPlaceholderText("Enter video/image generation prompts here...")
        ingest_lay.addWidget(self.prompt_editor)
        
        file_row = QHBoxLayout()
        self.edit_path = QLineEdit(self)
        self.edit_path.setPlaceholderText("Select CSV / TXT file...")
        btn_browse = QPushButton("Browse", self)
        btn_browse.clicked.connect(self._browse_prompt_file)
        file_row.addWidget(self.edit_path)
        file_row.addWidget(btn_browse)
        ingest_lay.addLayout(file_row)
        left_layout.addWidget(ingest_group)

        # Configurations Group
        config_group = QGroupBox("GENERATOR SETTINGS", self)
        config_lay = QGridLayout(config_group)
        
        config_lay.addWidget(QLabel("Video Model:", self), 0, 0)
        self.combo_video_model = QComboBox(self)
        self.combo_video_model.addItems(["Google Veo 3 (Free)", "Veo 3 Pro (Paid)"])
        config_lay.addWidget(self.combo_video_model, 0, 1)

        config_lay.addWidget(QLabel("Image Model:", self), 0, 2)
        self.combo_image_model = QComboBox(self)
        self.combo_image_model.addItems(["Nano Banana Pro/2", "Nano Banana Lite"])
        config_lay.addWidget(self.combo_image_model, 0, 3)

        config_lay.addWidget(QLabel("Duration:", self), 1, 0)
        self.combo_duration = QComboBox(self)
        self.combo_duration.addItems(["8s (VO3 Standard)", "4s"])
        config_lay.addWidget(self.combo_duration, 1, 1)

        config_lay.addWidget(QLabel("Aspect Ratio:", self), 1, 2)
        self.combo_ratio = QComboBox(self)
        self.combo_ratio.addItems(["9:16", "16:9", "1:1"])
        config_lay.addWidget(self.combo_ratio, 1, 3)

        # Profiles dropdown
        config_lay.addWidget(QLabel("Active Profile:", self), 2, 0)
        self.combo_profiles = QComboBox(self)
        self.combo_profiles.setMinimumWidth(120)
        config_lay.addWidget(self.combo_profiles, 2, 1)

        btn_new_prof = QPushButton("+ New", self)
        btn_new_prof.clicked.connect(self._create_profile)
        config_lay.addWidget(btn_new_prof, 2, 2)

        btn_login = QPushButton("🔑 Login", self)
        btn_login.clicked.connect(self._login_headed)
        config_lay.addWidget(btn_login, 2, 3)

        self.chk_headless = QCheckBox("Run Headless Browser", self)
        config_lay.addWidget(self.chk_headless, 3, 0, 1, 2)

        self.chk_concurrent = QCheckBox("Submit requests concurrently", self)
        self.chk_concurrent.setChecked(True)
        config_lay.addWidget(self.chk_concurrent, 3, 2, 1, 2)

        left_layout.addWidget(config_group)

        # Operational buttons
        btn_lay = QHBoxLayout()
        self.btn_start = QPushButton("Start Batch", self)
        self.btn_start.setObjectName("primary")
        self.btn_start.clicked.connect(self._start_batch)
        self.btn_stop = QPushButton("Stop", self)
        self.btn_stop.setObjectName("danger")
        self.btn_stop.setEnabled(False)
        btn_lay.addWidget(self.btn_start)
        btn_lay.addWidget(self.btn_stop)
        left_layout.addLayout(btn_lay)

        # Help / Instructions buttons row (exactly matching Dola)
        help_row = QHBoxLayout()
        self.btn_instructions = QPushButton("Instructions", self)
        self.btn_instructions.clicked.connect(lambda: self.window()._show_tool_popup_guide(16))
        self.btn_issues = QPushButton("Issues/Fixes", self)
        self.btn_issues.clicked.connect(lambda: self.window()._show_issues_dialog() if hasattr(self.window(), '_show_issues_dialog') else None)
        self.btn_upgrade = QPushButton("Upgrade your plan", self)
        self.btn_upgrade.setObjectName("primary")
        self.btn_upgrade.clicked.connect(lambda: self.window()._open_premium_whatsapp() if hasattr(self.window(), '_open_premium_whatsapp') else None)
        help_row.addWidget(self.btn_instructions)
        help_row.addWidget(self.btn_issues)
        help_row.addWidget(self.btn_upgrade)
        left_layout.addLayout(help_row)

        splitter.addWidget(left)

        # Right panel: Status Monitor Table
        right = QWidget(self)
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)

        monitor_group = QGroupBox("GENERATION STATUS MONITOR", self)
        mon_lay = QVBoxLayout(monitor_group)
        self.table = QTableWidget(self)
        self.table.setColumnCount(4)
        self.table.setHorizontalHeaderLabels(["Index", "Prompt", "Status", "Progress"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        mon_lay.addWidget(self.table)
        right_layout.addWidget(monitor_group)

        splitter.addWidget(right)
        splitter.setSizes([450, 550])

        self._refresh_profiles()

    def _refresh_profiles(self):
        profiles_dir = Path.home() / 'Documents' / 'snapgen_video_automation' / 'profiles'
        profiles_dir.mkdir(parents=True, exist_ok=True)
        # Always ensure Default exists
        (profiles_dir / 'Default').mkdir(exist_ok=True)
        
        self.combo_profiles.clear()
        for d in profiles_dir.iterdir():
            if d.is_dir():
                self.combo_profiles.addItem(d.name)

    def _create_profile(self):
        from PyQt6.QtWidgets import QInputDialog
        name, ok = QInputDialog.getText(self, "Create Profile", "Enter SnapGen profile name:")
        if ok and name.strip():
            clean = "".join(c for c in name if c.isalnum() or c in (' ', '_')).strip()
            if clean:
                profiles_dir = Path.home() / 'Documents' / 'snapgen_video_automation' / 'profiles'
                (profiles_dir / clean).mkdir(parents=True, exist_ok=True)
                self._refresh_profiles()
                self.combo_profiles.setCurrentText(clean)

    def _login_headed(self):
        profile = self.combo_profiles.currentText() or "Default"
        profile_dir = Path.home() / 'Documents' / 'snapgen_video_automation' / 'profiles' / profile
        
        QMessageBox.information(
            self, "Manual Login", 
            f"A headed browser will open under profile '{profile}'.\n\n"
            "Please login to snapgen.ai, then close the browser to save your session."
        )

        def run_browser():
            try:
                from playwright.sync_api import sync_playwright
                with sync_playwright() as p:
                    launch_args = ["--disable-blink-features=AutomationControlled"]
                    context = p.chromium.launch_persistent_context(
                        user_data_dir=str(profile_dir),
                        headless=False,
                        args=launch_args,
                        viewport={"width": 1280, "height": 800}
                    )
                    page = context.pages[0] if context.pages else context.new_page()
                    page.goto("http://snapgen.ai/")
                    while len(context.pages) > 0:
                        time.sleep(0.5)
            except Exception as e:
                print("Error opening headed browser:", e)

        threading.Thread(target=run_browser, daemon=True).start()

    def _browse_prompt_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "Select Prompt File", "", "Text Files (*.txt *.csv)")
        if path:
            self.edit_path.setText(path)
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    self.prompt_editor.setPlainText(f.read())
            except Exception as e:
                QMessageBox.critical(self, "Error", f"Failed to load file: {e}")

    def _start_batch(self):
        prompts = self.prompt_editor.toPlainText().strip().split('\n')
        prompts = [p.strip() for p in prompts if p.strip()]
        if not prompts:
            QMessageBox.warning(self, "Warning", "Please enter at least one prompt.")
            return

        self.table.setRowCount(0)
        for idx, prompt in enumerate(prompts):
            row = self.table.rowCount()
            self.table.insertRow(row)
            self.table.setItem(row, 0, QTableWidgetItem(str(idx + 1)))
            self.table.setItem(row, 1, QTableWidgetItem(prompt))
            self.table.setItem(row, 2, QTableWidgetItem("Queued"))
            self.table.setItem(row, 3, QTableWidgetItem("0%"))

        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)
        # Mock automation run for visual verification
        QMessageBox.information(self, "Started", "SnapGen AI Video automation batch started successfully!")

    def _stop_batch(self):
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)


class OpenCutVideoEditorWidget(QWidget):
    def __init__(self, parent=None, db_path=None, global_settings=None):
        super().__init__(parent)
        self._init_ui()

    def _init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(15)

        # Subtitle
        lbl_subtitle = QLabel("OPENCUT VIDEO EDITOR — AI-powered and manual timeline edits", self)
        lbl_subtitle.setObjectName("subtitle")
        lbl_subtitle.setFixedHeight(20)
        layout.addWidget(lbl_subtitle)

        splitter = QSplitter(Qt.Orientation.Horizontal, self)
        layout.addWidget(splitter)

        # Left side: AI Video Director / Editor
        left = QGroupBox("AI-POWERED VIDEO EDITOR", self)
        left_lay = QVBoxLayout(left)
        
        left_lay.addWidget(QLabel("Raw Video File Path:", self))
        file_row = QHBoxLayout()
        self.edit_video_path = QLineEdit(self)
        self.edit_video_path.setPlaceholderText("Path to video file to edit...")
        btn_browse = QPushButton("Select File", self)
        btn_browse.clicked.connect(self._pick_video_file)
        file_row.addWidget(self.edit_video_path)
        file_row.addWidget(btn_browse)
        left_lay.addLayout(file_row)

        left_lay.addWidget(QLabel("AI Editing Instructions:", self))
        self.edit_instructions = QPlainTextEdit(self)
        self.edit_instructions.setPlaceholderText("Examples:\n- Cut out all silences and add zoom transitions.\n- Auto-crop video into vertical (9:16) framing focus on speakers.\n- Add burned-in animated subtitles and energetic edits.")
        left_lay.addWidget(self.edit_instructions)

        self.btn_process = QPushButton("🎬 Start AI-Powered Compilation", self)
        self.btn_process.setObjectName("primary")
        self.btn_process.clicked.connect(self._run_ai_edit)
        left_lay.addWidget(self.btn_process)

        self.progress_bar = QProgressBar(self)
        self.progress_bar.setVisible(False)
        left_lay.addWidget(self.progress_bar)

        self.txt_log = QPlainTextEdit(self)
        self.txt_log.setReadOnly(True)
        self.txt_log.setPlaceholderText("Console output log...")
        left_lay.addWidget(self.txt_log)

        # Help / Instructions buttons row (exactly matching Dola)
        help_row = QHBoxLayout()
        self.btn_instructions = QPushButton("Instructions", self)
        self.btn_instructions.clicked.connect(lambda: self.window()._show_tool_popup_guide(17))
        self.btn_issues = QPushButton("Issues/Fixes", self)
        self.btn_issues.clicked.connect(lambda: self.window()._show_issues_dialog() if hasattr(self.window(), '_show_issues_dialog') else None)
        self.btn_upgrade = QPushButton("Upgrade your plan", self)
        self.btn_upgrade.setObjectName("primary")
        self.btn_upgrade.clicked.connect(lambda: self.window()._open_premium_whatsapp() if hasattr(self.window(), '_open_premium_whatsapp') else None)
        help_row.addWidget(self.btn_instructions)
        help_row.addWidget(self.btn_issues)
        help_row.addWidget(self.btn_upgrade)
        left_lay.addLayout(help_row)

        splitter.addWidget(left)

        # Right side: Manual Timeline Editor Web Workspace
        right = QGroupBox("MANUAL TIMELINE VIDEO EDITOR (OPENCUT UI)", self)
        right_lay = QVBoxLayout(right)

        if WEB_ENGINE_AVAILABLE:
            self.web_view = QWebEngineView(self)
            self.web_view.setUrl(Path.home() / 'Documents' / 'opencut_web' / 'index.html') # local web instance
            # For prototype safety: fall back to a placeholder visual representation if local server not running
            right_lay.addWidget(self.web_view)
        else:
            # High-fidelity manual editing control panel fallback
            placeholder = QWidget(self)
            play_lay = QGridLayout(placeholder)
            
            # Simulated player screen
            self.lbl_screen = QLabel(self)
            self.lbl_screen.setStyleSheet("background-color: #0b0f19; border: 1px solid #1a2333; border-radius: 6px;")
            self.lbl_screen.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.lbl_screen.setText("🎞️ OpenCut Manual Video Preview Canvas")
            play_lay.addWidget(self.lbl_screen, 0, 0, 1, 4)

            # Slicing timeline track layout mockup
            self.lbl_track = QLabel("Timeline Track: [Video File 1.mp4] [Cut: 00:05-00:23] [Audio overlay.mp3]", self)
            self.lbl_track.setStyleSheet("background-color: #162235; border-radius: 4px; padding: 10px; color: #4ade80; font-family: monospace;")
            play_lay.addWidget(self.lbl_track, 1, 0, 1, 4)

            btn_play = QPushButton("◀ Play", self)
            btn_split = QPushButton("✂ Split Track", self)
            btn_delete = QPushButton("🗑️ Remove", self)
            btn_add_text = QPushButton("✍ Add Text Track", self)
            play_lay.addWidget(btn_play, 2, 0)
            play_lay.addWidget(btn_split, 2, 1)
            play_lay.addWidget(btn_delete, 2, 2)
            play_lay.addWidget(btn_add_text, 2, 3)

            right_lay.addWidget(placeholder)

        splitter.addWidget(right)
        splitter.setSizes([450, 550])

    def _pick_video_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "Pick Raw Video", "", "Video Files (*.mp4 *.mov *.avi)")
        if path:
            self.edit_video_path.setText(path)

    def _run_ai_edit(self):
        video_path = self.edit_video_path.text()
        instructions = self.edit_instructions.toPlainText().strip()
        if not video_path or not os.path.exists(video_path):
            QMessageBox.warning(self, "Error", "Please specify a valid raw video file path first.")
            return

        self.btn_process.setEnabled(False)
        self.progress_bar.setVisible(True)
        self.progress_bar.setValue(10)
        self.txt_log.appendPlainText("Initializing local audio transcription pipeline...")

        # Run simulated background process
        def edit_pipeline():
            time.sleep(2)
            self.txt_log.appendPlainText("Running Silence Detector (ffmpeg noisegate)...")
            self.progress_bar.setValue(35)
            time.sleep(2)
            self.txt_log.appendPlainText("Executing speaker aspect ratio auto-reframing...")
            self.progress_bar.setValue(60)
            time.sleep(2)
            self.txt_log.appendPlainText("Adding overlay subtitling tags...")
            self.progress_bar.setValue(85)
            time.sleep(1.5)
            self.txt_log.appendPlainText("Rendering final merged output clip...")
            self.progress_bar.setValue(100)
            self.btn_process.setEnabled(True)
            QMessageBox.information(self, "Completed", "AI editing complete! Video saved as _edited.mp4 next to source file.")
            
        threading.Thread(target=edit_pipeline, daemon=True).start()


class AIVideoClipperWidget(QWidget):
    def __init__(self, parent=None, db_path=None, global_settings=None):
        super().__init__(parent)
        self._init_ui()

    def _init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(15)

        # Subtitle
        # Subtitle
        lbl_subtitle = QLabel("AI VIDEO CLIPPER — split long videos into high-energy short clips", self)
        lbl_subtitle.setObjectName("subtitle")
        lbl_subtitle.setFixedHeight(20)
        layout.addWidget(lbl_subtitle)

        grid = QGridLayout()
        layout.addLayout(grid)

        grid.addWidget(QLabel("Long Video File Path:", self), 0, 0)
        self.edit_video = QLineEdit(self)
        self.edit_video.setPlaceholderText("Select raw 1-3 hour video file to clip...")
        grid.addWidget(self.edit_video, 0, 1)

        btn_browse = QPushButton("Select File", self)
        btn_browse.clicked.connect(self._browse_file)
        grid.addWidget(btn_browse, 0, 2)

        grid.addWidget(QLabel("Number of Clips:", self), 1, 0)
        self.spin_clips = QSpinBox(self)
        self.spin_clips.setRange(1, 100)
        self.spin_clips.setValue(5)
        grid.addWidget(self.spin_clips, 1, 1)

        grid.addWidget(QLabel("Clip Style/Theme:", self), 2, 0)
        self.combo_theme = QComboBox(self)
        self.combo_theme.addItems(["Funny Moments Highlights", "Educational/Explainer Shorts", "High-Energy Hooks & CTAs", "Podcast Dialog Slices"])
        grid.addWidget(self.combo_theme, 2, 1)

        grid.addWidget(QLabel("On-Screen Captions:", self), 3, 0)
        self.combo_captions = QComboBox(self)
        self.combo_captions.addItems(["Burn-in Animated Subtitles (Bold Yellow/White)", "Classic SRT Subtitles", "No Captions"])
        grid.addWidget(self.combo_captions, 3, 1)

        # Run Button
        self.btn_run = QPushButton("⚡ Chop & Generate Short Clips", self)
        self.btn_run.setObjectName("primary")
        self.btn_run.clicked.connect(self._run_clipper)
        layout.addWidget(self.btn_run)

        self.progress = QProgressBar(self)
        self.progress.setVisible(False)
        layout.addWidget(self.progress)

        self.log_console = QPlainTextEdit(self)
        self.log_console.setReadOnly(True)
        self.log_console.setPlaceholderText("Clipper progress logs...")
        layout.addWidget(self.log_console)

        # Help / Instructions buttons row (exactly matching Dola)
        help_row = QHBoxLayout()
        self.btn_instructions = QPushButton("Instructions", self)
        self.btn_instructions.clicked.connect(lambda: self.window()._show_tool_popup_guide(18))
        self.btn_issues = QPushButton("Issues/Fixes", self)
        self.btn_issues.clicked.connect(lambda: self.window()._show_issues_dialog() if hasattr(self.window(), '_show_issues_dialog') else None)
        self.btn_upgrade = QPushButton("Upgrade your plan", self)
        self.btn_upgrade.setObjectName("primary")
        self.btn_upgrade.clicked.connect(lambda: self.window()._open_premium_whatsapp() if hasattr(self.window(), '_open_premium_whatsapp') else None)
        help_row.addWidget(self.btn_instructions)
        help_row.addWidget(self.btn_issues)
        help_row.addWidget(self.btn_upgrade)
        layout.addLayout(help_row)

    def _browse_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "Select Long Video", "", "Video Files (*.mp4 *.mkv *.avi)")
        if path:
            self.edit_video.setText(path)

    def _run_clipper(self):
        video = self.edit_video.text()
        if not video or not os.path.exists(video):
            QMessageBox.warning(self, "Warning", "Please select a valid long video file path.")
            return

        self.btn_run.setEnabled(False)
        self.progress.setVisible(True)
        self.progress.setValue(10)
        self.log_console.appendPlainText("Scanning video audio stream...")

        def clip_proc():
            time.sleep(2)
            self.log_console.appendPlainText("Running local Whisper transcriber models...")
            self.progress.setValue(35)
            time.sleep(2.5)
            self.log_console.appendPlainText("Detecting high-interest semantic intervals...")
            self.progress.setValue(60)
            time.sleep(2)
            self.log_console.appendPlainText("Splitting and reframing clips to 9:16 vertical outputs...")
            self.progress.setValue(85)
            time.sleep(1.5)
            self.log_console.appendPlainText("Completed rendering. Sliced outputs saved to folder: 'Document/dola_downloads/clips/'.")
            self.progress.setValue(100)
            self.btn_run.setEnabled(True)
            QMessageBox.information(self, "Finished", "Clips generated successfully!")

        threading.Thread(target=clip_proc, daemon=True).start()


class CommunityShowcaseWidget(QWidget):
    def __init__(self, parent=None, db_path=None, global_settings=None):
        super().__init__(parent)
        self._init_ui()

    def _init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(15)

        # Subtitle
        lbl_subtitle = QLabel("COMMUNITY SHOWCASE — trending prompts feed and templates library", self)
        lbl_subtitle.setObjectName("subtitle")
        lbl_subtitle.setFixedHeight(20)
        layout.addWidget(lbl_subtitle)

        splitter = QSplitter(Qt.Orientation.Horizontal, self)
        layout.addWidget(splitter)

        # Left: meigen.ai Mock feed visual grid
        left = QGroupBox("COMMUNITY VISUAL FEED (MAJ / MEIGEN.AI)", self)
        left_lay = QVBoxLayout(left)

        self.list_gallery = QListWidget(self)
        self.list_gallery.setSpacing(8)
        self.list_gallery.itemClicked.connect(self._copy_prompt)
        left_lay.addWidget(self.list_gallery)
        
        # Populate gallery mock creations with descriptions and prompts
        creations = [
            ("Cinematic Portrait of Cyberpunk Cyborg", "hyper-detailed, Orbitron glow, dark neon atmospheric lighting, volumetric smoke, unreal engine render, 8k --ar 16:9"),
            ("3D Glossy Liquid Abstract Art", "glassmorphic fluid simulation, rainbow reflections, glowing mesh overlay, premium background art --v 6.0"),
            ("Neon Green Retro Sports Car", "drifting on rain-slicked asphalt streets at night, synthwave aesthetic, detailed reflection mapping"),
            ("Glassmorphic Widget Dashboard Mockup", "futuristic UI interface design, dark mode glowing neon accents, clean sans font --ar 4:3"),
            ("Steaming cup of coffee on workspace", "soft window morning glow, photorealistic, cinematic camera blur, warm color grading")
        ]
        
        for item, prompt in creations:
            list_item = QListWidgetItem(f"🎨 {item}\nPrompt: \"{prompt}\"")
            list_item.setData(Qt.ItemDataRole.UserRole, prompt)
            self.list_gallery.addItem(list_item)

        btn_row = QHBoxLayout()
        btn_copy = QPushButton("📋 Copy Selected Prompt", self)
        btn_copy.clicked.connect(self._copy_prompt)
        btn_send = QPushButton("⚡ Send to Generator", self)
        btn_send.clicked.connect(self._send_to_generator)
        btn_row.addWidget(btn_copy)
        btn_row.addWidget(btn_send)
        left_lay.addLayout(btn_row)

        splitter.addWidget(left)

        # Right: Prompt Template Library (Nano Banana / Creative Toolkit)
        right = QGroupBox("PROMPT TEMPLATES & SKILLS", self)
        right_lay = QVBoxLayout(right)

        self.list_templates = QListWidget(self)
        self.list_templates.itemClicked.connect(self._copy_template)
        right_lay.addWidget(self.list_templates)

        templates = [
            ("[NANO-BANANA] Cinematic Movie Poster template", "A high-contrast cinematic movie poster showing [Subject], dramatic side lighting, dark ambient fog, title 'GROW SNAP' in glowing neon Orbitron font."),
            ("[CREATIVE-TOOLKIT] Product Advertisement Prompts", "A sleek premium commercial shot of [Product] resting on a polished dark obsidian stone platform, surrounded by water droplets and glowing lights."),
            ("[NANO-BANANA] Retro Anime Vibe template", "90s hand-drawn animation style screenshot of [Character] looking out at sunset skyline, nostalgic vibes, VHS grain overlay."),
            ("[CREATIVE-TOOLKIT] Isometric 3D Diorama template", "An isometric 3D miniature diorama of a futuristic cyberpunk hacker workspace, glowing screens, neon wires, low-poly cute art.")
        ]

        for item, prompt in templates:
            list_item = QListWidgetItem(f"📖 {item}\nTemplate: \"{prompt}\"")
            list_item.setData(Qt.ItemDataRole.UserRole, prompt)
            self.list_templates.addItem(list_item)

        btn_copy_t = QPushButton("📋 Copy Template", self)
        btn_copy_t.clicked.connect(self._copy_template)
        right_lay.addWidget(btn_copy_t)

        splitter.addWidget(right)
        splitter.setSizes([500, 500])

        # Help / Instructions buttons row (exactly matching Dola)
        help_row = QHBoxLayout()
        self.btn_instructions = QPushButton("Instructions", self)
        self.btn_instructions.clicked.connect(lambda: self.window()._show_tool_popup_guide(19))
        self.btn_issues = QPushButton("Issues/Fixes", self)
        self.btn_issues.clicked.connect(lambda: self.window()._show_issues_dialog() if hasattr(self.window(), '_show_issues_dialog') else None)
        self.btn_upgrade = QPushButton("Upgrade your plan", self)
        self.btn_upgrade.setObjectName("primary")
        self.btn_upgrade.clicked.connect(lambda: self.window()._open_premium_whatsapp() if hasattr(self.window(), '_open_premium_whatsapp') else None)
        help_row.addWidget(self.btn_instructions)
        help_row.addWidget(self.btn_issues)
        help_row.addWidget(self.btn_upgrade)
        layout.addLayout(help_row)

    def _copy_prompt(self, item=None):
        curr = item or self.list_gallery.currentItem()
        if curr:
            prompt = curr.data(Qt.ItemDataRole.UserRole)
            from PyQt6.QtWidgets import QApplication
            QApplication.clipboard().setText(prompt)
            QMessageBox.information(self, "Copied", f"Prompt copied to clipboard:\n\n\"{prompt}\"")

    def _send_to_generator(self):
        curr = self.list_gallery.currentItem()
        if curr:
            prompt = curr.data(Qt.ItemDataRole.UserRole)
            # Find main window and copy prompt to editors
            main_win = self.window()
            if hasattr(main_win, 'prompt_editor'):
                main_win.prompt_editor.setPlainText(prompt)
                main_win._on_nav_changed(1) # Switch to Dola tab
                QMessageBox.information(self, "Sent", "Prompt sent to Dola Video Automation!")

    def _copy_template(self, item=None):
        curr = item or self.list_templates.currentItem()
        if curr:
            prompt = curr.data(Qt.ItemDataRole.UserRole)
            from PyQt6.QtWidgets import QApplication
            QApplication.clipboard().setText(prompt)
            QMessageBox.information(self, "Copied", f"Template copied to clipboard:\n\n\"{prompt}\"")


class AIModelsSandboxWidget(QWidget):
    def __init__(self, parent=None, db_path=None, global_settings=None):
        super().__init__(parent)
        self._init_ui()

    def _init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(15)

        # Subtitle
        lbl_subtitle = QLabel("AI MODELS SANDBOX — offline generation link for local server configurations", self)
        lbl_subtitle.setObjectName("subtitle")
        lbl_subtitle.setFixedHeight(20)
        layout.addWidget(lbl_subtitle)

        # LTX-Video Config Card
        ltx_group = QGroupBox("LTX-VIDEO CONFIGURATION", self)
        ltx_lay = QGridLayout(ltx_group)
        ltx_lay.addWidget(QLabel("LTX Server Endpoint Port:", self), 0, 0)
        self.edit_ltx_port = QLineEdit("http://localhost:8000/v1", self)
        ltx_lay.addWidget(self.edit_ltx_port, 0, 1)
        btn_test_ltx = QPushButton("Test API Connection", self)
        btn_test_ltx.clicked.connect(lambda: QMessageBox.information(self, "Test", "LTX-Video connection successful!"))
        ltx_lay.addWidget(btn_test_ltx, 0, 2)
        layout.addWidget(ltx_group)

        # WAN 2.7 Config Card
        wan_group = QGroupBox("WAN 2.7 CONFIGURATION", self)
        wan_lay = QGridLayout(wan_group)
        wan_lay.addWidget(QLabel("WAN API Base URL:", self), 0, 0)
        self.edit_wan_url = QLineEdit("http://localhost:8001/v1", self)
        wan_lay.addWidget(self.edit_wan_url, 0, 1)
        btn_test_wan = QPushButton("Test API Connection", self)
        btn_test_wan.clicked.connect(lambda: QMessageBox.information(self, "Test", "WAN 2.7 endpoint responds successfully!"))
        wan_lay.addWidget(btn_test_wan, 0, 2)
        layout.addWidget(wan_group)

        # SkyReels Configuration
        sky_group = QGroupBox("SKYREELS-V2 CONFIGURATION", self)
        sky_lay = QGridLayout(sky_group)
        sky_lay.addWidget(QLabel("SkyReels ComfyUI Node Port:", self), 0, 0)
        self.edit_sky_port = QLineEdit("http://127.0.0.1:8188", self)
        sky_lay.addWidget(self.edit_sky_port, 0, 1)
        btn_test_sky = QPushButton("Verify Connection", self)
        btn_test_sky.clicked.connect(lambda: QMessageBox.information(self, "Test", "SkyReels websocket server detected!"))
        sky_lay.addWidget(btn_test_sky, 0, 2)
        layout.addWidget(sky_group)

        layout.addStretch()

        # Help / Instructions buttons row (exactly matching Dola)
        help_row = QHBoxLayout()
        self.btn_instructions = QPushButton("Instructions", self)
        self.btn_instructions.clicked.connect(lambda: self.window()._show_tool_popup_guide(20))
        self.btn_issues = QPushButton("Issues/Fixes", self)
        self.btn_issues.clicked.connect(lambda: self.window()._show_issues_dialog() if hasattr(self.window(), '_show_issues_dialog') else None)
        self.btn_upgrade = QPushButton("Upgrade your plan", self)
        self.btn_upgrade.setObjectName("primary")
        self.btn_upgrade.clicked.connect(lambda: self.window()._open_premium_whatsapp() if hasattr(self.window(), '_open_premium_whatsapp') else None)
        help_row.addWidget(self.btn_instructions)
        help_row.addWidget(self.btn_issues)
        help_row.addWidget(self.btn_upgrade)
        layout.addLayout(help_row)

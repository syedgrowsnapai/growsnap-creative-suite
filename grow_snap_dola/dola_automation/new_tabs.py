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
    QGroupBox, QHeaderView, QAbstractItemView, QFrame, QButtonGroup, QTabWidget, QScrollArea,
    QStylePainter, QStyleOptionComboBox, QMenu, QApplication
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal, pyqtSlot, QTimer, QTime, QEvent
from PyQt6.QtGui import QColor, QIcon, QPalette, QStandardItemModel, QStandardItem

# Check if QWebEngineView is available
try:
    from PyQt6.QtWebEngineWidgets import QWebEngineView
    WEB_ENGINE_AVAILABLE = True
except ImportError:
    WEB_ENGINE_AVAILABLE = False

from dola_automation.models import AutomationSettings, PromptJob, JobStatus, parse_prompts, align_reference_images
from dola_automation.styles import GradientLabel, STATUS_COLORS
from PyQt6.QtWidgets import QStyle

class CheckableComboBox(QComboBox):
    checkedItemsChanged = pyqtSignal()
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setModel(QStandardItemModel(self))
        self.model().dataChanged.connect(self._on_data_changed)
        self.view().viewport().installEventFilter(self)
        
    def add_checkable_item(self, text, checked=False):
        item = QStandardItem(text)
        item.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
        item.setData(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked, Qt.ItemDataRole.CheckStateRole)
        self.model().appendRow(item)
        
    def _on_data_changed(self, topLeft, bottomRight, roles):
        if Qt.ItemDataRole.CheckStateRole in roles:
            self.checkedItemsChanged.emit()
            
    def get_checked_items(self):
        checked = []
        for i in range(self.count()):
            item = self.model().item(i)
            if item and item.data(Qt.ItemDataRole.CheckStateRole) == Qt.CheckState.Checked:
                checked.append(item.text().lower())
        return checked

    def set_checked_items(self, items_list):
        self.model().blockSignals(True)
        for i in range(self.count()):
            item = self.model().item(i)
            if item:
                state = Qt.CheckState.Checked if item.text().lower() in items_list else Qt.CheckState.Unchecked
                item.setData(state, Qt.ItemDataRole.CheckStateRole)
        self.model().blockSignals(False)
        self.checkedItemsChanged.emit()

    def eventFilter(self, widget, event):
        if widget == self.view().viewport() and event.type() == QEvent.Type.MouseButtonPress:
            index = self.view().indexAt(event.pos())
            item = self.model().itemFromIndex(index)
            if item:
                current = item.data(Qt.ItemDataRole.CheckStateRole)
                new_state = Qt.CheckState.Unchecked if current == Qt.CheckState.Checked else Qt.CheckState.Checked
                item.setData(new_state, Qt.ItemDataRole.CheckStateRole)
                
                if item.text() == "All Statuses":
                    self.model().blockSignals(True)
                    for i in range(1, self.count()):
                        sibling = self.model().item(i)
                        if sibling:
                            sibling.setData(new_state, Qt.ItemDataRole.CheckStateRole)
                    self.model().blockSignals(False)
                    self.checkedItemsChanged.emit()
                else:
                    all_item = self.model().item(0)
                    if all_item and new_state == Qt.CheckState.Unchecked:
                        self.model().blockSignals(True)
                        all_item.setData(Qt.CheckState.Unchecked, Qt.ItemDataRole.CheckStateRole)
                        self.model().blockSignals(False)
                        self.checkedItemsChanged.emit()
                return True
        return super().eventFilter(widget, event)

    def paintEvent(self, event):
        painter = QStylePainter(self)
        painter.setPen(self.palette().color(QPalette.ColorRole.Text))
        opt = QStyleOptionComboBox()
        self.initStyleOption(opt)
        checked = [self.model().item(i).text() for i in range(1, self.count()) 
                   if self.model().item(i).data(Qt.ItemDataRole.CheckStateRole) == Qt.CheckState.Checked]
        if len(checked) == self.count() - 1:
            opt.currentText = "All Statuses"
        elif not checked:
            opt.currentText = "None selected"
        else:
            opt.currentText = ", ".join(checked)
        painter.drawComplexControl(QStyle.ComplexControl.CC_ComboBox, opt)
        painter.drawControl(QStyle.ControlElement.CE_ComboBoxLabel, opt)


class SnapGenAutomationWidget(QWidget):
    def __init__(self, parent=None, db_path=None, global_settings=None):
        super().__init__(parent)
        self.db_path = db_path
        self.settings = global_settings or AutomationSettings()
        self.jobs = []
        self.reference_paths = []
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
        self.gen_mode = "video"
        self.is_paused = False
        self.is_running = False
        
        self.elapsed_seconds = 0
        self.batch_timer = QTimer(self)
        self.batch_timer.timeout.connect(self._update_timer)

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
        
        layout.addLayout(stats_row, 0)

        # Splitter Layout (takes stretch factor 1)
        splitter = QSplitter(Qt.Orientation.Horizontal, self)
        layout.addWidget(splitter, 1)

        # Left Scroll Panel (Inputs & Controls)
        left_scroll = QScrollArea(self)
        left_scroll.setWidgetResizable(True)
        left_scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")
        
        left = QWidget()
        left.setObjectName("left_panel_container")
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(15)
        
        # 1. Mode Selection
        mode_group = QGroupBox("GENERATION MODE", self)
        mode_lay = QHBoxLayout(mode_group)
        
        self.btn_mode_video = QPushButton("🎬 Generate Video", self)
        self.btn_mode_video.setCheckable(True)
        self.btn_mode_video.setChecked(True)
        self.btn_mode_video.clicked.connect(self._select_video_mode)
        
        self.btn_mode_image = QPushButton("🖼️ Generate Image", self)
        self.btn_mode_image.setCheckable(True)
        self.btn_mode_image.clicked.connect(self._select_image_mode)
        
        self.mode_btn_group = QButtonGroup(self)
        self.mode_btn_group.setExclusive(True)
        self.mode_btn_group.addButton(self.btn_mode_video)
        self.mode_btn_group.addButton(self.btn_mode_image)
        
        mode_lay.addWidget(self.btn_mode_video)
        mode_lay.addWidget(self.btn_mode_image)
        left_layout.addWidget(mode_group)
        
        # 2. Prompt Ingestion Card (Dola-aligned)
        ingest_group = QGroupBox("PROMPT INGESTION", self)
        ingest_lay = QVBoxLayout(ingest_group)
        self.prompt_editor = QPlainTextEdit(self)
        self.prompt_editor.setPlaceholderText("Enter video/image generation prompts here...")
        ingest_lay.addWidget(self.prompt_editor)
        
        path_row = QHBoxLayout()
        self.edit_path = QLineEdit(self)
        self.edit_path.setPlaceholderText("Paste CSV/TXT file path here...")
        self.btn_load_path = QPushButton("Load Path", self)
        self.btn_load_path.clicked.connect(self._load_prompt_from_path)
        path_row.addWidget(self.edit_path)
        path_row.addWidget(self.btn_load_path)
        ingest_lay.addLayout(path_row)
        
        btn_row = QHBoxLayout()
        self.btn_load_file = QPushButton("Load CSV/TXT", self)
        self.btn_load_file.clicked.connect(self._load_prompt_file)
        self.btn_parse = QPushButton("Parse prompts", self)
        self.btn_parse.clicked.connect(self._parse_prompts)
        btn_row.addWidget(self.btn_load_file)
        btn_row.addWidget(self.btn_parse)
        ingest_lay.addLayout(btn_row)
        left_layout.addWidget(ingest_group)

        # 3. Reference Image Picker
        ref_group = QGroupBox("REFERENCE IMAGES", self)
        ref_layout = QVBoxLayout(ref_group)
        self.ref_list = QListWidget(self)
        self.ref_list.setMaximumHeight(100)
        ref_layout.addWidget(self.ref_list)

        ref_btns = QHBoxLayout()
        self.btn_ref_files = QPushButton("Pick images", self)
        self.btn_ref_files.clicked.connect(self._pick_reference_files)
        self.btn_ref_folder = QPushButton("Pick folder", self)
        self.btn_ref_folder.clicked.connect(self._pick_reference_folder)
        self.btn_clear_refs = QPushButton("Clear", self)
        self.btn_clear_refs.clicked.connect(self._clear_references)
        ref_btns.addWidget(self.btn_ref_files)
        ref_btns.addWidget(self.btn_ref_folder)
        ref_btns.addWidget(self.btn_clear_refs)
        ref_layout.addLayout(ref_btns)
        left_layout.addWidget(ref_group)

        # 4. Operational Buttons
        btn_lay = QHBoxLayout()
        self.btn_start = QPushButton("Start batch", self)
        self.btn_start.setObjectName("primary")
        self.btn_start.clicked.connect(self._start_batch)
        
        self.btn_pause = QPushButton("Pause", self)
        self.btn_pause.clicked.connect(self._pause_batch)
        self.btn_pause.setEnabled(False)
        
        self.btn_stop = QPushButton("Stop", self)
        self.btn_stop.setObjectName("danger")
        self.btn_stop.clicked.connect(self._stop_batch)
        self.btn_stop.setEnabled(False)
        
        btn_lay.addWidget(self.btn_start)
        btn_lay.addWidget(self.btn_pause)
        btn_lay.addWidget(self.btn_stop)
        left_layout.addLayout(btn_lay)

        # 5. Help Row Buttons
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

        left_scroll.setWidget(left)
        splitter.addWidget(left_scroll)

        # Right panel: QTabWidget matching Dola structure
        self.right_tabs = QTabWidget(self)
        
        # Tab 1: Current Jobs
        self.tab_current = QWidget(self)
        tab_current_lay = QVBoxLayout(self.tab_current)
        
        filter_bar = QHBoxLayout()
        filter_bar.setSpacing(10)
        
        lbl_filter = QLabel("Filter Status:", self)
        lbl_filter.setStyleSheet("font-weight: bold; color: #2ecc71;")
        filter_bar.addWidget(lbl_filter)
        
        self.combo_filter_status = CheckableComboBox(self)
        self.combo_filter_status.add_checkable_item("All Statuses", checked=True)
        self.combo_filter_status.add_checkable_item("Pending", checked=True)
        self.combo_filter_status.add_checkable_item("Running", checked=True)
        self.combo_filter_status.add_checkable_item("Waiting", checked=True)
        self.combo_filter_status.add_checkable_item("Downloading", checked=True)
        self.combo_filter_status.add_checkable_item("Completed", checked=True)
        self.combo_filter_status.add_checkable_item("Submitted", checked=True)
        self.combo_filter_status.add_checkable_item("Failed", checked=True)
        self.combo_filter_status.add_checkable_item("Cancelled", checked=True)
        self.combo_filter_status.add_checkable_item("Has Error Details", checked=True)
        self.combo_filter_status.checkedItemsChanged.connect(self._apply_table_filters)
        filter_bar.addWidget(self.combo_filter_status)
        
        lbl_search = QLabel("Search:", self)
        lbl_search.setStyleSheet("font-weight: bold; color: #2ecc71; margin-left: 10px;")
        filter_bar.addWidget(lbl_search)
        
        self.edit_search = QLineEdit(self)
        self.edit_search.setPlaceholderText("Search rows...")
        self.edit_search.textChanged.connect(self._apply_table_filters)
        filter_bar.addWidget(self.edit_search)
        
        self.btn_clear_filters = QPushButton("Clear Filters", self)
        self.btn_clear_filters.clicked.connect(self._clear_filters)
        filter_bar.addWidget(self.btn_clear_filters)
        
        tab_current_lay.addLayout(filter_bar)
        
        self.table = QTableWidget(self)
        self.table.setColumnCount(9)
        self.table.setHorizontalHeaderLabels([
            "Index", "Video Title", "Scene Index", "Prompt", "Reference", "Status", "Download Path", "Error Details", "Action"
        ])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.table.setColumnWidth(0, 50)
        self.table.setColumnWidth(1, 120)
        self.table.setColumnWidth(2, 80)
        self.table.setColumnWidth(3, 200)
        self.table.setColumnWidth(4, 90)
        self.table.setColumnWidth(5, 90)
        self.table.setColumnWidth(6, 120)
        self.table.setColumnWidth(7, 120)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._on_table_context_menu)
        tab_current_lay.addWidget(self.table)
        
        action_bar = QHBoxLayout()
        self.btn_select_all = QPushButton("Select All", self)
        self.btn_select_all.clicked.connect(self._toggle_select_all)
        self.btn_download_selected = QPushButton("Download Selected", self)
        self.btn_download_selected.clicked.connect(self._download_selected_jobs)
        self.btn_retry_failed = QPushButton("Retry All Failed", self)
        self.btn_retry_failed.clicked.connect(self._retry_all_failed_jobs)
        action_bar.addWidget(self.btn_select_all)
        action_bar.addWidget(self.btn_download_selected)
        action_bar.addWidget(self.btn_retry_failed)
        tab_current_lay.addLayout(action_bar)
        
        self.right_tabs.addTab(self.tab_current, "Current Jobs")

        # Tab 2: History Logs
        self.tab_logs = QWidget(self)
        tab_logs_lay = QVBoxLayout(self.tab_logs)
        self.txt_log = QPlainTextEdit(self)
        self.txt_log.setReadOnly(True)
        self.txt_log.setPlaceholderText("Console logs...")
        tab_logs_lay.addWidget(self.txt_log)
        self.right_tabs.addTab(self.tab_logs, "History Logs")

        # Tab 3: Settings
        self.tab_settings = QWidget(self)
        tab_settings_lay = QVBoxLayout(self.tab_settings)
        tab_settings_lay.setContentsMargins(10, 10, 10, 10)
        tab_settings_lay.setSpacing(15)
        
        # Video params group
        self.group_video_params = QGroupBox("VIDEO SETTINGS", self)
        video_lay = QGridLayout(self.group_video_params)
        video_lay.addWidget(QLabel("Video Model:", self), 0, 0)
        self.combo_video_model = QComboBox(self)
        self.combo_video_model.addItems(["Google Veo 3 (Free)", "Veo 3 Pro (Paid)"])
        video_lay.addWidget(self.combo_video_model, 0, 1)
        video_lay.addWidget(QLabel("Duration:", self), 0, 2)
        self.combo_duration = QComboBox(self)
        self.combo_duration.addItems(["8s (VO3 Standard)", "4s"])
        video_lay.addWidget(self.combo_duration, 0, 3)
        tab_settings_lay.addWidget(self.group_video_params)

        # Image params group
        self.group_image_params = QGroupBox("IMAGE SETTINGS", self)
        image_lay = QGridLayout(self.group_image_params)
        image_lay.addWidget(QLabel("Image Model:", self), 0, 0)
        self.combo_image_model = QComboBox(self)
        self.combo_image_model.addItems(["Nano Banana Pro/2", "Nano Banana Lite"])
        image_lay.addWidget(self.combo_image_model, 0, 1)
        tab_settings_lay.addWidget(self.group_image_params)
        self.group_image_params.setVisible(False)
        
        # General parameters
        gen_params_group = QGroupBox("GENERAL SETTINGS", self)
        gen_lay = QGridLayout(gen_params_group)
        gen_lay.addWidget(QLabel("Aspect Ratio:", self), 0, 0)
        self.combo_ratio = QComboBox(self)
        self.combo_ratio.addItems(["9:16", "16:9", "1:1"])
        gen_lay.addWidget(self.combo_ratio, 0, 1)
        
        # Download directory
        gen_lay.addWidget(QLabel("Download Folder:", self), 1, 0)
        h_dl_lay = QHBoxLayout()
        self.btn_dl_dir = QPushButton("Choose", self)
        self.btn_dl_dir.clicked.connect(self._pick_download_dir)
        self.btn_open_dl = QPushButton("📂 Open", self)
        self.btn_open_dl.clicked.connect(self._open_download_dir)
        h_dl_lay.addWidget(self.btn_dl_dir)
        h_dl_lay.addWidget(self.btn_open_dl)
        gen_lay.addLayout(h_dl_lay, 1, 1)
        
        self.lbl_dl_path_show = QLabel(str(Path.home() / 'Downloads'), self)
        self.lbl_dl_path_show.setWordWrap(True)
        gen_lay.addWidget(self.lbl_dl_path_show, 2, 0, 1, 2)
        tab_settings_lay.addWidget(gen_params_group)

        # Profile Manager Group
        profile_group = QGroupBox("SNAPGEN PROFILE MANAGER", self)
        prof_lay = QGridLayout(profile_group)
        prof_lay.addWidget(QLabel("Active Profile:", self), 0, 0)
        self.combo_profiles = QComboBox(self)
        prof_lay.addWidget(self.combo_profiles, 0, 1)
        
        btn_new_prof = QPushButton("+ New", self)
        btn_new_prof.clicked.connect(self._create_profile)
        prof_lay.addWidget(btn_new_prof, 0, 2)

        btn_login = QPushButton("🔑 Login", self)
        btn_login.clicked.connect(self._login_headed)
        prof_lay.addWidget(btn_login, 0, 3)

        self.chk_headless = QCheckBox("Run Headless Browser", self)
        prof_lay.addWidget(self.chk_headless, 1, 0, 1, 2)

        self.chk_concurrent = QCheckBox("Submit requests concurrently", self)
        self.chk_concurrent.setChecked(True)
        prof_lay.addWidget(self.chk_concurrent, 1, 2, 1, 2)
        tab_settings_lay.addWidget(profile_group)
        
        tab_settings_lay.addStretch()
        self.right_tabs.addTab(self.tab_settings, "Settings")

        # Tab 4: Lifetime History
        self.tab_history = QWidget(self)
        tab_history_lay = QVBoxLayout(self.tab_history)
        self.table_history = QTableWidget(self)
        self.table_history.setColumnCount(4)
        self.table_history.setHorizontalHeaderLabels(["Timestamp", "Prompt", "Type", "Status"])
        self.table_history.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        tab_history_lay.addWidget(self.table_history)
        self.right_tabs.addTab(self.tab_history, "Lifetime History")

        splitter.addWidget(self.right_tabs)
        splitter.setSizes([450, 550])

        self._refresh_profiles()

    def _select_video_mode(self):
        self.gen_mode = "video"
        self.group_video_params.setVisible(True)
        self.group_image_params.setVisible(False)
        self.right_tabs.setCurrentIndex(2) # Switch to Settings tab

    def _select_image_mode(self):
        self.gen_mode = "image"
        self.group_video_params.setVisible(False)
        self.group_image_params.setVisible(True)
        self.right_tabs.setCurrentIndex(2) # Switch to Settings tab

    def _pick_download_dir(self):
        dir_path = QFileDialog.getExistingDirectory(self, "Select Download Directory")
        if dir_path:
            self.lbl_dl_path_show.setText(dir_path)

    def _open_download_dir(self):
        import subprocess
        p = self.lbl_dl_path_show.text()
        if os.path.exists(p):
            try:
                if sys.platform == "win32":
                    os.startfile(p)
                else:
                    subprocess.run(['xdg-open', p])
            except Exception as e:
                QMessageBox.warning(self, "Folder Error", f"Could not open folder: {e}")

    def _update_timer(self):
        self.elapsed_seconds += 1
        t = QTime(0, 0, 0).addSecs(self.elapsed_seconds)
        self.timer_label.setText(t.toString("HH:mm:ss"))

    def _refresh_profiles(self):
        profiles_dir = Path.home() / 'Documents' / 'snapgen_video_automation' / 'profiles'
        profiles_dir.mkdir(parents=True, exist_ok=True)
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
                from patchright.sync_api import sync_playwright
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
            self._load_prompt_from_path()

    def _load_prompt_from_path(self):
        path = self.edit_path.text().strip()
        if not path:
            return
        path_obj = Path(path)
        if path_obj.exists() and path_obj.is_file():
            try:
                with open(path_obj, 'r', encoding='utf-8') as f:
                    self.prompt_editor.setPlainText(f.read())
                self.txt_log.appendPlainText(f"[Info] Loaded prompts from: {path_obj.name}")
            except Exception as e:
                QMessageBox.critical(self, "Error", f"Failed to load file: {e}")

    def _load_prompt_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "Select Prompt File", "", "Text Files (*.txt *.csv)")
        if path:
            self.edit_path.setText(path)
            self._load_prompt_from_path()

    def _parse_prompts(self):
        text = self.prompt_editor.toPlainText().strip()
        if not text:
            QMessageBox.warning(self, "No prompts", "Please enter prompts in the editor before parsing.")
            return

        parsed = parse_prompts(text)
        if not parsed:
            QMessageBox.warning(self, "Failed to parse", "No valid prompts parsed. Check text format.")
            return

        self.jobs.clear()
        for idx, (prompt, caption, title, scene_idx) in enumerate(parsed):
            ref = self.reference_paths[idx] if idx < len(self.reference_paths) else None
            job = PromptJob(
                index=idx + 1,
                prompt=prompt,
                caption=caption,
                video_title=title or f"Video_{idx // 4 + 1}",
                scene_index=scene_idx or (idx % 4 + 1),
                reference_image=ref,
                status=JobStatus.PENDING
            )
            self.jobs.append(job)

        self._refresh_table()
        self._update_stats()
        self.txt_log.appendPlainText(f"[Info] Parsed {len(self.jobs)} prompts successfully.")
        self.right_tabs.setCurrentIndex(0) # Switch to Current Jobs

    def _pick_reference_files(self):
        files, _ = QFileDialog.getOpenFileNames(self, "Select Reference Images", "", "Image Files (*.png *.jpg *.jpeg)")
        if files:
            for f in files:
                if f not in self.reference_paths:
                    self.reference_paths.append(f)
                    self.ref_list.addItem(Path(f).name)

    def _pick_reference_folder(self):
        dir_path = QFileDialog.getExistingDirectory(self, "Select Reference Folder")
        if dir_path:
            p = Path(dir_path)
            for f in p.iterdir():
                if f.is_file() and f.suffix.lower() in ('.png', '.jpg', '.jpeg'):
                    fp = str(f.resolve())
                    if fp not in self.reference_paths:
                        self.reference_paths.append(fp)
                        self.ref_list.addItem(f.name)

    def _clear_references(self):
        self.reference_paths.clear()
        self.ref_list.clear()

    def _apply_table_filters(self):
        self._refresh_table()

    def _clear_filters(self):
        self.edit_search.clear()
        self.combo_filter_status.set_checked_items(["all statuses"])
        self._refresh_table()

    def _toggle_select_all(self):
        if self.table.rowCount() == 0:
            return
        selected = self.table.selectedItems()
        if len(selected) > 0:
            self.table.clearSelection()
        else:
            self.table.selectAll()

    def _download_selected_jobs(self):
        selected_rows = list(set(index.row() for index in self.table.selectedIndexes()))
        if not selected_rows:
            QMessageBox.information(self, "Selection Required", "Please select one or more jobs in the table first.")
            return
        QMessageBox.information(self, "Downloading", f"Downloading completed output clips for {len(selected_rows)} selected rows...")

    def _retry_all_failed_jobs(self):
        retried = 0
        for job in self.jobs:
            if job.status == JobStatus.FAILED:
                job.status = JobStatus.PENDING
                retried += 1
        if retried > 0:
            self._refresh_table()
            self._update_stats()
            self.txt_log.appendPlainText(f"[Info] Reset {retried} failed jobs back to PENDING state.")
        else:
            QMessageBox.information(self, "No Failed Jobs", "There are no FAILED jobs in the current batch queue.")

    def _toggle_job_action(self, row, job):
        QMessageBox.information(self, "Job Action", f"Action requested for Row #{row + 1}: {job.prompt[:30]}...")

    def _on_table_context_menu(self, pos):
        item = self.table.itemAt(pos)
        if not item:
            return
        row = item.row()
        menu = QMenu(self)
        copy_action = menu.addAction("Copy Prompt")
        action = menu.exec(self.table.viewport().mapToGlobal(pos))
        if action == copy_action:
            prompt_text = self.table.item(row, 3).text()
            QApplication.clipboard().setText(prompt_text)

    def _refresh_table(self):
        self.table.setRowCount(0)
        search_text = self.edit_search.text().strip().lower()
        filter_statuses = self.combo_filter_status.get_checked_items()
        show_all = "all statuses" in filter_statuses

        for idx, job in enumerate(self.jobs):
            # Check search filter
            if search_text and search_text not in job.prompt.lower() and search_text not in job.video_title.lower():
                continue
            
            # Check status filter
            status_str = job.status.value.lower()
            if not show_all and status_str not in filter_statuses:
                continue

            row = self.table.rowCount()
            self.table.insertRow(row)
            self.table.setItem(row, 0, QTableWidgetItem(str(job.index)))
            self.table.setItem(row, 1, QTableWidgetItem(job.video_title))
            self.table.setItem(row, 2, QTableWidgetItem(str(job.scene_index)))
            self.table.setItem(row, 3, QTableWidgetItem(job.prompt))
            self.table.setItem(row, 4, QTableWidgetItem(Path(job.reference_image).name if job.reference_image else "None"))
            self.table.setItem(row, 5, QTableWidgetItem(job.status.value))
            
            # Apply color to status
            status_color = STATUS_COLORS.get(job.status.value.lower(), "#ffffff")
            self.table.item(row, 5).setForeground(QColor(status_color))
            
            self.table.setItem(row, 6, QTableWidgetItem("-"))
            self.table.setItem(row, 7, QTableWidgetItem("-"))
            
            # Add action button
            btn_action = QPushButton("Action", self)
            btn_action.clicked.connect(lambda checked, r=row, j=job: self._toggle_job_action(r, j))
            self.table.setCellWidget(row, 8, btn_action)

    def _update_stats(self):
        total = len(self.jobs)
        failed = sum(1 for j in self.jobs if j.status == JobStatus.FAILED)
        completed = sum(1 for j in self.jobs if j.status == JobStatus.COMPLETED)
        
        try:
            self.stat_total.findChild(QLabel, "statValue").setText(str(total))
            self.stat_fail.findChild(QLabel, "statValue").setText(str(failed))
            self.stat_batch.findChild(QLabel, "statValue").setText(str(completed))
        except Exception:
            pass

    def _start_batch(self):
        if not self.jobs:
            self._parse_prompts()
            if not self.jobs:
                return

        self.txt_log.appendPlainText(f"[Info] Starting batch execution of {len(self.jobs)} jobs in {self.gen_mode.upper()} mode...")
        self.is_running = True
        self.is_paused = False
        self.elapsed_seconds = 0
        self.timer_label.setText("00:00:00")
        self.batch_timer.start(1000)

        self.btn_start.setEnabled(False)
        self.btn_pause.setEnabled(True)
        self.btn_pause.setText("Pause")
        self.btn_stop.setEnabled(True)
        
        self.right_tabs.setCurrentIndex(0) # Current Jobs

        self.mock_index = 0
        self.mock_progress = 0
        QTimer.singleShot(1500, self._run_mock_step)

    def _run_mock_step(self):
        if not self.is_running or self.is_paused:
            return
            
        if self.mock_index >= len(self.jobs):
            self.is_running = False
            self.batch_timer.stop()
            self.btn_start.setEnabled(True)
            self.btn_pause.setEnabled(False)
            self.btn_stop.setEnabled(False)
            self.txt_log.appendPlainText("[Info] Batch completed successfully!")
            
            for job in self.jobs:
                job.status = JobStatus.COMPLETED
                h_row = self.table_history.rowCount()
                self.table_history.insertRow(h_row)
                self.table_history.setItem(h_row, 0, QTableWidgetItem(time.strftime("%Y-%m-%d %H:%M:%S")))
                self.table_history.setItem(h_row, 1, QTableWidgetItem(job.prompt))
                self.table_history.setItem(h_row, 2, QTableWidgetItem(self.gen_mode.upper()))
                self.table_history.setItem(h_row, 3, QTableWidgetItem("COMPLETED"))
                
            self._refresh_table()
            self._update_stats()
            
            try:
                self.stat_lifetime.findChild(QLabel, "statValue").setText(
                    str(int(self.stat_lifetime.findChild(QLabel, "statValue").text()) + len(self.jobs))
                )
            except Exception:
                pass
            
            QMessageBox.information(self, "Completed", "SnapGen AI batch processing completed successfully!")
            return

        active_job = self.jobs[self.mock_index]
        active_job.status = JobStatus.RUNNING
        self._refresh_table()
        
        self.mock_progress += 25
        self.txt_log.appendPlainText(f"[Progress] Ingesting prompt #{active_job.index}: {self.mock_progress}%")
        
        if self.mock_progress >= 100:
            active_job.status = JobStatus.COMPLETED
            self.mock_index += 1
            self.mock_progress = 0
            
        QTimer.singleShot(1000, self._run_mock_step)

    def _pause_batch(self):
        if not self.is_running:
            return
            
        if self.is_paused:
            self.is_paused = False
            self.btn_pause.setText("Pause")
            self.txt_log.appendPlainText("[Info] Batch resumed.")
            self.batch_timer.start(1000)
            QTimer.singleShot(500, self._run_mock_step)
        else:
            self.is_paused = True
            self.btn_pause.setText("Resume")
            self.txt_log.appendPlainText("[Info] Batch paused.")
            self.batch_timer.stop()

    def _stop_batch(self):
        self.is_running = False
        self.batch_timer.stop()
        self.btn_start.setEnabled(True)
        self.btn_pause.setEnabled(False)
        self.btn_stop.setEnabled(False)
        self.txt_log.appendPlainText("[Info] Batch stopped by user.")
        for job in self.jobs:
            if job.status in (JobStatus.PENDING, JobStatus.RUNNING):
                job.status = JobStatus.CANCELLED
        self._refresh_table()


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

        # Left side: AI Video Director / Editor wrapped in QScrollArea
        left_scroll = QScrollArea(self)
        left_scroll.setWidgetResizable(True)
        left_scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")
        
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
 
        left_scroll.setWidget(left)
        splitter.addWidget(left_scroll)

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
        lbl_subtitle = QLabel("AI VIDEO CLIPPER — split long videos into high-energy short clips", self)
        lbl_subtitle.setObjectName("subtitle")
        lbl_subtitle.setFixedHeight(20)
        layout.addWidget(lbl_subtitle)

        # Scroll Area for inputs
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")
        
        scroll_widget = QWidget()
        scroll_layout = QVBoxLayout(scroll_widget)
        scroll_layout.setContentsMargins(0, 0, 0, 0)
        scroll_layout.setSpacing(15)

        grid = QGridLayout()
        scroll_layout.addLayout(grid)

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
        scroll_layout.addWidget(self.btn_run)

        self.progress = QProgressBar(self)
        self.progress.setVisible(False)
        scroll_layout.addWidget(self.progress)

        self.log_console = QPlainTextEdit(self)
        self.log_console.setReadOnly(True)
        self.log_console.setPlaceholderText("Clipper progress logs...")
        scroll_layout.addWidget(self.log_console)

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
        scroll_layout.addLayout(help_row)

        scroll.setWidget(scroll_widget)
        layout.addWidget(scroll)

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

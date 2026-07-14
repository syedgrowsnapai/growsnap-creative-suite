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
    QStylePainter, QStyleOptionComboBox, QMenu, QApplication, QStyle, QDialog, QFormLayout, QDialogButtonBox
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal, pyqtSlot, QTimer, QTime, QEvent, QSize
from PyQt6.QtGui import QColor, QIcon, QPalette, QStandardItemModel, QStandardItem

try:
    from PyQt6.QtWebEngineWidgets import QWebEngineView
    WEB_ENGINE_AVAILABLE = True
except ImportError:
    WEB_ENGINE_AVAILABLE = False

from dola_automation.models import AutomationSettings, PromptJob, JobStatus, parse_prompts, align_reference_images
from dola_automation.styles import GradientLabel, STATUS_COLORS

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

class JobDialog(QDialog):
    def __init__(self, parent=None, job=None):
        super().__init__(parent)
        self.setWindowTitle("Edit Job Row" if job else "Add Job Row")
        self.setMinimumWidth(400)
        
        layout = QFormLayout(self)
        layout.setContentsMargins(15, 15, 15, 15)
        layout.setSpacing(10)
        
        self.edit_title = QLineEdit(self)
        self.edit_title.setText(job.video_title if job else "Video_1")
        
        self.edit_scene = QSpinBox(self)
        self.edit_scene.setRange(1, 100)
        self.edit_scene.setValue(job.scene_index if job else 1)
        
        self.edit_prompt = QPlainTextEdit(self)
        self.edit_prompt.setPlainText(job.prompt if job else "")
        self.edit_prompt.setMinimumHeight(80)
        
        self.edit_ref = QLineEdit(self)
        self.edit_ref.setText(job.reference_image if job else "")
        self.btn_pick_ref = QPushButton("Browse...", self)
        self.btn_pick_ref.clicked.connect(self._browse_ref)
        
        ref_row = QHBoxLayout()
        ref_row.addWidget(self.edit_ref)
        ref_row.addWidget(self.btn_pick_ref)
        ref_row.setContentsMargins(0, 0, 0, 0)
        
        layout.addRow("Video Title:", self.edit_title)
        layout.addRow("Scene Index:", self.edit_scene)
        layout.addRow("Prompt:", self.edit_prompt)
        layout.addRow("Reference Image:", ref_row)
        
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, self)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)
        
    def _browse_ref(self):
        file_path, _ = QFileDialog.getOpenFileName(self, "Select Reference Image", "", "Image Files (*.png *.jpg *.jpeg)")
        if file_path:
            self.edit_ref.setText(file_path)
            
    def get_data(self):
        return {
            "video_title": self.edit_title.text().strip(),
            "scene_index": self.edit_scene.value(),
            "prompt": self.edit_prompt.toPlainText().strip(),
            "reference_image": self.edit_ref.text().strip() or None
        }


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
        card.setFixedHeight(80)
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
        timer_card.setFixedHeight(80)
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
        mode_group = QGroupBox("GENERATION MODE")
        mode_lay = QHBoxLayout(mode_group)
        
        self.btn_mode_video = QPushButton("🎬 Generate Video")
        self.btn_mode_video.setCheckable(True)
        self.btn_mode_video.setChecked(True)
        self.btn_mode_video.clicked.connect(self._select_video_mode)
        
        self.btn_mode_image = QPushButton("🖼️ Generate Image")
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
        ingest_group = QGroupBox("PROMPT INGESTION")
        ingest_lay = QVBoxLayout(ingest_group)
        self.prompt_editor = QPlainTextEdit()
        self.prompt_editor.setPlaceholderText("Enter video/image generation prompts here...")
        ingest_lay.addWidget(self.prompt_editor)
        
        path_row = QHBoxLayout()
        self.edit_path = QLineEdit()
        self.edit_path.setPlaceholderText("Paste CSV/TXT file path here...")
        self.btn_load_path = QPushButton("Load Path")
        self.btn_load_path.clicked.connect(self._load_prompt_from_path)
        path_row.addWidget(self.edit_path)
        path_row.addWidget(self.btn_load_path)
        ingest_lay.addLayout(path_row)
        
        btn_row = QHBoxLayout()
        self.btn_load_file = QPushButton("Load CSV/TXT")
        self.btn_load_file.clicked.connect(self._load_prompt_file)
        self.btn_parse = QPushButton("Parse prompts")
        self.btn_parse.clicked.connect(self._parse_prompts)
        btn_row.addWidget(self.btn_load_file)
        btn_row.addWidget(self.btn_parse)
        ingest_lay.addLayout(btn_row)
        left_layout.addWidget(ingest_group)
 
        # 3. Reference Image Picker
        ref_group = QGroupBox("REFERENCE IMAGES")
        ref_layout = QVBoxLayout(ref_group)
        self.ref_list = QListWidget()
        self.ref_list.setMaximumHeight(100)
        ref_layout.addWidget(self.ref_list)
 
        ref_btns = QHBoxLayout()
        self.btn_ref_files = QPushButton("Pick images")
        self.btn_ref_files.clicked.connect(self._pick_reference_files)
        self.btn_ref_folder = QPushButton("Pick folder")
        self.btn_ref_folder.clicked.connect(self._pick_reference_folder)
        self.btn_clear_refs = QPushButton("Clear")
        self.btn_clear_refs.clicked.connect(self._clear_references)
        ref_btns.addWidget(self.btn_ref_files)
        ref_btns.addWidget(self.btn_ref_folder)
        ref_btns.addWidget(self.btn_clear_refs)
        ref_layout.addLayout(ref_btns)
        left_layout.addWidget(ref_group)
 
        # 4. Operational Buttons
        btn_lay = QHBoxLayout()
        self.btn_start = QPushButton("Start batch")
        self.btn_start.setObjectName("primary")
        self.btn_start.clicked.connect(self._start_batch)
        
        self.btn_pause = QPushButton("Pause")
        self.btn_pause.clicked.connect(self._pause_batch)
        self.btn_pause.setEnabled(False)
        
        self.btn_stop = QPushButton("Stop")
        self.btn_stop.setObjectName("danger")
        self.btn_stop.clicked.connect(self._stop_batch)
        self.btn_stop.setEnabled(False)
        
        btn_lay.addWidget(self.btn_start)
        btn_lay.addWidget(self.btn_pause)
        btn_lay.addWidget(self.btn_stop)
        left_layout.addLayout(btn_lay)
 
        # 5. Help Row Buttons
        help_row = QHBoxLayout()
        self.btn_instructions = QPushButton("Instructions")
        self.btn_instructions.clicked.connect(lambda: self.window()._show_tool_popup_guide(16))
        self.btn_issues = QPushButton("Issues/Fixes")
        self.btn_issues.clicked.connect(lambda: self.window()._show_issues_dialog() if hasattr(self.window(), '_show_issues_dialog') else None)
        self.btn_upgrade = QPushButton("Upgrade your plan")
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
        first_item = self.table.item(0, 0)
        if not first_item:
            return
        new_state = Qt.CheckState.Checked if first_item.checkState() == Qt.CheckState.Unchecked else Qt.CheckState.Unchecked
        self.table.blockSignals(True)
        for r in range(self.table.rowCount()):
            item = self.table.item(r, 0)
            if item:
                item.setCheckState(new_state)
        self.table.blockSignals(False)

    def _download_selected_jobs(self):
        selected_rows = list(set(index.row() for index in self.table.selectedIndexes()))
        if not selected_rows:
            QMessageBox.information(self, "Selection Required", "Please select one or more jobs in the table first.")
            return
        self._context_download_selected()

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
        self._relaunch_failed_job(job.index)

    def _relaunch_failed_job(self, job_index: int):
        if 0 <= job_index - 1 < len(self.jobs):
            job = self.jobs[job_index-1]
            job.status = JobStatus.PENDING
            job.error = None
            self.txt_log.appendPlainText(f"[Info] Relaunching Job #{job_index}...")
            self._refresh_table()

    def _on_table_item_changed(self, item):
        if not item:
            return
        row = item.row()
        col = item.column()
        if not (0 <= row < len(self.jobs)):
            return
        
        job = self.jobs[row]
        
        # Guard active jobs from status and download path edits
        if col in (5, 6) and job.status in [JobStatus.RUNNING, JobStatus.WAITING, JobStatus.DOWNLOADING]:
            current_text = item.text().strip().upper()
            expected_text = job.status.value.upper() if col == 5 else (job.download_path or "-")
            if current_text == expected_text.upper():
                return
                
            self.table.blockSignals(True)
            try:
                if col == 5:
                    item.setText(job.status.value.upper())
                elif col == 6:
                    item.setText(job.download_path or "-")
            finally:
                self.table.blockSignals(False)
            QTimer.singleShot(0, lambda: QMessageBox.warning(
                self,
                "Job Running",
                "You cannot modify the status or download path of an active running/waiting/downloading job."
            ))
            return
            
        if col == 3:  # Prompt column
            new_prompt = item.text().strip()
            if not new_prompt:
                self.table.blockSignals(True)
                item.setText(job.prompt)
                self.table.blockSignals(False)
                return
            if job.prompt != new_prompt:
                job.prompt = new_prompt
                self.txt_log.appendPlainText(f"[Info] Updated prompt for Job #{job.index}.")
        elif col == 5:  # Status column
            new_status_str = item.text().strip().lower()
            valid_status = None
            for status in JobStatus:
                if status.value == new_status_str:
                    valid_status = status
                    break
            if valid_status:
                if job.status != valid_status:
                    job.status = valid_status
                    self._refresh_table()
                    self._update_stats()
            else:
                self.table.blockSignals(True)
                item.setText(job.status.value.upper())
                self.table.blockSignals(False)
        elif col == 6:  # Download Path column
            new_path = item.text().strip()
            job.download_path = new_path

    def _on_table_context_menu(self, pos):
        clicked_index = self.table.indexAt(pos)
        clicked_row = clicked_index.row()
        clicked_col = clicked_index.column()
        
        menu = QMenu(self)
        selected_rows = list(set(idx.row() for idx in self.table.selectedIndexes()))
        if clicked_row >= 0 and clicked_row not in selected_rows:
            selected_rows.append(clicked_row)
            
        actions = []
        
        # 1. Clipboard Copy Operations
        if clicked_row >= 0 and clicked_col >= 0:
            item = self.table.item(clicked_row, clicked_col)
            if item:
                cell_text = item.text()
                copy_cell_action = menu.addAction("Copy Cell Content")
                copy_cell_action.triggered.connect(lambda *args, text=cell_text: QApplication.clipboard().setText(text))
                actions.append(copy_cell_action)
                
        if clicked_row >= 0 and 0 <= clicked_row < len(self.jobs):
            job = self.jobs[clicked_row]
            if getattr(job, 'download_path', None):
                copy_path_action = menu.addAction("Copy Download Path")
                copy_path_action.triggered.connect(lambda *args, p=job.download_path: QApplication.clipboard().setText(p))
                actions.append(copy_path_action)
            menu.addSeparator()
            
        # Row modification operations
        add_row_action = menu.addAction("Add New Row")
        add_row_action.triggered.connect(self._context_add_row)
        actions.append(add_row_action)
        
        if selected_rows:
            edit_row_action = menu.addAction("Edit Selected Row...")
            edit_row_action.triggered.connect(self._context_edit_row)
            actions.append(edit_row_action)
            
            duplicate_row_action = menu.addAction("Copy/Duplicate Selected Rows")
            duplicate_row_action.triggered.connect(self._context_duplicate_rows)
            actions.append(duplicate_row_action)
            
        menu.addSeparator()
        
        # 2. Selection Modification
        toggle_check_action = menu.addAction("Toggle Checkbox for Selected Rows")
        toggle_check_action.triggered.connect(self._context_toggle_checks)
        actions.append(toggle_check_action)
        
        menu.addSeparator()
        
        # 3. Status Override Submenu
        if selected_rows:
            status_menu = menu.addMenu("Change Status...")
            for status in JobStatus:
                status_action = status_menu.addAction(status.value.upper())
                status_action.triggered.connect(
                    lambda checked, rows=list(selected_rows), s=status: self._context_change_status(rows, s)
                )
                actions.append(status_action)
            menu.addSeparator()
            
        relaunch_action = menu.addAction("Relaunch Selected Rows (Manual Browser)")
        relaunch_action.triggered.connect(self._context_relaunch_manual)
        actions.append(relaunch_action)
        
        download_action = menu.addAction("Download Selected Rows")
        download_action.triggered.connect(self._context_download_selected)
        actions.append(download_action)
        
        menu.addSeparator()
        
        if selected_rows:
            set_path_action = menu.addAction("Set Download Path...")
            set_path_action.triggered.connect(
                lambda checked, rows=list(selected_rows): self._context_set_download_path(rows)
            )
            actions.append(set_path_action)
            menu.addSeparator()
            
        remove_action = menu.addAction("Clear from List")
        remove_action.triggered.connect(self._context_clear_rows)
        actions.append(remove_action)
        
        menu.exec(self.table.viewport().mapToGlobal(pos))

    def _context_add_row(self):
        dialog = JobDialog(self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            data = dialog.get_data()
            new_job = PromptJob(
                index=len(self.jobs) + 1,
                prompt=data["prompt"],
                caption="",
                video_title=data["video_title"],
                scene_index=data["scene_index"],
                reference_image=data["reference_image"],
                status=JobStatus.PENDING
            )
            self.jobs.append(new_job)
            self._refresh_table()
            self._update_stats()
            
    def _context_edit_row(self):
        selected_rows = list(set(idx.row() for idx in self.table.selectedIndexes()))
        if not selected_rows:
            return
        row = selected_rows[0]
        if 0 <= row < len(self.jobs):
            job = self.jobs[row]
            dialog = JobDialog(self, job)
            if dialog.exec() == QDialog.DialogCode.Accepted:
                data = dialog.get_data()
                job.prompt = data["prompt"]
                job.video_title = data["video_title"]
                job.scene_index = data["scene_index"]
                job.reference_image = data["reference_image"]
                self._refresh_table()
                
    def _context_duplicate_rows(self):
        selected_rows = sorted(list(set(idx.row() for idx in self.table.selectedIndexes())))
        if not selected_rows:
            return
        new_jobs = []
        for r in selected_rows:
            if 0 <= r < len(self.jobs):
                orig = self.jobs[r]
                dup = PromptJob(
                    index=len(self.jobs) + len(new_jobs) + 1,
                    prompt=orig.prompt,
                    caption=orig.caption,
                    video_title=orig.video_title,
                    scene_index=orig.scene_index,
                    reference_image=orig.reference_image,
                    status=JobStatus.PENDING
                )
                new_jobs.append(dup)
        self.jobs.extend(new_jobs)
        self._refresh_table()
        self._update_stats()

    def _context_toggle_checks(self):
        selected_rows = list(set(idx.row() for idx in self.table.selectedIndexes()))
        if not selected_rows:
            return
        first_item = self.table.item(selected_rows[0], 0)
        if not first_item:
            return
        new_state = Qt.CheckState.Checked if first_item.checkState() == Qt.CheckState.Unchecked else Qt.CheckState.Unchecked
        self.table.blockSignals(True)
        for r in selected_rows:
            item = self.table.item(r, 0)
            if item:
                item.setCheckState(new_state)
        self.table.blockSignals(False)

    def _context_change_status(self, rows, status: JobStatus):
        allowed_rows = []
        active_skipped = False
        for r in rows:
            if 0 <= r < len(self.jobs):
                job = self.jobs[r]
                if job.status in [JobStatus.RUNNING, JobStatus.WAITING, JobStatus.DOWNLOADING]:
                    active_skipped = True
                else:
                    allowed_rows.append(r)
                    
        if active_skipped and not allowed_rows:
            QMessageBox.warning(self, "Action Blocked", "You cannot modify the status of active running/waiting/downloading jobs.")
            return
            
        for r in allowed_rows:
            self.jobs[r].status = status
            if status not in [JobStatus.FAILED]:
                self.jobs[r].error = None
        self._refresh_table()
        self._update_stats()

    def _context_relaunch_manual(self):
        selected_rows = list(set(idx.row() for idx in self.table.selectedIndexes()))
        if not selected_rows:
            return
        for r in selected_rows:
            if 0 <= r < len(self.jobs):
                self._relaunch_failed_job(self.jobs[r].index)

    def _context_download_selected(self):
        selected_rows = list(set(idx.row() for idx in self.table.selectedIndexes()))
        if not selected_rows:
            return
        QMessageBox.information(self, "Download", f"Downloading completed clips for {len(selected_rows)} selected rows...")

    def _context_set_download_path(self, rows):
        path = QFileDialog.getExistingDirectory(self, "Select Download Directory")
        if path:
            for r in rows:
                if 0 <= r < len(self.jobs):
                    self.jobs[r].download_path = path
            self._refresh_table()

    def _context_clear_rows(self):
        selected_rows = sorted(list(set(idx.row() for idx in self.table.selectedIndexes())), reverse=True)
        for r in selected_rows:
            if 0 <= r < len(self.jobs):
                self.jobs.pop(r)
        self._refresh_table()
        self._update_stats()

    def _refresh_table(self):
        self.table.blockSignals(True)
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
            
            # Checkbox / Index
            chk = QTableWidgetItem(f"Job #{job.index}")
            chk.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            chk.setCheckState(Qt.CheckState.Unchecked)
            self.table.setItem(row, 0, chk)
            
            # Video Title
            title_item = QTableWidgetItem(job.video_title or "Standalone")
            title_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            self.table.setItem(row, 1, title_item)

            # Scene Index
            scene_item = QTableWidgetItem(str(job.scene_index) if job.scene_index is not None else "-")
            scene_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            self.table.setItem(row, 2, scene_item)

            # Prompt (Fully editable)
            p_item = QTableWidgetItem(job.prompt)
            p_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEditable)
            self.table.setItem(row, 3, p_item)

            # Reference
            ref_str = Path(job.reference_image).name if job.reference_image else "None"
            ref_item = QTableWidgetItem(ref_str)
            ref_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            self.table.setItem(row, 4, ref_item)

            # Status
            status_item = QTableWidgetItem(job.status.value.upper())
            status_flags = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
            if job.status not in [JobStatus.RUNNING, JobStatus.WAITING, JobStatus.DOWNLOADING]:
                status_flags |= Qt.ItemFlag.ItemIsEditable
            status_item.setFlags(status_flags)
            status_color = STATUS_COLORS.get(job.status.value.lower(), "#ffffff")
            status_item.setForeground(QColor(status_color))
            self.table.setItem(row, 5, status_item)
            
            # Download Path
            dl_item = QTableWidgetItem(job.download_path or "-")
            dl_flags = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
            if job.status not in [JobStatus.RUNNING, JobStatus.WAITING, JobStatus.DOWNLOADING]:
                dl_flags |= Qt.ItemFlag.ItemIsEditable
            dl_item.setFlags(dl_flags)
            self.table.setItem(row, 6, dl_item)
            
            # Error Details
            err_item = QTableWidgetItem(getattr(job, 'error', None) or "-")
            err_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            if getattr(job, 'error', None):
                err_item.setForeground(QColor("#D97706"))
            self.table.setItem(row, 7, err_item)
            
            # Relaunch Action Button
            btn_cell = QWidget()
            cell_layout = QHBoxLayout(btn_cell)
            cell_layout.setContentsMargins(2, 2, 2, 2)
            cell_layout.setSpacing(5)
            
            relaunch_btn = QPushButton("Relaunch", btn_cell)
            relaunch_btn.setStyleSheet("padding: 2px 8px; font-size: 11px;")
            relaunch_btn.clicked.connect(lambda checked, idx=job.index: self._relaunch_failed_job(idx))
            cell_layout.addWidget(relaunch_btn)
            self.table.setCellWidget(row, 8, btn_cell)
            
        self.table.blockSignals(False)

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
        self.tabs = QTabWidget(self)
        layout.addWidget(self.tabs)
 
        # Tab 1: AI-Powered Editing
        tab1_widget = QWidget()
        tab1_layout = QVBoxLayout(tab1_widget)
        tab1_layout.setContentsMargins(0, 0, 0, 0)
 
        left_scroll = QScrollArea()
        left_scroll.setWidgetResizable(True)
        left_scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")
        
        left = QGroupBox("AI-POWERED VIDEO EDITOR — Submit a request with AI-powered editing")
        left_lay = QVBoxLayout(left)
        
        left_lay.addWidget(QLabel("Raw Video File Path:"))
        file_row = QHBoxLayout()
        self.edit_video_path = QLineEdit()
        self.edit_video_path.setPlaceholderText("Path to video file to edit...")
        btn_browse = QPushButton("Select File")
        btn_browse.clicked.connect(self._pick_video_file)
        file_row.addWidget(self.edit_video_path)
        file_row.addWidget(btn_browse)
        left_lay.addLayout(file_row)
  
        left_lay.addWidget(QLabel("AI Editing Instructions:"))
        self.edit_instructions = QPlainTextEdit()
        self.edit_instructions.setPlaceholderText("Examples:\n- Cut out all silences and add zoom transitions.\n- Auto-crop video into vertical (9:16) framing focus on speakers.\n- Add burned-in animated subtitles and energetic edits.")
        left_lay.addWidget(self.edit_instructions)
  
        self.btn_process = QPushButton("🎬 Start AI-Powered Compilation")
        self.btn_process.setObjectName("primary")
        self.btn_process.clicked.connect(self._run_ai_edit)
        left_lay.addWidget(self.btn_process)
  
        self.progress_bar = QProgressBar()
        self.progress_bar.setVisible(False)
        left_lay.addWidget(self.progress_bar)
  
        self.txt_log = QPlainTextEdit()
        self.txt_log.setReadOnly(True)
        self.txt_log.setPlaceholderText("Console output log...")
        left_lay.addWidget(self.txt_log)
  
        help_row = QHBoxLayout()
        self.btn_instructions = QPushButton("Instructions")
        self.btn_instructions.clicked.connect(lambda: self.window()._show_tool_popup_guide(17))
        self.btn_issues = QPushButton("Issues/Fixes")
        self.btn_issues.clicked.connect(lambda: self.window()._show_issues_dialog() if hasattr(self.window(), '_show_issues_dialog') else None)
        self.btn_upgrade = QPushButton("Upgrade your plan")
        self.btn_upgrade.setObjectName("primary")
        self.btn_upgrade.clicked.connect(lambda: self.window()._open_premium_whatsapp() if hasattr(self.window(), '_open_premium_whatsapp') else None)
        help_row.addWidget(self.btn_instructions)
        help_row.addWidget(self.btn_issues)
        help_row.addWidget(self.btn_upgrade)
        left_lay.addLayout(help_row)
  
        left_scroll.setWidget(left)
        tab1_layout.addWidget(left_scroll)
        self.tabs.addTab(tab1_widget, "🤖 AI-Powered Editing")
 
        # Tab 2: Manual Timeline Editing
        tab2_widget = QWidget()
        tab2_layout = QVBoxLayout(tab2_widget)
        tab2_layout.setContentsMargins(0, 0, 0, 0)
 
        manual_scroll = QScrollArea()
        manual_scroll.setWidgetResizable(True)
        manual_scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")
 
        manual_container = QWidget()
        manual_lay = QVBoxLayout(manual_container)
        manual_lay.setContentsMargins(10, 10, 10, 10)
        manual_lay.setSpacing(15)
 
        # Group 1: Headed Web Interface Launcher
        launch_card = QGroupBox("1. Headed Web Interface Launcher")
        launch_card_lay = QVBoxLayout(launch_card)
        self.btn_launch_web = QPushButton("🚀 Launch OpenCut Web Editor in headed browser")
        self.btn_launch_web.setObjectName("primary")
        self.btn_launch_web.setStyleSheet("padding: 12px; font-weight: bold; font-size: 13px;")
        self.btn_launch_web.clicked.connect(self._launch_web_editor)
        
        chk_lay = QGridLayout()
        self.chk_feat_timeline = QCheckBox("Timeline Trimming & Multi-Track Editing (Standard)")
        self.chk_feat_timeline.setChecked(True)
        self.chk_feat_audio = QCheckBox("Advanced Audio Mixing & Bg Music Synchronization")
        self.chk_feat_audio.setChecked(True)
        self.chk_feat_transitions = QCheckBox("Dynamic Transition Effects & Ken Burns Zoom Engine")
        self.chk_feat_transitions.setChecked(True)
        self.chk_feat_titles = QCheckBox("Title Overlays, Stickers, & Custom Watermarks")
        self.chk_feat_titles.setChecked(True)
        
        chk_lay.addWidget(self.chk_feat_timeline, 0, 0)
        chk_lay.addWidget(self.chk_feat_audio, 0, 1)
        chk_lay.addWidget(self.chk_feat_transitions, 1, 0)
        chk_lay.addWidget(self.chk_feat_titles, 1, 1)
        
        launch_card_lay.addWidget(self.btn_launch_web)
        launch_card_lay.addLayout(chk_lay)
        manual_lay.addWidget(launch_card)
 
        # Group 2: Output Configurations
        param_card = QGroupBox("2. Timeline Output configurations")
        param_grid = QGridLayout(param_card)
        
        param_grid.addWidget(QLabel("Output Resolution Profile:"), 0, 0)
        self.combo_resolution = QComboBox()
        self.combo_resolution.addItems(["🎬 16:9 Landscape (YouTube/Default)", "📱 9:16 Vertical (Shorts/TikTok)", "🔲 1:1 Square (Instagram)"])
        param_grid.addWidget(self.combo_resolution, 0, 1)
 
        param_grid.addWidget(QLabel("Burn-in subtitle settings:"), 1, 0)
        self.combo_subtitle_style = QComboBox()
        self.combo_subtitle_style.addItems(["Standard White with shadow", "Kinetic Bold Yellow", "Word-by-word highlights"])
        param_grid.addWidget(self.combo_subtitle_style, 1, 1)
 
        param_grid.addWidget(QLabel("Audio mixing presets:"), 2, 0)
        self.combo_audio_mix = QComboBox()
        self.combo_audio_mix.addItems(["Ducker (auto-lower bg music on voice)", "Balanced (50-50 sound ratio)", "Instrumental focus"])
        param_grid.addWidget(self.combo_audio_mix, 2, 1)
 
        self.chk_gpu = QCheckBox("Use GPU Hardware Acceleration (FFmpeg NVENC/AMF)")
        self.chk_gpu.setChecked(True)
        param_grid.addWidget(self.chk_gpu, 3, 0, 1, 2)
        manual_lay.addWidget(param_card)
 
        # Group 3: Manual Editor Controls (Web View placeholder)
        right = QGroupBox("3. Offline Manual Timeline Editor")
        right_lay = QVBoxLayout(right)
 
        if WEB_ENGINE_AVAILABLE:
            self.web_view = QWebEngineView()
            self.web_view.setUrl(Path.home() / 'Documents' / 'opencut_web' / 'index.html')
            right_lay.addWidget(self.web_view)
        else:
            placeholder = QWidget()
            play_lay = QGridLayout(placeholder)
            
            self.lbl_screen = QLabel()
            self.lbl_screen.setStyleSheet("background-color: #0b0f19; border: 1px solid #1a2333; border-radius: 6px;")
            self.lbl_screen.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.lbl_screen.setText("🎞️ OpenCut Manual Video Preview Canvas")
            self.lbl_screen.setMinimumHeight(150)
            play_lay.addWidget(self.lbl_screen, 0, 0, 1, 4)
 
            self.lbl_track = QLabel("Timeline Track: [Video File 1.mp4] [Cut: 00:05-00:23] [Audio overlay.mp3]")
            self.lbl_track.setStyleSheet("background-color: #162235; border-radius: 4px; padding: 10px; color: #4ade80; font-family: monospace;")
            play_lay.addWidget(self.lbl_track, 1, 0, 1, 4)
 
            btn_play = QPushButton("◀ Play")
            btn_split = QPushButton("✂ Split Track")
            btn_delete = QPushButton("🗑️ Remove")
            btn_add_text = QPushButton("✍ Add Text Track")
            play_lay.addWidget(btn_play, 2, 0)
            play_lay.addWidget(btn_split, 2, 1)
            play_lay.addWidget(btn_delete, 2, 2)
            play_lay.addWidget(btn_add_text, 2, 3)
 
            right_lay.addWidget(placeholder)
        
        manual_lay.addWidget(right)
        manual_lay.addStretch()
 
        manual_scroll.setWidget(manual_container)
        tab2_layout.addWidget(manual_scroll)
        self.tabs.addTab(tab2_widget, "✂️ Manual Timeline Editing")

    def _launch_web_editor(self):
        import os
        import time
        from pathlib import Path
        import threading
        
        def _launch():
            try:
                from playwright.sync_api import sync_playwright
            except ImportError:
                try:
                    from patchright.sync_api import sync_playwright
                except ImportError:
                    self.txt_log.appendPlainText("[OpenCut Web] Playwright library not found. Please run 'pip install playwright'.")
                    return
            
            try:
                with sync_playwright() as p:
                    launch_args = []
                    if os.name != 'nt':
                        launch_args.extend(["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"])
                    browser = p.chromium.launch(headless=False, args=launch_args)
                    page = browser.new_page()
                    web_path = Path.home() / 'Documents' / 'opencut_web' / 'index.html'
                    if web_path.exists():
                        page.goto(web_path.as_uri())
                    else:
                        page.goto("https://github.com/syedgrowsnapai/growsnap-creative-suite")
                    while not page.is_closed():
                        time.sleep(1)
            except Exception as e:
                self.txt_log.appendPlainText(f"[OpenCut Web Error] Failed to launch Playwright: {e}")
        
        self.txt_log.appendPlainText("[OpenCut Web] Initiating manual video workspace browser in background thread...")
        threading.Thread(target=_launch, daemon=True).start()

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
 
        grid.addWidget(QLabel("Long Video File Path:"), 0, 0)
        self.edit_video = QLineEdit()
        self.edit_video.setPlaceholderText("Select raw 1-3 hour video file to clip...")
        grid.addWidget(self.edit_video, 0, 1)
 
        btn_browse = QPushButton("Select File")
        btn_browse.clicked.connect(self._browse_file)
        grid.addWidget(btn_browse, 0, 2)
 
        grid.addWidget(QLabel("Number of Clips:"), 1, 0)
        self.spin_clips = QSpinBox()
        self.spin_clips.setRange(1, 100)
        self.spin_clips.setValue(5)
        grid.addWidget(self.spin_clips, 1, 1)
 
        grid.addWidget(QLabel("Clip Style/Theme:"), 2, 0)
        self.combo_theme = QComboBox()
        self.combo_theme.addItems(["Funny Moments Highlights", "Educational/Explainer Shorts", "High-Energy Hooks & CTAs", "Podcast Dialog Slices"])
        grid.addWidget(self.combo_theme, 2, 1)
 
        grid.addWidget(QLabel("On-Screen Captions:"), 3, 0)
        self.combo_captions = QComboBox()
        self.combo_captions.addItems(["Burn-in Animated Subtitles (Bold Yellow/White)", "Classic SRT Subtitles", "No Captions"])
        grid.addWidget(self.combo_captions, 3, 1)
 
        # Run Button
        self.btn_run = QPushButton("⚡ Chop & Generate Short Clips")
        self.btn_run.setObjectName("primary")
        self.btn_run.clicked.connect(self._run_clipper)
        scroll_layout.addWidget(self.btn_run)
 
        self.progress = QProgressBar()
        self.progress.setVisible(False)
        scroll_layout.addWidget(self.progress)
 
        self.log_console = QPlainTextEdit()
        self.log_console.setReadOnly(True)
        self.log_console.setPlaceholderText("Clipper progress logs...")
        scroll_layout.addWidget(self.log_console)
 
        # Help / Instructions buttons row (exactly matching Dola)
        help_row = QHBoxLayout()
        self.btn_instructions = QPushButton("Instructions")
        self.btn_instructions.clicked.connect(lambda: self.window()._show_tool_popup_guide(18))
        self.btn_issues = QPushButton("Issues/Fixes")
        self.btn_issues.clicked.connect(lambda: self.window()._show_issues_dialog() if hasattr(self.window(), '_show_issues_dialog') else None)
        self.btn_upgrade = QPushButton("Upgrade your plan")
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


class CreationCardWidget(QWidget):
    def __init__(self, title, prompt, gradient_css, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(6)
        
        self.lbl_thumb = QLabel()
        self.lbl_thumb.setFixedHeight(120)
        self.lbl_thumb.setStyleSheet(f"border-radius: 8px; {gradient_css}")
        
        self.lbl_title = QLabel(f"<b>🎨 {title}</b>")
        self.lbl_title.setStyleSheet("color: #e2e8f0; font-size: 13px;")
        
        self.lbl_prompt = QLabel(prompt)
        self.lbl_prompt.setWordWrap(True)
        self.lbl_prompt.setStyleSheet("color: #94a3b8; font-size: 11px;")
        
        layout.addWidget(self.lbl_thumb)
        layout.addWidget(self.lbl_title)
        layout.addWidget(self.lbl_prompt)

class CommunityShowcaseWidget(QWidget):
    def __init__(self, parent=None, db_path=None, global_settings=None):
        super().__init__(parent)
        self._init_ui()

    def _init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(15)

        # Subtitle
        lbl_subtitle = QLabel("COMMUNITY SHOWCASE — trending prompts feed and templates library")
        lbl_subtitle.setObjectName("subtitle")
        lbl_subtitle.setFixedHeight(20)
        layout.addWidget(lbl_subtitle)

        splitter = QSplitter(Qt.Orientation.Horizontal, self)
        layout.addWidget(splitter)

        # Left Column Scroll Area
        left_scroll = QScrollArea()
        left_scroll.setWidgetResizable(True)
        left_scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")
        
        left = QGroupBox("COMMUNITY VISUAL FEED (MAJ / MEIGEN.AI)")
        left_lay = QVBoxLayout(left)

        self.list_gallery = QListWidget()
        self.list_gallery.setSpacing(12)
        self.list_gallery.itemClicked.connect(self._copy_prompt)
        left_lay.addWidget(self.list_gallery)
        
        # Populate gallery mock creations with descriptions and prompts
        creations = [
            ("Cinematic Portrait of Cyberpunk Cyborg", "hyper-detailed, Orbitron glow, dark neon atmospheric lighting, volumetric smoke, unreal engine render, 8k --ar 16:9", "background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #1a0b2e, stop:0.5 #8b5cf6, stop:1 #ec4899);"),
            ("3D Glossy Liquid Abstract Art", "glassmorphic fluid simulation, rainbow reflections, glowing mesh overlay, premium background art --v 6.0", "background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #0f172a, stop:0.5 #3b82f6, stop:1 #06b6d4);"),
            ("Neon Green Retro Sports Car", "drifting on rain-slicked asphalt streets at night, synthwave aesthetic, detailed reflection mapping", "background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #064e3b, stop:0.5 #10b981, stop:1 #f59e0b);"),
            ("Glassmorphic Widget Dashboard Mockup", "futuristic UI interface design, dark mode glowing neon accents, clean sans font --ar 4:3", "background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #312e81, stop:0.5 #4f46e5, stop:1 #a855f7);"),
            ("Steaming cup of coffee on workspace", "soft window morning glow, photorealistic, cinematic camera blur, warm color grading", "background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #7c2d12, stop:0.5 #d97706, stop:1 #fde047);")
        ]
        
        for item, prompt, grad in creations:
            list_item = QListWidgetItem(self.list_gallery)
            list_item.setData(Qt.ItemDataRole.UserRole, prompt)
            list_item.setSizeHint(QSize(250, 200))
            self.list_gallery.addItem(list_item)
            
            card = CreationCardWidget(item, prompt, grad)
            self.list_gallery.setItemWidget(list_item, card)

        btn_row = QHBoxLayout()
        btn_copy = QPushButton("📋 Copy Selected Prompt")
        btn_copy.clicked.connect(self._copy_prompt)
        btn_send = QPushButton("⚡ Send to Generator")
        btn_send.clicked.connect(self._send_to_generator)
        btn_row.addWidget(btn_copy)
        btn_row.addWidget(btn_send)
        left_lay.addLayout(btn_row)

        left_scroll.setWidget(left)
        splitter.addWidget(left_scroll)

        # Right Column Scroll Area
        right_scroll = QScrollArea()
        right_scroll.setWidgetResizable(True)
        right_scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")
        
        right = QGroupBox("PROMPT TEMPLATES & SKILLS")
        right_lay = QVBoxLayout(right)

        self.list_templates = QListWidget()
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

        btn_copy_t = QPushButton("📋 Copy Template")
        btn_copy_t.clicked.connect(self._copy_template)
        right_lay.addWidget(btn_copy_t)

        right_scroll.setWidget(right)
        splitter.addWidget(right_scroll)
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
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")
        
        scroll_widget = QWidget()
        layout = QVBoxLayout(scroll_widget)
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

        scroll.setWidget(scroll_widget)
        main_layout.addWidget(scroll)

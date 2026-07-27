import os
import time
import subprocess
import threading
from typing import Optional
from pathlib import Path
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame, QSplitter, QScrollArea,
    QGroupBox, QPlainTextEdit, QLineEdit, QPushButton, QTableWidget, QTableWidgetItem,
    QHeaderView, QAbstractItemView, QTabWidget, QComboBox, QCheckBox, QFileDialog,
    QMessageBox, QMenu, QDialog, QDialogButtonBox, QFormLayout, QSpinBox, QApplication,
    QGridLayout, QInputDialog, QListWidget, QListWidgetItem
)
from PyQt6.QtCore import Qt, QTimer, pyqtSlot
from PyQt6.QtGui import QColor, QAction

from dola_automation.models import AutomationSettings, PromptJob, JobStatus
from dola_automation.new_tabs import CheckableComboBox, JobDialog
from dola_automation.easemate_worker import EasemateBatchRunner
from dola_automation.logger import logger
from dola_automation.database import HistoryDatabase

def parse_easemate_csv(content_or_path: str) -> list[tuple[str, str, str, Optional[str]]]:
    """
    Parses CSV content or filepath mapping multi-mockup columns and reference images.
    Returns a list of tuples: (prompt, video_title, suffix, reference_image)
    """
    import io
    import csv
    
    content = content_or_path
    try:
        path_obj = Path(content_or_path)
        if path_obj.exists() and path_obj.is_file():
            with open(path_obj, 'r', encoding='utf-8') as f:
                content = f.read()
    except Exception:
        pass
        
    normalized = content.replace('\r\n', '\n').strip()
    
    try:
        f = io.StringIO(normalized)
        reader = csv.reader(f)
        rows = list(reader)
        if not rows:
            return []
            
        header = [col.strip().lower() for col in rows[0]]
        
        # Look for reference image column
        ref_image_idx = -1
        for idx, col in enumerate(header):
            if any(k in col for k in ["reference_image", "ref_image", "image_path", "image_file", "local_image"]):
                ref_image_idx = idx
                break
                
        # Detect multi-mockup columns
        if "product_title" in header or any(col.startswith("mockup_") for col in header):
            title_idx = header.index("product_title") if "product_title" in header else -1
            card_id_idx = header.index("card_id") if "card_id" in header else -1
            
            # Map column indices to suffix names
            mockup_cols = []
            suffix_map = {
                "mockup_1_hero_prompt": "hero image",
                "mockup_2_variant_white_standard_prompt": "variant white standard",
                "mockup_3_variant_white_luxury_prompt": "variant white luxury",
                "mockup_4_variant_gold_standard_prompt": "variant gold standard",
                "mockup_5_variant_gold_luxury_prompt": "variant gold luxury",
                "mockup_6_supplement_ugc_worn_prompt": "supplement ugc worn",
                "mockup_7_supplement_unboxing_prompt": "supplement unboxing",
                "mockup_8_supplement_reaction_prompt": "supplement reaction",
                "mockup_9_supplement_macro_detail_prompt": "supplement macro detail"
            }
            
            for idx, col in enumerate(header):
                if col.startswith("mockup_") and col.endswith("_prompt"):
                    suffix = suffix_map.get(col, col.replace("mockup_", "").replace("_prompt", "").replace("_", " "))
                    mockup_cols.append((idx, suffix))
                    
            mockup_cols.sort(key=lambda x: header[x[0]])
            
            results = []
            for r_idx, row in enumerate(rows[1:]):
                if not row or len(row) <= max(title_idx, 0):
                    continue
                
                card_id_prefix = f"[{row[card_id_idx].strip()}] " if card_id_idx != -1 and card_id_idx < len(row) and row[card_id_idx].strip() else ""
                prod_title = row[title_idx].strip() if title_idx != -1 and title_idx < len(row) else f"Product {r_idx + 1}"
                
                ref_img_val = None
                if ref_image_idx != -1 and ref_image_idx < len(row):
                    ref_img_val = row[ref_image_idx].strip() or None
                
                for col_idx, suffix in mockup_cols:
                    if col_idx < len(row) and row[col_idx].strip():
                        prompt_val = row[col_idx].strip()
                        full_title = f"{card_id_prefix}{prod_title} - {suffix}"
                        results.append((prompt_val, full_title, suffix, ref_img_val))
            if results:
                return results
    except Exception as e:
        logger.error(f"Error parsing CSV in Easemate layout: {e}")
        
    # Standard fallback layout parsing
    try:
        f = io.StringIO(normalized)
        reader = csv.reader(f)
        rows = list(reader)
        if rows:
            header = [col.strip().lower() for col in rows[0]]
            prompt_idx = -1
            ref_image_idx = -1
            title_idx = -1
            
            for i, col in enumerate(header):
                if "prompt" in col or "text" in col:
                    prompt_idx = i
                elif any(k in col for k in ["reference_image", "ref_image", "image_path", "image_file", "local_image"]):
                    ref_image_idx = i
                elif "title" in col or "name" in col:
                    title_idx = i
                    
            if prompt_idx != -1:
                results = []
                for idx, row in enumerate(rows[1:]):
                    if not row or len(row) <= prompt_idx:
                        continue
                    p = row[prompt_idx].strip()
                    t = row[title_idx].strip() if title_idx != -1 and title_idx < len(row) else f"Image_{idx+1}"
                    ref = row[ref_image_idx].strip() if ref_image_idx != -1 and ref_image_idx < len(row) else None
                    if p:
                        results.append((p, t, "image", ref))
                return results
    except Exception:
        pass
        
    from dola_automation.models import parse_prompts
    std_parsed = parse_prompts(content)
    return [(p, title or f"Image_{idx+1}", "image", None) for idx, (p, c, title, s_idx) in enumerate(std_parsed)]


class EasemateAIAutomationWidget(QWidget):
    def __init__(self, parent=None, db_path=None, global_settings=None):
        super().__init__(parent)
        self.db_path = db_path or (Path.home() / 'Documents' / 'easemate_video_automation' / 'history.db')
        self.db = HistoryDatabase(self.db_path)
        self.settings = global_settings or AutomationSettings()
        self.jobs = []
        self.runner = None
        self.current_session_id = None
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
        self.is_paused = False
        self.is_running = False
        self.elapsed_seconds = 0
        self.batch_timer = QTimer(self)
        self.batch_timer.timeout.connect(self._update_timer)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(15)

        # Header Title
        lbl_subtitle = QLabel("EASEMATE AI IMAGE GENERATOR — powered by ChatGPT image tool, Google Nano Banana, Seedrum Pro/5.0, or Kling 3.0", self)
        lbl_subtitle.setObjectName("subtitle")
        lbl_subtitle.setFixedHeight(20)
        layout.addWidget(lbl_subtitle)

        # Stats Bar
        stats_row = QHBoxLayout()
        stats_row.setSpacing(15)
        
        self.stat_lifetime = self._stat_card("LIFETIME IMAGES", "0")
        self.stat_batch = self._stat_card("BATCH IMAGES", "0")
        self.stat_total = self._stat_card("BATCH PROMPTS", "0")
        self.stat_fail = self._stat_card("BATCH FAILED", "0")

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

        # Splitter Layout
        splitter = QSplitter(Qt.Orientation.Horizontal, self)
        layout.addWidget(splitter, 1)

        # Left Scroll Panel (Inputs)
        left_scroll = QScrollArea(self)
        left_scroll.setWidgetResizable(True)
        left_scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")
        
        left = QWidget()
        left.setObjectName("left_panel_container")
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(15)

        # Mode Selection
        mode_group = QGroupBox("GENERATION MODE")
        mode_lay = QHBoxLayout(mode_group)
        self.btn_mode_image = QPushButton("🖼️ Generate Mockup Image", self)
        self.btn_mode_image.setCheckable(True)
        self.btn_mode_image.setChecked(True)
        mode_lay.addWidget(self.btn_mode_image)
        left_layout.addWidget(mode_group)

        # Prompt Ingestion
        ingest_group = QGroupBox("PROMPT INGESTION")
        ingest_lay = QVBoxLayout(ingest_group)
        self.prompt_editor = QPlainTextEdit()
        self.prompt_editor.setPlaceholderText("Enter or paste mockup prompts here...")
        self.prompt_editor.setMaximumHeight(180)
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
        
        # Bottom Operational Panel (Moved here to prevent overflow/cutoff issues)
        run_row = QHBoxLayout()
        self.btn_start = QPushButton("Start batch")
        self.btn_start.setObjectName("primary")
        self.btn_start.clicked.connect(self._start_batch)
        self.btn_pause = QPushButton("Pause")
        self.btn_pause.setEnabled(False)
        self.btn_pause.clicked.connect(self._pause_batch)
        self.btn_stop = QPushButton("Stop")
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self._stop_batch)
        run_row.addWidget(self.btn_start)
        run_row.addWidget(self.btn_pause)
        run_row.addWidget(self.btn_stop)
        left_layout.addLayout(run_row)
        
        left_scroll.setWidget(left)
        splitter.addWidget(left_scroll)

        # Right Tab Widget
        self.right_tabs = QTabWidget(self)
        
        # Tab 1: Current Jobs
        self.tab_current = QWidget(self)
        tab_current_lay = QVBoxLayout(self.tab_current)
        
        filter_bar = QHBoxLayout()
        lbl_filter = QLabel("Filter Status:", self)
        lbl_filter.setStyleSheet("font-weight: bold; color: #2ecc71;")
        filter_bar.addWidget(lbl_filter)
        
        self.combo_filter_status = CheckableComboBox(self)
        self.combo_filter_status.add_checkable_item("All Statuses", checked=True)
        self.combo_filter_status.add_checkable_item("Pending", checked=True)
        self.combo_filter_status.add_checkable_item("Running", checked=True)
        self.combo_filter_status.add_checkable_item("Completed", checked=True)
        self.combo_filter_status.add_checkable_item("Failed", checked=True)
        self.combo_filter_status.add_checkable_item("Cancelled", checked=True)
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
        self.table.setColumnCount(8)
        self.table.setHorizontalHeaderLabels([
            "Index", "Product Title", "Suffix", "Prompt", "Status", "Download Path", "Error Details", "Action"
        ])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.table.setColumnWidth(0, 50)
        self.table.setColumnWidth(1, 150)
        self.table.setColumnWidth(2, 100)
        self.table.setColumnWidth(3, 220)
        self.table.setColumnWidth(4, 90)
        self.table.setColumnWidth(5, 120)
        self.table.setColumnWidth(6, 120)
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

        # Tab 2: History & Logs (matching Dola)
        self.tab_history_logs = QWidget(self)
        tab_history_logs_lay = QHBoxLayout(self.tab_history_logs)
        
        history_group = QGroupBox("SESSION HISTORY", self)
        history_layout = QVBoxLayout(history_group)
        self.history_list = QListWidget(self)
        history_layout.addWidget(self.history_list)
        
        history_btns = QHBoxLayout()
        self.btn_refresh_history = QPushButton("Refresh", self)
        self.btn_refresh_history.clicked.connect(self._refresh_history)
        self.btn_load_session = QPushButton("Load session", self)
        self.btn_load_session.clicked.connect(self._load_selected_session)
        history_btns.addWidget(self.btn_refresh_history)
        history_btns.addWidget(self.btn_load_session)
        history_layout.addLayout(history_btns)
        
        tab_history_logs_lay.addWidget(history_group, 1)

        log_group = QGroupBox("CONSOLE LOGS", self)
        log_layout = QVBoxLayout(log_group)
        self.txt_log = QPlainTextEdit(self)
        self.txt_log.setReadOnly(True)
        log_layout.addWidget(self.txt_log)
        
        tab_history_logs_lay.addWidget(log_group, 2)
        
        self.right_tabs.addTab(self.tab_history_logs, "History & Logs")

        # Tab 3: Settings
        self.tab_settings = QWidget(self)
        tab_settings_lay = QVBoxLayout(self.tab_settings)
        tab_settings_lay.setContentsMargins(10, 10, 10, 10)
        tab_settings_lay.setSpacing(15)

        # Image settings group
        gen_params_group = QGroupBox("IMAGE SETTINGS", self)
        gen_lay = QGridLayout(gen_params_group)
        gen_lay.addWidget(QLabel("Image Model:", self), 0, 0)
        self.combo_model = QComboBox(self)
        models_list = [
            "GPT image 2", "GPT-4o", "GPT image 1.5",
            "Nano Banana 2", "Nano Banana Pro", "Nano Banana",
            "Seedream 5.0 Pro", "Seedream 5.0 lite", "Seedream 4.5", "Seedream 4.0",
            "Kling O1 Image", "Midjourney image",
            "Wan 2.7 image pro", "Wan 2.7 image", "Wan 2.5 image",
            "Qwen image", "Qwen image 2512", "Qwen image 2.0",
            "FLUX 2 Pro", "FLUX 2 Flex", "FLUX Kontext Pro", "FLUX Kontext Max",
            "Hunyuan Image 3"
        ]
        self.combo_model.addItems(models_list)
        self.combo_model.setCurrentText("GPT image 2")
        self.combo_model.setEnabled(False) # Locked by default for free generations
        gen_lay.addWidget(self.combo_model, 0, 1)

        gen_lay.addWidget(QLabel("Generation Mode:", self), 1, 0)
        self.combo_gen_mode = QComboBox(self)
        self.combo_gen_mode.addItems([
            "Auto (Use reference if path exists)",
            "Text to Image (Ignore reference paths)",
            "Image to Image (Requires reference path)"
        ])
        self.combo_gen_mode.setCurrentIndex(0)
        gen_lay.addWidget(self.combo_gen_mode, 1, 1)

        gen_lay.addWidget(QLabel("Aspect Ratio:", self), 2, 0)
        self.combo_ratio = QComboBox(self)
        self.combo_ratio.addItems([
            "1:1", "Auto", "9:16", "16:9", "4:3", "3:4", "3:2", "2:3", "2:1", "1:2", "3:1", "1:3", "21:9", "9:21"
        ])
        gen_lay.addWidget(self.combo_ratio, 2, 1)

        gen_lay.addWidget(QLabel("Resolution Ratio:", self), 3, 0)
        self.combo_resolution = QComboBox(self)
        self.combo_resolution.addItems(["1K", "2K", "4K"])
        self.combo_resolution.setCurrentText("1K")
        self.combo_resolution.setEnabled(False) # Locked by default for free generations
        gen_lay.addWidget(self.combo_resolution, 3, 1)

        gen_lay.addWidget(QLabel("Target URL:", self), 4, 0)
        self.txt_target_url = QLineEdit("https://www.easemate.ai/ai-image-generator", self)
        gen_lay.addWidget(self.txt_target_url, 4, 1)

        h_dl_lay = QHBoxLayout()
        self.lbl_download_dir_title = QLabel("Download Folder:", self)
        h_dl_lay.addWidget(self.lbl_download_dir_title)
        self.btn_dl_dir = QPushButton("Choose", self)
        self.btn_dl_dir.clicked.connect(self._pick_download_dir)
        self.btn_open_dl = QPushButton("📂 Open", self)
        self.btn_open_dl.clicked.connect(self._open_download_dir)
        h_dl_lay.addWidget(self.btn_dl_dir)
        h_dl_lay.addWidget(self.btn_open_dl)
        gen_lay.addLayout(h_dl_lay, 5, 1)
        
        self.lbl_dl_path_show = QLabel(str(Path.home() / 'Documents' / 'easemate_downloads'), self)
        self.lbl_dl_path_show.setWordWrap(True)
        gen_lay.addWidget(self.lbl_dl_path_show, 6, 0, 1, 2)
        tab_settings_lay.addWidget(gen_params_group)

        # Automation Settings Group
        automation_group = QGroupBox("AUTOMATION SETTINGS", self)
        auto_lay = QGridLayout(automation_group)
        
        self.chk_headless = QCheckBox("Run Headless Browser", self)
        auto_lay.addWidget(self.chk_headless, 0, 0, 1, 2)
 
        auto_lay.addWidget(QLabel("Concurrent Tasks:", self), 0, 2)
        self.spin_threads = QSpinBox(self)
        self.spin_threads.setRange(1, 16)
        self.spin_threads.setValue(1)
        auto_lay.addWidget(self.spin_threads, 0, 3)
 
        auto_lay.addWidget(QLabel("Page Load Timeout (sec):", self), 1, 0)
        self.spin_loading_timeout = QSpinBox(self)
        self.spin_loading_timeout.setRange(10, 600)
        self.spin_loading_timeout.setValue(getattr(self.settings, 'easemate_loading_timeout_sec', 300))
        auto_lay.addWidget(self.spin_loading_timeout, 1, 1, 1, 3)

        self.chk_submit_and_close = QCheckBox("Submit && Close Browser (Download Later)", self)
        self.chk_submit_and_close.setChecked(getattr(self.settings, 'submit_and_close', False))
        auto_lay.addWidget(self.chk_submit_and_close, 2, 0, 1, 4)

        tab_settings_lay.addWidget(automation_group)
        
        tab_settings_lay.addStretch()
        self.right_tabs.addTab(self.tab_settings, "Settings")

        # Tab 4: Lifetime History
        self.tab_lifetime = QWidget(self)
        tab_lifetime_layout = QVBoxLayout(self.tab_lifetime)

        lifetime_filter_layout = QHBoxLayout()
        lifetime_filter_layout.addWidget(QLabel("Date:", self))
        self.combo_lifetime_date = QComboBox(self)
        self.combo_lifetime_date.addItems(["All Time", "Today", "Last 7 Days", "Last 30 Days"])
        self.combo_lifetime_date.currentTextChanged.connect(self._refresh_lifetime_history)
        lifetime_filter_layout.addWidget(self.combo_lifetime_date)

        lifetime_filter_layout.addWidget(QLabel("Status:", self))
        self.combo_lifetime_filter = QComboBox(self)
        self.combo_lifetime_filter.addItems(["All", "completed", "failed", "pending", "submitted"])
        self.combo_lifetime_filter.currentTextChanged.connect(self._refresh_lifetime_history)
        lifetime_filter_layout.addWidget(self.combo_lifetime_filter)

        lifetime_filter_layout.addWidget(QLabel("Limit:", self))
        self.combo_lifetime_limit = QComboBox(self)
        self.combo_lifetime_limit.addItems(["100", "500", "1000", "5000", "10000"])
        self.combo_lifetime_limit.currentTextChanged.connect(self._refresh_lifetime_history)
        lifetime_filter_layout.addWidget(self.combo_lifetime_limit)

        lifetime_filter_layout.addWidget(QLabel("Search:", self))
        self.edit_lifetime_search = QLineEdit(self)
        self.edit_lifetime_search.setPlaceholderText("Search prompts...")
        self.edit_lifetime_search.returnPressed.connect(self._refresh_lifetime_history)
        lifetime_filter_layout.addWidget(self.edit_lifetime_search)

        self.btn_lifetime_refresh = QPushButton("Refresh", self)
        self.btn_lifetime_refresh.clicked.connect(self._refresh_lifetime_history)
        lifetime_filter_layout.addWidget(self.btn_lifetime_refresh)

        self.btn_lifetime_export = QPushButton("Export CSV", self)
        self.btn_lifetime_export.clicked.connect(self._export_lifetime_csv)
        lifetime_filter_layout.addWidget(self.btn_lifetime_export)

        tab_lifetime_layout.addLayout(lifetime_filter_layout)

        self.table_lifetime = QTableWidget(self)
        self.table_lifetime.setColumnCount(8)
        lifetime_headers = [
            "DB ID", "Session", "Index", "Product Title", "Suffix", "Prompt", "Status", "Download Path"
        ]
        self.table_lifetime.setHorizontalHeaderLabels(lifetime_headers)
        self.table_lifetime.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.table_lifetime.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table_lifetime.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table_lifetime.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table_lifetime.customContextMenuRequested.connect(self._on_lifetime_table_context_menu)
        tab_lifetime_layout.addWidget(self.table_lifetime)

        lifetime_action_bar = QHBoxLayout()
        self.btn_lifetime_select_all = QPushButton("Select All", self)
        self.btn_lifetime_select_all.clicked.connect(self._toggle_lifetime_select_all)
        self.btn_lifetime_download_selected = QPushButton("Download Selected", self)
        self.btn_lifetime_download_selected.clicked.connect(self._download_lifetime_selected_jobs)
        self.btn_lifetime_retry_failed = QPushButton("Retry All Failed", self)
        self.btn_lifetime_retry_failed.clicked.connect(self._retry_lifetime_all_failed_jobs)
        lifetime_action_bar.addWidget(self.btn_lifetime_select_all)
        lifetime_action_bar.addWidget(self.btn_lifetime_download_selected)
        lifetime_action_bar.addWidget(self.btn_lifetime_retry_failed)
        tab_lifetime_layout.addLayout(lifetime_action_bar)

        self.right_tabs.addTab(self.tab_lifetime, "Lifetime History")

        splitter.addWidget(self.right_tabs)
        splitter.setSizes([350, 650])

        self._refresh_history()
        self._refresh_lifetime_history()

    def _translate_windows_path(self, path_str: str) -> str:
        path_str = path_str.strip().strip('"').strip("'")
        if not path_str:
            return ""
        import os
        import re
        if os.name != 'nt': # WSL/Linux mode
            match = re.match(r'^([a-zA-Z]):[\\/](.*)', path_str)
            if match:
                drive = match.group(1).lower()
                rest = match.group(2).replace('\\', '/')
                return f"/mnt/{drive}/{rest}"
            if '\\' in path_str:
                path_str = path_str.replace('\\', '/')
        else: # Native Windows mode
            if '/' in path_str:
                path_str = path_str.replace('/', '\\')
        return path_str

    def _load_prompt_from_path(self):
        raw_path = self.edit_path.text().strip()
        if not raw_path:
            return
        translated = self._translate_windows_path(raw_path)
        path_obj = Path(translated)
        if path_obj.exists() and path_obj.is_file():
            try:
                with open(path_obj, 'r', encoding='utf-8') as f:
                    self.prompt_editor.setPlainText(f.read())
                self.txt_log.appendPlainText(f"[Info] Loaded prompts from pasted path: {path_obj.name}")
            except Exception as e:
                QMessageBox.critical(self, "Error", f"Failed to read file: {e}")
        else:
            # Fuzzy match in parent directory
            suggestions = []
            try:
                parent_dir = path_obj.parent
                if parent_dir.exists():
                    import difflib
                    all_files = [f.name for f in parent_dir.iterdir() if f.is_file()]
                    suggestions = difflib.get_close_matches(path_obj.name, all_files, n=5, cutoff=0.3)
            except Exception:
                pass
                
            if suggestions:
                item, ok = QInputDialog.getItem(
                    self, "File Not Found - Suggestions",
                    f"File path does not exist:\n{raw_path}\n\nDid you mean one of these files in the directory?\nSelect a file to load:",
                    suggestions, 0, False
                )
                if ok and item:
                    # Replace basename in raw_path
                    import re
                    # Split path from the right by either slash or backslash
                    parts = re.split(r'([\\/])', raw_path)
                    if len(parts) > 1:
                        parts[-1] = item
                        new_val = "".join(parts)
                    else:
                        new_val = str(path_obj.parent / item)
                    self.edit_path.setText(new_val)
                    self._load_prompt_from_path()
            else:
                QMessageBox.critical(self, "Error", f"File path does not exist:\n{raw_path}")

    def _load_prompt_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "Select Prompt File", "", "Text/CSV Files (*.txt *.csv)")
        if path:
            self.edit_path.setText(path)
            self._load_prompt_from_path()

    def _parse_prompts(self):
        text = self.prompt_editor.toPlainText().strip()
        if not text:
            QMessageBox.warning(self, "No Prompts", "Please enter prompts or load a sheet file first.")
            return

        parsed = parse_easemate_csv(text)
        if not parsed:
            QMessageBox.warning(self, "No Valid Rows", "No valid prompts parsed. Verify sheet headers.")
            return

        self.jobs.clear()
        for idx, (prompt, title, suffix, ref_img) in enumerate(parsed):
            job = PromptJob(
                index=idx + 1,
                prompt=prompt,
                video_title=title,
                caption=suffix,
                reference_image=Path(self._translate_windows_path(ref_img)) if ref_img else None,
                status=JobStatus.PENDING
            )
            self.jobs.append(job)

        self._refresh_table()
        self._update_stats()
        self.txt_log.appendPlainText(f"[Info] Successfully loaded {len(self.jobs)} mockup generation jobs.")

    def _refresh_table(self):
        self.table.blockSignals(True)
        self.table.setRowCount(len(self.jobs))
        for row, job in enumerate(self.jobs):
            # Index
            idx_item = QTableWidgetItem(str(job.index))
            idx_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            self.table.setItem(row, 0, idx_item)

            # Product Title
            title_item = QTableWidgetItem(job.video_title)
            title_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            self.table.setItem(row, 1, title_item)

            # Suffix
            suffix_item = QTableWidgetItem(job.caption or "mockup")
            suffix_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            self.table.setItem(row, 2, suffix_item)

            # Prompt
            prompt_item = QTableWidgetItem(job.prompt)
            prompt_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            self.table.setItem(row, 3, prompt_item)

            # Status
            status_item = QTableWidgetItem(job.status.value.upper())
            status_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            if job.status == JobStatus.COMPLETED:
                status_item.setForeground(QColor("#2ecc71"))
            elif job.status == JobStatus.FAILED:
                status_item.setForeground(QColor("#e74c3c"))
            elif job.status == JobStatus.RUNNING:
                status_item.setForeground(QColor("#3498db"))
            self.table.setItem(row, 4, status_item)

            # Download Path
            dp_item = QTableWidgetItem(job.download_path or "-")
            dp_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            self.table.setItem(row, 5, dp_item)

            # Error Details
            err_item = QTableWidgetItem(job.error or "-")
            err_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            if job.error:
                err_item.setForeground(QColor("#e67e22"))
            self.table.setItem(row, 6, err_item)

            # Action Button
            btn_widget = QWidget()
            btn_lay = QHBoxLayout(btn_widget)
            btn_lay.setContentsMargins(2, 2, 2, 2)
            relaunch_btn = QPushButton("Relaunch")
            relaunch_btn.setStyleSheet("padding: 2px 8px; font-size: 11px;")
            relaunch_btn.clicked.connect(lambda checked, idx=job.index: self._relaunch_job_manual(idx))
            btn_lay.addWidget(relaunch_btn)
            self.table.setCellWidget(row, 7, btn_widget)

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

    def _update_timer(self):
        self.elapsed_seconds += 1
        hours, remainder = divmod(self.elapsed_seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        self.timer_label.setText(f"{hours:02d}:{minutes:02d}:{seconds:02d}")

    def _start_batch(self):
        if not self.jobs:
            self._parse_prompts()
            if not self.jobs:
                return

        self.txt_log.appendPlainText(f"[Info] Starting Easemate AI generation for {len(self.jobs)} items...")
        self.is_running = True
        self.is_paused = False
        self.elapsed_seconds = 0
        self.timer_label.setText("00:00:00")
        self.batch_timer.start(1000)

        self.btn_start.setEnabled(False)
        self.btn_pause.setEnabled(True)
        self.btn_pause.setText("Pause")
        self.btn_stop.setEnabled(True)
        self.right_tabs.setCurrentIndex(0)

        # Set runner configs
        self.settings.download_dir = Path(self.lbl_dl_path_show.text())
        self.settings.headless = self.chk_headless.isChecked()
        self.settings.active_profile_name = "Easemate_Free"
        self.settings.easemate_loading_timeout_sec = self.spin_loading_timeout.value()
        self.settings.model = self.combo_model.currentText()
        self.settings.ratio = self.combo_ratio.currentText()
        self.settings.resolution = self.combo_resolution.currentText()
        self.settings.generation_mode = self.combo_gen_mode.currentText()
        self.settings.target_url = self.txt_target_url.text().strip()
        self.settings.thread_count = 1 # Force sequential execution to avoid concurrent VPN/session conflicts
        self.settings.submit_and_close = self.chk_submit_and_close.isChecked()

        # Save session in database
        session_name = f"Session {time.strftime('%Y-%m-%d %H:%M')}"
        try:
            self.current_session_id = self.db.create_session(session_name, self.jobs)
        except Exception as de:
            logger.warning(f"Failed to create database session: {de}")
            self.current_session_id = None

        # Start QThread BatchRunner
        runner_mode = "submit_only" if self.settings.submit_and_close else "full"
        self.runner = EasemateBatchRunner(
            self.jobs, self.settings, db=self.db, session_id=self.current_session_id, mode=runner_mode
        )
        self.runner.job_progress.connect(self._on_runner_progress)
        self.runner.job_finished.connect(self._on_runner_finished)
        self.runner.batch_finished.connect(self._on_runner_done)
        self.runner.profile_rotated.connect(self._on_runner_profile_rotated)
        self.runner.start()

    def _on_runner_progress(self, index: int, message: str):
        self.txt_log.appendPlainText(message)
        # Update progress column in the table widget
        for row in range(self.table.rowCount()):
            idx_item = self.table.item(row, 0)
            if idx_item and idx_item.text() == str(index):
                status_item = self.table.item(row, 4)
                if status_item:
                    status_item.setText(message.upper() if len(message) < 20 else message)
                break

    def _on_runner_profile_rotated(self, next_profile: str):
        self.settings.active_profile_name = next_profile
        logger.info(f"UI Settings updated with next rotated profile: {next_profile}")

    def _on_runner_finished(self, index: int, success: bool, download_path: str, error: str):
        self._refresh_table()
        self._update_stats()

    def _on_runner_done(self):
        self.is_running = False
        self.batch_timer.stop()
        self.btn_start.setEnabled(True)
        self.btn_pause.setEnabled(False)
        self.btn_stop.setEnabled(False)
        
        # Add to history log table
        for job in self.jobs:
            if job.status == JobStatus.COMPLETED:
                h_row = self.table_history.rowCount()
                self.table_history.insertRow(h_row)
                self.table_history.setItem(h_row, 0, QTableWidgetItem(time.strftime("%Y-%m-%d %H:%M:%S")))
                self.table_history.setItem(h_row, 1, QTableWidgetItem(job.prompt[:80]))
                self.table_history.setItem(h_row, 2, QTableWidgetItem(self.combo_model.currentText()))
                self.table_history.setItem(h_row, 3, QTableWidgetItem("COMPLETED"))

        try:
            val_lbl = self.stat_lifetime.findChild(QLabel, "statValue")
            val_lbl.setText(str(int(val_lbl.text()) + sum(1 for j in self.jobs if job.status == JobStatus.COMPLETED)))
        except Exception:
            pass

        QMessageBox.information(self, "Batch Completed", "Easemate AI batch generation complete!")

    def _pause_batch(self):
        if not self.is_running:
            return
        if self.is_paused:
            self.is_paused = False
            self.btn_pause.setText("Pause")
            self.txt_log.appendPlainText("[Info] Batch resumed.")
            self.batch_timer.start(1000)
            # Resume runner
        else:
            self.is_paused = True
            self.btn_pause.setText("Resume")
            self.txt_log.appendPlainText("[Info] Batch paused.")
            self.batch_timer.stop()

    def _stop_batch(self):
        if self.runner:
            self.runner.cancel()
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

    def _relaunch_job_manual(self, index: int):
        target_job = next((j for j in self.jobs if j.index == index), None)
        if not target_job:
            return
            
        profile_dir = Path.home() / 'Documents' / 'easemate_video_automation' / 'profiles' / 'Easemate_Free'
        profile_dir.mkdir(parents=True, exist_ok=True)
        
        self.txt_log.appendPlainText(f"[Manual] Launching browser for Job #{index}...")
        
        def run_headed():
            try:
                # Try to use patchright for stealth, fallback to playwright
                try:
                    from patchright.sync_api import sync_playwright
                except ImportError:
                    from playwright.sync_api import sync_playwright

                with sync_playwright() as p:
                    launch_args = ["--disable-blink-features=AutomationControlled"]
                    try:
                        context = p.chromium.launch_persistent_context(
                            user_data_dir=str(profile_dir),
                            headless=False,
                            args=launch_args,
                            viewport={"width": 1280, "height": 800}
                        )
                    except Exception as e:
                        logger.error(f"Error launching persistent context: {e}")
                        return
                    page = context.pages[0] if context.pages else context.new_page()
                    page.goto("https://www.easemate.ai/ai-image-generator")
                    
                    # Fill prompt if textarea is visible
                    textarea = page.locator("textarea").first
                    if textarea.is_visible():
                        textarea.click()
                        textarea.fill(target_job.prompt)
                        
                    while len(context.pages) > 0:
                        time.sleep(0.5)
            except Exception as e:
                logger.error(f"Error launching headed manual browser: {e}")
                
        threading.Thread(target=run_headed, daemon=True).start()

    def _pick_download_dir(self):
        folder = QFileDialog.getExistingDirectory(self, "Select Download Folder", self.lbl_dl_path_show.text())
        if folder:
            self.lbl_dl_path_show.setText(folder)

    def _open_download_dir(self):
        path = self.lbl_dl_path_show.text()
        if os.path.exists(path):
            if os.name == 'nt':
                os.startfile(path)
            else:
                import subprocess
                subprocess.run(["xdg-open", path])

    # Table Actions Context Menu
    def _on_table_context_menu(self, pos):
        menu = QMenu(self)
        selected_rows = set(idx.row() for idx in self.table.selectedIndexes())
        
        actions = []
        if selected_rows:
            row = list(selected_rows)[0]
            job = self.jobs[row]
            
            if job.download_path and os.path.exists(job.download_path):
                open_item_action = menu.addAction("Open Image File")
                open_item_action.triggered.connect(lambda *args, p=job.download_path: os.startfile(p) if os.name == 'nt' else subprocess.run(["xdg-open", p]))
                actions.append(open_item_action)
                
                copy_path_action = menu.addAction("Copy Download Path")
                copy_path_action.triggered.connect(lambda *args, p=job.download_path: QApplication.clipboard().setText(p))
                actions.append(copy_path_action)
            menu.addSeparator()

        add_row_action = menu.addAction("Add New Row")
        add_row_action.triggered.connect(self._context_add_row)
        actions.append(add_row_action)
        
        if selected_rows:
            edit_row_action = menu.addAction("Edit Selected Row...")
            edit_row_action.triggered.connect(self._context_edit_row)
            actions.append(edit_row_action)

            copy_prompt_action = menu.addAction("Copy Prompt Text")
            copy_prompt_action.triggered.connect(self._context_copy_prompt)
            actions.append(copy_prompt_action)
            
            copy_err_action = menu.addAction("Copy Error Details")
            copy_err_action.triggered.connect(self._context_copy_error)
            actions.append(copy_err_action)
            
            duplicate_row_action = menu.addAction("Duplicate Selected Rows")
            duplicate_row_action.triggered.connect(self._context_duplicate_rows)
            actions.append(duplicate_row_action)
            
            # Change Status Submenu
            status_menu = menu.addMenu("Change Status")
            set_pending = status_menu.addAction("Pending")
            set_pending.triggered.connect(lambda: self._set_selected_rows_status(JobStatus.PENDING))
            set_running = status_menu.addAction("Running")
            set_running.triggered.connect(lambda: self._set_selected_rows_status(JobStatus.RUNNING))
            set_completed = status_menu.addAction("Completed")
            set_completed.triggered.connect(lambda: self._set_selected_rows_status(JobStatus.COMPLETED))
            set_failed = status_menu.addAction("Failed")
            set_failed.triggered.connect(lambda: self._set_selected_rows_status(JobStatus.FAILED))
            
        menu.addSeparator()

        relaunch_action = menu.addAction("Relaunch Selected Rows (Manual Browser)")
        relaunch_action.triggered.connect(self._context_relaunch_manual)
        actions.append(relaunch_action)

        download_action = menu.addAction("Download Selected Images (Download Only)")
        download_action.triggered.connect(self._download_selected_jobs)
        actions.append(download_action)

        remove_action = menu.addAction("Clear from List")
        remove_action.triggered.connect(self._context_clear_rows)
        actions.append(remove_action)
        
        menu.exec(self.table.viewport().mapToGlobal(pos))

    def _set_selected_rows_status(self, status: JobStatus):
        selected_rows = set(idx.row() for idx in self.table.selectedIndexes())
        if not selected_rows:
            return
        
        self.table.blockSignals(True)
        for row in selected_rows:
            if row < len(self.jobs):
                job = self.jobs[row]
                job.status = status
                # Clear error details if status is reset to Pending or Completed
                if status in (JobStatus.PENDING, JobStatus.COMPLETED):
                    job.error = None
                
                # Sync status changes to SQLite DB history
                if job.job_id:
                    try:
                        self.db.update_job(job.job_id, status=status, error=job.error)
                    except Exception as e:
                        logger.error(f"Failed to update job status in DB: {e}")
                        
        self.table.blockSignals(False)
        self._refresh_table()
        self._update_stats()

    def _context_add_row(self):
        dialog = JobDialog(self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            data = dialog.get_data()
            new_job = PromptJob(
                index=len(self.jobs) + 1,
                prompt=data["prompt"],
                video_title=data["video_title"],
                caption="mockup",
                status=JobStatus.PENDING
            )
            self.jobs.append(new_job)
            self._refresh_table()
            self._update_stats()

    def _context_edit_row(self):
        selected = list(set(idx.row() for idx in self.table.selectedIndexes()))
        if not selected:
            return
        row = selected[0]
        if 0 <= row < len(self.jobs):
            job = self.jobs[row]
            dialog = JobDialog(self, job)
            if dialog.exec() == QDialog.DialogCode.Accepted:
                data = dialog.get_data()
                job.prompt = data["prompt"]
                job.video_title = data["video_title"]
                self._refresh_table()

    def _context_duplicate_rows(self):
        selected = sorted(list(set(idx.row() for idx in self.table.selectedIndexes())))
        if not selected:
            return
        for r in selected:
            job = self.jobs[r]
            dup = PromptJob(
                index=len(self.jobs) + 1,
                prompt=job.prompt,
                video_title=job.video_title,
                caption=job.caption,
                status=JobStatus.PENDING
            )
            self.jobs.append(dup)
        self._refresh_table()
        self._update_stats()

    def _context_copy_prompt(self):
        selected = list(set(idx.row() for idx in self.table.selectedIndexes()))
        if not selected:
            return
        row = selected[0]
        item = self.table.item(row, 3)
        if item:
            QApplication.clipboard().setText(item.text())

    def _context_copy_error(self):
        selected = list(set(idx.row() for idx in self.table.selectedIndexes()))
        if not selected:
            return
        row = selected[0]
        item = self.table.item(row, 6)
        if item:
            QApplication.clipboard().setText(item.text())

    def _context_relaunch_manual(self):
        selected = set(idx.row() for idx in self.table.selectedIndexes())
        for r in selected:
            self._relaunch_job_manual(self.jobs[r].index)

    def _context_clear_rows(self):
        selected = sorted(list(set(idx.row() for idx in self.table.selectedIndexes())), reverse=True)
        for r in selected:
            self.jobs.pop(r)
        # Re-index
        for idx, job in enumerate(self.jobs):
            job.index = idx + 1
        self._refresh_table()
        self._update_stats()

    def _toggle_select_all(self):
        self.table.selectAll()

    def _download_selected_jobs(self):
        if self.runner and self.runner.isRunning():
            QMessageBox.warning(self, "Runner Active", "Another batch automation process is currently running. Please stop or wait for it to complete.")
            return

        selected_rows = list(set(index.row() for index in self.table.selectedIndexes()))
        selected_jobs = [self.jobs[r] for r in selected_rows]
        
        if not selected_jobs:
            selected_jobs = self.jobs

        jobs_to_dl = [j for j in selected_jobs if j.chat_url]
        skipped_count = len(selected_jobs) - len(jobs_to_dl)

        if not jobs_to_dl:
            QMessageBox.warning(self, "No Chat URL", "None of the target jobs have a chat URL. Please relaunch them or run prompt submission first.")
            return

        if skipped_count > 0:
            self.txt_log.appendPlainText(f"[Info] Skipped {skipped_count} jobs because they do not have a chat URL.")

        self.txt_log.appendPlainText(f"[Info] Downloading {len(jobs_to_dl)} jobs...")
        self.is_running = True
        self.is_paused = False
        self.btn_start.setEnabled(False)
        self.btn_pause.setEnabled(True)
        self.btn_stop.setEnabled(True)
        
        # Set runner configs
        self.settings.download_dir = Path(self.lbl_dl_path_show.text())
        self.settings.headless = self.chk_headless.isChecked()
        self.settings.active_profile_name = "Easemate_Free"
        self.settings.easemate_loading_timeout_sec = self.spin_loading_timeout.value()
        self.settings.ratio = self.combo_ratio.currentText()
        self.settings.resolution = self.combo_resolution.currentText()
        self.settings.generation_mode = self.combo_gen_mode.currentText()
        self.settings.target_url = self.txt_target_url.text().strip()
        self.settings.thread_count = 1

        self.runner = EasemateBatchRunner(
            jobs_to_dl, self.settings, db=self.db, session_id=self.current_session_id, mode="download_only"
        )
        self.runner.job_progress.connect(self._on_runner_progress)
        self.runner.job_finished.connect(self._on_runner_finished)
        self.runner.batch_finished.connect(self._on_runner_done)
        self.runner.start()

    def _retry_all_failed_jobs(self):
        failed_jobs = [j for j in self.jobs if j.status == JobStatus.FAILED]
        for job in failed_jobs:
            job.status = JobStatus.PENDING
            job.error = None
        self._refresh_table()
        self._start_batch()

    def _apply_table_filters(self):
        checked_statuses = self.combo_filter_status.checkedItems()
        search_text = self.edit_search.text().strip().lower()
        
        self.table.blockSignals(True)
        for row in range(self.table.rowCount()):
            job = self.jobs[row]
            status_match = "All Statuses" in checked_statuses or job.status.value.upper() in [s.upper() for s in checked_statuses]
            search_match = not search_text or search_text in job.video_title.lower() or search_text in job.prompt.lower()
            
            self.table.setRowHidden(row, not (status_match and search_match))
        self.table.blockSignals(False)

    def _clear_filters(self):
        self.edit_search.clear()
        self.combo_filter_status.clear()
        self.combo_filter_status.add_checkable_item("All Statuses", checked=True)
        self.combo_filter_status.add_checkable_item("Pending", checked=True)
        self.combo_filter_status.add_checkable_item("Running", checked=True)
        self.combo_filter_status.add_checkable_item("Completed", checked=True)
        self.combo_filter_status.add_checkable_item("Failed", checked=True)
        self.combo_filter_status.add_checkable_item("Cancelled", checked=True)
        self._apply_table_filters()

    # Session & Database Helpers (matching Dola)
    def _refresh_history(self):
        self.history_list.clear()
        try:
            sessions = self.db.list_sessions(limit=50)
            for s in sessions:
                lbl = f"ID: {s['id']} | {s['name']} (Completed: {s['completed_count']}, Failed: {s['failed_count']})"
                item = QListWidgetItem(lbl)
                item.setData(Qt.ItemDataRole.UserRole, s['id'])
                self.history_list.addItem(item)
        except Exception as e:
            logger.error(f"Failed to refresh sessions: {e}")

    def _load_selected_session(self):
        curr = self.history_list.currentItem()
        if not curr:
            return
        session_id = curr.data(Qt.ItemDataRole.UserRole)
        self.current_session_id = session_id
        try:
            self.jobs = self.db.load_session_jobs(session_id)
            self._refresh_table()
            self._update_stats()
            self.txt_log.appendPlainText(f"[Info] Loaded historic Session #{session_id} into workspace.")
        except Exception as e:
            logger.error(f"Failed to load session {session_id}: {e}")

    def _refresh_lifetime_history(self):
        limit = int(self.combo_lifetime_limit.currentText())
        date_f = self.combo_lifetime_date.currentText()
        status_f = self.combo_lifetime_filter.currentText()
        search_t = self.edit_lifetime_search.text().strip()

        try:
            rows = self.db.get_all_jobs_with_filters(
                status_filter=status_f,
                search_text=search_t,
                date_filter=date_f,
                limit_val=limit
            )

            self.table_lifetime.setRowCount(len(rows))
            for idx, row in enumerate(rows):
                self.table_lifetime.setItem(idx, 0, QTableWidgetItem(str(row['id'])))
                self.table_lifetime.setItem(idx, 1, QTableWidgetItem(row['session_name']))
                self.table_lifetime.setItem(idx, 2, QTableWidgetItem(str(row['job_index'])))
                self.table_lifetime.setItem(idx, 3, QTableWidgetItem(row['video_title'] or ""))
                self.table_lifetime.setItem(idx, 4, QTableWidgetItem(row['caption'] or ""))
                self.table_lifetime.setItem(idx, 5, QTableWidgetItem(row['prompt']))
                self.table_lifetime.setItem(idx, 6, QTableWidgetItem(row['status']))
                self.table_lifetime.setItem(idx, 7, QTableWidgetItem(row['download_path'] or ""))
        except Exception as e:
            logger.error(f"Failed to refresh lifetime history: {e}")

    def _export_lifetime_csv(self):
        import csv
        path, _ = QFileDialog.getSaveFileName(self, "Export History CSV", "", "CSV Files (*.csv)")
        if not path:
            return
        try:
            limit = int(self.combo_lifetime_limit.currentText())
            date_f = self.combo_lifetime_date.currentText()
            status_f = self.combo_lifetime_filter.currentText()
            search_t = self.edit_lifetime_search.text().strip()
            
            rows = self.db.get_all_jobs_with_filters(
                status_filter=status_f,
                search_text=search_t,
                date_filter=date_f,
                limit_val=limit
            )
            with open(path, 'w', newline='', encoding='utf-8') as f:
                writer = csv.writer(f)
                writer.writerow(["DB ID", "Session Name", "Job Index", "Product Title", "Suffix", "Prompt", "Status", "Finished At", "Download Path"])
                for r in rows:
                    writer.writerow([
                        r['id'], r['session_name'], r['job_index'], r['video_title'],
                        r['caption'], r['prompt'], r['status'], r['finished_at'], r['download_path']
                    ])
            QMessageBox.information(self, "Success", "Lifetime history exported successfully!")
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to export CSV: {e}")

    def _on_lifetime_table_context_menu(self, pos):
        menu = QMenu(self)
        relaunch_action = QAction("Relaunch Selected Historic Job", self)
        batch_relaunch_action = QAction("Launch Historic Jobs as new Batch", self)
        delete_action = QAction("Delete from DB", self)

        relaunch_action.triggered.connect(self._relaunch_historic_job)
        batch_relaunch_action.triggered.connect(self._launch_historic_jobs_as_new_batch)
        delete_action.triggered.connect(self._delete_historic_jobs)

        menu.addAction(relaunch_action)
        menu.addAction(batch_relaunch_action)
        menu.addSeparator()
        menu.addAction(delete_action)
        menu.exec(self.table_lifetime.viewport().mapToGlobal(pos))

    def _relaunch_historic_job(self):
        curr_row = self.table_lifetime.currentRow()
        if curr_row < 0:
            return
        db_job_id = int(self.table_lifetime.item(curr_row, 0).text())
        try:
            jobs = self.db.get_jobs_by_ids([db_job_id])
            if jobs:
                job = jobs[0]
                self._relaunch_job_manual(job.index)
        except Exception as e:
            logger.error(f"Failed to relaunch historic job: {e}")

    def _launch_historic_jobs_as_new_batch(self):
        selected_indexes = self.table_lifetime.selectedIndexes()
        rows = sorted(list(set(idx.row() for idx in selected_indexes)))
        if not rows:
            return
        
        job_ids = []
        for r in rows:
            db_job_id = int(self.table_lifetime.item(r, 0).text())
            job_ids.append(db_job_id)
            
        try:
            historic_jobs = self.db.get_jobs_by_ids(job_ids)
            self.jobs.clear()
            for idx, j in enumerate(historic_jobs):
                new_job = PromptJob(
                    index=idx + 1,
                    prompt=j.prompt,
                    video_title=j.video_title,
                    caption=j.caption or "mockup",
                    status=JobStatus.PENDING
                )
                self.jobs.append(new_job)
            self._refresh_table()
            self._update_stats()
            self.right_tabs.setCurrentIndex(0) # Switch to Current Jobs tab
            self.txt_log.appendPlainText(f"[Info] Loaded {len(historic_jobs)} historic jobs as a new batch.")
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to load batch: {e}")

    def _delete_historic_jobs(self):
        selected_indexes = self.table_lifetime.selectedIndexes()
        rows = sorted(list(set(idx.row() for idx in selected_indexes)), reverse=True)
        if not rows:
            return
            
        confirm = QMessageBox.question(
            self, "Confirm Delete",
            f"Are you sure you want to delete {len(rows)} selected entries from the database?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        if confirm == QMessageBox.StandardButton.Yes:
            try:
                conn = self.db._connect()
                try:
                    cur = conn.cursor()
                    for r in rows:
                        db_job_id = int(self.table_lifetime.item(r, 0).text())
                        cur.execute("DELETE FROM jobs WHERE id = ?", (db_job_id,))
                    conn.commit()
                finally:
                    conn.close()
                self._refresh_lifetime_history()
            except Exception as e:
                QMessageBox.critical(self, "Error", f"Failed to delete records: {e}")

    def _toggle_lifetime_select_all(self):
        self.table_lifetime.selectAll()

    def _download_lifetime_selected_jobs(self):
        QMessageBox.information(self, "Info", "Historic downloads are already located in their respective directories.")

    def _retry_lifetime_all_failed_jobs(self):
        selected_indexes = self.table_lifetime.selectedIndexes()
        rows = sorted(list(set(idx.row() for idx in selected_indexes)))
        if not rows:
            return
            
        job_ids = []
        for r in rows:
            db_job_id = int(self.table_lifetime.item(r, 0).text())
            job_ids.append(db_job_id)
            
        try:
            historic_jobs = self.db.get_jobs_by_ids(job_ids)
            failed_historic = [j for j in historic_jobs if j.status == JobStatus.FAILED]
            if not failed_historic:
                QMessageBox.information(self, "Info", "No failed jobs found in selection.")
                return
                
            self.jobs.clear()
            for idx, j in enumerate(failed_historic):
                new_job = PromptJob(
                    index=idx + 1,
                    prompt=j.prompt,
                    video_title=j.video_title,
                    caption=j.caption or "mockup",
                    status=JobStatus.PENDING
                )
                self.jobs.append(new_job)
            self._refresh_table()
            self._update_stats()
            self.right_tabs.setCurrentIndex(0) # Switch to Current Jobs tab
            self._start_batch()
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to retry historic jobs: {e}")

import sys
import os
import time
import json
import csv
import datetime
import urllib.parse
import webbrowser
import threading
import copy
from pathlib import Path
from typing import List, Tuple, Optional

from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QPushButton,
    QFrame, QProgressBar, QTableWidget, QTableWidgetItem, QCheckBox, QSpinBox, QComboBox,
    QFileDialog, QMessageBox, QTabWidget, QSplitter, QListWidget, QListWidgetItem,
    QLineEdit, QPlainTextEdit, QGroupBox, QAbstractItemView, QHeaderView, QMenu, QDialog,
    QApplication, QSystemTrayIcon, QButtonGroup, QStackedWidget
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal, pyqtSlot, QTimer, QTime, QElapsedTimer, QPoint, QEvent
from PyQt6.QtGui import QColor, QCursor, QAction, QKeySequence, QShortcut, QIcon

# Import our automation modules
from dola_automation.models import AutomationSettings, PromptJob, JobStatus, parse_prompts, align_reference_images
from dola_automation.database import HistoryDatabase
from dola_automation.browser_worker import DolaBrowserWorker, DolaAutomationError
from dola_automation.ffmpeg_utils import process_video_watermark, concatenate_videos, ConverterWorker, MergerWorker, get_video_duration
from dola_automation.styles import APP_STYLE, STATUS_COLORS, GradientLabel
from dola_automation.info_dialogs import InstructionsDialog, IssuesDialog, SupportDialog, ThreadsWarningDialog, WatermarkHelpDialog, MergerHelpDialog
from dola_automation.logger import logger
from dola_automation.telemetry import TelemetryTracker

class BatchRunner(QThread):
    job_progress = pyqtSignal(int, str)  # job_index, message
    chat_created = pyqtSignal(int, str)  # job_index, chat_url
    job_finished = pyqtSignal(int, bool, str, str)  # job_index, success, download_path, error
    batch_finished = pyqtSignal()

    def __init__(self, jobs: List[PromptJob], settings: AutomationSettings, db: HistoryDatabase, session_id: int, mode: str = "full"):
        super().__init__()
        self.jobs = jobs
        self.settings = settings
        self.db = db
        self.session_id = session_id
        self.mode = mode  # "full", "submit_only", "download_only"
        self._stop = False
        self._paused = False
        self.telemetry = TelemetryTracker(enabled=True)
        self.active_workers = {}
        self.lock = threading.Lock()

    def stop(self) -> None:
        self._stop = True
        with self.lock:
            for w in list(self.active_workers.values()):
                try:
                    w.cancel()
                except Exception:
                    pass

    def pause_resume(self) -> bool:
        self._paused = not self._paused
        return self._paused

    def run(self):
        logger.info(f"BatchRunner starting in '{self.mode}' mode with {len(self.jobs)} jobs. Concurrency limit = {self.settings.thread_count}.")
        
        # Filter jobs depending on mode
        runnable_jobs = []
        for job in self.jobs:
            if self.mode == "submit_only" and job.status == JobStatus.PENDING:
                runnable_jobs.append(job)
            elif self.mode == "download_only" and job.chat_url and job.status in [JobStatus.PENDING, JobStatus.RUNNING, JobStatus.WAITING, JobStatus.DOWNLOADING, JobStatus.SUBMITTED]:
                runnable_jobs.append(job)
            elif self.mode == "full" and job.status in [JobStatus.PENDING, JobStatus.RUNNING, JobStatus.WAITING, JobStatus.DOWNLOADING, JobStatus.SUBMITTED]:
                runnable_jobs.append(job)

        self.job_progress.emit(-1, f"BatchRunner started. Concurrency: {self.settings.thread_count}. Runnable jobs: {len(runnable_jobs)}")
        
        queue = list(runnable_jobs)
        running_threads = []
        
        def thread_target(job_obj):
            try:
                self.run_job_wrapper(job_obj)
            except Exception as e:
                logger.error(f"Error in thread execution for job #{job_obj.index}: {e}", exc_info=True)
                self.job_finished.emit(job_obj.index, False, "", str(e))
        
        while (queue or running_threads) and not self._stop:
            # Clean up finished threads
            running_threads = [t for t in running_threads if t.is_alive()]
            
            # Handle pause
            while self._paused and not self._stop:
                time.sleep(0.5)
                # Keep active threads monitored during pause
                running_threads = [t for t in running_threads if t.is_alive()]
                
            if self._stop:
                break
                
            # Spawn next job if slots available
            if len(running_threads) < self.settings.thread_count and queue:
                job_to_run = queue.pop(0)
                t = threading.Thread(target=thread_target, args=(job_to_run,), name=f"GSWorker-{job_to_run.index}")
                running_threads.append(t)
                t.start()
                
            time.sleep(0.2)
            
        # Wait for all running threads to finish
        for t in running_threads:
            t.join()
            
        self.batch_finished.emit()

    def run_job_wrapper(self, job: PromptJob):
        if self._stop:
            self.job_progress.emit(job.index, "Cancelled")
            return
            
        while self._paused:
            if self._stop:
                self.job_progress.emit(job.index, "Cancelled")
                return
            time.sleep(0.5)
            
        # Safe thread settings isolation
        thread_settings = copy.deepcopy(self.settings)
        worker_mode = "download_only" if self.mode == "download_only" else "full"
        if self.mode == "submit_only":
            thread_settings.submit_and_close = True
            
        # Telemetry reporting
        telemetry_id = None
        try:
            telemetry_id = self.telemetry.report_job_started(
                chrome_profile="SharedProfile",
                prompt=job.prompt
            )
        except Exception as e:
            logger.warning(f"Telemetry failed to start: {e}")

        # Update DB state and in-memory object starting time
        import datetime
        job.started_at = datetime.datetime.utcnow().isoformat()
        self.db.update_job(job.job_id, status=JobStatus.RUNNING, mark_started=True)
        self.job_progress.emit(job.index, f"Starting job #{job.index}...")
        
        success = False
        error_msg = ""
        retries = 0
        max_retries = 3
        
        while retries < max_retries and not self._stop:
            # Re-create worker inside retry loop to get fresh context
            worker = DolaBrowserWorker(
                settings=thread_settings,
                on_progress=lambda p_val, p_msg: self.job_progress.emit(job.index, p_msg),
                on_chat_created=lambda j_obj, u_str: self._handle_chat_created(job, u_str)
            )
            
            with self.lock:
                if self._stop:
                    break
                self.active_workers[job.index] = worker
                
            try:
                success = worker.run_job(job, mode=worker_mode)
                if success:
                    break
                else:
                    error_msg = job.error or "Execution failed"
            except Exception as e:
                error_msg = str(e)
                logger.error(f"Error executing job #{job.index} (attempt {retries+1}): {e}", exc_info=True)
                
            retries += 1
            if retries < max_retries and not self._stop:
                self.job_progress.emit(job.index, f"Attempt {retries} failed: {error_msg}. Retrying in 5 seconds (Attempt {retries+1}/{max_retries})...")
                # Wait 5s, checking stop/pause state frequently
                for _ in range(10):
                    if self._stop:
                        break
                    time.sleep(0.5)
                    
        # Cleanup worker registration
        with self.lock:
            self.active_workers.pop(job.index, None)
            
        # Update final status
        final_status = JobStatus.FAILED
        telemetry_status = "Failed"

        if success:
            if self.mode == "submit_only":
                final_status = JobStatus.SUBMITTED
                telemetry_status = "Submitted"
            else:
                final_status = JobStatus.COMPLETED
                telemetry_status = "Completed"
        else:
            if "not yet available" in error_msg.lower() or "not yet available" in str(job.error).lower():
                final_status = JobStatus.SUBMITTED
                telemetry_status = "Submitted"
                job.error = error_msg or "The video is not yet available."
            elif "not found" in error_msg.lower():
                final_status = JobStatus.NOT_FOUND
                telemetry_status = "Not Found"
            elif self._stop:
                final_status = JobStatus.CANCELLED
                telemetry_status = "Cancelled"
            else:
                final_status = JobStatus.FAILED
                telemetry_status = "Failed"
                job.error = error_msg or "Unknown execution error"

        job.status = final_status
        self.db.update_job(
            job.job_id, 
            status=final_status, 
            download_path=Path(job.download_path) if job.download_path else None,
            error=job.error,
            mark_finished=True
        )
        
        if final_status == JobStatus.COMPLETED and job.download_path:
            self.db.record_download(job.job_id, Path(job.download_path))
            self.db.bump_session_counts(self.session_id, completed=1)
        elif final_status in [JobStatus.FAILED, JobStatus.NOT_FOUND]:
            self.db.bump_session_counts(self.session_id, failed=1)

        # Update Telemetry
        if telemetry_id:
            try:
                self.telemetry.report_job_finished(telemetry_id, telemetry_status)
            except Exception as e:
                logger.warning(f"Telemetry finish report failed: {e}")

        self.job_finished.emit(job.index, success, job.download_path or "", job.error or "")

    def _handle_chat_created(self, job: PromptJob, chat_url: str):
        job.chat_url = chat_url
        self.db.update_job(job.job_id, chat_url=chat_url)
        self.chat_created.emit(job.index, chat_url)

class AutoDownloadDialog(QDialog):
    def __init__(self, delay_minutes: int, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Batch Submitted")
        self.setFixedSize(360, 220)
        self.remaining_seconds = delay_minutes * 60
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self._build_ui()
        self.timer.start(1000)

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(15)
        layout.setContentsMargins(20, 20, 20, 20)

        lbl_title = QLabel("All prompts submitted successfully.", self)
        lbl_title.setStyleSheet("font-size: 14px; font-weight: bold; color: #ffffff;")
        lbl_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(lbl_title)

        self.lbl_info = QLabel("Waiting to start auto-download...", self)
        self.lbl_info.setStyleSheet("color: rgba(255,255,255,0.7);")
        self.lbl_info.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.lbl_info)

        self.lbl_timer = QLabel(self._format_time(self.remaining_seconds), self)
        self.lbl_timer.setObjectName("timer_label")
        self.lbl_timer.setStyleSheet("font-size: 32px; font-weight: 800; color: #2ecc71; padding: 10px;")
        self.lbl_timer.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.lbl_timer)

        btn_layout = QHBoxLayout()
        self.btn_cancel = QPushButton("Cancel Download", self)
        self.btn_cancel.clicked.connect(self.reject)
        
        self.btn_instant = QPushButton("Instant Download", self)
        self.btn_instant.setObjectName("primary")
        self.btn_instant.clicked.connect(self.accept)

        btn_layout.addWidget(self.btn_cancel)
        btn_layout.addWidget(self.btn_instant)
        layout.addLayout(btn_layout)

    def _format_time(self, total_seconds: int) -> str:
        mins = total_seconds // 60
        secs = total_seconds % 60
        return f"{mins:02d}:{secs:02d}"

    def _tick(self):
        self.remaining_seconds -= 1
        if self.remaining_seconds <= 0:
            self.timer.stop()
            self.accept()
        else:
            self.lbl_timer.setText(self._format_time(self.remaining_seconds))

def get_resource_path(relative_path: str) -> Path:
    base_path = Path(__file__).parent.resolve()
    if hasattr(sys, '_MEIPASS'):
        base_path = Path(sys._MEIPASS) / 'dola_automation'
    return base_path / relative_path

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        
        # Load active license details
        from dola_automation.licensing import check_license_stored
        is_valid, lic_data = check_license_stored()
        if is_valid:
            email = lic_data.get('email', 'N/A')
            plan = lic_data.get('plan', 'N/A')
            days_left = lic_data.get('days_left', 0)
            self.setWindowTitle(f"GrowSnap Creative Suite — User: {email} | Plan: {plan} ({days_left} days left)")
        else:
            self.setWindowTitle("GrowSnap Creative Suite")
            
        self.resize(1300, 850)
        
        # Set window icon
        icon_path = get_resource_path("resources/icon.png")
        if icon_path.exists():
            self.setWindowIcon(QIcon(str(icon_path)))
        self.setStyleSheet(APP_STYLE)

        # Setup paths & storage
        self.download_dir = Path.home() / 'Documents' / 'dola_downloads'
        self.db_path = Path.home() / 'Documents' / 'dola_video_automation' / 'history.db'
        self.db = HistoryDatabase(self.db_path)
        self.backup_path = Path.home() / 'Documents' / 'dola_video_automation' / 'grow_snap_backup.json'
        
        self.jobs: List[PromptJob] = []
        self.reference_paths: List[Path] = []
        self.current_session_id: Optional[int] = None
        self.runner: Optional[BatchRunner] = None
        self.settings = AutomationSettings()
        
        # Concurrency Warning States
        self._threads_warning_confirmed = False
        self._is_loading_backup = False
        self._showing_warning_dialog = False
        
        # System tray icon initialization
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray_icon = QSystemTrayIcon(self)
            self.tray_icon.setIcon(self.windowIcon() if not self.windowIcon().isNull() else QIcon())
            self.tray_icon.show()
        else:
            self.tray_icon = None
            
        # Batch elapsed timer
        self.batch_timer = QTimer(self)
        self.batch_timer.timeout.connect(self._update_batch_timer)
        self.batch_start_time = QElapsedTimer()

        self._build_ui()
        self.table.itemChanged.connect(self._on_table_item_changed)
        
        # Ctrl+C Copy Shortcut for Table
        self.table_copy_shortcut = QShortcut(QKeySequence("Ctrl+C"), self.table)
        self.table_copy_shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self.table_copy_shortcut.activated.connect(self._copy_table_selection)
        
        # F5 Refresh Shortcut
        self.shortcut_refresh = QShortcut(QKeySequence("F5"), self)
        self.shortcut_refresh.activated.connect(self._refresh_application)
        self._load_json_backup()
        self._enforce_license_limits()
        self._refresh_history()
        self._refresh_lifetime_history()
        self._update_stats()

    def _build_ui(self):
        central_widget = QWidget(self)
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)
        main_layout.setContentsMargins(15, 15, 15, 15)
        main_layout.setSpacing(15)

        # 1. Header Row
        header_layout = QHBoxLayout()
        title_box = QVBoxLayout()
        title_lbl = GradientLabel("GrowSnap Creative Suite", self)
        title_lbl.setObjectName("title")
        title_box.addWidget(title_lbl)
        header_layout.addLayout(title_box)

        header_layout.addStretch()
        
        # Show active license details in the header
        from dola_automation.licensing import check_license_stored
        is_valid, lic_data = check_license_stored()
        email = lic_data.get('email', 'N/A')
        plan = lic_data.get('plan', 'N/A')
        days_left = lic_data.get('days_left', 0)
        
        if is_valid:
            lic_lbl = QLabel(f"ACTIVE: {email} | {plan} ({days_left} Days Left)", self)
        else:
            lic_lbl = QLabel("UNACTIVATED / TRIAL", self)
        lic_lbl.setStyleSheet("color: #2ecc71; font-weight: bold; background: rgba(46, 204, 113, 0.08); border: 1px solid rgba(46, 204, 113, 0.22); border-radius: 6px; padding: 5px 12px; font-size: 11px;")
        header_layout.addWidget(lic_lbl)
        
        version_lbl = QLabel("V1.0 PREMIUM", self)
        version_lbl.setObjectName("version_badge")
        header_layout.addWidget(version_lbl)

        btn_update_check = QPushButton("Check Updates", self)
        btn_update_check.clicked.connect(self._manual_update_check)
        header_layout.addWidget(btn_update_check)

        btn_refresh = QPushButton("Refresh (F5)", self)
        btn_refresh.clicked.connect(self._refresh_application)
        header_layout.addWidget(btn_refresh)

        main_layout.addLayout(header_layout)

        # 1.5 Master Navigation Bar
        nav_layout = QHBoxLayout()
        nav_layout.setSpacing(10)
        nav_layout.setContentsMargins(0, 0, 0, 5)
        
        self.btn_nav_dola = QPushButton("Dola Video Automation", self)
        self.btn_nav_dola.setCheckable(True)
        self.btn_nav_dola.setChecked(True)
        self.btn_nav_dola.setObjectName("nav_button")
        
        self.btn_nav_converter = QPushButton("Watermark Removal", self)
        self.btn_nav_converter.setCheckable(True)
        self.btn_nav_converter.setObjectName("nav_button")
        
        self.btn_nav_merger = QPushButton("Video Merger", self)
        self.btn_nav_merger.setCheckable(True)
        self.btn_nav_merger.setObjectName("nav_button")
        
        self.nav_group = QButtonGroup(self)
        self.nav_group.setExclusive(True)
        self.nav_group.addButton(self.btn_nav_dola, 0)
        self.nav_group.addButton(self.btn_nav_converter, 1)
        self.nav_group.addButton(self.btn_nav_merger, 2)
        self.nav_group.idClicked.connect(self._on_nav_changed)
        
        nav_layout.addWidget(self.btn_nav_dola)
        nav_layout.addWidget(self.btn_nav_converter)
        nav_layout.addWidget(self.btn_nav_merger)
        nav_layout.addStretch()
        
        main_layout.addLayout(nav_layout)

        # 1.6 Stacked Widget Page Setup
        self.stacked_widget = QStackedWidget(self)
        main_layout.addWidget(self.stacked_widget)

        # ─── PAGE 1: DOLA VIDEO AUTOMATION ───────────────────
        self.page_dola = QWidget(central_widget)
        page_dola_layout = QVBoxLayout(self.page_dola)
        page_dola_layout.setContentsMargins(0, 0, 0, 0)
        page_dola_layout.setSpacing(15)

        lbl_dola_subtitle = QLabel("DOLA VIDEO AUTOMATION — powered by SeaDance 2.0", self)
        lbl_dola_subtitle.setObjectName("subtitle")
        page_dola_layout.addWidget(lbl_dola_subtitle)

        # 2. Stats Dashboard Bar
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

        page_dola_layout.addLayout(stats_row)

        # 3. Main Splitter View
        splitter = QSplitter(Qt.Orientation.Horizontal, self)
        
        # Left Panel (Inputs & Controls)
        left = QWidget(self)
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(15)

        # Import & Parsing card
        import_group = QGroupBox("PROMPT INGESTION", self)
        import_layout = QVBoxLayout(import_group)
        self.prompt_editor = QPlainTextEdit(self)
        self.prompt_editor.setPlaceholderText("Paste prompts here or load from a custom CSV / TXT file...")
        self.prompt_editor.textChanged.connect(self._save_json_backup)
        import_layout.addWidget(self.prompt_editor)

        path_row = QHBoxLayout()
        self.edit_file_path = QLineEdit(self)
        self.edit_file_path.setPlaceholderText("Paste CSV/TXT file path here...")
        self.btn_load_path = QPushButton("Load Path", self)
        self.btn_load_path.clicked.connect(self._load_prompt_from_path)
        path_row.addWidget(self.edit_file_path)
        path_row.addWidget(self.btn_load_path)
        import_layout.addLayout(path_row)

        btn_row = QHBoxLayout()
        self.btn_load_file = QPushButton("Load CSV/TXT", self)
        self.btn_load_file.clicked.connect(self._load_prompt_file)
        self.btn_parse = QPushButton("Parse prompts", self)
        self.btn_parse.clicked.connect(self._parse_prompts)
        btn_row.addWidget(self.btn_load_file)
        btn_row.addWidget(self.btn_parse)
        import_layout.addLayout(btn_row)
        left_layout.addWidget(import_group)

        # Reference image picker card
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

        # Automation config card (Moved to Right Tab)
        settings_group = QGroupBox("AUTOMATION SETTINGS", self)
        settings_grid = QGridLayout(settings_group)
        settings_grid.setSpacing(10)
        settings_grid.setColumnStretch(0, 1)
        settings_grid.setColumnStretch(1, 2)
        settings_grid.setColumnStretch(2, 1)
        settings_grid.setColumnStretch(3, 2)

        self.chk_one_browser = QCheckBox("New browser per video", self)
        self.chk_one_browser.setChecked(True)
        self.chk_one_browser.stateChanged.connect(self._update_runner_settings)
        settings_grid.addWidget(self.chk_one_browser, 0, 0, 1, 2)

        self.chk_headless = QCheckBox("Headless mode", self)
        self.chk_headless.stateChanged.connect(self._update_runner_settings)
        settings_grid.addWidget(self.chk_headless, 0, 2, 1, 2)

        self.chk_submit_and_close = QCheckBox("Submit && Close", self)
        self.chk_submit_and_close.stateChanged.connect(self._update_runner_settings)
        settings_grid.addWidget(self.chk_submit_and_close, 1, 0, 1, 2)

        self.chk_inject_ui = QCheckBox("Inject Chrome UI", self)
        self.chk_inject_ui.setChecked(True)
        self.chk_inject_ui.stateChanged.connect(self._update_runner_settings)
        settings_grid.addWidget(self.chk_inject_ui, 1, 2, 1, 2)

        self.chk_auto_remove_watermark = QCheckBox("Auto Remove Watermark", self)
        self.chk_auto_remove_watermark.setChecked(True)
        self.chk_auto_remove_watermark.stateChanged.connect(self._update_runner_settings)
        settings_grid.addWidget(self.chk_auto_remove_watermark, 2, 0, 1, 4)

        settings_grid.addWidget(QLabel("Threads", self), 3, 0)
        self.spin_threads = QSpinBox(self)
        self.spin_threads.setRange(1, 16)
        self.spin_threads.setValue(1)
        self.spin_threads.valueChanged.connect(self._update_runner_settings)
        settings_grid.addWidget(self.spin_threads, 3, 1)

        settings_grid.addWidget(QLabel("Duration", self), 3, 2)
        self.combo_duration = QComboBox(self)
        self.combo_duration.addItems(["10s", "5s"])
        self.combo_duration.currentTextChanged.connect(self._update_runner_settings)
        settings_grid.addWidget(self.combo_duration, 3, 3)

        settings_grid.addWidget(QLabel("Ratio", self), 4, 0)
        self.combo_ratio = QComboBox(self)
        self.combo_ratio.addItems(["9:16", "16:9", "1:1", "3:4", "4:3", "21:9"])
        self.combo_ratio.currentTextChanged.connect(self._update_runner_settings)
        settings_grid.addWidget(self.combo_ratio, 4, 1)

        settings_grid.addWidget(QLabel("Launch Delay (s)", self), 4, 2)
        self.spin_launch_delay = QSpinBox(self)
        self.spin_launch_delay.setRange(0, 120)
        self.spin_launch_delay.setValue(5)
        self.spin_launch_delay.valueChanged.connect(self._update_runner_settings)
        settings_grid.addWidget(self.spin_launch_delay, 4, 3)

        settings_grid.addWidget(QLabel("Paste Delay (s)", self), 5, 0)
        self.spin_paste_delay = QSpinBox(self)
        self.spin_paste_delay.setRange(0, 60)
        self.spin_paste_delay.setValue(2)
        self.spin_paste_delay.valueChanged.connect(self._update_runner_settings)
        settings_grid.addWidget(self.spin_paste_delay, 5, 1)

        settings_grid.addWidget(QLabel("Timeout (s)", self), 5, 2)
        self.spin_timeout = QSpinBox(self)
        self.spin_timeout.setRange(0, 1200)
        self.spin_timeout.setValue(500)
        self.spin_timeout.setToolTip("Set to 0 for infinite wait until video generates.")
        self.spin_timeout.valueChanged.connect(self._update_runner_settings)
        settings_grid.addWidget(self.spin_timeout, 5, 3)

        settings_grid.addWidget(QLabel("Wait (s)", self), 6, 0)
        self.spin_submit_delay = QSpinBox(self)
        self.spin_submit_delay.setRange(0, 300)
        self.spin_submit_delay.setValue(15)
        self.spin_submit_delay.valueChanged.connect(self._update_runner_settings)
        settings_grid.addWidget(self.spin_submit_delay, 6, 1)

        settings_grid.addWidget(QLabel("Auto DL Delay", self), 6, 2)
        self.spin_auto_download_delay = QSpinBox(self)
        self.spin_auto_download_delay.setRange(1, 60)
        self.spin_auto_download_delay.setValue(5)
        self.spin_auto_download_delay.setSuffix(" min")
        self.spin_auto_download_delay.valueChanged.connect(self._update_runner_settings)
        settings_grid.addWidget(self.spin_auto_download_delay, 6, 3)

        settings_grid.addWidget(QLabel("Watermark Method", self), 7, 0)
        self.combo_watermark_method = QComboBox(self)
        self.combo_watermark_method.addItems(["Blur", "Crop"])
        self.combo_watermark_method.currentTextChanged.connect(self._update_runner_settings)
        settings_grid.addWidget(self.combo_watermark_method, 7, 1)

        settings_grid.addWidget(QLabel("Model", self), 7, 2)
        self.combo_model = QComboBox(self)
        self.combo_model.addItems(["SeaDance 2.0 Fast", "SeaDance 2.0 Quality", "SeaDance 2.5 Quality", "SeaDance 2.5 Fast"])
        self.combo_model.currentTextChanged.connect(self._update_runner_settings)
        settings_grid.addWidget(self.combo_model, 7, 3)

        settings_grid.addWidget(QLabel("Download Folder", self), 8, 0)
        self.btn_download_dir = QPushButton("Choose", self)
        self.btn_download_dir.clicked.connect(self._pick_download_dir)
        settings_grid.addWidget(self.btn_download_dir, 8, 1)
        self.lbl_download_dir_show = QLabel(str(self.download_dir.name), self)
        self.lbl_download_dir_show.setWordWrap(True)
        settings_grid.addWidget(self.lbl_download_dir_show, 8, 2, 1, 2)

        settings_grid.addWidget(QLabel("Success Phrase", self), 9, 0)
        self.edit_success_phrase = QLineEdit(self)
        self.edit_success_phrase.setPlaceholderText("Enter success confirmation phrase...")
        self.edit_success_phrase.textChanged.connect(self._update_runner_settings)
        settings_grid.addWidget(self.edit_success_phrase, 9, 1, 1, 3)

        # Operational buttons
        run_row = QHBoxLayout()
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
        run_row.addWidget(self.btn_start)
        run_row.addWidget(self.btn_pause)
        run_row.addWidget(self.btn_stop)
        left_layout.addLayout(run_row)

        # Help dialog buttons
        help_row = QHBoxLayout()
        self.btn_instructions = QPushButton("Instructions", self)
        self.btn_instructions.clicked.connect(self._show_instructions_dialog)
        self.btn_issues = QPushButton("Issues/Fixes", self)
        self.btn_issues.clicked.connect(self._show_issues_dialog)
        self.btn_upgrade = QPushButton("Upgrade your plan", self)
        self.btn_upgrade.setObjectName("primary")
        self.btn_upgrade.clicked.connect(self._open_premium_whatsapp)
        help_row.addWidget(self.btn_instructions)
        help_row.addWidget(self.btn_issues)
        help_row.addWidget(self.btn_upgrade)
        left_layout.addLayout(help_row)

        splitter.addWidget(left)

        # Right Panel (Tab Widgets for logs, lists, history - standalone sub-tabs)
        right = QTabWidget(self)

        # Tab 1: Current Batch
        tab_current = QWidget(self)
        tab_current_layout = QVBoxLayout(tab_current)
        
        self.table = QTableWidget(self)
        self.table.setColumnCount(9)
        headers = ["Index", "Video Title", "Scene Index", "Prompt", "Reference", "Status", "Download Path", "Error Details", "Action"]
        self.table.setHorizontalHeaderLabels(headers)
        header_tooltips = {
            "Index": "The sequential index of the prompt scene in the batch",
            "Video Title": "Title grouping for the scene. Scenes with the exact same title are merged.",
            "Scene Index": "The sequence index of the scene in the video group",
            "Prompt": "The AI prompt used to generate this video scene",
            "Reference": "Optional reference image path for generation style",
            "Status": "Current processing state of this video scene",
            "Download Path": "Path to the downloaded raw 10-second video segment",
            "Error Details": "Details on why video generation or download failed",
            "Action": "Actions available for this specific row"
        }
        for col in range(len(headers)):
            item = self.table.horizontalHeaderItem(col)
            if item:
                item.setToolTip(header_tooltips.get(headers[col], ""))
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.table.setColumnWidth(0, 50)
        self.table.setColumnWidth(1, 130)
        self.table.setColumnWidth(2, 80)
        self.table.setColumnWidth(3, 260)
        self.table.setColumnWidth(4, 100)
        self.table.setColumnWidth(5, 90)
        self.table.setColumnWidth(6, 140)
        self.table.setColumnWidth(7, 180)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._on_table_context_menu)
        
        tab_current_layout.addWidget(self.table)

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
        tab_current_layout.addLayout(action_bar)
        
        right.addTab(tab_current, "Current Jobs")

        # Tab 2: Session History & Console logs
        tab_history = QWidget(self)
        tab_history_layout = QHBoxLayout(tab_history)
        
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
        
        tab_history_layout.addWidget(history_group, 1)

        log_group = QGroupBox("CONSOLE LOGS", self)
        log_layout = QVBoxLayout(log_group)
        self.log_view = QPlainTextEdit(self)
        self.log_view.setReadOnly(True)
        log_layout.addWidget(self.log_view)
        
        tab_history_layout.addWidget(log_group, 2)
        
        right.addTab(tab_history, "History & Logs")

        # Tab 3: Automation Settings (Relocated)
        tab_settings_tab = QWidget(self)
        tab_settings_layout = QVBoxLayout(tab_settings_tab)
        tab_settings_layout.addWidget(settings_group)
        tab_settings_layout.addStretch()
        right.addTab(tab_settings_tab, "Settings")

        # Tab 4: All-time jobs (Lifetime)
        tab_lifetime = QWidget(self)
        tab_lifetime_layout = QVBoxLayout(tab_lifetime)

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
        self.table_lifetime.setColumnCount(10)
        lifetime_headers = [
            "DB ID", "Session", "Index", "Video Title", "Scene Index", "Prompt", "Status", "Finished At", "Download Path", "Error Details"
        ]
        self.table_lifetime.setHorizontalHeaderLabels(lifetime_headers)
        lifetime_tooltips = {
            "DB ID": "Internal SQLite database identifier for this job entry",
            "Session": "Name of the batch execution run session",
            "Index": "The sequential index of the prompt scene in the batch",
            "Video Title": "Title grouping for the scene. Scenes with the exact same title are merged.",
            "Scene Index": "The sequence index of the scene in the video group",
            "Prompt": "The AI prompt used to generate this video scene",
            "Status": "Current processing state of this video scene",
            "Finished At": "Date and time when processing for this scene finished",
            "Download Path": "Path to the downloaded raw 10-second video segment",
            "Error Details": "Details on why video generation or download failed"
        }
        for col in range(len(lifetime_headers)):
            item = self.table_lifetime.horizontalHeaderItem(col)
            if item:
                item.setToolTip(lifetime_tooltips.get(lifetime_headers[col], ""))
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

        right.addTab(tab_lifetime, "Lifetime History")

        splitter.addWidget(right)
        
        # Balance sizes (Left Panel gets smaller portion, Right Panel gets larger)
        splitter.setSizes([450, 850])
        page_dola_layout.addWidget(splitter)
        self.stacked_widget.addWidget(self.page_dola)

        # ─── PAGE 2: WATERMARK REMOVAL TOOL ──────────────────
        self.page_converter = QWidget(central_widget)
        page_conv_layout = QVBoxLayout(self.page_converter)
        page_conv_layout.setContentsMargins(0, 0, 0, 0)
        page_conv_layout.setSpacing(15)

        conv_header_layout = QHBoxLayout()
        lbl_conv_subtitle = QLabel("WATERMARK REMOVAL TOOL — Visually Lossless Watermark Blurring & Cropping", self)
        lbl_conv_subtitle.setObjectName("subtitle")
        self.btn_conv_help = QPushButton("Help / Instructions", self)
        self.btn_conv_help.setFixedWidth(160)
        self.btn_conv_help.clicked.connect(self._show_conv_help_dialog)
        conv_header_layout.addWidget(lbl_conv_subtitle)
        conv_header_layout.addStretch()
        conv_header_layout.addWidget(self.btn_conv_help)
        page_conv_layout.addLayout(conv_header_layout)

        tab_converter = QWidget(self)
        converter_layout = QVBoxLayout(tab_converter)
        converter_layout.setContentsMargins(0, 0, 0, 0)
        converter_layout.setSpacing(15)

        conv_settings_group = QGroupBox("CONVERSION SETTINGS", self)
        conv_grid = QGridLayout(conv_settings_group)
        conv_grid.setSpacing(10)

        self.btn_conv_input = QPushButton("Select Input File/Folder", self)
        self.btn_conv_input.clicked.connect(self._pick_conv_input)
        conv_grid.addWidget(self.btn_conv_input, 0, 0)
        self.lbl_conv_input = QLabel("No input selected", self)
        self.lbl_conv_input.setWordWrap(True)
        conv_grid.addWidget(self.lbl_conv_input, 0, 1, 1, 3)

        self.btn_conv_output = QPushButton("Select Output Folder", self)
        self.btn_conv_output.clicked.connect(self._pick_conv_output)
        conv_grid.addWidget(self.btn_conv_output, 1, 0)
        self.lbl_conv_output = QLabel("No output selected", self)
        self.lbl_conv_output.setWordWrap(True)
        conv_grid.addWidget(self.lbl_conv_output, 1, 1, 1, 3)

        conv_grid.addWidget(QLabel("Mode", self), 2, 0)
        self.combo_conv_mode = QComboBox(self)
        self.combo_conv_mode.addItems(["Folder Batch", "Single Video"])
        conv_grid.addWidget(self.combo_conv_mode, 2, 1)

        conv_grid.addWidget(QLabel("Method", self), 2, 2)
        self.combo_conv_method = QComboBox(self)
        self.combo_conv_method.addItems(["Blur", "Crop"])
        conv_grid.addWidget(self.combo_conv_method, 2, 3)

        conv_grid.addWidget(QLabel("Blur X:Y:W:H", self), 3, 0)
        blur_hlay = QHBoxLayout()
        self.spin_blur_x = QSpinBox(self)
        self.spin_blur_x.setRange(0, 4000)
        self.spin_blur_x.setValue(540)
        self.spin_blur_y = QSpinBox(self)
        self.spin_blur_y.setRange(0, 4000)
        self.spin_blur_y.setValue(1220)
        self.spin_blur_w = QSpinBox(self)
        self.spin_blur_w.setRange(0, 2000)
        self.spin_blur_w.setValue(170)
        self.spin_blur_h = QSpinBox(self)
        self.spin_blur_h.setRange(0, 2000)
        self.spin_blur_h.setValue(80)
        blur_hlay.addWidget(self.spin_blur_x)
        blur_hlay.addWidget(self.spin_blur_y)
        blur_hlay.addWidget(self.spin_blur_w)
        blur_hlay.addWidget(self.spin_blur_h)
        conv_grid.addLayout(blur_hlay, 3, 1, 1, 3)

        conv_grid.addWidget(QLabel("Crop Bottom Px", self), 4, 0)
        self.spin_crop_px = QSpinBox(self)
        self.spin_crop_px.setRange(0, 1000)
        self.spin_crop_px.setValue(80)
        conv_grid.addWidget(self.spin_crop_px, 4, 1)

        conv_grid.addWidget(QLabel("Threads", self), 4, 2)
        self.spin_conv_threads = QSpinBox(self)
        self.spin_conv_threads.setRange(1, 16)
        self.spin_conv_threads.setValue(4)
        conv_grid.addWidget(self.spin_conv_threads, 4, 3)

        converter_layout.addWidget(conv_settings_group)

        self.btn_conv_start = QPushButton("START PROCESSING", self)
        self.btn_conv_start.setObjectName("primary")
        self.btn_conv_start.clicked.connect(self._start_conversion)
        converter_layout.addWidget(self.btn_conv_start)

        self.conv_progress = QProgressBar(self)
        self.conv_progress.setValue(0)
        converter_layout.addWidget(self.conv_progress)

        self.conv_log = QPlainTextEdit(self)
        self.conv_log.setReadOnly(True)
        self.conv_log.setPlaceholderText("Console logs...")
        converter_layout.addWidget(self.conv_log)

        page_conv_layout.addWidget(tab_converter)
        self.stacked_widget.addWidget(self.page_converter)

        # ─── PAGE 3: VIDEO MERGER ────────────────────────────
        self.page_merger = QWidget(central_widget)
        page_merge_layout = QVBoxLayout(self.page_merger)
        page_merge_layout.setContentsMargins(0, 0, 0, 0)
        page_merge_layout.setSpacing(15)

        merge_header_layout = QHBoxLayout()
        lbl_merge_subtitle = QLabel("VIDEO MERGER — Concatenate video segments losslessly", self)
        lbl_merge_subtitle.setObjectName("subtitle")
        self.btn_merge_help = QPushButton("Help / Instructions", self)
        self.btn_merge_help.setFixedWidth(160)
        self.btn_merge_help.clicked.connect(self._show_merge_help_dialog)
        merge_header_layout.addWidget(lbl_merge_subtitle)
        merge_header_layout.addStretch()
        merge_header_layout.addWidget(self.btn_merge_help)
        page_merge_layout.addLayout(merge_header_layout)

        tab_merger = QWidget(self)
        merger_layout = QVBoxLayout(tab_merger)
        merger_layout.setContentsMargins(0, 0, 0, 0)
        merger_layout.setSpacing(15)
        
        merger_settings_group = QGroupBox("VIDEO MERGER (CONCAT)", self)
        merger_grid = QGridLayout(merger_settings_group)
        merger_grid.setSpacing(10)
        
        self.list_merge_files = QListWidget(self)
        merger_grid.addWidget(self.list_merge_files, 0, 0, 4, 3)
        
        v_btn_layout = QVBoxLayout()
        self.btn_merge_add = QPushButton("Add Videos", self)
        self.btn_merge_add.clicked.connect(self._add_merge_files)
        self.btn_merge_remove = QPushButton("Remove Selected", self)
        self.btn_merge_remove.clicked.connect(self._remove_merge_file)
        self.btn_merge_clear = QPushButton("Clear All", self)
        self.btn_merge_clear.clicked.connect(self._clear_merge_files)
        self.btn_merge_up = QPushButton("Move Up", self)
        self.btn_merge_up.clicked.connect(self._move_merge_file_up)
        self.btn_merge_down = QPushButton("Move Down", self)
        self.btn_merge_down.clicked.connect(self._move_merge_file_down)
        
        v_btn_layout.addWidget(self.btn_merge_add)
        v_btn_layout.addWidget(self.btn_merge_remove)
        v_btn_layout.addWidget(self.btn_merge_clear)
        v_btn_layout.addWidget(self.btn_merge_up)
        v_btn_layout.addWidget(self.btn_merge_down)
        v_btn_layout.addStretch()
        merger_grid.addLayout(v_btn_layout, 0, 3, 4, 1)
        
        self.btn_merge_output = QPushButton("Select Output File", self)
        self.btn_merge_output.clicked.connect(self._pick_merge_output)
        merger_grid.addWidget(self.btn_merge_output, 4, 0)
        self.lbl_merge_output = QLabel("No output selected", self)
        self.lbl_merge_output.setWordWrap(True)
        merger_grid.addWidget(self.lbl_merge_output, 4, 1, 1, 3)
        
        merger_layout.addWidget(merger_settings_group)
        
        self.btn_merge_start = QPushButton("START MERGING", self)
        self.btn_merge_start.setObjectName("primary")
        self.btn_merge_start.clicked.connect(self._start_merging)
        merger_layout.addWidget(self.btn_merge_start)
        
        self.merger_progress = QProgressBar(self)
        self.merger_progress.setValue(0)
        merger_layout.addWidget(self.merger_progress)
        
        self.merger_log = QPlainTextEdit(self)
        self.merger_log.setReadOnly(True)
        self.merger_log.setPlaceholderText("Merger console logs...")
        merger_layout.addWidget(self.merger_log)

        page_merge_layout.addWidget(tab_merger)
        self.stacked_widget.addWidget(self.page_merger)

    def _on_nav_changed(self, button_id):
        self.stacked_widget.setCurrentIndex(button_id)

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

    def _log(self, message: str):
        t_stamp = datetime.datetime.now().strftime("%H:%M:%S")
        self.log_view.appendPlainText(f"[{t_stamp}] {message}")
        logger.info(message)

    # settings management
    def _collect_settings(self) -> AutomationSettings:
        s = AutomationSettings()
        s.thread_count = self.spin_threads.value()
        s.one_browser_per_video = self.chk_one_browser.isChecked()
        s.headless = self.chk_headless.isChecked()
        s.submit_and_close = self.chk_submit_and_close.isChecked()
        s.submit_close_delay_sec = self.spin_submit_delay.value()
        s.inject_ui_downloader = self.chk_inject_ui.isChecked()
        s.model = self.combo_model.currentText()
        s.duration = self.combo_duration.currentText()
        s.ratio = self.combo_ratio.currentText()
        s.generation_timeout_sec = self.spin_timeout.value()
        s.launch_delay_sec = self.spin_launch_delay.value()
        s.paste_delay_sec = self.spin_paste_delay.value()
        s.auto_remove_watermark = self.chk_auto_remove_watermark.isChecked()
        s.watermark_method = self.combo_watermark_method.currentText()
        s.download_dir = self.download_dir
        s.auth_state_path = Path.home() / 'Documents' / 'dola_video_automation' / 'auth_state.json'
        s.generation_success_phrase = self.edit_success_phrase.text()
        
        # Coordinates
        s.watermark_blur_x = 540
        s.watermark_blur_y = 1220
        s.watermark_blur_w = 170
        s.watermark_blur_h = 80
        s.watermark_crop_pixels = 80
        return s

    def _update_runner_settings(self):
        if getattr(self, '_is_loading_backup', False):
            s = self._collect_settings()
            self.settings.thread_count = s.thread_count
        else:
            new_threads = self.spin_threads.value()
            if new_threads > 1 and not getattr(self, '_threads_warning_confirmed', False):
                self._trigger_threads_warning()
                if not getattr(self, '_threads_warning_confirmed', False):
                    self.spin_threads.blockSignals(True)
                    self.spin_threads.setValue(1)
                    self.spin_threads.blockSignals(False)
            s = self._collect_settings()
            self.settings.thread_count = s.thread_count

        self.settings.one_browser_per_video = s.one_browser_per_video
        self.settings.headless = s.headless
        self.settings.submit_and_close = s.submit_and_close
        self.settings.submit_close_delay_sec = s.submit_close_delay_sec
        self.settings.inject_ui_downloader = s.inject_ui_downloader
        self.settings.model = s.model
        self.settings.duration = s.duration
        self.settings.ratio = s.ratio
        self.settings.generation_timeout_sec = s.generation_timeout_sec
        self.settings.launch_delay_sec = s.launch_delay_sec
        self.settings.paste_delay_sec = s.paste_delay_sec
        self.settings.auto_remove_watermark = s.auto_remove_watermark
        self.settings.watermark_method = s.watermark_method
        self.settings.download_dir = s.download_dir
        
        if self.runner and self.runner.isRunning():
            self.runner.settings = self.settings
            
        self._save_json_backup()
        
    def _enforce_license_limits(self):
        from dola_automation.licensing import check_license_stored
        is_valid, lic_data = check_license_stored()
        plan_name = lic_data.get('plan', '1-Day Trial') if is_valid else '1-Day Trial'
        
        if plan_name == '1-Day Trial':
            self.settings.auto_remove_watermark = False
            self.chk_auto_remove_watermark.setChecked(False)
            self.chk_auto_remove_watermark.setEnabled(False)
            self.chk_auto_remove_watermark.setToolTip("Auto-remove watermark is disabled in the 1-Day Trial plan.")

    def _trigger_threads_warning(self):
        if getattr(self, '_showing_warning_dialog', False):
            return
        self._showing_warning_dialog = True
        
        dialog = ThreadsWarningDialog(self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._threads_warning_confirmed = True
        else:
            self._threads_warning_confirmed = False
            self.spin_threads.blockSignals(True)
            self.spin_threads.setValue(1)
            self.spin_threads.blockSignals(False)
            self.spin_threads.clearFocus()
            if self.centralWidget():
                self.centralWidget().setFocus()
                
        self._showing_warning_dialog = False

    def _copy_table_selection(self):
        selected_ranges = self.table.selectedRanges()
        if selected_ranges:
            copied_text = ""
            for r in range(selected_ranges[0].topRow(), selected_ranges[0].bottomRow() + 1):
                row_text = []
                for c in range(selected_ranges[0].leftColumn(), selected_ranges[0].rightColumn() + 1):
                    item = self.table.item(r, c)
                    row_text.append(item.text() if item else "")
                copied_text += "\t".join(row_text) + "\n"
            if copied_text.endswith("\n"):
                copied_text = copied_text[:-1]
            QApplication.clipboard().setText(copied_text)

    def eventFilter(self, watched, event):
        return super().eventFilter(watched, event)

    def _refresh_application(self):
        try:
            self._load_json_backup()
            self._refresh_history()
            self._refresh_lifetime_history()
            self._update_stats()
            self._log("Application settings and history refreshed successfully.")
        except Exception as e:
            self._log(f"Refresh failed: {e}")

    def _save_json_backup(self):
        try:
            self.backup_path.parent.mkdir(parents=True, exist_ok=True)
            data = {
                'prompts_draft': self.prompt_editor.toPlainText(),
                'thread_count': self.spin_threads.value(),
                'one_browser_per_video': self.chk_one_browser.isChecked(),
                'headless': self.chk_headless.isChecked(),
                'submit_and_close': self.chk_submit_and_close.isChecked(),
                'submit_close_delay_sec': self.spin_submit_delay.value(),
                'inject_ui_downloader': self.chk_inject_ui.isChecked(),
                'auto_remove_watermark': self.chk_auto_remove_watermark.isChecked(),
                'model': self.combo_model.currentText(),
                'duration': self.combo_duration.currentText(),
                'ratio': self.combo_ratio.currentText(),
                'launch_delay_sec': self.spin_launch_delay.value(),
                'paste_delay_sec': self.spin_paste_delay.value(),
                'generation_timeout_sec': self.spin_timeout.value(),
                'auto_download_delay': self.spin_auto_download_delay.value(),
                'watermark_method': self.combo_watermark_method.currentText(),
                'download_dir': str(self.download_dir),
                'generation_success_phrase': self.edit_success_phrase.text()
            }
            with open(self.backup_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=4)
        except Exception as e:
            logger.warning(f"Failed to save backup: {e}")

    def _load_json_backup(self):
        if not self.backup_path.exists():
            return
        self._is_loading_backup = True
        try:
            with open(self.backup_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            self.prompt_editor.setPlainText(data.get('prompts_draft', ''))
            self.spin_threads.setValue(data.get('thread_count', 1))
            self.chk_one_browser.setChecked(data.get('one_browser_per_video', True))
            self.chk_headless.setChecked(data.get('headless', False))
            self.chk_submit_and_close.setChecked(data.get('submit_and_close', False))
            self.spin_submit_delay.setValue(data.get('submit_close_delay_sec', 15))
            self.chk_inject_ui.setChecked(data.get('inject_ui_downloader', True))
            self.chk_auto_remove_watermark.setChecked(data.get('auto_remove_watermark', True))
            self.combo_model.setCurrentText(data.get('model', 'SeaDance 2.0 Fast'))
            self.combo_duration.setCurrentText(data.get('duration', '10s'))
            self.combo_ratio.setCurrentText(data.get('ratio', '9:16'))
            self.spin_launch_delay.setValue(data.get('launch_delay_sec', 5))
            self.spin_paste_delay.setValue(data.get('paste_delay_sec', 2))
            self.spin_timeout.setValue(data.get('generation_timeout_sec', 500))
            self.spin_auto_download_delay.setValue(data.get('auto_download_delay', 5))
            self.combo_watermark_method.setCurrentText(data.get('watermark_method', 'Blur'))
            self.edit_success_phrase.setText(data.get('generation_success_phrase', 'will be generated using'))
            
            d_dir = data.get('download_dir', '')
            if d_dir:
                self.download_dir = Path(d_dir)
                self.lbl_download_dir_show.setText(self.download_dir.name)
            
            self.settings = self._collect_settings()
        except Exception as e:
            logger.warning(f"Failed to load backup: {e}")
        finally:
            self._is_loading_backup = False

    # file picking & image mapping
    def _pick_download_dir(self):
        folder = QFileDialog.getExistingDirectory(self, "Select Download Folder", str(self.download_dir))
        if folder:
            self.download_dir = Path(folder)
            self.lbl_download_dir_show.setText(self.download_dir.name)
            self._save_json_backup()

    def _load_prompt_file(self):
        filepath, _ = QFileDialog.getOpenFileName(
            self, "Open CSV or Text Prompts", str(Path.home() / "Downloads"),
            "CSV and Text files (*.csv *.txt);;CSV files (*.csv);;Text files (*.txt);;All Files (*.*)"
        )
        if filepath:
            try:
                text = Path(filepath).read_text(encoding='utf-8')
                self.prompt_editor.setPlainText(text)
                self._log(f"Loaded prompts from: {Path(filepath).name}")
            except Exception as e:
                QMessageBox.critical(self, "Error", f"Failed to load file: {e}")

    def _translate_windows_path(self, path_str: str) -> str:
        path_str = path_str.strip().strip('"').strip("'")
        if not path_str:
            return ""
            
        import re
        match = re.match(r'^([a-zA-Z]):[\\/](.*)', path_str)
        if match:
            drive = match.group(1).lower()
            rest = match.group(2).replace('\\', '/')
            return f"/mnt/{drive}/{rest}"
            
        if '\\' in path_str:
            path_str = path_str.replace('\\', '/')
            
        return path_str

    def _load_prompt_from_path(self):
        raw_path = self.edit_file_path.text().strip()
        if not raw_path:
            QMessageBox.warning(self, "Empty Path", "Please enter or paste a file path first.")
            return
            
        translated_path = self._translate_windows_path(raw_path)
        path_obj = Path(translated_path)
        
        if not path_obj.exists():
            QMessageBox.critical(
                self, "File Not Found", 
                f"File does not exist at:\n{raw_path}\n\nTranslated path:\n{translated_path}"
            )
            return
            
        try:
            text = path_obj.read_text(encoding='utf-8')
            self.prompt_editor.setPlainText(text)
            self._log(f"Loaded prompts from pasted path: {path_obj.name}")
            self._save_json_backup()
        except Exception as e:
            QMessageBox.critical(self, "Read Error", f"Failed to read file:\n{e}")

    def _parse_prompts(self):
        text = self.prompt_editor.toPlainText().strip()
        if not text:
            QMessageBox.warning(self, "No prompts", "Please paste or load prompts before parsing.")
            return

        parsed = parse_prompts(text)
        if not parsed:
            QMessageBox.warning(self, "Failed to parse", "No valid prompts or CSV rows parsed. Check format.")
            return

        self.jobs.clear()
        ref_images = align_reference_images(parsed, self.reference_paths)
        
        for idx, (prompt, caption, title, scene_idx) in enumerate(parsed):
            ref = ref_images[idx] if idx < len(ref_images) else None
            job = PromptJob(
                index=idx + 1,
                prompt=prompt,
                caption=caption,
                video_title=title,
                scene_index=scene_idx,
                reference_image=ref,
                status=JobStatus.PENDING
            )
            self.jobs.append(job)

        self._refresh_table()
        self._update_stats()
        self._log(f"Parsed {len(self.jobs)} prompts successfully.")

    def _pick_reference_files(self):
        files, _ = QFileDialog.getOpenFileNames(
            self, "Select Reference Images", str(Path.home()),
            "Images (*.png *.jpg *.jpeg *.webp)"
        )
        if files:
            self.reference_paths = sorted([Path(f) for f in files])
            self._refresh_ref_list()

    def _pick_reference_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Select Reference Folder")
        if folder:
            paths = []
            for item in Path(folder).iterdir():
                if item.is_file() and item.suffix.lower() in ['.png', '.jpg', '.jpeg', '.webp']:
                    paths.append(item)
            self.reference_paths = sorted(paths)
            self._refresh_ref_list()

    def _clear_references(self):
        self.reference_paths.clear()
        self._refresh_ref_list()

    def _refresh_ref_list(self):
        self.ref_list.clear()
        for idx, p in enumerate(self.reference_paths):
            self.ref_list.addItem(f"#{idx+1}: {p.name}")
        self._log(f"Reference images mapped: {len(self.reference_paths)} files.")

    def _refresh_table(self):
        self.table.blockSignals(True)
        try:
            self.table.setRowCount(len(self.jobs))
            for i, job in enumerate(self.jobs):
                # Checkbox / Index
                chk = QTableWidgetItem(f"Job #{job.index}")
                chk.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                chk.setCheckState(Qt.CheckState.Unchecked)
                self.table.setItem(i, 0, chk)

                # Video Title
                title_item = QTableWidgetItem(job.video_title or "Standalone")
                title_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                self.table.setItem(i, 1, title_item)

                # Scene index
                scene_item = QTableWidgetItem(str(job.scene_index) if job.scene_index is not None else "-")
                scene_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                self.table.setItem(i, 2, scene_item)

                # Prompt (Fully editable)
                p_item = QTableWidgetItem(job.prompt)
                p_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEditable)
                self.table.setItem(i, 3, p_item)

                # Reference
                ref_str = job.reference_image.name if job.has_reference else "None"
                ref_item = QTableWidgetItem(ref_str)
                ref_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                self.table.setItem(i, 4, ref_item)

                # Status
                status_item = QTableWidgetItem(job.status.value.upper())
                status_flags = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
                if job.status not in [JobStatus.RUNNING, JobStatus.WAITING, JobStatus.DOWNLOADING]:
                    status_flags |= Qt.ItemFlag.ItemIsEditable
                status_item.setFlags(status_flags)
                color = STATUS_COLORS.get(job.status.value, '#ffffff')
                status_item.setForeground(QColor(color))
                self.table.setItem(i, 5, status_item)

                # Download Path
                dl_item = QTableWidgetItem(job.download_path or "-")
                dl_flags = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
                if job.status not in [JobStatus.RUNNING, JobStatus.WAITING, JobStatus.DOWNLOADING]:
                    dl_flags |= Qt.ItemFlag.ItemIsEditable
                dl_item.setFlags(dl_flags)
                self.table.setItem(i, 6, dl_item)

                # Error Details
                err_item = QTableWidgetItem(job.error or "-")
                err_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                if job.error:
                    err_item.setForeground(QColor("#D97706")) # orange color for errors
                self.table.setItem(i, 7, err_item)

                # Action Column
                btn_cell = QWidget()
                cell_layout = QHBoxLayout(btn_cell)
                cell_layout.setContentsMargins(2, 2, 2, 2)
                cell_layout.setSpacing(5)
                
                relaunch_btn = QPushButton("Relaunch", btn_cell)
                relaunch_btn.setStyleSheet("padding: 2px 8px; font-size: 11px;")
                relaunch_btn.clicked.connect(lambda checked, idx=job.index: self._relaunch_failed_job(idx))
                
                cell_layout.addWidget(relaunch_btn)
                cell_layout.addStretch()
                self.table.setCellWidget(i, 8, btn_cell)
        finally:
            self.table.blockSignals(False)

    def _on_table_item_changed(self, item):
        if not item:
            return
        row = item.row()
        col = item.column()
        logger.debug(f"[_on_table_item_changed] row={row}, col={col}, text='{item.text()}'")
        if not (0 <= row < len(self.jobs)):
            return
        
        job = self.jobs[row]
        
        # Guard active jobs from status and download path edits
        if col in (5, 6) and job.status in [JobStatus.RUNNING, JobStatus.WAITING, JobStatus.DOWNLOADING]:
            # If the change is programmatic (re-setting status/path to same value), ignore warning
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
                self._save_json_backup()
                if job.job_id:
                    try:
                        self.db.update_job(job_id=job.job_id, prompt=new_prompt)
                        self._log(f"Updated prompt for Job #{job.index} in database.")
                    except Exception as e:
                        logger.error(f"Failed to update job prompt in DB: {e}")
        elif col == 5:  # Status column
            new_status_str = item.text().strip().lower()
            valid_status = None
            for status in JobStatus:
                if status.value == new_status_str:
                    valid_status = status
                    break
            
            if valid_status is None:
                valid_list = ", ".join([s.value.upper() for s in JobStatus])
                self.table.blockSignals(True)
                item.setText(job.status.value.upper())
                self.table.blockSignals(False)
                QTimer.singleShot(0, lambda ns=new_status_str, vl=valid_list: QMessageBox.warning(
                    self,
                    "Invalid Status",
                    f"'{ns}' is not a valid status.\nValid options: {vl}"
                ))
                return
            
            if job.status != valid_status:
                job.status = valid_status
                if valid_status not in [JobStatus.FAILED, JobStatus.NOT_FOUND]:
                    job.error = None
                
                self.table.blockSignals(True)
                item.setText(valid_status.value.upper())
                color = STATUS_COLORS.get(valid_status.value, '#ffffff')
                item.setForeground(QColor(color))
                
                # Dynamically update status flags
                status_flags = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
                if valid_status not in [JobStatus.RUNNING, JobStatus.WAITING, JobStatus.DOWNLOADING]:
                    status_flags |= Qt.ItemFlag.ItemIsEditable
                item.setFlags(status_flags)
                
                # Dynamically update download path flags
                dl_item = self.table.item(row, 6)
                if dl_item:
                    dl_flags = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
                    if valid_status not in [JobStatus.RUNNING, JobStatus.WAITING, JobStatus.DOWNLOADING]:
                        dl_flags |= Qt.ItemFlag.ItemIsEditable
                    dl_item.setFlags(dl_flags)
                
                # Clear error details column text if error is None
                if job.error is None:
                    err_item = self.table.item(row, 7)
                    if err_item:
                        err_item.setText("-")
                self.table.blockSignals(False)
                
                self._save_json_backup()
                if job.job_id:
                    try:
                        self.db.update_job(job_id=job.job_id, status=valid_status, error=job.error)
                        self._log(f"Updated status for Job #{job.index} to {valid_status.value.upper()} in database.")
                    except Exception as e:
                        logger.error(f"Failed to update job status in DB: {e}")
                self._update_stats()
                
        elif col == 6:  # Download Path column
            new_path_str = item.text().strip()
            if new_path_str == "-" or not new_path_str:
                new_path_str = None
            
            if job.download_path != new_path_str:
                job.download_path = new_path_str
                self._save_json_backup()
                if job.job_id:
                    try:
                        self.db.update_job(job_id=job.job_id, download_path=Path(new_path_str) if new_path_str else None)
                        self._log(f"Updated download path for Job #{job.index} in database.")
                    except Exception as e:
                        logger.error(f"Failed to update download path in DB: {e}")

    def _update_stats(self):
        lifetime_completed = self.db.get_lifetime_count()
        self.stat_lifetime.findChild(QLabel, "statValue").setText(str(lifetime_completed))
        
        batch_completed = sum(1 for j in self.jobs if j.status == JobStatus.COMPLETED)
        self.stat_batch.findChild(QLabel, "statValue").setText(str(batch_completed))
        
        self.stat_total.findChild(QLabel, "statValue").setText(str(len(self.jobs)))
        
        batch_failed = sum(1 for j in self.jobs if j.status in [JobStatus.FAILED, JobStatus.NOT_FOUND])
        self.stat_fail.findChild(QLabel, "statValue").setText(str(batch_failed))

    # batch operational execution
    def _start_batch(self):
        # Check plan-specific trial limits
        from dola_automation.licensing import check_license_stored
        is_valid, lic_data = check_license_stored()
        plan_name = lic_data.get('plan', '1-Day Trial') if is_valid else '1-Day Trial'
        
        if plan_name == '1-Day Trial':
            if len(self.jobs) > 2:
                QMessageBox.critical(
                    self,
                    "Plan Limit Reached",
                    "Trial Plan Limit: You can only generate up to 2 videos per batch in the 1-Day Trial plan.\n"
                    "Please upgrade to Creator or Studio Pro plans for unlimited batches."
                )
                return
            if self.settings.auto_remove_watermark:
                QMessageBox.critical(
                    self,
                    "Plan Limit Reached",
                    "Trial Plan Limit: Watermark removal is disabled in the 1-Day Trial plan.\n"
                    "Please upgrade to Creator or Studio Pro plans to use watermark removal."
                )
                return

        if self.runner and self.runner.isRunning():
            QMessageBox.warning(self, "Runner Active", "Another batch automation process is currently running. Please stop or wait for it to complete.")
            return

        if not self.jobs:
            QMessageBox.warning(self, "No jobs", "Please parse or load prompts first.")
            return

        self._update_runner_settings()

        has_failed_or_cancelled = any(j.status in [JobStatus.FAILED, JobStatus.CANCELLED] for j in self.jobs)
        if has_failed_or_cancelled:
            confirm = QMessageBox.question(
                self,
                "Retry Failed / Cancelled Jobs",
                "Would you like to retry all the failed and cancelled jobs in this batch?\n\n"
                "Click 'Yes' to reset them to PENDING and run them.\n"
                "Click 'No' to skip them and only run pending jobs.\n"
                "Click 'Cancel' to abort starting the batch.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Yes
            )
            if confirm == QMessageBox.StandardButton.Cancel:
                return
            elif confirm == QMessageBox.StandardButton.Yes:
                self.table.blockSignals(True)
                try:
                    for job in self.jobs:
                        if job.status in [JobStatus.FAILED, JobStatus.CANCELLED]:
                            job.status = JobStatus.PENDING
                            job.error = None
                            if job.job_id:
                                try:
                                    self.db.update_job(job_id=job.job_id, status=JobStatus.PENDING, error=None)
                                except Exception as e:
                                    logger.error(f"Failed to reset status in database: {e}")
                            
                            row_idx = self.jobs.index(job)
                            status_item = self.table.item(row_idx, 5)
                            if status_item:
                                status_item.setText(JobStatus.PENDING.value.upper())
                                status_item.setForeground(QColor(STATUS_COLORS.get('pending', '#ffffff')))
                            err_item = self.table.item(row_idx, 7)
                            if err_item:
                                err_item.setText("-")
                finally:
                    self.table.blockSignals(False)
                self._save_json_backup()
                self._update_stats()
        
        # Save session in database
        session_name = f"Session {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}"
        self.current_session_id = self.db.create_session(session_name, self.jobs)

        # Decide whether we are doing "full" batch run, or sequential submit first
        runner_mode = "submit_only" if self.settings.submit_and_close else "full"
        
        self.btn_start.setEnabled(False)
        self.btn_pause.setEnabled(True)
        self.btn_stop.setEnabled(True)
        self.btn_pause.setText("Pause")

        # Timer start
        self.batch_start_time.start()
        self.batch_timer.start(1000)

        self._log(f"Starting batch in mode: {runner_mode}")
        self.runner = BatchRunner(self.jobs, self.settings, self.db, self.current_session_id, mode=runner_mode)
        self.runner.job_progress.connect(self._on_job_progress)
        self.runner.chat_created.connect(self._on_chat_created)
        self.runner.job_finished.connect(self._on_job_finished)
        self.runner.batch_finished.connect(self._on_batch_finished)
        self.runner.start()
        self._send_notification("Batch Started", f"Processing {len(self.jobs)} jobs...")

    def _pause_batch(self):
        if self.runner and self.runner.isRunning():
            is_paused = self.runner.pause_resume()
            if is_paused:
                self.btn_pause.setText("Resume")
                self._log("Batch PAUSED. Running jobs will finish, but no new jobs will start.")
            else:
                self.btn_pause.setText("Pause")
                self._log("Batch RESUMED.")

    def _stop_batch(self):
        if self.runner and self.runner.isRunning():
            self.runner.stop()
            self._log("Stop requested. Waiting for active workers to exit...")

    def _update_batch_timer(self):
        elapsed = self.batch_start_time.elapsed()
        t = QTime(0, 0, 0).addMSecs(elapsed)
        self.timer_label.setText(t.toString("HH:mm:ss"))

    @pyqtSlot(int, str)
    def _on_job_progress(self, job_index: int, message: str):
        if job_index > 0:
            self._log(f"Job #{job_index}: {message}")
            for row in range(self.table.rowCount()):
                chk_item = self.table.item(row, 0)
                if chk_item and chk_item.text() == f"Job #{job_index}":
                    self.table.blockSignals(True)
                    try:
                        self.table.setItem(row, 5, QTableWidgetItem("RUNNING"))
                        self.table.item(row, 5).setForeground(QColor(STATUS_COLORS['running']))
                    finally:
                        self.table.blockSignals(False)
                    break
        else:
            self._log(message)

    @pyqtSlot(int, str)
    def _on_chat_created(self, job_index: int, chat_url: str):
        self._log(f"Job #{job_index}: Chat URL saved to database mid-flight: {chat_url}")
        for row in range(self.table.rowCount()):
            chk_item = self.table.item(row, 0)
            if chk_item and chk_item.text() == f"Job #{job_index}":
                self.jobs[job_index-1].chat_url = chat_url
                self._refresh_table()
                break

    @pyqtSlot(int, bool, str, str)
    def _on_job_finished(self, job_index: int, success: bool, download_path: str, error: str):
        job = self.jobs[job_index-1]
        self._refresh_table()
        self._update_stats()
        
        status_txt = "Success" if success else f"Failed: {error}"
        self._send_notification(f"Job #{job_index} Finished", status_txt)
        
        # Check scene merging trigger if job is completed
        if job.status == JobStatus.COMPLETED and job.video_title:
            self._check_and_merge_scenes(job.video_title)

    @pyqtSlot()
    def _on_batch_finished(self):
        self.batch_timer.stop()
        self.btn_start.setEnabled(True)
        self.btn_pause.setEnabled(False)
        self.btn_stop.setEnabled(False)
        self._log("Batch runner completed.")
        self._send_notification("Batch Finished", "All jobs completed.")

        # Check if we were in submit-only mode, so we trigger countdown to Phase 2 (Downloading)
        if self.settings.submit_and_close and self.runner and self.runner.mode == "submit_only":
            self.runner = None
            dlg = AutoDownloadDialog(self.spin_auto_download_delay.value(), self)
            if dlg.exec() == QDialog.DialogCode.Accepted:
                self._log("Auto-downloading submitted jobs...")
                self._start_download_phase()
            else:
                self._log("Auto-download cancelled by user.")

    def _start_download_phase(self):
        # Starts downloading jobs sequentially/multi-threaded
        self._update_runner_settings()
        self.btn_start.setEnabled(False)
        self.btn_pause.setEnabled(True)
        self.btn_stop.setEnabled(True)
        self.btn_pause.setText("Pause")

        self.batch_start_time.start()
        self.batch_timer.start(1000)

        self._log("Downloading batch videos...")
        self.runner = BatchRunner(self.jobs, self.settings, self.db, self.current_session_id, mode="download_only")
        self.runner.job_progress.connect(self._on_job_progress)
        self.runner.chat_created.connect(self._on_chat_created)
        self.runner.job_finished.connect(self._on_job_finished)
        self.runner.batch_finished.connect(self._on_batch_finished)
        self.runner.start()

    def _check_and_merge_scenes(self, video_title: str):
        # Fetch current session jobs
        title_jobs = [j for j in self.jobs if j.video_title == video_title]
        if not title_jobs:
            return
            
        all_completed = True
        for j in title_jobs:
            if j.status != JobStatus.COMPLETED or not j.download_path or not Path(j.download_path).exists():
                all_completed = False
                break
                
        if all_completed:
            # Sort scenes
            title_jobs.sort(key=lambda x: x.scene_index or 0)
            input_paths = [j.download_path for j in title_jobs]
            
            # Compute total expected duration from individual scenes
            sum_durations = sum(get_video_duration(Path(p)) for p in input_paths)
            
            slug_title = self._slug(video_title)
            output_path = self.download_dir / f"{slug_title}.mp4"
            
            self._log(f"All scenes for video '{video_title}' are downloaded. Expected duration: {sum_durations:.2f}s. Concatenating losslessly...")
            success = concatenate_videos(input_paths, str(output_path))
            if success:
                self._log(f"Lossless merge success! Video generated: {output_path.name}")
                
                # Auto-remove watermark from the final merged video if active
                if self.settings.auto_remove_watermark:
                    self._log(f"Post-processing: Auto-removing watermark from merged video ({self.settings.watermark_method})...")
                    coords = (
                        self.settings.watermark_blur_x,
                        self.settings.watermark_blur_y,
                        self.settings.watermark_blur_w,
                        self.settings.watermark_blur_h
                    )
                    success_watermark = process_video_watermark(
                        output_path,
                        self.settings.watermark_method,
                        output_path,
                        coords,
                        self.settings.watermark_crop_pixels
                    )
                    if success_watermark:
                        self._log("Watermark removed from merged video successfully.")
                    else:
                        self._log("Failed to remove watermark from merged video.")
                
                # Perform duration-based quality checks
                merged_duration = get_video_duration(output_path)
                if merged_duration > 0 and abs(merged_duration - sum_durations) < 2.0:
                    self._log(f"Quality Check Passed: Merged video duration ({merged_duration:.2f}s) matches sum of scenes ({sum_durations:.2f}s).")
                else:
                    self._log(f"Quality Check Warning: Merged video duration ({merged_duration:.2f}s) mismatch with sum of scenes ({sum_durations:.2f}s)!")
                
                # Save captions text sidecar
                caption_text = title_jobs[0].caption or ""
                if caption_text:
                    txt_path = output_path.with_suffix('.txt')
                    txt_path.write_text(caption_text, encoding='utf-8')
                    self._log(f"Saved sidecar caption file: {txt_path.name}")
                
                # Send system notification and show pop-up message
                notif_msg = f"Video merger and post-processing completed successfully for: '{video_title}'."
                self._send_notification("Video Merger Complete", notif_msg)
                
                confirm = QMessageBox.question(
                    self,
                    "Delete Individual Scene Clips?",
                    f"Video merger completed for '{video_title}'.\n\n"
                    "Would you like to delete the individual raw scene clips from the downloads folder to save storage space?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No
                )
                if confirm == QMessageBox.StandardButton.Yes:
                    self._log("Deleting individual raw scene clips as requested...")
                    del_count = 0
                    for p in input_paths:
                        try:
                            p_obj = Path(p)
                            if p_obj.exists():
                                p_obj.unlink()
                                del_count += 1
                        except Exception as e:
                            logger.error(f"Failed to delete individual raw clip '{p}': {e}")
                    self._log(f"Successfully deleted {del_count} individual raw scene clips.")
                else:
                    self._log("Kept individual raw scene clips in the downloads folder.")
            else:
                self._log(f"Failed to losslessly concatenate scenes for '{video_title}'. Check FFmpeg path/logs.")

    def _send_notification(self, title: str, message: str):
        if hasattr(self, 'tray_icon') and self.tray_icon and QSystemTrayIcon.isSystemTrayAvailable():
            self.tray_icon.showMessage(title, message, QSystemTrayIcon.MessageIcon.Information, 10000)
        else:
            self._log(f"System notification: {title} - {message}")

    def _slug(self, s: str) -> str:
        import re
        s = re.sub(r'[^\w\-]+', '_', s)
        return s.strip('_')[:50]

    # context menu actions
    def _on_table_context_menu(self, pos: QPoint):
        clicked_index = self.table.indexAt(pos)
        clicked_row = clicked_index.row()
        clicked_col = clicked_index.column()
        
        menu = QMenu(self)
        selected_rows = list(set(idx.row() for idx in self.table.selectedIndexes()))
        if clicked_row >= 0 and clicked_row not in selected_rows:
            selected_rows.append(clicked_row)
        
        # Keep references to actions to prevent garbage collection of Python wrappers
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
            if job.download_path:
                copy_path_action = menu.addAction("Copy Download Path")
                copy_path_action.triggered.connect(lambda *args, p=job.download_path: QApplication.clipboard().setText(p))
                actions.append(copy_path_action)
            if job.chat_url:
                copy_url_action = menu.addAction("Copy Chat URL")
                copy_url_action.triggered.connect(lambda *args, u=job.chat_url: QApplication.clipboard().setText(u))
                actions.append(copy_url_action)
                
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

    def _context_change_status(self, rows: List[int], status: JobStatus):
        logger.info(f"Changing status of rows {rows} to {status.value.upper()}")
        
        # Filter out active running/waiting/downloading jobs
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
            
        self.table.blockSignals(True)
        try:
            for r in allowed_rows:
                if 0 <= r < len(self.jobs):
                    job = self.jobs[r]
                    if job.status != status:
                        job.status = status
                        if status not in [JobStatus.FAILED, JobStatus.NOT_FOUND]:
                            job.error = None
                        
                        if job.job_id:
                            try:
                                self.db.update_job(job_id=job.job_id, status=status, error=job.error)
                            except Exception as e:
                                logger.error(f"Failed to update status in DB context action: {e}")
                        
                        status_item = self.table.item(r, 5)
                        if status_item:
                            status_item.setText(status.value.upper())
                            color = STATUS_COLORS.get(status.value, '#ffffff')
                            status_item.setForeground(QColor(color))
                            
                            # Dynamically update status flags (read-only)
                            status_flags = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
                            status_item.setFlags(status_flags)
                        
                        # Dynamically update download path flags (read-only)
                        dl_item = self.table.item(r, 6)
                        if dl_item:
                            dl_flags = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
                            dl_item.setFlags(dl_flags)

                        if job.error is None:
                            err_item = self.table.item(r, 7)
                            if err_item:
                                err_item.setText("-")
            self._save_json_backup()
        finally:
            self.table.blockSignals(False)
        self._update_stats()

    def _context_set_download_path(self, rows: List[int]):
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "Select Video File",
            str(self.download_dir),
            "Video Files (*.mp4 *.mkv *.avi *.mov);;All Files (*)"
        )
        if not file_path:
            return
            
        self.table.blockSignals(True)
        try:
            for r in rows:
                if 0 <= r < len(self.jobs):
                    job = self.jobs[r]
                    if job.status in [JobStatus.RUNNING, JobStatus.WAITING, JobStatus.DOWNLOADING]:
                        continue
                    job.download_path = file_path
                    
                    dl_item = self.table.item(r, 6)
                    if dl_item:
                        dl_item.setText(file_path)
                    
                    if job.job_id:
                        try:
                            self.db.update_job(job_id=job.job_id, download_path=file_path)
                        except Exception as e:
                            logger.error(f"Failed to update download path in DB: {e}")
            self._save_json_backup()
        finally:
            self.table.blockSignals(False)

    def _context_toggle_checks(self):
        for item in self.table.selectedItems():
            if item.column() == 0:
                new_state = Qt.CheckState.Unchecked if item.checkState() == Qt.CheckState.Checked else Qt.CheckState.Checked
                item.setCheckState(new_state)

    def _context_relaunch_manual(self):
        selected_rows = list(set(index.row() for index in self.table.selectedIndexes()))
        for r in selected_rows:
            job_index = r + 1
            self._relaunch_manual_browser(job_index)

    def _context_download_selected(self):
        if self.runner and self.runner.isRunning():
            QMessageBox.warning(self, "Runner Active", "Another batch automation process is currently running. Please stop or wait for it to complete before downloading.")
            return

        selected_rows = list(set(index.row() for index in self.table.selectedIndexes()))
        selected_jobs = [self.jobs[r] for r in selected_rows]
        
        if not selected_jobs:
            return

        jobs_to_dl = [j for j in selected_jobs if j.chat_url]
        skipped_count = len(selected_jobs) - len(jobs_to_dl)

        if not jobs_to_dl:
            QMessageBox.warning(self, "No Chat URL", "None of the selected jobs have a chat URL. Please relaunch them or run prompt submission first.")
            return

        if skipped_count > 0:
            self._log(f"Skipped {skipped_count} selected jobs because they do not have a chat URL.")

        self._log(f"Downloading {len(jobs_to_dl)} selected jobs...")
        self.btn_start.setEnabled(False)
        self.runner = BatchRunner(jobs_to_dl, self.settings, self.db, self.current_session_id, mode="download_only")
        self.runner.job_progress.connect(self._on_job_progress)
        self.runner.job_finished.connect(self._on_job_finished)
        self.runner.batch_finished.connect(self._on_batch_finished)
        self.runner.start()

    def _context_clear_rows(self):
        selected_rows = sorted(list(set(index.row() for index in self.table.selectedIndexes())), reverse=True)
        for r in selected_rows:
            self.jobs.pop(r)
            self.table.removeRow(r)
        # Fix remaining job indexes
        for idx, job in enumerate(self.jobs):
            job.index = idx + 1
        self._refresh_table()
        self._update_stats()

    def _toggle_select_all(self):
        any_unchecked = False
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item and item.checkState() == Qt.CheckState.Unchecked:
                any_unchecked = True
                break
        
        new_state = Qt.CheckState.Checked if any_unchecked else Qt.CheckState.Unchecked
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item:
                item.setCheckState(new_state)

    def _download_selected_jobs(self):
        if self.runner and self.runner.isRunning():
            QMessageBox.warning(self, "Runner Active", "Another batch automation process is currently running. Please stop or wait for it to complete before downloading.")
            return

        selected_jobs = []
        selected_rows = set(index.row() for index in self.table.selectedIndexes())
        
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if (item and item.checkState() == Qt.CheckState.Checked) or (row in selected_rows):
                selected_jobs.append(self.jobs[row])
                
        if not selected_jobs:
            QMessageBox.warning(self, "No selection", "Please check checkboxes or highlight/select rows to select jobs for downloading.")
            return

        # Gracefully handle jobs with missing chat URL
        jobs_to_dl = [j for j in selected_jobs if j.chat_url]
        skipped_count = len(selected_jobs) - len(jobs_to_dl)

        if not jobs_to_dl:
            QMessageBox.warning(self, "No Chat URL", "None of the selected jobs have a chat URL. Please relaunch them or run prompt submission first.")
            return

        if skipped_count > 0:
            self._log(f"Skipped {skipped_count} selected jobs because they do not have a chat URL.")

        self._log(f"Downloading {len(jobs_to_dl)} selected jobs...")
        self.btn_start.setEnabled(False)
        self.runner = BatchRunner(jobs_to_dl, self.settings, self.db, self.current_session_id, mode="download_only")
        self.runner.job_progress.connect(self._on_job_progress)
        self.runner.job_finished.connect(self._on_job_finished)
        self.runner.batch_finished.connect(self._on_batch_finished)
        self.runner.start()

    def _retry_all_failed_jobs(self):
        if self.runner and self.runner.isRunning():
            QMessageBox.warning(self, "Runner Active", "Another batch automation process is currently running. Please stop or wait for it to complete.")
            return

        failed_jobs = [j for j in self.jobs if j.status in [JobStatus.FAILED, JobStatus.NOT_FOUND, JobStatus.CANCELLED]]
        if not failed_jobs:
            QMessageBox.information(self, "No Failed Jobs", "No failed/cancelled jobs found in current batch.")
            return
            
        self._log(f"Retrying {len(failed_jobs)} failed jobs...")
        self.btn_start.setEnabled(False)
        self.runner = BatchRunner(failed_jobs, self.settings, self.db, self.current_session_id, mode="full")
        self.runner.job_progress.connect(self._on_job_progress)
        self.runner.job_finished.connect(self._on_job_finished)
        self.runner.batch_finished.connect(self._on_batch_finished)
        self.runner.start()

    def _relaunch_failed_job(self, job_index: int):
        job = self.jobs[job_index-1]
        self._relaunch_manual_browser(job_index)

    def _relaunch_manual_browser(self, job_index: int):
        self._log(f"Relaunching manual browser for Job #{job_index} in thread...")
        job = self.jobs[job_index-1]
        
        def _launch():
            try:
                from playwright.sync_api import sync_playwright
            except ImportError:
                from patchright.sync_api import sync_playwright
                
            with sync_playwright() as p:
                launch_args = []
                import os
                if os.name != 'nt':
                    launch_args.extend(["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"])
                launch_args.append("--disable-blink-features=AutomationControlled")
                browser = p.chromium.launch(headless=False, args=launch_args)
                context_kwargs = {"viewport": {"width": 1280, "height": 800}}
                
                # Load context
                state_path = Path.home() / 'Documents' / 'dola_video_automation' / 'auth_state.json'
                if state_path.exists():
                    context_kwargs["storage_state"] = str(state_path)
                    
                context = browser.new_context(**context_kwargs)
                page = context.new_page()
                
                target_url = job.chat_url if job.chat_url else "https://www.dola.com/chat/create-image"
                page.goto(target_url)
                
                # Keep browser open while page is active
                while page.url != "" and not page.is_closed():
                    try:
                        curr_url = page.url
                        is_dola = "dola.com" in curr_url
                        if is_dola:
                            # Accept if it is a specific chat session or if it has a video element visible
                            is_specific_chat = "/chat/" in curr_url and not curr_url.endswith("create-image") and not curr_url.endswith("create-video")
                            has_video = page.evaluate("() => document.querySelector('video') !== null")
                            
                            if is_specific_chat or has_video:
                                if job.chat_url != curr_url:
                                    job.chat_url = curr_url
                                    self.db.update_job(job.job_id, chat_url=curr_url)
                                    from PyQt6.QtCore import QTimer
                                    QTimer.singleShot(0, lambda url=curr_url: self._log(f"Captured/updated chat URL for Job #{job_index}: {url}"))
                                    QTimer.singleShot(0, self._refresh_table)
                    except Exception:
                        pass
                    time.sleep(1)
                context.close()
                browser.close()
                
        import threading
        t = threading.Thread(target=_launch, daemon=True)
        t.start()

    # Session History Tab
    def _refresh_history(self):
        self.history_list.clear()
        sessions = self.db.list_sessions(limit=50)
        for s in sessions:
            lbl = f"ID: {s['id']} | {s['name']} (Completed: {s['completed_count']}, Failed: {s['failed_count']})"
            item = QListWidgetItem(lbl)
            item.setData(Qt.ItemDataRole.UserRole, s['id'])
            self.history_list.addItem(item)

    def _load_selected_session(self):
        curr = self.history_list.currentItem()
        if not curr:
            return
        session_id = curr.data(Qt.ItemDataRole.UserRole)
        self.current_session_id = session_id
        self.jobs = self.db.load_session_jobs(session_id)
        self._refresh_table()
        self._update_stats()
        self._log(f"Loaded historic Session #{session_id} into workspace.")

    # Lifetime Tab
    def _refresh_lifetime_history(self):
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

        self.table_lifetime.setRowCount(len(rows))
        for idx, row in enumerate(rows):
            self.table_lifetime.setItem(idx, 0, QTableWidgetItem(str(row['id'])))
            self.table_lifetime.setItem(idx, 1, QTableWidgetItem(row['session_name']))
            self.table_lifetime.setItem(idx, 2, QTableWidgetItem(str(row['job_index'])))
            self.table_lifetime.setItem(idx, 3, QTableWidgetItem(row['video_title'] or "-"))
            self.table_lifetime.setItem(idx, 4, QTableWidgetItem(str(row['scene_index']) if row['scene_index'] is not None else "-"))
            self.table_lifetime.setItem(idx, 5, QTableWidgetItem(row['prompt'][:100]))
            self.table_lifetime.setItem(idx, 6, QTableWidgetItem(row['status'].upper()))
            self.table_lifetime.setItem(idx, 7, QTableWidgetItem(row['finished_at'] or "-"))
            self.table_lifetime.setItem(idx, 8, QTableWidgetItem(row['download_path'] or "-"))
            
            err_item = QTableWidgetItem(row['error'] or "-")
            if row['error']:
                err_item.setForeground(QColor("#D97706"))
            self.table_lifetime.setItem(idx, 9, err_item)

    def _on_lifetime_table_context_menu(self, pos: QPoint):
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
        jobs = self.db.get_jobs_by_ids([db_job_id])
        if jobs:
            job = jobs[0]
            # launch browser manual thread
            def _launch():
                try:
                    from playwright.sync_api import sync_playwright
                except ImportError:
                    from patchright.sync_api import sync_playwright
                with sync_playwright() as p:
                    browser = p.chromium.launch(headless=False)
                    context_kwargs = {"viewport": {"width": 1280, "height": 800}}
                    state_path = Path.home() / 'Documents' / 'dola_video_automation' / 'auth_state.json'
                    if state_path.exists():
                        context_kwargs["storage_state"] = str(state_path)
                    context = browser.new_context(**context_kwargs)
                    page = context.new_page()
                    target_url = job.chat_url if job.chat_url else "https://www.dola.com/chat/create-image"
                    page.goto(target_url)
                    while page.url != "" and not page.is_closed():
                        try:
                            curr_url = page.url
                            is_dola = "dola.com" in curr_url
                            if is_dola:
                                is_specific_chat = "/chat/" in curr_url and not curr_url.endswith("create-image") and not curr_url.endswith("create-video")
                                has_video = page.evaluate("() => document.querySelector('video') !== null")
                                if is_specific_chat or has_video:
                                    if job.chat_url != curr_url:
                                        job.chat_url = curr_url
                                        self.db.update_job(job.job_id, chat_url=curr_url)
                                        # Update workspace memory too if it matches
                                        for w_job in self.jobs:
                                            if w_job.job_id == job.job_id:
                                                w_job.chat_url = curr_url
                                        from PyQt6.QtCore import QTimer
                                        QTimer.singleShot(0, lambda url=curr_url: self._log(f"Captured/updated historic chat URL: {url}"))
                                        QTimer.singleShot(0, self._refresh_lifetime_history)
                                        QTimer.singleShot(0, self._refresh_table)
                        except Exception:
                            pass
                        time.sleep(1)
                    context.close()
                    browser.close()
            import threading
            threading.Thread(target=_launch, daemon=True).start()

    def _launch_historic_jobs_as_new_batch(self):
        selected_rows = list(set(idx.row() for idx in self.table_lifetime.selectedIndexes()))
        db_ids = [int(self.table_lifetime.item(r, 0).text()) for r in selected_rows]
        jobs = self.db.get_jobs_by_ids(db_ids)
        if not jobs:
            return

        self.jobs.clear()
        for idx, job in enumerate(jobs):
            job.index = idx + 1
            job.status = JobStatus.PENDING
            job.download_path = None
            job.error = None
            self.jobs.append(job)

        self._refresh_table()
        self._update_stats()
        self._log(f"Imported {len(self.jobs)} historic jobs as a new session.")

    def _delete_historic_jobs(self):
        selected_rows = list(set(idx.row() for idx in self.table_lifetime.selectedIndexes()))
        db_ids = [int(self.table_lifetime.item(r, 0).text()) for r in selected_rows]
        if db_ids:
            self.db.delete_jobs_by_ids(db_ids)
            self._refresh_lifetime_history()
            self._log(f"Deleted {len(db_ids)} historic jobs from database.")

    def _toggle_lifetime_select_all(self):
        self.table_lifetime.selectAll()

    def _download_lifetime_selected_jobs(self):
        selected_rows = list(set(idx.row() for idx in self.table_lifetime.selectedIndexes()))
        db_ids = [int(self.table_lifetime.item(r, 0).text()) for r in selected_rows]
        jobs = self.db.get_jobs_by_ids(db_ids)
        jobs_to_dl = [j for j in jobs if j.chat_url]
        if not jobs_to_dl:
            QMessageBox.warning(self, "No selection", "Please select historic jobs with chat URLs to download.")
            return

        self._log(f"Downloading selected historic jobs...")
        self.btn_start.setEnabled(False)
        self.runner = BatchRunner(jobs_to_dl, self.settings, self.db, self.current_session_id, mode="download_only")
        self.runner.job_progress.connect(self._on_job_progress)
        self.runner.job_finished.connect(self._on_job_finished)
        self.runner.batch_finished.connect(self._on_batch_finished)
        self.runner.start()

    def _retry_lifetime_all_failed_jobs(self):
        selected_rows = list(set(idx.row() for idx in self.table_lifetime.selectedIndexes()))
        db_ids = [int(self.table_lifetime.item(r, 0).text()) for r in selected_rows]
        jobs = self.db.get_jobs_by_ids(db_ids)
        failed_jobs = [j for j in jobs if j.status in [JobStatus.FAILED, JobStatus.NOT_FOUND, JobStatus.CANCELLED]]
        if not failed_jobs:
            QMessageBox.information(self, "No failed jobs", "No failed/cancelled jobs found in current selection.")
            return

        self.jobs.clear()
        for idx, job in enumerate(failed_jobs):
            job.index = idx + 1
            job.status = JobStatus.PENDING
            job.download_path = None
            job.error = None
            self.jobs.append(job)

        self._refresh_table()
        self._update_stats()
        self._log(f"Loaded {len(self.jobs)} failed historic jobs for retry.")
        self._start_batch()

    def _export_lifetime_csv(self):
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

        if not rows:
            QMessageBox.information(self, "Export", "No jobs to export.")
            return

        filepath, _ = QFileDialog.getSaveFileName(self, "Save Exported CSV", str(Path.home() / "lifetime_history.csv"), "CSV Files (*.csv)")
        if filepath:
            try:
                with open(filepath, 'w', newline='', encoding='utf-8') as f:
                    writer = csv.writer(f)
                    writer.writerow(["DB ID", "Session Name", "Job Index", "Video Title", "Scene Index", "Prompt", "Status", "Finished At", "Download Path", "Caption"])
                    for row in rows:
                        writer.writerow([
                            row['id'], row['session_name'], row['job_index'], row['video_title'], row['scene_index'],
                            row['prompt'], row['status'], row['finished_at'], row['download_path'], row['caption']
                        ])
                self._log(f"Exported {len(rows)} jobs to CSV: {Path(filepath).name}")
                QMessageBox.information(self, "Success", "Export completed successfully.")
            except Exception as e:
                QMessageBox.critical(self, "Error", f"Failed to export CSV: {e}")

    # Video Converter Tab
    def _pick_conv_input(self):
        mode = self.combo_conv_mode.currentText()
        if mode == "Single Video":
            filepath, _ = QFileDialog.getOpenFileName(self, "Select Video File", str(Path.home() / "Downloads"), "Video Files (*.mp4 *.mkv *.mov *.avi)")
            if filepath:
                self.lbl_conv_input.setText(filepath)
        else:
            folder = QFileDialog.getExistingDirectory(self, "Select Input Folder")
            if folder:
                self.lbl_conv_input.setText(folder)

    def _pick_conv_output(self):
        folder = QFileDialog.getExistingDirectory(self, "Select Output Folder")
        if folder:
            self.lbl_conv_output.setText(folder)

    def _start_conversion(self):
        in_str = self.lbl_conv_input.text()
        out_str = self.lbl_conv_output.text()
        if in_str == "No input selected" or not in_str:
            QMessageBox.warning(self, "Error", "Please select a valid input.")
            return
        if out_str == "No output selected" or not out_str:
            QMessageBox.warning(self, "Error", "Please select a valid output folder.")
            return

        method = self.combo_conv_method.currentText()
        coords = (
            self.spin_blur_x.value(),
            self.spin_blur_y.value(),
            self.spin_blur_w.value(),
            self.spin_blur_h.value()
        )
        crop_px = self.spin_crop_px.value()
        threads = self.spin_conv_threads.value()

        # Build list of input Path objects
        input_paths = []
        if self.combo_conv_mode.currentText() == "Single Video":
            if Path(in_str).is_file():
                input_paths.append(Path(in_str))
        else:
            p = Path(in_str)
            if p.is_dir():
                for ext in ["*.mp4", "*.mkv", "*.mov", "*.avi"]:
                    input_paths.extend(p.glob(ext))

        if not input_paths:
            QMessageBox.warning(self, "Error", "No videos found in selected folder/path.")
            return

        self.btn_conv_start.setEnabled(False)
        self.conv_progress.setValue(0)
        self.conv_log.clear()

        self.conv_worker = ConverterWorker(
            input_paths=input_paths,
            output_dir=Path(out_str),
            method=method,
            blur_coords=coords,
            crop_pixels=crop_px,
            max_threads=threads
        )
        self.conv_worker.progress.connect(self.conv_progress.setValue)
        self.conv_worker.log.connect(self.conv_log.appendPlainText)
        self.conv_worker.finished_batch.connect(self._conv_finished)
        self.conv_worker.start()

    def _conv_finished(self):
        self.btn_conv_start.setEnabled(True)
        self._send_notification("Watermark Removal Complete", "Process finished successfully.")
        QMessageBox.information(self, "Finished", "Conversion process finished successfully.")

    # Video Merger Tab operations
    def _add_merge_files(self):
        files, _ = QFileDialog.getOpenFileNames(
            self, "Select Video Files to Merge", str(Path.home() / "Downloads"), "Video Files (*.mp4 *.avi *.mkv *.mov *.webm)"
        )
        if files:
            for f in files:
                self.list_merge_files.addItem(f)
                
    def _remove_merge_file(self):
        selected = self.list_merge_files.selectedItems()
        if selected:
            for item in selected:
                self.list_merge_files.takeItem(self.list_merge_files.row(item))
                
    def _clear_merge_files(self):
        self.list_merge_files.clear()
        
    def _move_merge_file_up(self):
        row = self.list_merge_files.currentRow()
        if row > 0:
            item = self.list_merge_files.takeItem(row)
            self.list_merge_files.insertItem(row - 1, item)
            self.list_merge_files.setCurrentRow(row - 1)
            
    def _move_merge_file_down(self):
        row = self.list_merge_files.currentRow()
        if row < self.list_merge_files.count() - 1 and row >= 0:
            item = self.list_merge_files.takeItem(row)
            self.list_merge_files.insertItem(row + 1, item)
            self.list_merge_files.setCurrentRow(row + 1)
            
    def _pick_merge_output(self):
        f, _ = QFileDialog.getSaveFileName(
            self, "Select Merged Output Video Path", str(Path.home() / "Downloads"), "Video Files (*.mp4)"
        )
        if f:
            if not f.endswith(".mp4"):
                f += ".mp4"
            self.lbl_merge_output.setText(f)
            
    def _start_merging(self):
        count = self.list_merge_files.count()
        if count < 2:
            QMessageBox.warning(self, "Invalid Request", "Please add at least two videos to merge.")
            return
            
        out_path = self.lbl_merge_output.text()
        if out_path == "No output selected":
            QMessageBox.warning(self, "Invalid Request", "Please select an output file destination.")
            return
            
        input_paths = [self.list_merge_files.item(i).text() for i in range(count)]
        
        self.btn_merge_start.setEnabled(False)
        self.merger_progress.setValue(0)
        self.merger_log.clear()
        
        self.merger_worker = MergerWorker(input_paths, out_path)
        self.merger_worker.log.connect(self.merger_log.appendPlainText)
        self.merger_worker.progress.connect(self.merger_progress.setValue)
        self.merger_worker.finished.connect(self._on_merge_finished)
        self.merger_worker.start()
        
    def _on_merge_finished(self, success: bool, msg: str):
        self.btn_merge_start.setEnabled(True)
        if success:
            self.merger_progress.setValue(100)
            self._send_notification("Video Merger Complete", "Merge completed successfully.")
            QMessageBox.information(self, "Success", f"Merge Completed!\nVideo saved to:\n{self.lbl_merge_output.text()}")
        else:
            QMessageBox.critical(self, "Failed", f"Merge failed: {msg}")

    # dialog triggers
    def _show_instructions_dialog(self):
        dlg = InstructionsDialog(self)
        dlg.exec()

    def _show_conv_help_dialog(self):
        dlg = WatermarkHelpDialog(self)
        dlg.exec()

    def _show_merge_help_dialog(self):
        dlg = MergerHelpDialog(self)
        dlg.exec()

    def _show_issues_dialog(self):
        dlg = IssuesDialog(self)
        dlg.exec()

    def _show_support_dialog(self):
        dlg = SupportDialog(self)
        dlg.exec()

    def _open_premium_whatsapp(self):
        msg = urllib.parse.quote("Hi! I'm interested in purchasing the premium license for GrowSnap AI.")
        webbrowser.open(f"https://wa.me/923138694809?text={msg}")

    def _manual_update_check(self):
        self._log("Checking for updates online...")
        from grow_snap_dola.main import APP_VERSION
        from dola_automation.updater import check_for_updates
        
        has_update, update_data, error_msg = check_for_updates(APP_VERSION)
        if error_msg:
            self._log(f"Update check failed: {error_msg}")
            QMessageBox.warning(
                self,
                "Update Check Error",
                f"Unable to contact update server. Please check your internet connection or verify the URL config.\n\n(Error: {error_msg})"
            )
        elif has_update:
            self._log(f"New update found: Version {update_data.get('version')}")
            from dola_automation.info_dialogs import UpdateDialog
            dialog = UpdateDialog(update_data, is_mandatory=update_data.get("mandatory", False), parent=self)
            dialog.exec()
        else:
            self._log("You are running the latest version.")
            QMessageBox.information(self, "Update Check", f"You are running the latest version: GrowSnap Creative Suite v{APP_VERSION}")

    def closeEvent(self, event):
        self._save_json_backup()
        if self.runner and self.runner.isRunning():
            self.runner.stop()
            self.runner.wait()
        event.accept()

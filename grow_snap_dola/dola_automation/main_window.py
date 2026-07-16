import sys
import os
import subprocess
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
    QApplication, QSystemTrayIcon, QButtonGroup, QStackedWidget, QStylePainter, QStyleOptionComboBox, QStyle,
    QInputDialog, QScrollArea
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal, pyqtSlot, QTimer, QTime, QElapsedTimer, QPoint, QEvent
from PyQt6.QtGui import QColor, QCursor, QAction, QKeySequence, QShortcut, QIcon, QPalette, QStandardItemModel, QStandardItem

# Import our automation modules
from dola_automation.models import AutomationSettings, PromptJob, JobStatus, parse_prompts, align_reference_images
from dola_automation.database import HistoryDatabase
from dola_automation.browser_worker import DolaBrowserWorker, DolaAutomationError
from dola_automation.ffmpeg_utils import process_video_watermark, concatenate_videos, ConverterWorker, MergerWorker, get_video_duration, get_ffmpeg_path, get_video_resolution
from dola_automation.styles import APP_STYLE, STATUS_COLORS, GradientLabel
from dola_automation.info_dialogs import InstructionsDialog, IssuesDialog, SupportDialog, ThreadsWarningDialog, WatermarkHelpDialog, MergerHelpDialog
from dola_automation.hook_factory import ViralHookFactoryWidget, ProfileOutliersWidget, HookLibraryWidget
from dola_automation.logger import logger
from dola_automation.telemetry import TelemetryTracker

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
                # Toggle check state
                current = item.data(Qt.ItemDataRole.CheckStateRole)
                new_state = Qt.CheckState.Unchecked if current == Qt.CheckState.Checked else Qt.CheckState.Checked
                item.setData(new_state, Qt.ItemDataRole.CheckStateRole)
                
                # Special behavior for "All Statuses"
                if item.text() == "All Statuses":
                    self.model().blockSignals(True)
                    for i in range(1, self.count()):
                        sibling = self.model().item(i)
                        if sibling:
                            sibling.setData(new_state, Qt.ItemDataRole.CheckStateRole)
                    self.model().blockSignals(False)
                    self.checkedItemsChanged.emit()
                else:
                    # If any individual status is unchecked, "All Statuses" should be unchecked
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
        
        checked = self.get_checked_items()
        if not checked:
            opt.currentText = "Select Status..."
        elif len(checked) == self.count():
            opt.currentText = "All Statuses"
        else:
            opt.currentText = ", ".join(item.capitalize() for item in checked)
            
        painter.drawComplexControl(QStyle.ComplexControl.CC_ComboBox, opt)
        painter.drawControl(QStyle.ControlElement.CE_ComboBoxLabel, opt)


class BatchRunner(QThread):
    job_progress = pyqtSignal(int, str)  # job_index, message
    chat_created = pyqtSignal(int, str)  # job_index, chat_url
    job_finished = pyqtSignal(int, bool, str, str)  # job_index, success, download_path, error
    batch_finished = pyqtSignal()
    profile_rotated = pyqtSignal(str)

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
        if job.chat_url and self.mode != "submit_only":
            worker_mode = "download_only"
        else:
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
                
            # Calculate relative video index for jobs sharing the same chat_url and video_title
            video_idx = 0
            if job.chat_url:
                try:
                    shared_jobs = self.db.get_jobs_by_chat_url(job.chat_url)
                    # Filter by video_title to avoid mixing up unrelated jobs that got mapped due to the historical redirect recovery bug
                    shared_jobs = [j for j in shared_jobs if j.video_title == job.video_title]
                    job_ids = [j.job_id for j in shared_jobs]
                    if job.job_id in job_ids:
                        video_idx = job_ids.index(job.job_id)
                        logger.info(f"Job #{job.index} (ID: {job.job_id}) has video index {video_idx} in shared chat {job.chat_url} (filtered by video title)")
                except Exception as e:
                    logger.warning(f"Could not calculate video index: {e}")
                    
            try:
                success = worker.run_job(job, mode=worker_mode, video_index=video_idx)
                if success:
                    break
                else:
                    error_msg = job.error or "Execution failed"
            except Exception as e:
                error_msg = str(e)
                logger.error(f"Error executing job #{job.index} (attempt {retries+1}): {e}", exc_info=True)
                
            if not success and thread_settings.auto_rotate_profiles:
                lower_err = error_msg.lower()
                if "limit exceeded" in lower_err or "quota reached" in lower_err or "out of credits" in lower_err or "points" in lower_err or "country switch" in lower_err:
                    profiles = []
                    if thread_settings.profile_list_str:
                        profiles = [p.strip() for p in thread_settings.profile_list_str.split(',') if p.strip()]
                    if not profiles:
                        profiles_dir = Path.home() / 'Documents' / 'dola_video_automation' / 'profiles'
                        if profiles_dir.exists():
                            profiles = sorted([d.name for d in profiles_dir.iterdir() if d.is_dir()])
                    
                    if profiles and thread_settings.active_profile_name in profiles:
                        curr_idx = profiles.index(thread_settings.active_profile_name)
                        next_idx = (curr_idx + 1) % len(profiles)
                        next_profile = profiles[next_idx]
                        logger.info(f"Credit Limit Hit! Auto-rotating Dola profile: {thread_settings.active_profile_name} -> {next_profile}")
                        thread_settings.active_profile_name = next_profile
                        self.profile_rotated.emit(next_profile)
                        self.job_progress.emit(job.index, f"Quota exceeded. Auto-rotated to profile: {next_profile}")
                
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
            self.setWindowTitle(f"GrowSnap One — User: {email} | Plan: {plan} ({days_left} days left)")
        else:
            self.setWindowTitle("GrowSnap One")
            
        self.resize(1280, 680)
        
        # Set window icon
        icon_path = get_resource_path("resources/icon.png")
        if icon_path.exists():
            self.setWindowIcon(QIcon(str(icon_path)))
        self.setStyleSheet(APP_STYLE)

        # Setup paths & storage
        self.download_dir = Path.home() / 'Documents' / 'dola_downloads'
        self.db_path = Path.home() / 'Documents' / 'dola_video_automation' / 'history.db'
        self.db = HistoryDatabase(self.db_path)
        from dola_automation.reach_snap import init_reach_snap_db
        init_reach_snap_db(self.db_path)
        self.backup_path = Path.home() / 'Documents' / 'dola_video_automation' / 'grow_snap_backup.json'
        
        self.jobs: List[PromptJob] = []
        self.reference_paths: List[Path] = []
        self.current_session_id: Optional[int] = None
        self.runner: Optional[BatchRunner] = None
        self.settings = AutomationSettings()
        self.telemetry = TelemetryTracker(enabled=True)
        
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
        self.shown_popups = {}

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
        self._refresh_profile_list()
        self._enforce_license_limits()
        self._refresh_history()
        self._refresh_lifetime_history()
        self._update_stats()

    def _toggle_creative_panel(self):
        visible = not self.panel_creative.isVisible()
        self.panel_creative.setVisible(visible)
        arrow = "▼" if visible else "▶"
        self.btn_cat_creative.setText(f"CreativeSnap {arrow}")
        self._on_nav_changed(11)

    def _toggle_reach_panel(self):
        visible = not self.panel_reach.isVisible()
        self.panel_reach.setVisible(visible)
        arrow = "▼" if visible else "▶"
        self.btn_cat_reach.setText(f"ReachSnap {arrow}")
        self._on_nav_changed(12)

    def _build_ui(self):
        central_widget = QWidget(self)
        self.setCentralWidget(central_widget)
        
        # Main horizontal layout to split Sidebar and Content
        root_layout = QHBoxLayout(central_widget)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        # 1. Left Sidebar
        sidebar_widget = QWidget(self)
        sidebar_widget.setObjectName("sidebar")
        sidebar_widget.setFixedWidth(240)
        sidebar_layout = QVBoxLayout(sidebar_widget)
        sidebar_layout.setContentsMargins(15, 20, 15, 20)
        sidebar_layout.setSpacing(10)

        # Clickable header container containing Logo + Text
        self.home_header_widget = QWidget(self)
        self.home_header_widget.setCursor(Qt.CursorShape.PointingHandCursor)
        self.home_header_widget.mousePressEvent = lambda event: self._on_nav_changed(10)
        self.home_header_widget.setStyleSheet("background: transparent; border: none;")
        
        header_layout = QHBoxLayout(self.home_header_widget)
        header_layout.setContentsMargins(10, 5, 10, 5)
        header_layout.setSpacing(10)
        
        # Logo label
        self.lbl_home_logo_icon = QLabel(self)
        self.lbl_home_logo_icon.setStyleSheet("border: none; background: transparent;")
        icon_path = get_resource_path("resources/icon.png")
        if icon_path.exists():
            from PyQt6.QtGui import QPixmap
            pixmap = QPixmap(str(icon_path))
            self.lbl_home_logo_icon.setPixmap(pixmap.scaled(32, 32, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
        header_layout.addWidget(self.lbl_home_logo_icon)
        
        # Title label
        self.lbl_home_logo_text = QLabel("GrowSnap One", self)
        self.lbl_home_logo_text.setStyleSheet("font-family: 'Orbitron'; font-size: 18px; font-weight: 900; color: #ffffff; border: none; background: transparent; padding: 0px; margin: 0px;")
        
        # Add glowing neon-green shadow effect to the text label
        try:
            from PyQt6.QtWidgets import QGraphicsDropShadowEffect
            from PyQt6.QtGui import QColor
            glow = QGraphicsDropShadowEffect()
            glow.setBlurRadius(20)
            glow.setColor(QColor("#2ecc71"))
            glow.setOffset(0, 0)
            self.lbl_home_logo_text.setGraphicsEffect(glow)
        except Exception:
            pass
            
        header_layout.addWidget(self.lbl_home_logo_text)
        header_layout.addStretch()
        
        sidebar_layout.addWidget(self.home_header_widget)

        # Accordion: CreativeSnap
        self.btn_cat_creative = QPushButton("CreativeSnap ▼", self)
        self.btn_cat_creative.setObjectName("category_header")
        self.btn_cat_creative.clicked.connect(self._toggle_creative_panel)
        sidebar_layout.addWidget(self.btn_cat_creative)

        self.panel_creative = QWidget(self)
        panel_creative_layout = QVBoxLayout(self.panel_creative)
        panel_creative_layout.setContentsMargins(0, 0, 0, 0)
        panel_creative_layout.setSpacing(5)

        self.btn_nav_platform_automator = QPushButton("AI Platform Automator", self)
        self.btn_nav_platform_automator.setCheckable(True)
        self.btn_nav_platform_automator.setChecked(True)
        self.btn_nav_platform_automator.setObjectName("sub_nav_button")

        self.btn_nav_dola = QPushButton("Dola Video Automation", self)
        self.btn_nav_dola.setCheckable(True)
        self.btn_nav_dola.setObjectName("sub_nav_button")

        self.btn_nav_converter = QPushButton("Watermark Removal", self)
        self.btn_nav_converter.setCheckable(True)
        self.btn_nav_converter.setObjectName("sub_nav_button")

        self.btn_nav_merger = QPushButton("Video Merger", self)
        self.btn_nav_merger.setCheckable(True)
        self.btn_nav_merger.setObjectName("sub_nav_button")

        self.btn_nav_hook_factory = QPushButton("Viral Hook Factory", self)
        self.btn_nav_hook_factory.setCheckable(True)
        self.btn_nav_hook_factory.setObjectName("sub_nav_button")

        self.btn_nav_profile_outliers = QPushButton("Profile Outliers", self)
        self.btn_nav_profile_outliers.setCheckable(True)
        self.btn_nav_profile_outliers.setObjectName("sub_nav_button")

        self.btn_nav_hook_library = QPushButton("Hook Library", self)
        self.btn_nav_hook_library.setCheckable(True)
        self.btn_nav_hook_library.setObjectName("sub_nav_button")

        self.btn_nav_voice_cloner = QPushButton("Voice Cloner & TTS", self)
        self.btn_nav_voice_cloner.setCheckable(True)
        self.btn_nav_voice_cloner.setObjectName("sub_nav_button")

        self.btn_nav_script_to_video = QPushButton("Script-to-Video Agent", self)
        self.btn_nav_script_to_video.setCheckable(True)
        self.btn_nav_script_to_video.setObjectName("sub_nav_button")

        self.btn_nav_snapgen = QPushButton("SnapGen AI", self)
        self.btn_nav_snapgen.setCheckable(True)
        self.btn_nav_snapgen.setObjectName("sub_nav_button")

        self.btn_nav_easemate = QPushButton("Easemate AI", self)
        self.btn_nav_easemate.setCheckable(True)
        self.btn_nav_easemate.setObjectName("sub_nav_button")

        self.btn_nav_opencut = QPushButton("Video Editor", self)
        self.btn_nav_opencut.setCheckable(True)
        self.btn_nav_opencut.setObjectName("sub_nav_button")

        self.btn_nav_clipper = QPushButton("AI Video Clipper", self)
        self.btn_nav_clipper.setCheckable(True)
        self.btn_nav_clipper.setObjectName("sub_nav_button")

        self.btn_nav_showcase = QPushButton("Showcase & Prompts", self)
        self.btn_nav_showcase.setCheckable(True)
        self.btn_nav_showcase.setObjectName("sub_nav_button")

        self.btn_nav_sandbox = QPushButton("AI Models Sandbox", self)
        self.btn_nav_sandbox.setCheckable(True)
        self.btn_nav_sandbox.setObjectName("sub_nav_button")

        panel_creative_layout.addWidget(self.btn_nav_platform_automator)
        panel_creative_layout.addWidget(self.btn_nav_dola)
        panel_creative_layout.addWidget(self.btn_nav_snapgen)
        panel_creative_layout.addWidget(self.btn_nav_easemate)
        panel_creative_layout.addWidget(self.btn_nav_converter)
        panel_creative_layout.addWidget(self.btn_nav_merger)
        panel_creative_layout.addWidget(self.btn_nav_hook_factory)
        panel_creative_layout.addWidget(self.btn_nav_profile_outliers)
        panel_creative_layout.addWidget(self.btn_nav_hook_library)
        panel_creative_layout.addWidget(self.btn_nav_voice_cloner)
        panel_creative_layout.addWidget(self.btn_nav_script_to_video)
        panel_creative_layout.addWidget(self.btn_nav_opencut)
        panel_creative_layout.addWidget(self.btn_nav_clipper)
        panel_creative_layout.addWidget(self.btn_nav_showcase)
        panel_creative_layout.addWidget(self.btn_nav_sandbox)
        sidebar_layout.addWidget(self.panel_creative)

        # Accordion: ReachSnap
        self.btn_cat_reach = QPushButton("ReachSnap ▶", self)
        self.btn_cat_reach.setObjectName("category_header")
        self.btn_cat_reach.clicked.connect(self._toggle_reach_panel)
        sidebar_layout.addWidget(self.btn_cat_reach)

        self.panel_reach = QWidget(self)
        self.panel_reach.setVisible(False)  # Collapsed by default
        panel_reach_layout = QVBoxLayout(self.panel_reach)
        panel_reach_layout.setContentsMargins(0, 0, 0, 0)
        panel_reach_layout.setSpacing(5)

        self.btn_nav_sms = QPushButton("SMS Gateway (httpSMS)", self)
        self.btn_nav_sms.setCheckable(True)
        self.btn_nav_sms.setObjectName("sub_nav_button")

        self.btn_nav_whatsapp = QPushButton("WhatsApp Automation", self)
        self.btn_nav_whatsapp.setCheckable(True)
        self.btn_nav_whatsapp.setObjectName("sub_nav_button")

        self.btn_nav_telephony = QPushButton("AI Voice Telephony", self)
        self.btn_nav_telephony.setCheckable(True)
        self.btn_nav_telephony.setObjectName("sub_nav_button")

        self.btn_nav_gmaps_scraper = QPushButton("GMaps Leads Scraper", self)
        self.btn_nav_gmaps_scraper.setCheckable(True)
        self.btn_nav_gmaps_scraper.setObjectName("sub_nav_button")
        panel_reach_layout.addWidget(self.btn_nav_sms)
        panel_reach_layout.addWidget(self.btn_nav_whatsapp)
        panel_reach_layout.addWidget(self.btn_nav_telephony)
        panel_reach_layout.addWidget(self.btn_nav_gmaps_scraper)
        sidebar_layout.addWidget(self.panel_reach)

        sidebar_layout.addStretch()
        root_layout.addWidget(sidebar_widget)

        # 2. Right Content Panel
        right_panel = QWidget(self)
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(20, 5, 20, 15)
        right_layout.setSpacing(10)

        # Header Row
        header_layout = QHBoxLayout()
        header_layout.setContentsMargins(0, 0, 0, 0)
        header_layout.setSpacing(10)

        title_box = QVBoxLayout()
        title_box.setContentsMargins(0, 0, 0, 0)
        title_box.setSpacing(0)

        self.title_lbl = GradientLabel("AI Platform Automator", self)
        self.title_lbl.setObjectName("title")
        title_box.addWidget(self.title_lbl)
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
        
        try:
            from dola_automation.version import APP_VERSION
        except ImportError:
            APP_VERSION = "1.0.8"
        version_lbl = QLabel(f"V{APP_VERSION} PREMIUM", self)
        version_lbl.setObjectName("version_badge")
        header_layout.addWidget(version_lbl)

        btn_update_check = QPushButton("Check Updates", self)
        btn_update_check.clicked.connect(self._manual_update_check)
        header_layout.addWidget(btn_update_check)

        btn_refresh = QPushButton("Refresh (F5)", self)
        btn_refresh.clicked.connect(self._refresh_application)
        header_layout.addWidget(btn_refresh)

        right_layout.addLayout(header_layout)

        # Button groups mapping buttons to page indexes
        self.nav_group = QButtonGroup(self)
        self.nav_group.setExclusive(True)
        self.nav_group.addButton(self.btn_nav_platform_automator, 0)
        self.nav_group.addButton(self.btn_nav_dola, 1)
        self.nav_group.addButton(self.btn_nav_converter, 2)
        self.nav_group.addButton(self.btn_nav_merger, 3)
        self.nav_group.addButton(self.btn_nav_hook_factory, 4)
        self.nav_group.addButton(self.btn_nav_profile_outliers, 5)
        self.nav_group.addButton(self.btn_nav_hook_library, 6)
        self.nav_group.addButton(self.btn_nav_sms, 7)
        self.nav_group.addButton(self.btn_nav_whatsapp, 8)
        self.nav_group.addButton(self.btn_nav_telephony, 9)
        self.nav_group.addButton(self.btn_nav_voice_cloner, 13)
        self.nav_group.addButton(self.btn_nav_gmaps_scraper, 14)
        self.nav_group.addButton(self.btn_nav_script_to_video, 15)
        self.nav_group.addButton(self.btn_nav_snapgen, 16)
        self.nav_group.addButton(self.btn_nav_opencut, 17)
        self.nav_group.addButton(self.btn_nav_clipper, 18)
        self.nav_group.addButton(self.btn_nav_showcase, 19)
        self.nav_group.addButton(self.btn_nav_sandbox, 20)
        self.nav_group.addButton(self.btn_nav_easemate, 21)
        self.nav_group.idClicked.connect(self._on_nav_changed)

        # Stacked Widget Page Setup
        self.stacked_widget = QStackedWidget(self)
        right_layout.addWidget(self.stacked_widget, 1)
        root_layout.addWidget(right_panel)

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

        page_dola_layout.addLayout(stats_row)

        # 3. Main Splitter View
        splitter = QSplitter(Qt.Orientation.Horizontal, self)
        
        # Left Panel (Inputs & Controls) wrapped in QScrollArea
        left_scroll = QScrollArea(self)
        left_scroll.setWidgetResizable(True)
        left_scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")
        
        left = QWidget()
        left.setObjectName("left_panel_container")
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(15)

        # Import & Parsing card
        import_group = QGroupBox("PROMPT INGESTION")
        import_layout = QVBoxLayout(import_group)
        self.prompt_editor = QPlainTextEdit()
        self.prompt_editor.setPlaceholderText("Paste prompts here or load from a custom CSV / TXT file...")
        self.prompt_editor.textChanged.connect(self._save_json_backup)
        import_layout.addWidget(self.prompt_editor)

        path_row = QHBoxLayout()
        self.edit_file_path = QLineEdit()
        self.edit_file_path.setPlaceholderText("Paste CSV/TXT file path here...")
        self.btn_load_path = QPushButton("Load Path")
        self.btn_load_path.clicked.connect(self._load_prompt_from_path)
        path_row.addWidget(self.edit_file_path)
        path_row.addWidget(self.btn_load_path)
        import_layout.addLayout(path_row)

        btn_row = QHBoxLayout()
        self.btn_load_file = QPushButton("Load CSV/TXT")
        self.btn_load_file.clicked.connect(self._load_prompt_file)
        self.btn_parse = QPushButton("Parse prompts")
        self.btn_parse.clicked.connect(self._parse_prompts)
        btn_row.addWidget(self.btn_load_file)
        btn_row.addWidget(self.btn_parse)
        import_layout.addLayout(btn_row)
        left_layout.addWidget(import_group)

        # Reference image picker card
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
        settings_grid.addWidget(self.chk_auto_remove_watermark, 2, 0, 1, 2)

        self.chk_auto_delete_scene_clips = QCheckBox("Auto Delete Scene Clips", self)
        self.chk_auto_delete_scene_clips.setChecked(True)
        self.chk_auto_delete_scene_clips.stateChanged.connect(self._update_runner_settings)
        settings_grid.addWidget(self.chk_auto_delete_scene_clips, 2, 2, 1, 2)

        settings_grid.addWidget(QLabel("Threads", self), 3, 0)
        self.spin_threads = QSpinBox(self)
        self.spin_threads.setRange(1, 16)
        self.spin_threads.setValue(1)
        self.spin_threads.valueChanged.connect(self._on_threads_changed)
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
        self.combo_watermark_method.currentTextChanged.connect(self._on_left_watermark_method_changed)
        settings_grid.addWidget(self.combo_watermark_method, 7, 1)

        settings_grid.addWidget(QLabel("Model", self), 7, 2)
        self.combo_model = QComboBox(self)
        self.combo_model.addItems(["SeaDance 2.0 Fast", "SeaDance 2.0 Quality", "SeaDance 2.5 Quality", "SeaDance 2.5 Fast"])
        self.combo_model.currentTextChanged.connect(self._update_runner_settings)
        settings_grid.addWidget(self.combo_model, 7, 3)

        settings_grid.addWidget(QLabel("Watermark Preset", self), 8, 0)
        self.combo_watermark_preset = QComboBox(self)
        self.combo_watermark_preset.addItems(["Dola (SeaDance)", "HeyGen", "Runway (Gen-3)", "Luma (Dream Machine)", "Kling AI", "MiniMax (Hailuo)", "Pika", "NotebookLM", "Custom (Manual)"])
        self.combo_watermark_preset.currentTextChanged.connect(self._on_left_preset_changed)
        settings_grid.addWidget(self.combo_watermark_preset, 8, 1)

        settings_grid.addWidget(QLabel("Download Folder", self), 8, 2)
        h_lay_dl_dir = QHBoxLayout()
        self.btn_download_dir = QPushButton("Choose", self)
        self.btn_download_dir.clicked.connect(self._pick_download_dir)
        self.btn_open_download_dir = QPushButton("📂 Open", self)
        self.btn_open_download_dir.clicked.connect(lambda: self._open_folder_of_path(str(self.download_dir)))
        h_lay_dl_dir.addWidget(self.btn_download_dir)
        h_lay_dl_dir.addWidget(self.btn_open_download_dir)
        settings_grid.addLayout(h_lay_dl_dir, 8, 3)

        settings_grid.addWidget(QLabel("Download Dir:", self), 9, 0)
        self.lbl_download_dir_show = QLabel(str(self.download_dir.name), self)
        self.lbl_download_dir_show.setWordWrap(True)
        settings_grid.addWidget(self.lbl_download_dir_show, 9, 1, 1, 3)

        settings_grid.addWidget(QLabel("Success Phrase", self), 10, 0)
        self.edit_success_phrase = QLineEdit(self)
        self.edit_success_phrase.setPlaceholderText("Enter success confirmation phrase...")
        self.edit_success_phrase.textChanged.connect(self._update_runner_settings)
        settings_grid.addWidget(self.edit_success_phrase, 10, 1, 1, 3)

        self.chk_prepend_hook = QCheckBox("Prepend Viral Hook", self)
        self.chk_prepend_hook.stateChanged.connect(self._update_runner_settings)
        settings_grid.addWidget(self.chk_prepend_hook, 11, 0, 1, 2)

        settings_grid.addWidget(QLabel("Select Hook", self), 11, 2)
        self.combo_select_hook = QComboBox(self)
        self.combo_select_hook.currentIndexChanged.connect(self._update_runner_settings)
        settings_grid.addWidget(self.combo_select_hook, 11, 3)
        # Dola Profile settings UI group
        profile_group = QGroupBox("DOLA PROFILE ROTATION MANAGER", self)
        profile_layout = QGridLayout(profile_group)
        profile_layout.setSpacing(10)

        profile_layout.addWidget(QLabel("Active Profile:", self), 0, 0)
        self.combo_profiles = QComboBox(self)
        self.combo_profiles.setMinimumWidth(150)
        self.combo_profiles.currentTextChanged.connect(self._on_profile_changed)
        profile_layout.addWidget(self.combo_profiles, 0, 1)

        self.btn_new_profile = QPushButton("Create Profile", self)
        self.btn_new_profile.clicked.connect(self._create_new_profile)
        profile_layout.addWidget(self.btn_new_profile, 0, 2)

        self.btn_manual_login = QPushButton("🔑 Manual Login (headed)", self)
        self.btn_manual_login.clicked.connect(self._launch_manual_login)
        profile_layout.addWidget(self.btn_manual_login, 0, 3)

        self.chk_auto_rotate = QCheckBox("Auto-rotate profiles on out of credits", self)
        self.chk_auto_rotate.setChecked(False)
        self.chk_auto_rotate.stateChanged.connect(self._update_runner_settings)
        profile_layout.addWidget(self.chk_auto_rotate, 1, 0, 1, 4)
        
        self.profile_group = profile_group

        # Operational buttons
        run_row = QHBoxLayout()
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
        run_row.addWidget(self.btn_start)
        run_row.addWidget(self.btn_pause)
        run_row.addWidget(self.btn_stop)
        left_layout.addLayout(run_row)

        # Help dialog buttons
        help_row = QHBoxLayout()
        self.btn_instructions = QPushButton("Instructions")
        self.btn_instructions.clicked.connect(self._show_instructions_dialog)
        self.btn_issues = QPushButton("Issues/Fixes")
        self.btn_issues.clicked.connect(self._show_issues_dialog)
        self.btn_upgrade = QPushButton("Upgrade your plan")
        self.btn_upgrade.setObjectName("primary")
        self.btn_upgrade.clicked.connect(self._open_premium_whatsapp)
        help_row.addWidget(self.btn_instructions)
        help_row.addWidget(self.btn_issues)
        help_row.addWidget(self.btn_upgrade)
        left_layout.addLayout(help_row)

        left_scroll.setWidget(left)
        splitter.addWidget(left_scroll)

        # Right Panel (Tab Widgets for logs, lists, history - standalone sub-tabs)
        right = QTabWidget(self)

        # Tab 1: Current Batch
        tab_current = QWidget(self)
        tab_current_layout = QVBoxLayout(tab_current)
        
        # Excel-style Filter & Search Bar
        filter_bar = QHBoxLayout()
        filter_bar.setSpacing(10)
        
        lbl_filter_status = QLabel("Filter Status:", self)
        lbl_filter_status.setStyleSheet("font-weight: bold; color: #2ecc71;")
        filter_bar.addWidget(lbl_filter_status)
        
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
        
        self.edit_filter_text = QLineEdit(self)
        self.edit_filter_text.setPlaceholderText("Search rows by prompt, video title, status, error...")
        self.edit_filter_text.textChanged.connect(self._apply_table_filters)
        filter_bar.addWidget(self.edit_filter_text)
        
        self.btn_clear_filters = QPushButton("Clear Filters", self)
        self.btn_clear_filters.clicked.connect(self._clear_table_filters)
        filter_bar.addWidget(self.btn_clear_filters)
        
        tab_current_layout.addLayout(filter_bar)
        
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
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
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
        tab_settings_layout.addWidget(self.profile_group)
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

        # ─── PAGE 2: WATERMARK REMOVAL TOOL ──────────────────
        self.page_converter = QWidget(central_widget)
        page_conv_layout = QVBoxLayout(self.page_converter)
        page_conv_layout.setContentsMargins(0, 0, 0, 0)
        page_conv_layout.setSpacing(15)

        conv_header_layout = QHBoxLayout()
        lbl_conv_subtitle = QLabel("WATERMARK REMOVAL TOOL — Visually Lossless Watermark Blurring & Cropping", self)
        lbl_conv_subtitle.setObjectName("subtitle")
        self.btn_conv_help = QPushButton("Help / Instructions", self)
        self.btn_conv_help.setMinimumWidth(180)
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
        conv_grid.addWidget(self.lbl_conv_input, 0, 1, 1, 2)
        
        self.btn_open_conv_input = QPushButton("📂 Open", self)
        self.btn_open_conv_input.clicked.connect(lambda: self._open_folder_of_path(self.lbl_conv_input.text()))
        conv_grid.addWidget(self.btn_open_conv_input, 0, 3)

        self.btn_conv_output = QPushButton("Select Output Folder", self)
        self.btn_conv_output.clicked.connect(self._pick_conv_output)
        conv_grid.addWidget(self.btn_conv_output, 1, 0)
        self.lbl_conv_output = QLabel("No output selected", self)
        self.lbl_conv_output.setWordWrap(True)
        conv_grid.addWidget(self.lbl_conv_output, 1, 1, 1, 2)
        
        self.btn_open_conv_output = QPushButton("📂 Open", self)
        self.btn_open_conv_output.clicked.connect(lambda: self._open_folder_of_path(self.lbl_conv_output.text()))
        conv_grid.addWidget(self.btn_open_conv_output, 1, 3)

        conv_grid.addWidget(QLabel("Preset Platform", self), 2, 0)
        self.combo_conv_preset = QComboBox(self)
        self.combo_conv_preset.addItems(["Dola (SeaDance)", "HeyGen", "Runway (Gen-3)", "Luma (Dream Machine)", "Kling AI", "MiniMax (Hailuo)", "Pika", "NotebookLM", "Custom (Manual)"])
        self.combo_conv_preset.currentTextChanged.connect(self._on_conv_preset_changed)
        conv_grid.addWidget(self.combo_conv_preset, 2, 1, 1, 3)

        conv_grid.addWidget(QLabel("Mode", self), 3, 0)
        self.combo_conv_mode = QComboBox(self)
        self.combo_conv_mode.addItems(["Folder Batch", "Single Video"])
        conv_grid.addWidget(self.combo_conv_mode, 3, 1)

        conv_grid.addWidget(QLabel("Method", self), 3, 2)
        self.combo_conv_method = QComboBox(self)
        self.combo_conv_method.addItems(["Blur", "Crop"])
        self.combo_conv_method.currentTextChanged.connect(self._on_conv_method_changed)
        conv_grid.addWidget(self.combo_conv_method, 3, 3)

        conv_grid.addWidget(QLabel("Blur X:Y:W:H", self), 4, 0)
        blur_hlay = QHBoxLayout()
        self.spin_blur_x = QSpinBox(self)
        self.spin_blur_x.setRange(0, 4000)
        self.spin_blur_x.setValue(540)
        self.spin_blur_x.valueChanged.connect(self._on_manual_watermark_change)
        self.spin_blur_y = QSpinBox(self)
        self.spin_blur_y.setRange(0, 4000)
        self.spin_blur_y.setValue(1220)
        self.spin_blur_y.valueChanged.connect(self._on_manual_watermark_change)
        self.spin_blur_w = QSpinBox(self)
        self.spin_blur_w.setRange(0, 2000)
        self.spin_blur_w.setValue(170)
        self.spin_blur_w.valueChanged.connect(self._on_manual_watermark_change)
        self.spin_blur_h = QSpinBox(self)
        self.spin_blur_h.setRange(0, 2000)
        self.spin_blur_h.setValue(80)
        self.spin_blur_h.valueChanged.connect(self._on_manual_watermark_change)
        blur_hlay.addWidget(self.spin_blur_x)
        blur_hlay.addWidget(self.spin_blur_y)
        blur_hlay.addWidget(self.spin_blur_w)
        blur_hlay.addWidget(self.spin_blur_h)
        conv_grid.addLayout(blur_hlay, 4, 1, 1, 3)

        conv_grid.addWidget(QLabel("Crop Bottom Px", self), 5, 0)
        self.spin_crop_px = QSpinBox(self)
        self.spin_crop_px.setRange(0, 1000)
        self.spin_crop_px.setValue(80)
        self.spin_crop_px.valueChanged.connect(self._on_manual_watermark_change)
        conv_grid.addWidget(self.spin_crop_px, 5, 1)

        conv_grid.addWidget(QLabel("Threads", self), 5, 2)
        self.spin_conv_threads = QSpinBox(self)
        self.spin_conv_threads.setRange(1, 16)
        self.spin_conv_threads.setValue(4)
        conv_grid.addWidget(self.spin_conv_threads, 5, 3)

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

        # ─── PAGE 3: VIDEO MERGER ────────────────────────────
        self.page_merger = QWidget(central_widget)
        page_merge_layout = QVBoxLayout(self.page_merger)
        page_merge_layout.setContentsMargins(0, 0, 0, 0)
        page_merge_layout.setSpacing(15)

        merge_header_layout = QHBoxLayout()
        lbl_merge_subtitle = QLabel("VIDEO MERGER — Concatenate video segments losslessly", self)
        lbl_merge_subtitle.setObjectName("subtitle")
        self.btn_merge_help = QPushButton("Help / Instructions", self)
        self.btn_merge_help.setMinimumWidth(180)
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

        # ─── PAGE 4: VIRAL HOOK FACTORY ──────────────────────
        self.page_hook_factory = ViralHookFactoryWidget(self, self.db, self.settings)
        self.page_hook_factory.hook_saved_signal.connect(self._refresh_hooks_combobox)

        # ─── PAGE 4B: PROFILE OUTLIERS & PERFORMANCE ANALYZER ──
        self.page_profile_outliers = ProfileOutliersWidget(self, self.db, self.settings)
        self.page_profile_outliers.load_url_to_downloader.connect(self._on_load_url_to_downloader)

        # ─── PAGE 4C: SAVED HOOK LIBRARY & CATALOGUE ───────────
        self.page_hook_library = HookLibraryWidget(self, self.db, self.settings)
        self.page_hook_library.select_hook_for_merging.connect(self._on_select_hook_for_merging)
        
        # When a hook is cropped and saved in hook_factory, automatically refresh the hook library cards
        self.page_hook_factory.hook_saved_signal.connect(self.page_hook_library._load_saved_hooks)

        # ─── PAGE 5: PLATFORM AUTOMATOR ──────────────────────
        from dola_automation.platform_automator import PlatformAutomatorWidget
        self.page_platform_automator = PlatformAutomatorWidget(self)

        # Stacked Widget Page Ordering
        self.stacked_widget.addWidget(self.page_platform_automator) # Index 0
        self.stacked_widget.addWidget(self.page_dola)                # Index 1
        self.stacked_widget.addWidget(self.page_converter)           # Index 2
        self.stacked_widget.addWidget(self.page_merger)              # Index 3
        self.stacked_widget.addWidget(self.page_hook_factory)         # Index 4
        self.stacked_widget.addWidget(self.page_profile_outliers)     # Index 5
        self.stacked_widget.addWidget(self.page_hook_library)         # Index 6

        # ─── PAGE 5, 6, 7: REACHSNAP MODULES ──────────────────
        from dola_automation.reach_snap import SMSGatewayWidget, WhatsAppAutomationWidget, VoiceTelephonyWidget
        self.page_sms_gateway = SMSGatewayWidget(self, self.db_path, self.settings)
        self.page_whatsapp_automation = WhatsAppAutomationWidget(self, self.db_path, self.settings)
        self.page_voice_telephony = VoiceTelephonyWidget(self, self.db_path, self.settings)

        self.stacked_widget.addWidget(self.page_sms_gateway)         # Index 7
        self.stacked_widget.addWidget(self.page_whatsapp_automation) # Index 8
        self.stacked_widget.addWidget(self.page_voice_telephony)     # Index 9

        # ─── PAGE 8, 9, 10: CATEGORY OVERVIEW DASHBOARDS ──────
        from dola_automation.dashboards import MasterHomeDashboardWidget, CreativeDashboardWidget, ReachDashboardWidget
        self.page_master_dashboard = MasterHomeDashboardWidget(self, self.db_path)
        self.page_creative_dashboard = CreativeDashboardWidget(self, self.db_path)
        self.page_reach_dashboard = ReachDashboardWidget(self, self.db_path)

        self.stacked_widget.addWidget(self.page_master_dashboard)     # Index 10
        self.stacked_widget.addWidget(self.page_creative_dashboard)   # Index 11
        self.stacked_widget.addWidget(self.page_reach_dashboard)      # Index 12

        # ─── PHASE 2 PIPELINE PIPES ──────────────────────────
        from dola_automation.phase2_widgets import VoiceClonerWidget, GMapsScraperWidget, ScriptToVideoAgentWidget
        self.page_voice_cloner = VoiceClonerWidget(self, self.db_path, self.settings)
        self.page_gmaps_scraper = GMapsScraperWidget(self, self.db_path, self.settings)
        self.page_script_to_video = ScriptToVideoAgentWidget(self, self.db_path, self.settings)

        self.stacked_widget.addWidget(self.page_voice_cloner)         # Index 13
        self.stacked_widget.addWidget(self.page_gmaps_scraper)        # Index 14
        self.stacked_widget.addWidget(self.page_script_to_video)       # Index 15

        from dola_automation.new_tabs import (
            SnapGenAutomationWidget, OpenCutVideoEditorWidget, AIVideoClipperWidget,
            CommunityShowcaseWidget, AIModelsSandboxWidget
        )
        from dola_automation.easemate_automation import EasemateAIAutomationWidget

        self.page_snapgen = SnapGenAutomationWidget(self, self.db_path, self.settings)
        self.page_opencut = OpenCutVideoEditorWidget(self, self.db_path, self.settings)
        self.page_clipper = AIVideoClipperWidget(self, self.db_path, self.settings)
        self.page_showcase = CommunityShowcaseWidget(self, self.db_path, self.settings)
        self.page_sandbox = AIModelsSandboxWidget(self, self.db_path, self.settings)
        self.page_easemate = EasemateAIAutomationWidget(self, self.db_path, self.settings)

        self.stacked_widget.addWidget(self.page_snapgen)             # Index 16
        self.stacked_widget.addWidget(self.page_opencut)             # Index 17
        self.stacked_widget.addWidget(self.page_clipper)             # Index 18
        self.stacked_widget.addWidget(self.page_showcase)            # Index 19
        self.stacked_widget.addWidget(self.page_sandbox)             # Index 20
        self.stacked_widget.addWidget(self.page_easemate)            # Index 21

    def _on_nav_changed(self, button_id):
        self.stacked_widget.setCurrentIndex(button_id)
        
        titles = {
            0: "AI Platform Automator",
            1: "Dola Video Automation",
            2: "Watermark Removal",
            3: "Video Merger",
            4: "Viral Hook Factory",
            5: "Profile Outliers",
            6: "Hook Library",
            7: "SMS Gateway (httpSMS)",
            8: "WhatsApp Automation",
            9: "AI Voice Telephony",
            10: "GrowSnap One Dashboard",
            11: "CreativeSnap Dashboard",
            12: "ReachSnap Dashboard",
            13: "Voice Cloner & TTS Engine",
            15: "Autonomous Script-to-Video Agent",
            16: "SnapGen Video and Image Automation",
            17: "OpenCut Video Editor",
            18: "AI Video Clipper",
            19: "Community Showcase & Prompts",
            20: "AI Models Sandbox",
            21: "Easemate AI Image Generator"
        }
        if hasattr(self, 'title_lbl'):
            self.title_lbl.setText(titles.get(button_id, "GrowSnap One"))
            
        # Keep button states in sync
        if 10 <= button_id <= 12:
            self.nav_group.setExclusive(False)
            for btn in self.nav_group.buttons():
                btn.setChecked(False)
            self.nav_group.setExclusive(True)
        else:
            btn = self.nav_group.button(button_id)
            if btn:
                btn.setChecked(True)

        # Trigger tool-specific instructions popups on switch
        if button_id in [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 13, 14, 15, 16, 17, 18, 19, 20]:
            QTimer.singleShot(100, lambda: self._show_tool_popup_guide(button_id))

    def _on_load_url_to_downloader(self, url: str):
        # Switch to Viral Hook Factory (page index 4)
        self._on_nav_changed(4)
        # Select "Media Downloader" tab (Index 0) on the tab widget
        self.page_hook_factory.tabs.setCurrentIndex(0)
        # Set text to the input field
        self.page_hook_factory.edit_url.setText(url)
        # Log it to the downloader log console
        self.page_hook_factory.log_downloader.appendPlainText(f"Received outlier URL from Analyzer: {url}")

    def _on_select_hook_for_merging(self, file_path: str, hook_title: str):
        # Switch to Viral Hook Factory (page index 4)
        self._on_nav_changed(4)
        # Select "Hook Merger" tab (Index 2) on the tab widget
        self.page_hook_factory.tabs.setCurrentIndex(2)
        # Set text to the hook file path
        self.page_hook_factory.edit_merge_hook.setText(file_path)
        # Log it to the merger log console
        self.page_hook_factory.log_merger.appendPlainText(f"Selected hook '{hook_title}' from Saved Library.")

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
        s.auto_delete_scene_clips = self.chk_auto_delete_scene_clips.isChecked()
        s.watermark_method = self.combo_watermark_method.currentText()
        s.download_dir = self.download_dir
        s.auth_state_path = Path.home() / 'Documents' / 'dola_video_automation' / 'auth_state.json'
        s.generation_success_phrase = self.edit_success_phrase.text()
        s.prepend_viral_hook = self.chk_prepend_hook.isChecked()
        s.selected_hook_id = self.combo_select_hook.currentData() or -1
        s.active_profile_name = self.combo_profiles.currentText() or 'Default'
        s.auto_rotate_profiles = self.chk_auto_rotate.isChecked()
        
        # Coordinates from right page spinboxes and preset from combo
        s.watermark_blur_x = self.spin_blur_x.value()
        s.watermark_blur_y = self.spin_blur_y.value()
        s.watermark_blur_w = self.spin_blur_w.value()
        s.watermark_blur_h = self.spin_blur_h.value()
        s.watermark_crop_pixels = self.spin_crop_px.value()
        s.watermark_preset = self.combo_watermark_preset.currentText()
        return s

    def _update_runner_settings(self):
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
        self.settings.auto_delete_scene_clips = s.auto_delete_scene_clips
        self.settings.prepend_viral_hook = s.prepend_viral_hook
        self.settings.selected_hook_id = s.selected_hook_id
        self.settings.watermark_method = s.watermark_method
        self.settings.download_dir = s.download_dir
        self.settings.active_profile_name = s.active_profile_name
        self.settings.auto_rotate_profiles = s.auto_rotate_profiles
        
        if self.runner and self.runner.isRunning():
            self.runner.settings = self.settings
            
        self._save_json_backup()
        
    def _enforce_license_limits(self):
        from dola_automation.licensing import check_license_stored
    def _enforce_license_limits(self):
        from dola_automation.licensing import check_license_stored
        is_valid, lic_data = check_license_stored()
        plan_name = lic_data.get('plan', '1-Day Trial') if is_valid else '1-Day Trial'
        
        # Call Hook Factory plan tab enforcements
        if hasattr(self, 'page_hook_factory'):
            self.page_hook_factory.enforce_plan_limits(plan_name)
            
        if plan_name == '1-Day Trial':
            # 1-Day Trial limits (Thread limit = 1, CSV disabled, Watermark Auto-remove disabled)
            self.spin_threads.blockSignals(True)
            self.spin_threads.setValue(1)
            self.spin_threads.setRange(1, 1)
            self.spin_threads.setEnabled(False)
            self.spin_threads.setToolTip("Multi-threaded generation is disabled in the 1-Day Trial plan.")
            self.spin_threads.blockSignals(False)
            
            self.btn_load_file.setEnabled(False)
            self.btn_load_path.setEnabled(False)
            self.edit_file_path.setEnabled(False)
            self.btn_load_file.setToolTip("CSV bulk load is disabled in the 1-Day Trial plan.")
            
            self.chk_auto_remove_watermark.blockSignals(True)
            self.chk_auto_remove_watermark.setChecked(False)
            self.chk_auto_remove_watermark.setEnabled(False)
            self.chk_auto_remove_watermark.setToolTip("Auto-remove watermark is disabled in the 1-Day Trial plan.")
            self.chk_auto_remove_watermark.blockSignals(False)
            
            if hasattr(self, 'combo_conv_mode'):
                self.combo_conv_mode.setEnabled(False)
                
            self.chk_prepend_hook.blockSignals(True)
            self.chk_prepend_hook.setChecked(False)
            self.chk_prepend_hook.setEnabled(False)
            self.chk_prepend_hook.setToolTip("Prepend Hook is disabled in the 1-Day Trial plan.")
            self.chk_prepend_hook.blockSignals(False)
            
        elif plan_name == 'Creator Plan':
            # Creator Plan limits (Thread limit = 5, CSV enabled (500 max batch size), Watermark enabled, Prepend disabled)
            self.spin_threads.blockSignals(True)
            if self.spin_threads.value() > 5:
                self.spin_threads.setValue(5)
            self.spin_threads.setRange(1, 5)
            self.spin_threads.setEnabled(True)
            self.spin_threads.setToolTip("Creator Plan allows up to 5 concurrent threads. Upgrade to Studio Pro for up to 16 threads!")
            self.spin_threads.blockSignals(False)
            
            self.btn_load_file.setEnabled(True)
            self.btn_load_path.setEnabled(True)
            self.edit_file_path.setEnabled(True)
            self.btn_load_file.setToolTip("Import batch prompts from CSV or TXT file (Max 500 prompts per batch for Creator Plan)")
            
            self.chk_auto_remove_watermark.setEnabled(True)
            self.chk_auto_remove_watermark.setToolTip("Toggle auto-watermark removal on generated files")
            
            if hasattr(self, 'combo_conv_mode'):
                self.combo_conv_mode.setEnabled(True)
                self.combo_conv_mode.setToolTip("Select watermark removal processing mode")
                
            # Auto-prepend hook pipeline belongs to Studio Pro (disabled for Creator)
            self.chk_prepend_hook.blockSignals(True)
            self.chk_prepend_hook.setChecked(False)
            self.chk_prepend_hook.setEnabled(False)
            self.chk_prepend_hook.setToolTip("Auto-prepend viral hooks is a Studio Pro feature. Upgrade to unlock!")
            self.chk_prepend_hook.blockSignals(False)
            
        else:
            # Studio Pro (Unlimited features, up to 16 threads)
            self.spin_threads.setEnabled(True)
            self.spin_threads.setRange(1, 16)
            self.spin_threads.setToolTip("Set number of parallel browser threads (up to 16)")
            
            self.btn_load_file.setEnabled(True)
            self.btn_load_path.setEnabled(True)
            self.edit_file_path.setEnabled(True)
            self.btn_load_file.setToolTip("Import batch prompts from CSV or TXT file (Unlimited size)")
            
            self.chk_auto_remove_watermark.setEnabled(True)
            self.chk_auto_remove_watermark.setToolTip("Toggle auto-watermark removal on generated files")
            
            if hasattr(self, 'combo_conv_mode'):
                self.combo_conv_mode.setEnabled(True)
                self.combo_conv_mode.setToolTip("Select watermark removal processing mode")
                
            self.chk_prepend_hook.setEnabled(True)
            self.chk_prepend_hook.setToolTip("Prepend selected viral hook to the start of generated videos")

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
                'auto_delete_scene_clips': self.chk_auto_delete_scene_clips.isChecked(),
                'prepend_viral_hook': self.chk_prepend_hook.isChecked(),
                'selected_hook_id': self.combo_select_hook.currentData() or -1,
                'model': self.combo_model.currentText(),
                'duration': self.combo_duration.currentText(),
                'ratio': self.combo_ratio.currentText(),
                'launch_delay_sec': self.spin_launch_delay.value(),
                'paste_delay_sec': self.spin_paste_delay.value(),
                'generation_timeout_sec': self.spin_timeout.value(),
                'auto_download_delay': self.spin_auto_download_delay.value(),
                'watermark_method': self.combo_watermark_method.currentText(),
                'watermark_preset': self.combo_watermark_preset.currentText(),
                'watermark_blur_x': self.spin_blur_x.value(),
                'watermark_blur_y': self.spin_blur_y.value(),
                'watermark_blur_w': self.spin_blur_w.value(),
                'watermark_blur_h': self.spin_blur_h.value(),
                'watermark_crop_pixels': self.spin_crop_px.value(),
                'download_dir': str(self.download_dir),
                'generation_success_phrase': self.edit_success_phrase.text(),
                'active_profile_name': self.combo_profiles.currentText() or 'Default',
                'auto_rotate_profiles': self.chk_auto_rotate.isChecked()
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
            self.chk_auto_delete_scene_clips.setChecked(data.get('auto_delete_scene_clips', True))
            self.chk_prepend_hook.setChecked(data.get('prepend_viral_hook', False))
            self._refresh_hooks_combobox()
            selected_id = data.get('selected_hook_id', -1)
            idx = self.combo_select_hook.findData(selected_id)
            if idx >= 0:
                self.combo_select_hook.setCurrentIndex(idx)
            self.combo_model.setCurrentText(data.get('model', 'SeaDance 2.0 Fast'))
            self.combo_duration.setCurrentText(data.get('duration', '10s'))
            self.combo_ratio.setCurrentText(data.get('ratio', '9:16'))
            self.spin_launch_delay.setValue(data.get('launch_delay_sec', 5))
            self.spin_paste_delay.setValue(data.get('paste_delay_sec', 2))
            self.spin_timeout.setValue(data.get('generation_timeout_sec', 500))
            self.spin_auto_download_delay.setValue(data.get('auto_download_delay', 5))
            
            # Load watermark preset and coordinates
            preset = data.get('watermark_preset', 'Dola (SeaDance)')
            if preset == "Dola":
                preset = "Dola (SeaDance)"
            self.combo_watermark_preset.setCurrentText(preset)
            self.combo_conv_preset.setCurrentText(preset)
            
            # Restore method and spinboxes (Custom case is preserved)
            self.combo_watermark_method.setCurrentText(data.get('watermark_method', 'Blur'))
            self.combo_conv_method.setCurrentText(data.get('watermark_method', 'Blur'))
            self.spin_blur_x.setValue(data.get('watermark_blur_x', 540))
            self.spin_blur_y.setValue(data.get('watermark_blur_y', 1220))
            self.spin_blur_w.setValue(data.get('watermark_blur_w', 170))
            self.spin_blur_h.setValue(data.get('watermark_blur_h', 80))
            self.spin_crop_px.setValue(data.get('watermark_crop_pixels', 80))
            
            self.edit_success_phrase.setText(data.get('generation_success_phrase', 'will be generated using'))
            
            d_dir = data.get('download_dir', '')
            if d_dir:
                self.download_dir = Path(d_dir)
                self.lbl_download_dir_show.setText(self.download_dir.name)
            
            self.chk_auto_rotate.setChecked(data.get('auto_rotate_profiles', False))
            
            self.settings = self._collect_settings()
            self.settings.active_profile_name = data.get('active_profile_name', 'Default')
        except Exception as e:
            logger.warning(f"Failed to load backup: {e}")
        finally:
            self._is_loading_backup = False

    def _refresh_profile_list(self):
        profiles_dir = Path.home() / 'Documents' / 'dola_video_automation' / 'profiles'
        profiles_dir.mkdir(parents=True, exist_ok=True)
        default_dir = profiles_dir / 'Default'
        default_dir.mkdir(exist_ok=True)
        
        profiles = []
        for p in profiles_dir.iterdir():
            if p.is_dir():
                profiles.append(p.name)
        
        self.combo_profiles.blockSignals(True)
        self.combo_profiles.clear()
        self.combo_profiles.addItems(sorted(profiles))
        
        active = getattr(self.settings, 'active_profile_name', 'Default')
        if active in profiles:
            self.combo_profiles.setCurrentText(active)
        else:
            self.combo_profiles.setCurrentText('Default')
        self.combo_profiles.blockSignals(False)

    def _on_profile_changed(self, text):
        if not text:
            return
        self.settings.active_profile_name = text
        self._save_json_backup()
        self._log(f"Switched active Dola profile to: {text}")

    def _create_new_profile(self):
        name, ok = QInputDialog.getText(self, "Create Profile", "Enter new profile name:")
        if ok and name.strip():
            clean_name = "".join([c for c in name if c.isalnum() or c in (' ', '_', '-')]).strip()
            if not clean_name:
                QMessageBox.warning(self, "Error", "Invalid profile name.")
                return
            profiles_dir = Path.home() / 'Documents' / 'dola_video_automation' / 'profiles'
            new_profile_dir = profiles_dir / clean_name
            new_profile_dir.mkdir(parents=True, exist_ok=True)
            self._refresh_profile_list()
            self.combo_profiles.setCurrentText(clean_name)
            self._log(f"Created new Dola profile: {clean_name}")

    def _launch_manual_login(self):
        profile = self.combo_profiles.currentText()
        if not profile:
            QMessageBox.warning(self, "Warning", "Please select or create a profile first.")
            return
        
        profile_dir = Path.home() / 'Documents' / 'dola_video_automation' / 'profiles' / profile
        profile_dir.mkdir(parents=True, exist_ok=True)
        
        QMessageBox.information(
            self, 
            "Manual Login", 
            f"A headed browser will now open using profile '{profile}'.\n\n"
            "Please log in to Dola in the browser window, then close the browser to save your session."
        )
        
        def run_headed_browser():
            try:
                from playwright.sync_api import sync_playwright
                with sync_playwright() as p:
                    launch_args = ["--disable-blink-features=AutomationControlled"]
                    try:
                        # Attempt to use native Chrome browser to permit Google Login authentication
                        context = p.chromium.launch_persistent_context(
                            user_data_dir=str(profile_dir),
                            headless=False,
                            channel="chrome",
                            args=launch_args,
                            viewport={"width": 1280, "height": 800}
                        )
                    except Exception as ce:
                        logger.info(f"Failed to launch native Chrome channel, falling back to default Chromium: {ce}")
                        context = p.chromium.launch_persistent_context(
                            user_data_dir=str(profile_dir),
                            headless=False,
                            args=launch_args,
                            viewport={"width": 1280, "height": 800}
                        )
                    page = context.pages[0] if context.pages else context.new_page()
                    page.goto("https://dola.com")
                    while len(context.pages) > 0:
                        time.sleep(0.5)
            except Exception as e:
                logger.error(f"Error launching headed browser: {e}")
                
        threading.Thread(target=run_headed_browser, daemon=True).start()

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

        # Check plan limits (Creator Plan max 500 prompts per batch)
        from dola_automation.licensing import check_license_stored
        is_valid, lic_data = check_license_stored()
        plan_name = lic_data.get('plan', '1-Day Trial') if is_valid else '1-Day Trial'
        
        if plan_name in ['1-Day Trial', 'Creator Plan'] and len(parsed) > 500:
            QMessageBox.warning(
                self, 
                "Plan Batch Limit Reached", 
                "Creator Plan Limit: Ingestion of prompts is capped at 500 per batch.\n\n"
                "Your list has been truncated to the first 500 prompts. Please upgrade to Studio Pro for unlimited batch ingestion sizes!"
            )
            parsed = parsed[:500]

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
        self._apply_table_filters()

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
        self.runner.profile_rotated.connect(self._on_profile_rotated)
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

    @pyqtSlot(str)
    def _on_profile_rotated(self, next_profile):
        self._log(f"[Auto-Rotation] Switching active Dola profile to: {next_profile}")
        self.combo_profiles.setCurrentText(next_profile)
        self.settings.active_profile_name = next_profile

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
            
            temp_hook_path = None
            if self.settings.prepend_viral_hook:
                hook_path_str = None
                hook_id = self.settings.selected_hook_id
                try:
                    hooks = self.db.list_viral_hooks()
                    if hooks:
                        if hook_id == -1: # Random Hook
                            import random
                            selected_hook = random.choice(hooks)
                            hook_path_str = selected_hook['file_path']
                        elif hook_id == -2: # Most Recent Hook
                            selected_hook = hooks[0]
                            hook_path_str = selected_hook['file_path']
                        else:
                            selected_hook = next((h for h in hooks if h['id'] == hook_id), None)
                            if selected_hook:
                                hook_path_str = selected_hook['file_path']
                            else:
                                hook_path_str = hooks[0]['file_path']
                except Exception as e:
                    self._log(f"Failed to query hook library database: {e}")
                
                if hook_path_str and Path(hook_path_str).exists():
                    p_hook = Path(hook_path_str)
                    temp_hook_path = p_hook.parent / f"temp_align_{self._slug(video_title)}_{p_hook.name}"
                    self._log(f"Prepending Viral Hook: '{p_hook.name}'... Aligning parameters to match generated clip...")
                    if self._align_hook_to_video(p_hook, Path(input_paths[0]), temp_hook_path):
                        input_paths = [str(temp_hook_path)] + input_paths
                        self._log("Hook parameters aligned and prepended successfully.")
                    else:
                        self._log("Warning: Failed to align hook parameters. Attempting merge with original hook file.")
                        input_paths = [str(p_hook)] + input_paths
                else:
                    self._log("Warning: Prepend hook active but selected hook file was not found or database is empty.")

            self._log(f"All scenes for video '{video_title}' are downloaded. Expected duration: {sum_durations:.2f}s. Concatenating losslessly...")
            success = concatenate_videos(input_paths, str(output_path))
            
            # Clean up temporary aligned hook if generated
            if temp_hook_path and temp_hook_path.exists():
                try:
                    temp_hook_path.unlink()
                except Exception as e:
                    logger.error(f"Failed to delete temporary aligned hook file: {e}")
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
                
                # Gather unique captions from all scene files to merge/consolidate
                captions = []
                for p in input_paths:
                    p_obj = Path(p)
                    txt_file = p_obj.with_suffix('.txt')
                    if txt_file.exists():
                        try:
                            content = txt_file.read_text(encoding='utf-8').strip()
                            if content and content not in captions:
                                captions.append(content)
                        except Exception as e:
                            logger.error(f"Failed to read caption file '{txt_file}': {e}")
                
                # Fallback to job attributes if no file-level captions were found
                if not captions:
                    for j in title_jobs:
                        if j.caption and j.caption.strip() not in captions:
                            captions.append(j.caption.strip())
                
                merged_caption = "\n\n".join(captions)
                if merged_caption:
                    txt_path = output_path.with_suffix('.txt')
                    txt_path.write_text(merged_caption, encoding='utf-8')
                    self._log(f"Saved consolidated sidecar caption file: {txt_path.name}")
                
                # Send system notification
                notif_msg = f"Video merger and post-processing completed successfully for: '{video_title}'."
                self._send_notification("Video Merger Complete", notif_msg)
                
                # Perform auto-delete / clean up if setting is enabled
                if self.settings.auto_delete_scene_clips:
                    self._log("Auto Delete Scene Clips is enabled. Deleting individual raw scene clips and sidecar caption text files...")
                    del_count = 0
                    del_txt_count = 0
                    for p in input_paths:
                        p_obj = Path(p)
                        try:
                            if p_obj.exists():
                                p_obj.unlink()
                                del_count += 1
                        except Exception as e:
                            logger.error(f"Failed to delete individual raw clip '{p}': {e}")
                        
                        try:
                            txt_obj = p_obj.with_suffix('.txt')
                            if txt_obj.exists():
                                txt_obj.unlink()
                                del_txt_count += 1
                        except Exception as e:
                            logger.error(f"Failed to delete sidecar txt '{txt_obj}': {e}")
                    self._log(f"Auto Delete: Successfully deleted {del_count} individual raw scene clips and {del_txt_count} sidecar caption text files.")
                else:
                    self._log("Auto Delete Scene Clips is disabled. Kept individual raw scene clips and sidecar caption text files in the downloads folder.")
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

        # Report watermark removal to telemetry
        self.telemetry.report_watermark_job(method=method, files_count=len(input_paths))

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

        # Report video merger operation to telemetry
        self.telemetry.report_merger_job(files_count=len(input_paths))
        
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

    def _on_threads_changed(self, value):
        if not getattr(self, '_is_loading_backup', False):
            if value > 1 and not getattr(self, '_threads_warning_confirmed', False):
                self._trigger_threads_warning()
        self._update_runner_settings()

    def _apply_table_filters(self):
        # Prevent crash if widgets are not initialized yet
        if not hasattr(self, 'combo_filter_status') or not hasattr(self, 'edit_filter_text'):
            return
            
        checked_statuses = self.combo_filter_status.get_checked_items()
        search_text = self.edit_filter_text.text().strip().lower()
        
        for row in range(self.table.rowCount()):
            # Get values
            status_item = self.table.item(row, 5) # column 5 is Status
            status_val = status_item.text().lower() if status_item else ""
            
            title_item = self.table.item(row, 1) # column 1 is Video Title
            title_val = title_item.text().lower() if title_item else ""
            
            prompt_item = self.table.item(row, 3) # column 3 is Prompt
            prompt_val = prompt_item.text().lower() if prompt_item else ""
            
            err_item = self.table.item(row, 7) # column 7 is Error Details
            err_val = err_item.text().lower() if err_item else ""
            
            # Status check
            status_match = False
            if not checked_statuses:
                status_match = True
            elif "all statuses" in checked_statuses:
                status_match = True
            elif "has error details" in checked_statuses:
                status_match = (len(err_val) > 0 and err_val != "-") or (status_val in checked_statuses)
            else:
                status_match = (status_val in checked_statuses)
                
            # Search check
            search_match = True
            if search_text:
                search_match = (
                    search_text in title_val or 
                    search_text in prompt_val or 
                    search_text in err_val or
                    search_text in status_val
                )
                
            # Hide/show row
            self.table.setRowHidden(row, not (status_match and search_match))

    def _clear_table_filters(self):
        all_items = [
            "all statuses", 
            "pending", 
            "running", 
            "waiting", 
            "downloading", 
            "completed", 
            "submitted", 
            "failed", 
            "cancelled", 
            "has error details"
        ]
        self.combo_filter_status.set_checked_items(all_items)
        self.edit_filter_text.clear()
        self._apply_table_filters()

    def _on_conv_preset_changed(self, text):
        presets = {
            "Dola (SeaDance)": {
                "method": "Blur",
                "blur_x": 520,
                "blur_y": 1200,
                "blur_w": 190,
                "blur_h": 80,
                "crop_px": 90
            },
            "HeyGen": {
                "method": "Blur",
                "blur_x": 460,
                "blur_y": 1130,
                "blur_w": 250,
                "blur_h": 130,
                "crop_px": 150
            },
            "Runway (Gen-3)": {
                "method": "Blur",
                "blur_x": 480,
                "blur_y": 1160,
                "blur_w": 220,
                "blur_h": 110,
                "crop_px": 120
            },
            "Luma (Dream Machine)": {
                "method": "Blur",
                "blur_x": 500,
                "blur_y": 1180,
                "blur_w": 210,
                "blur_h": 95,
                "crop_px": 100
            },
            "Kling AI": {
                "method": "Blur",
                "blur_x": 520,
                "blur_y": 15,
                "blur_w": 190,
                "blur_h": 80,
                "crop_px": 100
            },
            "MiniMax (Hailuo)": {
                "method": "Blur",
                "blur_x": 480,
                "blur_y": 1160,
                "blur_w": 220,
                "blur_h": 110,
                "crop_px": 120
            },
            "Pika": {
                "method": "Blur",
                "blur_x": 500,
                "blur_y": 1180,
                "blur_w": 210,
                "blur_h": 90,
                "crop_px": 100
            },
            "NotebookLM": {
                "method": "Blur",
                "blur_x": 520,
                "blur_y": 1200,
                "blur_w": 180,
                "blur_h": 75,
                "crop_px": 80
            }
        }
        
        if text in presets:
            p = presets[text]
            # Block signals temporarily to prevent circular updates
            self.combo_conv_method.blockSignals(True)
            self.spin_blur_x.blockSignals(True)
            self.spin_blur_y.blockSignals(True)
            self.spin_blur_w.blockSignals(True)
            self.spin_blur_h.blockSignals(True)
            self.spin_crop_px.blockSignals(True)
            
            self.combo_conv_method.setCurrentText(p["method"])
            self.spin_blur_x.setValue(p["blur_x"])
            self.spin_blur_y.setValue(p["blur_y"])
            self.spin_blur_w.setValue(p["blur_w"])
            self.spin_blur_h.setValue(p["blur_h"])
            self.spin_crop_px.setValue(p["crop_px"])
            
            self.combo_conv_method.blockSignals(False)
            self.spin_blur_x.blockSignals(False)
            self.spin_blur_y.blockSignals(False)
            self.spin_blur_w.blockSignals(False)
            self.spin_blur_h.blockSignals(False)
            self.spin_crop_px.blockSignals(False)
            
            # Sync with Left panel watermark method
            if hasattr(self, 'combo_watermark_method'):
                self.combo_watermark_method.blockSignals(True)
                self.combo_watermark_method.setCurrentText(p["method"])
                self.combo_watermark_method.blockSignals(False)
                
            # Update Left preset selection if different
            if hasattr(self, 'combo_watermark_preset'):
                self.combo_watermark_preset.blockSignals(True)
                self.combo_watermark_preset.setCurrentText(text)
                self.combo_watermark_preset.blockSignals(False)
                
            self._update_runner_settings()

    def _on_conv_method_changed(self, text):
        if hasattr(self, 'combo_watermark_method'):
            self.combo_watermark_method.blockSignals(True)
            self.combo_watermark_method.setCurrentText(text)
            self.combo_watermark_method.blockSignals(False)
        self._on_manual_watermark_change()

    def _on_left_preset_changed(self, text):
        if hasattr(self, 'combo_conv_preset'):
            self.combo_conv_preset.blockSignals(True)
            self.combo_conv_preset.setCurrentText(text)
            self.combo_conv_preset.blockSignals(False)
        self._on_conv_preset_changed(text)

    def _on_left_watermark_method_changed(self, text):
        if hasattr(self, 'combo_conv_method'):
            self.combo_conv_method.blockSignals(True)
            self.combo_conv_method.setCurrentText(text)
            self.combo_conv_method.blockSignals(False)
        self._update_runner_settings()

    def _on_manual_watermark_change(self):
        # Change preset combo box to Custom if user adjusts coordinates manually
        if not self.spin_blur_x.signalsBlocked():
            if hasattr(self, 'combo_conv_preset'):
                self.combo_conv_preset.blockSignals(True)
                self.combo_conv_preset.setCurrentText("Custom (Manual)")
                self.combo_conv_preset.blockSignals(False)
            if hasattr(self, 'combo_watermark_preset'):
                self.combo_watermark_preset.blockSignals(True)
                self.combo_watermark_preset.setCurrentText("Custom (Manual)")
                self.combo_watermark_preset.blockSignals(False)
            self._update_runner_settings()

    def _open_premium_whatsapp(self):
        msg = urllib.parse.quote("Hi! I'm interested in purchasing the premium license for GrowSnap One.")
        webbrowser.open(f"https://wa.me/923138694809?text={msg}")

    def _manual_update_check(self):
        self._log("Checking for updates online...")
        try:
            from dola_automation.version import APP_VERSION
        except ImportError:
            APP_VERSION = "1.0.8"
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
            QMessageBox.information(self, "Update Check", f"You are running the latest version: GrowSnap One v{APP_VERSION}")

    def _align_hook_to_video(self, hook_path: Path, ref_path: Path, temp_path: Path) -> bool:
        ffmpeg_exe = get_ffmpeg_path()
        try:
            width, height = get_video_resolution(ref_path)
            
            # Check for audio
            probe_cmd = [str(ffmpeg_exe), '-i', str(hook_path)]
            creationflags = 0
            if os.name == 'nt':
                creationflags = subprocess.CREATE_NO_WINDOW
            probe_res = subprocess.run(
                probe_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                creationflags=creationflags
            )
            has_audio = 'audio' in probe_res.stderr.lower() or 'audio:' in probe_res.stderr.lower()
            
            cmd = [str(ffmpeg_exe), '-y', '-i', str(hook_path)]
            if not has_audio:
                cmd.extend(['-f', 'lavfi', '-i', 'anullsrc=channel_layout=stereo:sample_rate=44100'])
                
            cmd.extend([
                '-vf', f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},setsar=1",
                '-c:v', 'libx264',
                '-preset', 'fast',
                '-crf', '23',
                '-r', '30',
                '-c:a', 'aac',
                '-ar', '44100',
                '-ac', '2'
            ])
            if not has_audio:
                cmd.append('-shortest')
                
            cmd.append(str(temp_path))
            
            res = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                creationflags=creationflags
            )
            return res.returncode == 0
        except Exception as e:
            logger.error(f"Failed to align hook properties: {e}")
            return False

    def _refresh_hooks_combobox(self):
        if not hasattr(self, 'combo_select_hook'):
            return
        self.combo_select_hook.blockSignals(True)
        self.combo_select_hook.clear()
        self.combo_select_hook.addItem("Random Hook", -1)
        self.combo_select_hook.addItem("Most Recent Hook", -2)
        try:
            hooks = self.db.list_viral_hooks()
            for h in hooks:
                self.combo_select_hook.addItem(h['title'], h['id'])
        except Exception as e:
            logger.error(f"Failed to load hooks for combo box: {e}")
            
        selected_id = getattr(self.settings, 'selected_hook_id', -1)
        idx = self.combo_select_hook.findData(selected_id)
        if idx >= 0:
            self.combo_select_hook.setCurrentIndex(idx)
        else:
            self.combo_select_hook.setCurrentIndex(0)
        self.combo_select_hook.blockSignals(False)

    def _open_folder_of_path(self, path):
        path = path.strip()
        if path and path != "No input selected" and path != "No output selected":
            try:
                import platform
                import subprocess
                import os
                p = os.path.abspath(path)
                if not os.path.exists(p):
                    return
                if os.path.isfile(p):
                    p = os.path.dirname(p)
                system = platform.system()
                if system == 'Windows':
                    subprocess.run(['explorer', p])
                elif system == 'Darwin':
                    subprocess.run(['open', p])
                else:
                    subprocess.run(['xdg-open', p])
            except Exception as e:
                QMessageBox.warning(self, "Folder Error", f"Could not open folder: {e}")

    def _show_tool_popup_guide(self, button_id):
        guides = {
            0: (
                "AI Platform Automator Guide",
                "Configure your target accounts, channels, API settings, and manage automation campaign dispatches.\n\n"
                "Instructions:\n"
                "1. Connect social, email, or webhook channels.\n"
                "2. Configure scheduling parameters.\n"
                "3. Keep track of queue dispatches."
            ),
            1: (
                "Dola Video Automation Guide",
                "Generate high-fidelity vertical short videos automatically from text prompts using Dola SeaDance.\n\n"
                "Instructions:\n"
                "1. Enter your text description prompt in the editor.\n"
                "2. Set the resolution (e.g. 1080x1920) and style preset.\n"
                "3. Click 'Generate Video' to process it via the remote GPU engine."
            ),
            2: (
                "Watermark Removal Guide",
                "Automatically clean watermarks, logos, or overlay text from your videos.\n\n"
                "Instructions:\n"
                "1. Load your video file.\n"
                "2. Define the region coordinates containing the logo/watermark.\n"
                "3. Click 'Clean Video' to apply the inpainting blur filters."
            ),
            3: (
                "Video Merger Guide",
                "Combine multiple short clips or hooks into a single compiled sequence.\n\n"
                "Instructions:\n"
                "1. Add the short videos to the compile list.\n"
                "2. Drag or select sorting to define chronological order.\n"
                "3. Click 'Merge and Compile' to output a unified video."
            ),
            4: (
                "Viral Hook Factory Guide",
                "Analyze and slice viral hooks from downloaded public clips.\n\n"
                "Instructions:\n"
                "1. Enter a video link in the downloader tab to fetch locally.\n"
                "2. Use transcript-based slicing to capture hook segments.\n"
                "3. Save the sliced hooks to your local library."
            ),
            5: (
                "Profile Outliers Analyzer Guide",
                "Scan competitors' public profiles to detect viral outlier content.\n\n"
                "Instructions:\n"
                "1. Input the target creator profile handle.\n"
                "2. Click 'Analyze Outliers' to fetch view counts and statistics.\n"
                "3. Identify which topics received 3x-10x typical viewership."
            ),
            6: (
                "Hook Library Guide",
                "Manage and browse your saved repository of high-performing viral hooks.\n\n"
                "Instructions:\n"
                "1. View previously sliced hook videos and transcribed texts.\n"
                "2. Click 'Copy Text' or export directly to the merger tab."
            ),
            7: (
                "SMS Gateway (httpSMS) Guide",
                "Dispatch cold outreach campaigns via SMS using your Android phone.\n\n"
                "Instructions:\n"
                "1. Input your httpSMS API key in the connection box.\n"
                "2. Import a text/CSV file with list of lead numbers.\n"
                "3. Click 'Send Campaign' to begin dispatching."
            ),
            8: (
                "WhatsApp Automation Guide",
                "Send automated bulk messages to targeted lead groups using WhatsApp Web.\n\n"
                "Instructions:\n"
                "1. Click 'Link WhatsApp Session' to launch a headed browser.\n"
                "2. Scan the QR code using your phone to authenticate (the session will remain persistently saved!).\n"
                "3. Load lead contacts, enter template message, and click 'Send WhatsApp Campaign'."
            ),
            9: (
                "AI Voice Telephony Guide",
                "Launch custom outbound voice calls to cold leads using interactive AI voice agents.\n\n"
                "Instructions:\n"
                "1. Input your Twilio and Retell AI credentials.\n"
                "2. Choose the agent voice prompt personality.\n"
                "3. Schedule or trigger outbound calls to phone list."
            ),
            13: (
                "Voice Cloner & TTS Engine Guide",
                "Clone any voice or generate speech voiceovers offline using AI TTS engines.\n\n"
                "Instructions:\n"
                "1. Input the narration script you want the voice to speak.\n"
                "2. Pick the source voice preset, record mic, or upload WAV audio sample.\n"
                "3. Click 'Generate Speech Voiceover' to compile natural offline voice track."
            ),
            14: (
                "GMaps Leads Scraper Guide",
                "Extract B2B lead info from Google Maps local searches.\n\n"
                "Instructions:\n"
                "1. Enter search queries (e.g. 'Dentists New York').\n"
                "2. Specify limit count and click 'Start Scraping'.\n"
                "3. Export leads with name, phone, website, and address to CSV/TXT."
            ),
            15: (
                "Autonomous Script-to-Video Agent Guide",
                "Provide a single topic prompt, and let local AI storyboard and assemble a full video automatically.\n\n"
                "Instructions:\n"
                "1. Type the subject topic prompt.\n"
                "2. Click 'Step 1: Brainstorm Storyboard' to generate outline scenes using local Odysseus model.\n"
                "3. Edit storyboard text, choose visual generator model, and click 'Step 2: Approve & Assemble Video'."
            ),
            16: (
                "SnapGen Video Automation Guide",
                "Use Google Veo 3 and Nano Banana Pro/2 to generate high-fidelity 8-second clips and premium images.\n\n"
                "Instructions:\n"
                "1. Input your prompts manually in the Prompt Ingestion editor or browse to load a CSV/TXT file.\n"
                "2. Choose your preferred profile from the Active Profile selector.\n"
                "3. Click 'Login' to authenticate and store your persistent session folder.\n"
                "4. Select the video/image model, duration, and aspect ratio.\n"
                "5. Click 'Start Batch' to begin concurrent processing."
            ),
            17: (
                "OpenCut Video Editor Guide",
                "AI-Powered & Manual Video Timeline Editor.\n\n"
                "Instructions:\n"
                "1. Browse and select your raw input video file path.\n"
                "2. Provide plain-text AI instructions (e.g., 'Cut out all silences, zoom in on faces, and apply 9:16 portrait crop').\n"
                "3. Click 'Start AI-Powered Compilation' to trigger automated editing.\n"
                "4. Use the manual timeline tracks at the bottom to adjust or preview custom cuts."
            ),
            18: (
                "AI Video Clipper Guide",
                "Chop long-form (1-3 hours) videos into vertical TikTok/Reels clips.\n\n"
                "Instructions:\n"
                "1. Browse and select your raw long video file.\n"
                "2. Input target number of clips and output style/theme.\n"
                "3. Enable burn-in animated subtitles if desired.\n"
                "4. Click 'Chop & Generate' to start slicing."
            ),
            19: (
                "Community Showcase & Templates Guide",
                "Explore trending prompts and copy templates directly to your clipboard.\n\n"
                "Instructions:\n"
                "1. Browse the gallery of community creations in the visual feed.\n"
                "2. Click any showcase item or prompt template card to copy the prompt or send it directly to the generator pipeline."
            ),
            20: (
                "AI Models Sandbox Guide",
                "Connect to locally hosted models (LTX-Video, WAN 2.7, SkyReels ComfyUI) completely offline.\n\n"
                "Instructions & Offline Setup:\n"
                "1. Start your local inference server (e.g. ComfyUI or local REST API backend).\n"
                "2. Set the correct API Server Endpoint URL and port for the models.\n"
                "3. Click 'Verify Connection' or 'Test Connection' to verify if local GrowSnap is linked with your GPU.\n\n"
                "💡 Why use this?\n"
                "Linking local offline models allows you to run unlimited free generations on your own graphics card (GPU) without needing an internet connection or using any credits!"
            )
        }
        
        if button_id in guides:
            title, text = guides[button_id]
            QMessageBox.information(self, title, text)

    def closeEvent(self, event):
        self._save_json_backup()
        if self.runner and self.runner.isRunning():
            self.runner.stop()
            self.runner.wait()
        event.accept()

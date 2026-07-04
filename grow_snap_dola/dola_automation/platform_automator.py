import os
import csv
from pathlib import Path
from typing import List, Dict, Any, Optional
from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QComboBox, QPushButton,
    QTableWidget, QTableWidgetItem, QFileDialog, QSlider, QCheckBox,
    QPlainTextEdit, QFrame, QHeaderView, QMessageBox, QLineEdit, QAbstractItemView
)
from dola_automation.platform_workers import (
    GrokWorker, ChatGPTWorker, MetaAIWorker, FlowWorker, BasePlatformWorker
)

class PlatformBatchWorkerThread(QThread):
    progress_signal = pyqtSignal(str)
    finished_signal = pyqtSignal(list)
    error_signal = pyqtSignal(str)

    def __init__(self, worker: BasePlatformWorker, prompts: List[str], target_url: str, headless: bool, auto_clean: bool):
        super().__init__()
        self.worker = worker
        self.prompts = prompts
        self.target_url = target_url
        self.headless = headless
        self.auto_clean = auto_clean

    def run(self):
        # Redirect worker logging to UI progress_signal
        original_progress = self.worker.on_progress
        self.worker.on_progress = lambda msg: self.progress_signal.emit(msg)
        
        try:
            outputs = self.worker.run_batch(
                prompts=self.prompts,
                target_url=self.target_url,
                headless=self.headless,
                auto_clean=self.auto_clean
            )
            self.finished_signal.emit(outputs)
        except Exception as e:
            self.error_signal.emit(str(e))
        finally:
            self.worker.on_progress = original_progress

class PlatformAutomatorWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.workers: Dict[str, BasePlatformWorker] = {}
        self.current_worker: Optional[BasePlatformWorker] = None
        self.init_workers()
        self.init_ui()
        self.update_session_status()

    def init_workers(self):
        download_dir = Path.home() / 'Documents' / 'dola_downloads'
        self.workers = {
            "xAI Grok": GrokWorker(download_dir),
            "ChatGPT": ChatGPTWorker(download_dir),
            "Meta AI": MetaAIWorker(download_dir),
            "Google Flow (Veo 3)": FlowWorker(download_dir)
        }

    def init_ui(self):
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(15, 15, 15, 15)
        main_layout.setSpacing(15)

        # 1. Header Card
        header_card = QFrame(self)
        header_card.setObjectName("card")
        header_layout = QHBoxLayout(header_card)
        header_layout.setContentsMargins(15, 12, 15, 12)
        
        title_layout = QVBoxLayout()
        title_lbl = QLabel("AI Platform Automator", header_card)
        title_lbl.setObjectName("title")
        title_lbl.setStyleSheet("font-size: 20px; font-weight: 800; color: #fff;")
        subtitle_lbl = QLabel("Run bulk video and image drafts using your own subscriptions", header_card)
        subtitle_lbl.setObjectName("subtitle")
        subtitle_lbl.setStyleSheet("font-size: 11px; color: rgba(255,255,255,0.6);")
        title_layout.addWidget(title_lbl)
        title_layout.addWidget(subtitle_lbl)
        
        header_layout.addLayout(title_layout)
        header_layout.addStretch()
        
        # Platform selector dropdown
        self.combo_platform = QComboBox(header_card)
        self.combo_platform.addItems(list(self.workers.keys()))
        self.combo_platform.currentTextChanged.connect(self.on_platform_changed)
        self.combo_platform.setMinimumWidth(180)
        header_layout.addWidget(self.combo_platform)
        
        main_layout.addWidget(header_card)

        # 2. Control Layout (Split into Settings and Queue Editor)
        content_layout = QHBoxLayout()
        content_layout.setSpacing(15)

        # Settings Panel (Left side)
        settings_panel = QFrame(self)
        settings_panel.setObjectName("card")
        settings_panel.setFixedWidth(280)
        settings_layout = QVBoxLayout(settings_panel)
        settings_layout.setContentsMargins(15, 15, 15, 15)
        settings_layout.setSpacing(12)

        settings_title = QLabel("Platform Settings", settings_panel)
        settings_title.setStyleSheet("font-size: 14px; font-weight: 700; color: #fff;")
        settings_layout.addWidget(settings_title)

        # Session configuration
        self.btn_configure_session = QPushButton("🔐 Configure Session / Log In", settings_panel)
        self.btn_configure_session.clicked.connect(self.on_configure_session)
        settings_layout.addWidget(self.btn_configure_session)

        # Session Status
        self.lbl_status = QLabel("Session Status: Checking...", settings_panel)
        self.lbl_status.setStyleSheet("font-size: 12px; font-weight: 600; color: #a3bdae;")
        settings_layout.addWidget(self.lbl_status)

        # Output folder copy paths
        lbl_repurp = QLabel("Output Repurposing Settings:", settings_panel)
        settings_layout.addWidget(lbl_repurp)
        
        self.chk_auto_clean = QCheckBox("Auto-Clean Overlays (Regional Blur)", settings_panel)
        self.chk_auto_clean.setChecked(True)
        settings_layout.addWidget(self.chk_auto_clean)

        self.chk_headless = QCheckBox("Run Headless (Hidden Browser)", settings_panel)
        self.chk_headless.setChecked(False)
        settings_layout.addWidget(self.chk_headless)

        lbl_threads_title = QLabel("Concurrency Threads:", settings_panel)
        settings_layout.addWidget(lbl_threads_title)
        
        self.slider_threads = QSlider(Qt.Orientation.Horizontal, settings_panel)
        self.slider_threads.setRange(1, 5)
        self.slider_threads.setValue(1)
        settings_layout.addWidget(self.slider_threads)

        self.lbl_threads = QLabel("Active Threads: 1", settings_panel)
        self.slider_threads.valueChanged.connect(lambda v: self.lbl_threads.setText(f"Active Threads: {v}"))
        settings_layout.addWidget(self.lbl_threads)

        settings_layout.addStretch()

        # Run Button
        self.btn_run = QPushButton("🚀 Run Batch Ingestion", settings_panel)
        self.btn_run.setObjectName("btn-primary")
        self.btn_run.setStyleSheet("background-color: #2ecc71; color: #000; font-weight: 800; font-size: 14px; padding: 12px;")
        self.btn_run.clicked.connect(self.on_run_batch)
        settings_layout.addWidget(self.btn_run)

        # Cancel Button
        self.btn_cancel = QPushButton("🛑 Stop Active Run", settings_panel)
        self.btn_cancel.clicked.connect(self.on_cancel_batch)
        self.btn_cancel.setEnabled(False)
        settings_layout.addWidget(self.btn_cancel)

        content_layout.addWidget(settings_panel)

        # Queue / Table Editor Panel (Right side)
        queue_panel = QFrame(self)
        queue_panel.setObjectName("card")
        queue_layout = QVBoxLayout(queue_panel)
        queue_layout.setContentsMargins(15, 15, 15, 15)
        queue_layout.setSpacing(12)

        queue_title_layout = QHBoxLayout()
        queue_title = QLabel("Ingestion Prompt Queue", queue_panel)
        queue_title.setStyleSheet("font-size: 14px; font-weight: 700; color: #fff;")
        queue_title_layout.addWidget(queue_title)
        queue_title_layout.addStretch()
        
        # Batch buttons
        self.edit_import_path = QLineEdit(queue_panel)
        self.edit_import_path.setPlaceholderText("Paste CSV path...")
        self.edit_import_path.setToolTip("Paste a valid absolute path and press Enter to load the CSV automatically.")
        self.edit_import_path.returnPressed.connect(self.on_path_entered)
        self.edit_import_path.setMinimumWidth(180)

        self.btn_import_csv = QPushButton("📑 Import CSV", queue_panel)
        self.btn_import_csv.clicked.connect(self.on_import_csv)
        self.btn_add_row = QPushButton("➕ Add Row", queue_panel)
        self.btn_add_row.clicked.connect(self.on_add_row)
        self.btn_clear = QPushButton("🗑️ Clear Queue", queue_panel)
        self.btn_clear.clicked.connect(self.on_clear_queue)
        
        queue_title_layout.addWidget(self.edit_import_path)
        queue_title_layout.addWidget(self.btn_import_csv)
        queue_title_layout.addWidget(self.btn_add_row)
        queue_title_layout.addWidget(self.btn_clear)
        queue_layout.addLayout(queue_title_layout)

        # Prompt Table
        self.table_prompts = QTableWidget(queue_panel)
        self.table_prompts.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table_prompts.setColumnCount(3)
        self.table_prompts.setHorizontalHeaderLabels(["Prompt Text", "Status", "Actions"])
        self.table_prompts.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table_prompts.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.table_prompts.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.table_prompts.setStyleSheet("background-color: #122218; gridline-color: #2e4a38; color: #f0fdf4;")
        queue_layout.addWidget(self.table_prompts)

        content_layout.addWidget(queue_panel)
        main_layout.addLayout(content_layout)

        # 3. Log Console Terminal (Bottom)
        self.log_terminal = QPlainTextEdit(self)
        self.log_terminal.setReadOnly(True)
        self.log_terminal.setPlaceholderText("Execution logging logs will stream here...")
        self.log_terminal.setStyleSheet("background-color: #050b07; color: #2ecc71; font-family: 'JetBrains Mono', monospace; font-size: 12px; border: 1px solid #2e4a38; border-radius: 8px;")
        self.log_terminal.setMaximumHeight(160)
        main_layout.addWidget(self.log_terminal)

        # Add initial row
        self.on_add_row()

    def append_log(self, text: str):
        self.log_terminal.appendPlainText(text)

    def on_platform_changed(self, text: str):
        self.update_session_status()
        self.append_log(f"Switched platform engine to: {text}")

    def get_current_worker(self) -> BasePlatformWorker:
        platform = self.combo_platform.currentText()
        return self.workers[platform]

    def update_session_status(self):
        worker = self.get_current_worker()
        if worker.has_session():
            self.lbl_status.setText("Session Status: Active (Logged In) ✓")
            self.lbl_status.setStyleSheet("color: #2ecc71; font-weight: 700;")
        else:
            self.lbl_status.setText("Session Status: No Active Session ⚠️")
            self.lbl_status.setStyleSheet("color: #ffc107; font-weight: 700;")

    def on_configure_session(self):
        worker = self.get_current_worker()
        target_urls = {
            "xAI Grok": "https://grok.com",
            "ChatGPT": "https://chatgpt.com",
            "Meta AI": "https://meta.ai",
            "Google Flow (Veo 3)": "https://aitestkitchen.withgoogle.com/tools/video-fx"
        }
        url = target_urls.get(self.combo_platform.currentText(), "https://google.com")
        
        # Temporarily disable GUI to launch interactive config browser
        self.append_log(f"Launching configuration context window for: {worker.platform_name}")
        worker.launch_session_config(url)
        self.update_session_status()

    def on_add_row(self, prompt_text: str = ""):
        row = self.table_prompts.rowCount()
        self.table_prompts.insertRow(row)
        
        prompt_item = QTableWidgetItem(prompt_text)
        status_item = QTableWidgetItem("Pending")
        status_item.setFlags(status_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        
        del_btn = QPushButton("Delete", self)
        del_btn.setStyleSheet("background-color: #c0392b; color: #fff; padding: 4px; font-size: 11px;")
        del_btn.clicked.connect(lambda: self.on_delete_row(row))
        
        self.table_prompts.setItem(row, 0, prompt_item)
        self.table_prompts.setItem(row, 1, status_item)
        self.table_prompts.setCellWidget(row, 2, del_btn)

    def on_delete_row(self, row_idx: int):
        self.table_prompts.removeRow(row_idx)

    def on_clear_queue(self):
        self.table_prompts.setRowCount(0)
        self.append_log("Cleared the prompt ingestion queue.")

    def load_csv_file(self, path: str):
        if not path or not os.path.exists(path):
            QMessageBox.warning(self, "Invalid Path", f"The specified file path does not exist:\n{path}")
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                reader = csv.reader(f)
                for row in reader:
                    if row:
                        self.on_add_row(row[0])
            self.append_log(f"Successfully imported prompts from: {Path(path).name}")
            self.edit_import_path.setText(path)
        except Exception as e:
            QMessageBox.critical(self, "CSV Error", f"Failed to parse CSV file: {e}")

    def on_path_entered(self):
        path = self.edit_import_path.text().strip()
        if path:
            self.load_csv_file(path)

    def on_import_csv(self):
        path = self.edit_import_path.text().strip()
        if path:
            self.load_csv_file(path)
        else:
            path, _ = QFileDialog.getOpenFileName(self, "Open Ingestion CSV", "", "CSV Files (*.csv)")
            if path:
                self.load_csv_file(path)

    def on_run_batch(self):
        worker = self.get_current_worker()
        
        # Pre-execution check
        if not worker.has_session():
            QMessageBox.warning(
                self,
                "Authentication Needed",
                f"No active session profile found for {worker.platform_name}.\n\n"
                "Please click the '🔐 Configure Session / Log In' button first."
            )
            return
            
        # Collect prompts
        prompts = []
        for r in range(self.table_prompts.rowCount()):
            p_item = self.table_prompts.item(r, 0)
            if p_item and p_item.text().strip():
                prompts.append(p_item.text().strip())
                self.table_prompts.setItem(r, 1, QTableWidgetItem("Queued"))
                
        if not prompts:
            QMessageBox.warning(self, "Empty Queue", "No prompts entered in the queue.")
            return

        target_urls = {
            "xAI Grok": "https://grok.com",
            "ChatGPT": "https://chatgpt.com",
            "Meta AI": "https://meta.ai",
            "Google Flow (Veo 3)": "https://aitestkitchen.withgoogle.com/tools/video-fx"
        }
        url = target_urls.get(self.combo_platform.currentText())

        # Toggle UI controls
        self.btn_run.setEnabled(False)
        self.btn_cancel.setEnabled(True)
        self.btn_configure_session.setEnabled(False)
        self.combo_platform.setEnabled(False)
        
        # Reset log view
        self.log_terminal.clear()
        self.append_log("Initializing Platform Batch Worker Thread...")

        # Spawn Worker Thread
        self.thread = PlatformBatchWorkerThread(
            worker=worker,
            prompts=prompts,
            target_url=url,
            headless=self.chk_headless.isChecked(),
            auto_clean=self.chk_auto_clean.isChecked()
        )
        self.thread.progress_signal.connect(self.append_log)
        self.thread.finished_signal.connect(self.on_batch_finished)
        self.thread.error_signal.connect(self.on_batch_error)
        self.thread.start()

    def on_cancel_batch(self):
        worker = self.get_current_worker()
        worker.cancel()
        self.append_log("Stop signal triggered. Finishing active execution...")
        self.btn_cancel.setEnabled(False)

    def on_batch_finished(self, outputs: list):
        self.append_log(f"\n===================================================")
        self.append_log(f"   SUCCESS: Ingested {len(outputs)} clips successfully.")
        self.append_log(f"===================================================")
        self.reset_gui_state()

    def on_batch_error(self, err_msg: str):
        self.append_log(f"\n[ERROR] Ingestion process failed: {err_msg}")
        QMessageBox.critical(self, "Worker Error", f"Automation failed: {err_msg}")
        self.reset_gui_state()

    def reset_gui_state(self):
        self.btn_run.setEnabled(True)
        self.btn_cancel.setEnabled(False)
        self.btn_configure_session.setEnabled(True)
        self.combo_platform.setEnabled(True)
        self.update_session_status()

import os
import sys
import re
import sqlite3
import subprocess
from pathlib import Path
import logging
from typing import List, Tuple, Dict, Any

from PyQt6.QtCore import QThread, pyqtSignal, pyqtSlot, Qt
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QTableWidget, QTableWidgetItem, QHeaderView, QProgressBar, QDoubleSpinBox,
    QComboBox, QTabWidget, QFileDialog, QMessageBox, QPlainTextEdit, QGroupBox,
    QAbstractItemView, QGridLayout
)

import yt_dlp
from dola_automation.ffmpeg_utils import get_ffmpeg_path, get_video_duration, get_video_resolution
from dola_automation.database import HistoryDatabase
from dola_automation.models import AutomationSettings

logger = logging.getLogger(__name__)


# ─── WORKER THREADS ─────────────────────────────────────────────────────────

class DownloadWorker(QThread):
    progress = pyqtSignal(int)
    log = pyqtSignal(str)
    # success, message, downloaded_file_path
    finished = pyqtSignal(bool, str, str)

    def __init__(self, url: str, output_dir: Path, ffmpeg_exe: Path):
        super().__init__()
        self.url = url
        self.output_dir = output_dir
        self.ffmpeg_exe = ffmpeg_exe
        self._cancelled = False

    def run(self):
        self.log.emit(f"Initializing download for URL: {self.url}")
        
        # Configure yt-dlp logger callback
        class YtdlLogger:
            def __init__(self, thread):
                self.thread = thread
            def debug(self, msg):
                if "debug" in msg.lower():
                    return
                self.thread.log.emit(msg)
            def warning(self, msg):
                self.thread.log.emit(f"[Warning] {msg}")
            def error(self, msg):
                self.thread.log.emit(f"[Error] {msg}")

        def progress_hook(d):
            if d['status'] == 'downloading':
                total = d.get('total_bytes') or d.get('total_bytes_estimate')
                downloaded = d.get('downloaded_bytes', 0)
                if total:
                    pct = int(downloaded / total * 100)
                    self.progress.emit(pct)
            elif d['status'] == 'finished':
                self.progress.emit(100)

        ydl_opts = {
            'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
            'outtmpl': str(self.output_dir / '%(title)s.%(ext)s'),
            'quiet': True,
            'no_warnings': True,
            'logger': YtdlLogger(self),
            'progress_hooks': [progress_hook],
            'ffmpeg_location': str(self.ffmpeg_exe.parent),
        }

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(self.url, download=True)
                filename = ydl.prepare_filename(info)
                # Resolve format extensions (like .temp or similar if merged)
                filename_path = Path(filename)
                if not filename_path.exists():
                    # Fallback lookup in directory
                    base_name = filename_path.stem
                    matches = list(self.output_dir.glob(f"{base_name}*"))
                    if matches:
                        filename_path = matches[0]
                
                self.log.emit("Download and conversion complete!")
                self.finished.emit(True, "Download successful!", str(filename_path))
        except Exception as e:
            logger.error(f"yt-dlp download error: {e}")
            self.log.emit(f"[Error] Download failed: {str(e)}")
            self.finished.emit(False, str(e), "")


class ProfileAnalyzerWorker(QThread):
    log = pyqtSignal(str)
    finished = pyqtSignal(list, float)  # list of entries, median views

    def __init__(self, url: str):
        super().__init__()
        self.url = url

    def run(self):
        self.log.emit(f"Analyzing account profile feed: {self.url} ...")
        ydl_opts = {
            'extract_flat': True,
            'quiet': True,
            'playlist_items': '1-30',
        }
        
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(self.url, download=False)
                entries = info.get('entries', [])
                
                if not entries:
                    self.log.emit("No video feed entries found for this profile URL.")
                    self.finished.emit([], 0.0)
                    return
                
                parsed_entries = []
                view_counts = []
                
                for entry in entries:
                    if not entry:
                        continue
                    title = entry.get('title') or "Untitled Video"
                    url = entry.get('url') or entry.get('webpage_url')
                    views = entry.get('view_count')
                    duration = entry.get('duration')
                    
                    if views is not None:
                        try:
                            v_int = int(views)
                            view_counts.append(v_int)
                        except (ValueError, TypeError):
                            pass
                    
                    parsed_entries.append({
                        'title': title,
                        'url': url,
                        'views': views,
                        'duration': duration
                    })
                
                # Compute median views
                median_views = 0.0
                if view_counts:
                    view_counts.sort()
                    n = len(view_counts)
                    if n % 2 == 1:
                        median_views = float(view_counts[n // 2])
                    else:
                        median_views = (view_counts[n // 2 - 1] + view_counts[n // 2]) / 2.0
                
                self.log.emit(f"Analysis complete. Analyzed {len(parsed_entries)} videos. Median View Count: {median_views:,.0f} views.")
                self.finished.emit(parsed_entries, median_views)
        except Exception as e:
            logger.error(f"Profile analyzer error: {e}")
            self.log.emit(f"[Error] Analysis failed: {str(e)}")
            self.finished.emit([], 0.0)


class CropWorker(QThread):
    log = pyqtSignal(str)
    finished = pyqtSignal(bool, str)

    def __init__(self, ffmpeg_exe: Path, input_path: str, output_path: str, 
                 start_time: float, end_time: float, vertical_9_16: bool):
        super().__init__()
        self.ffmpeg_exe = ffmpeg_exe
        self.input_path = input_path
        self.output_path = output_path
        self.start_time = start_time
        self.end_time = end_time
        self.vertical_9_16 = vertical_9_16

    def run(self):
        duration = self.end_time - self.start_time
        self.log.emit(f"Cropping video segment: {self.start_time}s to {self.end_time}s (Duration: {duration:.1f}s)...")
        
        # Build ffmpeg command
        cmd = [
            str(self.ffmpeg_exe), '-y',
            '-ss', f"{self.start_time:.2f}",
            '-to', f"{self.end_time:.2f}",
            '-i', self.input_path
        ]
        
        if self.vertical_9_16:
            self.log.emit("Transcoding and cropping to Vertical 9:16 aspect ratio (1080x1920)...")
            cmd.extend([
                '-vf', 'scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,setsar=1',
                '-c:v', 'libx264',
                '-preset', 'fast',
                '-crf', '23',
                '-r', '30',
                '-c:a', 'aac',
                '-ar', '44100',
                '-ac', '2'
            ])
        else:
            self.log.emit("Performing fast lossless crop copy...")
            cmd.extend([
                '-c', 'copy'
            ])
            
        cmd.append(self.output_path)
        
        creationflags = 0
        if os.name == 'nt':
            creationflags = subprocess.CREATE_NO_WINDOW
            
        try:
            res = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                creationflags=creationflags
            )
            if res.returncode == 0:
                self.log.emit("Cropped hook exported successfully!")
                self.finished.emit(True, "Crop success")
            else:
                self.log.emit(f"[Error] FFmpeg cropping failed: {res.stderr}")
                self.finished.emit(False, res.stderr)
        except Exception as e:
            logger.error(f"Cropping process error: {e}")
            self.log.emit(f"[Error] Crop process failed: {str(e)}")
            self.finished.emit(False, str(e))


class ManualMergeWorker(QThread):
    log = pyqtSignal(str)
    progress = pyqtSignal(int)
    finished = pyqtSignal(bool, str)

    def __init__(self, ffmpeg_exe: Path, hook_path: str, video_path: str, output_path: str):
        super().__init__()
        self.ffmpeg_exe = ffmpeg_exe
        self.hook_path = hook_path
        self.video_path = video_path
        self.output_path = output_path

    def run(self):
        self.log.emit("Starting manual merge process...")
        self.progress.emit(10)
        
        hook = Path(self.hook_path)
        video = Path(self.video_path)
        
        if not hook.exists() or not video.exists():
            self.log.emit("[Error] Input files do not exist.")
            self.finished.emit(False, "Input files do not exist.")
            return

        # 1. Probe reference video resolution and properties
        self.log.emit(f"Probing target video properties: {video.name}...")
        width, height = get_video_resolution(video)
        self.log.emit(f"Target properties identified: {width}x{height}")
        self.progress.emit(25)
        
        # 2. Check if hook has audio stream
        has_audio = False
        try:
            probe_cmd = [str(self.ffmpeg_exe), '-i', str(hook)]
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
        except Exception as e:
            logger.error(f"Audio probe failed: {e}")

        # 3. Transcode hook to match reference video format exactly
        temp_hook = hook.parent / f"temp_aligned_{hook.name}"
        self.log.emit("Aligning hook parameters (framerate, codecs, resolution, audio) to match target video...")
        
        transcode_cmd = [
            str(self.ffmpeg_exe), '-y',
            '-i', str(hook)
        ]
        
        if not has_audio:
            self.log.emit("Hook lacks audio track. Injecting silent audio track for concatenation safety...")
            transcode_cmd.extend([
                '-f', 'lavfi', '-i', 'anullsrc=channel_layout=stereo:sample_rate=44100'
            ])
            
        transcode_cmd.extend([
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
            transcode_cmd.append('-shortest')
            
        transcode_cmd.append(str(temp_hook))
        
        creationflags = 0
        if os.name == 'nt':
            creationflags = subprocess.CREATE_NO_WINDOW

        try:
            self.log.emit("Running hook alignment transcode...")
            res_transcode = subprocess.run(
                transcode_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                creationflags=creationflags
            )
            if res_transcode.returncode != 0:
                self.log.emit(f"[Error] Hook parameter alignment failed: {res_transcode.stderr}")
                self.finished.emit(False, "Hook transcoding alignment failed.")
                return
        except Exception as e:
            self.log.emit(f"[Error] Hook transcode exception: {e}")
            self.finished.emit(False, str(e))
            return
            
        self.progress.emit(60)
        
        # 4. Perform lossless merge concatenation
        self.log.emit("Merging aligned hook video with target video losslessly...")
        concat_txt = Path(self.output_path).with_name('manual_concat_list.txt')
        try:
            with open(concat_txt, 'w', encoding='utf-8') as f:
                h_str = str(temp_hook.resolve()).replace('\\', '/')
                v_str = str(video.resolve()).replace('\\', '/')
                f.write(f"file '{h_str}'\n")
                f.write(f"file '{v_str}'\n")
                
            merge_cmd = [
                str(self.ffmpeg_exe), '-y',
                '-f', 'concat',
                '-safe', '0',
                '-i', str(concat_txt),
                '-c', 'copy',
                self.output_path
            ]
            
            res_merge = subprocess.run(
                merge_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                creationflags=creationflags
            )
            
            # Clean up temp files
            if temp_hook.exists():
                temp_hook.unlink()
                
            if res_merge.returncode == 0:
                self.progress.emit(100)
                self.log.emit("Manual hook merge process completed successfully!")
                self.finished.emit(True, "Merge success")
            else:
                self.log.emit(f"[Error] Video merge failed: {res_merge.stderr}")
                self.finished.emit(False, res_merge.stderr)
        except Exception as e:
            logger.error(f"Merge execution error: {e}")
            self.log.emit(f"[Error] Merge execution failed: {str(e)}")
            self.finished.emit(False, str(e))
        finally:
            if concat_txt.exists():
                concat_txt.unlink()


# ─── MAIN UI WIDGET ─────────────────────────────────────────────────────────

class ViralHookFactoryWidget(QWidget):
    # Signal emitted when a new hook is saved to reload settings UI combo box
    hook_saved_signal = pyqtSignal()

    def __init__(self, parent=None, db: HistoryDatabase = None, settings: AutomationSettings = None):
        super().__init__(parent)
        self.parent_window = parent
        self.db = db
        self.settings = settings
        
        # Ensure hook output directory is constructed
        self.hooks_dir = self.settings.download_dir / "viral_hooks"
        self.hooks_dir.mkdir(parents=True, exist_ok=True)
        
        # Build UI Structure
        self._build_ui()
        self._load_saved_hooks()

    def _build_ui(self):
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(15)

        # Header titles
        lbl_subtitle = QLabel("VIRAL HOOK FACTORY — Scraping outliers, cropping hooks, & auto-merging", self)
        lbl_subtitle.setObjectName("subtitle")
        main_layout.addWidget(lbl_subtitle)

        # Stacked Tabs layout
        self.tabs = QTabWidget(self)
        
        # ─── TAB 1: GRABBER / DOWNLOADER ───────────────────
        tab_grabber = QWidget(self)
        grabber_layout = QVBoxLayout(tab_grabber)
        grabber_layout.setSpacing(12)
        
        # Url Search Panel
        search_group = QGroupBox("DOWNLOAD CLIP / REEL", self)
        search_grid = QHBoxLayout(search_group)
        self.edit_url = QLineEdit(self)
        self.edit_url.setPlaceholderText("Paste Instagram Reel, YouTube Video, TikTok, or Facebook URL...")
        self.btn_download = QPushButton("Download Clip", self)
        self.btn_download.clicked.connect(self._start_download)
        
        search_grid.addWidget(self.edit_url)
        search_grid.addWidget(self.btn_download)
        grabber_layout.addWidget(search_group)
        
        # Account Feed Analyzer Panel
        analyzer_group = QGroupBox("NICHE CHANNEL / PROFILE FEED ANALYZER", self)
        analyzer_grid = QVBoxLayout(analyzer_group)
        
        analyzer_input_row = QHBoxLayout()
        self.edit_profile_url = QLineEdit(self)
        self.edit_profile_url.setPlaceholderText("Enter channel/account profile feed URL (e.g. YouTube / Instagram URL)...")
        self.btn_analyze = QPushButton("Analyze Account (Outliers)", self)
        self.btn_analyze.clicked.connect(self._analyze_profile)
        analyzer_input_row.addWidget(self.edit_profile_url)
        analyzer_input_row.addWidget(self.btn_analyze)
        analyzer_grid.addLayout(analyzer_input_row)
        
        # Analysis Table
        self.table_analysis = QTableWidget(self)
        self.table_analysis.setColumnCount(5)
        self.table_analysis.setHorizontalHeaderLabels(["Outlier Alert", "Title", "Views", "Duration", "Video URL"])
        self.table_analysis.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.table_analysis.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table_analysis.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table_analysis.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table_analysis.doubleClicked.connect(self._on_analyzer_row_double_click)
        analyzer_grid.addWidget(self.table_analysis)
        
        lbl_hint = QLabel("💡 Tip: Double-click any row to load the URL into the Downloader tab automatically.", self)
        lbl_hint.setStyleSheet("color: rgba(255,255,255,0.5); font-style: italic; font-size: 11px;")
        analyzer_grid.addWidget(lbl_hint)
        grabber_layout.addWidget(analyzer_group)

        # Progress elements
        self.progress_download = QProgressBar(self)
        self.progress_download.setValue(0)
        grabber_layout.addWidget(self.progress_download)
        
        self.log_downloader = QPlainTextEdit(self)
        self.log_downloader.setReadOnly(True)
        self.log_downloader.setPlaceholderText("Downloader output logs...")
        grabber_layout.addWidget(self.log_downloader)
        
        self.tabs.addTab(tab_grabber, "Media Grabber")

        # ─── TAB 2: HOOK CROPPER ───────────────────────────
        tab_cropper = QWidget(self)
        cropper_layout = QVBoxLayout(tab_cropper)
        cropper_layout.setSpacing(12)
        
        config_group = QGroupBox("CROP SETTINGS", self)
        config_grid = QGridLayout(config_group)
        config_grid.setSpacing(10)
        
        config_grid.addWidget(QLabel("Source Video File", self), 0, 0)
        self.edit_crop_source = QLineEdit(self)
        self.btn_browse_crop = QPushButton("Browse...", self)
        self.btn_browse_crop.clicked.connect(self._browse_crop_file)
        config_grid.addWidget(self.edit_crop_source, 0, 1)
        config_grid.addWidget(self.btn_browse_crop, 0, 2)
        
        config_grid.addWidget(QLabel("Hook Start (seconds)", self), 1, 0)
        self.spin_crop_start = QDoubleSpinBox(self)
        self.spin_crop_start.setRange(0.0, 3600.0)
        self.spin_crop_start.setSingleStep(0.5)
        self.spin_crop_start.setValue(0.0)
        config_grid.addWidget(self.spin_crop_start, 1, 1, 1, 2)
        
        config_grid.addWidget(QLabel("Hook End (seconds)", self), 2, 0)
        self.spin_crop_end = QDoubleSpinBox(self)
        self.spin_crop_end.setRange(0.1, 3600.0)
        self.spin_crop_end.setSingleStep(0.5)
        self.spin_crop_end.setValue(3.0)
        config_grid.addWidget(self.spin_crop_end, 2, 1, 1, 2)
        
        config_grid.addWidget(QLabel("Output Aspect Ratio", self), 3, 0)
        self.combo_crop_ratio = QComboBox(self)
        self.combo_crop_ratio.addItems(["Original Aspect Ratio", "Crop to Vertical (9:16)"])
        config_grid.addWidget(self.combo_crop_ratio, 3, 1, 1, 2)
        
        config_grid.addWidget(QLabel("Hook Title / Name", self), 4, 0)
        self.edit_crop_title = QLineEdit(self)
        self.edit_crop_title.setPlaceholderText("Enter hook descriptive name (e.g., Marketing Hook Alpha)...")
        config_grid.addWidget(self.edit_crop_title, 4, 1, 1, 2)
        
        cropper_layout.addWidget(config_group)
        
        self.btn_crop_start = QPushButton("Crop & Export to Library", self)
        self.btn_crop_start.clicked.connect(self._start_crop)
        cropper_layout.addWidget(self.btn_crop_start)
        
        self.log_cropper = QPlainTextEdit(self)
        self.log_cropper.setReadOnly(True)
        self.log_cropper.setPlaceholderText("Cropping output logs...")
        cropper_layout.addWidget(self.log_cropper)
        
        self.tabs.addTab(tab_cropper, "Hook Cropper")

        # ─── TAB 3: HOOK LIBRARY (MARKETPLACE) ─────────────
        tab_library = QWidget(self)
        library_layout = QVBoxLayout(tab_library)
        library_layout.setSpacing(12)
        
        # Table of hooks
        self.table_hooks = QTableWidget(self)
        self.table_hooks.setColumnCount(6)
        self.table_hooks.setHorizontalHeaderLabels(["ID", "Hook Name", "Duration", "Format", "Created At", "File Path"])
        self.table_hooks.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.table_hooks.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table_hooks.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table_hooks.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table_hooks.itemSelectionChanged.connect(self._on_library_selection_changed)
        library_layout.addWidget(self.table_hooks)
        
        # Controls Row
        ctrl_row = QHBoxLayout()
        self.btn_refresh_lib = QPushButton("Refresh Library", self)
        self.btn_refresh_lib.clicked.connect(self._load_saved_hooks)
        self.btn_preview_hook = QPushButton("Play / Preview Hook", self)
        self.btn_preview_hook.clicked.connect(self._preview_selected_hook)
        self.btn_delete_hook = QPushButton("Delete Selected Hook", self)
        self.btn_delete_hook.clicked.connect(self._delete_selected_hook)
        
        ctrl_row.addWidget(self.btn_refresh_lib)
        ctrl_row.addWidget(self.btn_preview_hook)
        ctrl_row.addWidget(self.btn_delete_hook)
        library_layout.addLayout(ctrl_row)
        
        # Direct Merger section inside library tab (as requested by user comment)
        merger_group = QGroupBox("MANUAL HOOK MERGER TOOL (Prepend Hook to Video)", self)
        merger_grid = QGridLayout(merger_group)
        merger_grid.setSpacing(10)
        
        merger_grid.addWidget(QLabel("Selected Hook File", self), 0, 0)
        self.edit_merge_hook = QLineEdit(self)
        self.edit_merge_hook.setReadOnly(True)
        merger_grid.addWidget(self.edit_merge_hook, 0, 1, 1, 2)
        
        merger_grid.addWidget(QLabel("Target Video File", self), 1, 0)
        self.edit_merge_video = QLineEdit(self)
        self.btn_browse_merge_video = QPushButton("Browse...", self)
        self.btn_browse_merge_video.clicked.connect(self._browse_merge_video_file)
        merger_grid.addWidget(self.edit_merge_video, 1, 1)
        merger_grid.addWidget(self.btn_browse_merge_video, 1, 2)
        
        merger_grid.addWidget(QLabel("Output File Path", self), 2, 0)
        self.edit_merge_output = QLineEdit(self)
        self.btn_browse_merge_output = QPushButton("Browse...", self)
        self.btn_browse_merge_output.clicked.connect(self._browse_merge_output_file)
        merger_grid.addWidget(self.edit_merge_output, 2, 1)
        merger_grid.addWidget(self.btn_browse_merge_output, 2, 2)
        
        self.btn_merge_manual = QPushButton("Merge Hook with Video", self)
        self.btn_merge_manual.clicked.connect(self._start_manual_merge)
        merger_grid.addWidget(self.btn_merge_manual, 3, 0, 1, 3)
        
        library_layout.addWidget(merger_group)
        
        self.progress_merger = QProgressBar(self)
        self.progress_merger.setValue(0)
        library_layout.addWidget(self.progress_merger)
        
        self.log_merger = QPlainTextEdit(self)
        self.log_merger.setReadOnly(True)
        self.log_merger.setPlaceholderText("Merge console output logs...")
        library_layout.addWidget(self.log_merger)
        
        self.tabs.addTab(tab_library, "Hook Library & Merger")
        
        main_layout.addWidget(self.tabs)

    # ─── ACTION SLOTS ────────────────────────────────────────────────────────

    # DOWNLOADER Tab Actions
    def _start_download(self):
        url = self.edit_url.text().strip()
        if not url:
            QMessageBox.warning(self, "Invalid URL", "Please enter a valid video or reel URL to download.")
            return

        self.btn_download.setEnabled(False)
        self.btn_analyze.setEnabled(False)
        self.progress_download.setValue(0)
        self.log_downloader.clear()
        
        ffmpeg_exe = get_ffmpeg_path()
        self.dl_worker = DownloadWorker(url, self.settings.download_dir, ffmpeg_exe)
        self.dl_worker.log.connect(self.log_downloader.appendPlainText)
        self.dl_worker.progress.connect(self.progress_download.setValue)
        self.dl_worker.finished.connect(self._on_download_finished)
        self.dl_worker.start()

    def _on_download_finished(self, success: bool, msg: str, file_path: str):
        self.btn_download.setEnabled(True)
        self.btn_analyze.setEnabled(True)
        if success:
            self.progress_download.setValue(100)
            QMessageBox.information(self, "Success", f"Download Completed!\nFile saved to downloads directory.")
            # Auto-fill crop source input field and switch to Cropper Tab
            self.edit_crop_source.setText(file_path)
            self.edit_crop_title.setText(Path(file_path).stem)
            self.tabs.setCurrentIndex(1)
        else:
            QMessageBox.critical(self, "Failed", f"Download failed: {msg}")

    def _analyze_profile(self):
        url = self.edit_profile_url.text().strip()
        if not url:
            QMessageBox.warning(self, "Invalid URL", "Please enter an account profile feed URL.")
            return
            
        self.btn_download.setEnabled(False)
        self.btn_analyze.setEnabled(False)
        self.log_downloader.clear()
        self.table_analysis.setRowCount(0)
        
        self.analyzer_worker = ProfileAnalyzerWorker(url)
        self.analyzer_worker.log.connect(self.log_downloader.appendPlainText)
        self.analyzer_worker.finished.connect(self._on_analysis_finished)
        self.analyzer_worker.start()

    def _on_analysis_finished(self, entries: list, median_views: float):
        self.btn_download.setEnabled(True)
        self.btn_analyze.setEnabled(True)
        
        if not entries:
            QMessageBox.warning(self, "No Videos Found", "No videos or content clips could be found/parsed for this account URL.")
            return
            
        self.table_analysis.setRowCount(len(entries))
        for row_idx, item in enumerate(entries):
            title = item.get('title') or "Untitled Video"
            views = item.get('views')
            duration = item.get('duration')
            v_url = item.get('url') or ""
            
            is_outlier = False
            views_str = "N/A"
            if views is not None:
                try:
                    v_val = int(views)
                    views_str = f"{v_val:,}"
                    if median_views > 0 and v_val >= 1.5 * median_views:
                        is_outlier = True
                except (ValueError, TypeError):
                    pass
            
            duration_str = "N/A"
            if duration is not None:
                try:
                    d_sec = float(duration)
                    duration_str = f"{int(d_sec // 60)}m {int(d_sec % 60)}s"
                except (ValueError, TypeError):
                    pass
            
            outlier_item = QTableWidgetItem("🔥 OUTLIER!" if is_outlier else "")
            if is_outlier:
                outlier_item.setForeground(Qt.GlobalColor.green)
                # Bold the font
                font = outlier_item.font()
                font.setBold(True)
                outlier_item.setFont(font)
                
            self.table_analysis.setItem(row_idx, 0, outlier_item)
            self.table_analysis.setItem(row_idx, 1, QTableWidgetItem(title))
            self.table_analysis.setItem(row_idx, 2, QTableWidgetItem(views_str))
            self.table_analysis.setItem(row_idx, 3, QTableWidgetItem(duration_str))
            self.table_analysis.setItem(row_idx, 4, QTableWidgetItem(v_url))
            
        self.log_downloader.appendPlainText(f"Analysis loaded! Populated {len(entries)} items. Outliers highlighted in table.")

    def _on_analyzer_row_double_click(self, index):
        row = index.row()
        url_item = self.table_analysis.item(row, 4)
        if url_item:
            v_url = url_item.text().strip()
            if v_url:
                self.edit_url.setText(v_url)
                self.log_downloader.appendPlainText(f"Loaded outlier URL: {v_url}")
                # Optional visual cue
                QMessageBox.information(self, "URL Loaded", "The outlier video URL has been loaded into the Downloader input. Click 'Download Clip' to download it.")

    # CROPPER Tab Actions
    def _browse_crop_file(self):
        f_path, _ = QFileDialog.getOpenFileName(
            self, "Select Source Video", str(self.settings.download_dir), "Video Files (*.mp4 *.mkv *.avi *.mov)"
        )
        if f_path:
            self.edit_crop_source.setText(f_path)
            self.edit_crop_title.setText(Path(f_path).stem)

    def _start_crop(self):
        src_path = self.edit_crop_source.text().strip()
        title = self.edit_crop_title.text().strip()
        start_t = self.spin_crop_start.value()
        end_t = self.spin_crop_end.value()
        
        if not src_path or not Path(src_path).exists():
            QMessageBox.warning(self, "Invalid File", "Please select a valid source video file to crop.")
            return
            
        if not title:
            QMessageBox.warning(self, "Invalid Title", "Please enter a descriptive title for this hook.")
            return
            
        if end_t <= start_t:
            QMessageBox.warning(self, "Invalid Time Range", "End time must be greater than start time.")
            return

        self.btn_crop_start.setEnabled(False)
        self.log_cropper.clear()
        
        ffmpeg_exe = get_ffmpeg_path()
        # slug title name
        import re
        slug_title = re.sub(r'[^\w\-]+', '_', title).strip('_')[:50]
        
        # Save output in downloads/viral_hooks subfolder
        out_filename = f"hook_{slug_title}.mp4"
        out_path = self.hooks_dir / out_filename
        
        vertical_9_16 = self.combo_crop_ratio.currentIndex() == 1
        
        self.crop_worker = CropWorker(ffmpeg_exe, src_path, str(out_path), start_t, end_t, vertical_9_16)
        self.crop_worker.log.connect(self.log_cropper.appendPlainText)
        self.crop_worker.finished.connect(lambda success, msg: self._on_crop_finished(success, msg, out_path, title, end_t - start_t))
        self.crop_worker.start()

    def _on_crop_finished(self, success: bool, msg: str, hook_path: Path, title: str, duration: float):
        self.btn_crop_start.setEnabled(True)
        if success:
            QMessageBox.information(self, "Success", f"Cropped segment successfully added to library!")
            # Add to local db
            ratio_lbl = "Vertical (9:16)" if self.combo_crop_ratio.currentIndex() == 1 else "Original"
            self.db.add_viral_hook(title, str(hook_path), duration, ratio_lbl, "", "")
            
            # Emit hook saved signal to refresh main window settings combo
            self.hook_saved_signal.emit()
            
            # Refresh Hook Library view & switch to Library Tab
            self._load_saved_hooks()
            self.tabs.setCurrentIndex(2)
        else:
            QMessageBox.critical(self, "Failed", f"Crop failed: {msg}")

    # LIBRARY Tab Actions
    def _load_saved_hooks(self):
        self.table_hooks.setRowCount(0)
        try:
            hooks = self.db.list_viral_hooks()
            self.table_hooks.setRowCount(len(hooks))
            for row_idx, r in enumerate(hooks):
                self.table_hooks.setItem(row_idx, 0, QTableWidgetItem(str(r['id'])))
                self.table_hooks.setItem(row_idx, 1, QTableWidgetItem(r['title']))
                self.table_hooks.setItem(row_idx, 2, QTableWidgetItem(f"{r['duration']:.1f}s"))
                self.table_hooks.setItem(row_idx, 3, QTableWidgetItem(r['aspect_ratio']))
                self.table_hooks.setItem(row_idx, 4, QTableWidgetItem(r['created_at'][:19].replace('T', ' ')))
                self.table_hooks.setItem(row_idx, 5, QTableWidgetItem(r['file_path']))
        except Exception as e:
            logger.error(f"Error loading saved hooks: {e}")

    def _on_library_selection_changed(self):
        selected = self.table_hooks.selectedItems()
        if len(selected) >= 6:
            hook_file = selected[5].text().strip()
            hook_title = selected[1].text().strip()
            self.edit_merge_hook.setText(hook_file)
            self.log_merger.appendPlainText(f"Selected hook for merging: {hook_title}")

    def _preview_selected_hook(self):
        selected = self.table_hooks.selectedItems()
        if len(selected) >= 6:
            hook_file = selected[5].text().strip()
            if os.path.exists(hook_file):
                import platform
                try:
                    if platform.system() == 'Windows':
                        os.startfile(hook_file)
                    elif platform.system() == 'Darwin':
                        subprocess.run(['open', hook_file])
                    else:
                        subprocess.run(['xdg-open', hook_file])
                except Exception as e:
                    QMessageBox.warning(self, "Player Error", f"Could not launch media player: {e}")
            else:
                QMessageBox.warning(self, "File Not Found", "The hook video file no longer exists at the saved path.")
        else:
            QMessageBox.warning(self, "Select Hook", "Please select a hook from the table to preview.")

    def _delete_selected_hook(self):
        selected = self.table_hooks.selectedItems()
        if len(selected) >= 6:
            hook_id = int(selected[0].text())
            hook_file = selected[5].text().strip()
            hook_title = selected[1].text().strip()
            
            confirm = QMessageBox.question(
                self, "Confirm Delete",
                f"Are you sure you want to delete the hook '{hook_title}' from the library and disk?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No
            )
            
            if confirm == QMessageBox.StandardButton.Yes:
                # Delete DB entry
                self.db.delete_viral_hook(hook_id)
                # Delete actual file
                try:
                    f_path = Path(hook_file)
                    if f_path.exists():
                        f_path.unlink()
                except Exception as e:
                    logger.error(f"Failed to delete hook file '{hook_file}': {e}")
                    
                self.hook_saved_signal.emit()
                self._load_saved_hooks()
                self.edit_merge_hook.clear()
        else:
            QMessageBox.warning(self, "Select Hook", "Please select a hook from the table to delete.")

    # MANUAL HOOK MERGER SLOTS
    def _browse_merge_video_file(self):
        f_path, _ = QFileDialog.getOpenFileName(
            self, "Select Target Video", str(self.settings.download_dir), "Video Files (*.mp4 *.mkv *.avi *.mov)"
        )
        if f_path:
            self.edit_merge_video.setText(f_path)
            # Pre-populate output file path
            p = Path(f_path)
            self.edit_merge_output.setText(str(p.parent / f"merged_with_hook_{p.name}"))

    def _browse_merge_output_file(self):
        f_path, _ = QFileDialog.getSaveFileName(
            self, "Save Merged Output As", str(self.settings.download_dir), "Video Files (*.mp4)"
        )
        if f_path:
            if not f_path.endswith(".mp4"):
                f_path += ".mp4"
            self.edit_merge_output.setText(f_path)

    def _start_manual_merge(self):
        hook_p = self.edit_merge_hook.text().strip()
        video_p = self.edit_merge_video.text().strip()
        out_p = self.edit_merge_output.text().strip()
        
        if not hook_p or not os.path.exists(hook_p):
            QMessageBox.warning(self, "Select Hook", "Please select a valid hook from the table first.")
            return
            
        if not video_p or not os.path.exists(video_p):
            QMessageBox.warning(self, "Select Video", "Please browse and select a valid target video file.")
            return
            
        if not out_p:
            QMessageBox.warning(self, "Select Output", "Please choose a destination save file path.")
            return

        self.btn_merge_manual.setEnabled(False)
        self.progress_merger.setValue(0)
        self.log_merger.clear()
        
        ffmpeg_exe = get_ffmpeg_path()
        
        self.merge_worker = ManualMergeWorker(ffmpeg_exe, hook_p, video_p, out_p)
        self.merge_worker.log.connect(self.log_merger.appendPlainText)
        self.merge_worker.progress.connect(self.progress_merger.setValue)
        self.merge_worker.finished.connect(self._on_manual_merge_finished)
        self.merge_worker.start()

    def _on_manual_merge_finished(self, success: bool, msg: str):
        self.btn_merge_manual.setEnabled(True)
        if success:
            self.progress_merger.setValue(100)
            QMessageBox.information(self, "Success", f"Hook merged and output video generated successfully!")
        else:
            QMessageBox.critical(self, "Failed", f"Merging failed: {msg}")

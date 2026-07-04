import os
import sys
import re
import sqlite3
import subprocess
from pathlib import Path
import logging
from typing import List, Tuple, Dict, Any

from PyQt6.QtCore import QThread, pyqtSignal, pyqtSlot, Qt, QUrl
from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QTableWidget, QTableWidgetItem, QHeaderView, QProgressBar, QDoubleSpinBox,
    QComboBox, QTabWidget, QFileDialog, QMessageBox, QPlainTextEdit, QGroupBox,
    QAbstractItemView, QGridLayout, QScrollArea, QFrame, QCheckBox, QDialog, QSlider
)
from PyQt6.QtMultimedia import QMediaPlayer, QAudioOutput
from PyQt6.QtMultimediaWidgets import QVideoWidget

import yt_dlp
from dola_automation.ffmpeg_utils import get_ffmpeg_path, get_video_duration, get_video_resolution
from dola_automation.database import HistoryDatabase
from dola_automation.models import AutomationSettings

logger = logging.getLogger(__name__)


def open_media_file(file_path):
    import platform
    import subprocess
    import os
    import webbrowser
    
    path = os.path.abspath(file_path)
    if not os.path.exists(path):
        raise FileNotFoundError(f"File not found: {path}")
        
    system = platform.system()
    if system == 'Windows':
        os.startfile(path)
    elif system == 'Darwin':
        subprocess.run(['open', path], check=True)
    else:
        # Linux fallbacks
        try:
            subprocess.run(['xdg-open', path], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            try:
                webbrowser.open(f"file://{path}")
            except Exception:
                try:
                    subprocess.Popen(['vlc', path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                except Exception:
                    raise RuntimeError("No media player could be launched. Please locate the file manually.")


def open_in_file_manager(path):
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


class VideoPlayerDialog(QDialog):
    def __init__(self, file_path, title="Built-in Video Player", parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(600, 500)
        
        # Premium dark styling matching the main app
        self.setStyleSheet("""
            QDialog { background-color: #0c1a12; color: #F0FDF4; }
            QPushButton { background-color: #122218; border: 1px solid #2ecc71; border-radius: 4px; color: #F0FDF4; padding: 6px; font-weight: bold; }
            QPushButton:hover { background-color: #1a3324; border-color: #27ae60; }
            QSlider::groove:horizontal { border: 1px solid #2ecc71; height: 8px; background: #0c1a12; border-radius: 4px; }
            QSlider::handle:horizontal { background: #2ecc71; width: 14px; margin: -3px 0; border-radius: 7px; }
        """)
        
        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        
        # Video widget
        self.video_widget = QVideoWidget(self)
        self.video_widget.setMinimumHeight(350)
        layout.addWidget(self.video_widget)
        
        # Progress slider & labels
        slider_row = QHBoxLayout()
        self.lbl_time = QLabel("00:00", self)
        self.lbl_time.setStyleSheet("font-size: 10px; color: rgba(240, 253, 244, 0.7);")
        self.slider = QSlider(Qt.Orientation.Horizontal, self)
        self.slider.setRange(0, 0)
        self.slider.sliderMoved.connect(self._set_position)
        self.lbl_duration = QLabel("00:00", self)
        self.lbl_duration.setStyleSheet("font-size: 10px; color: rgba(240, 253, 244, 0.7);")
        
        slider_row.addWidget(self.lbl_time)
        slider_row.addWidget(self.slider)
        slider_row.addWidget(self.lbl_duration)
        layout.addLayout(slider_row)
        
        # Controls row
        ctrl_layout = QHBoxLayout()
        self.btn_play_pause = QPushButton("Pause", self)
        self.btn_play_pause.clicked.connect(self._toggle_play)
        ctrl_layout.addWidget(self.btn_play_pause)
        
        self.btn_system_play = QPushButton("Open in System Player", self)
        self.btn_system_play.clicked.connect(lambda: self._open_system(file_path))
        ctrl_layout.addWidget(self.btn_system_play)
        
        btn_close = QPushButton("Close", self)
        btn_close.clicked.connect(self.close)
        ctrl_layout.addWidget(btn_close)
        
        layout.addLayout(ctrl_layout)
        
        # Setup audio output and media player
        self.player = QMediaPlayer()
        self.audio_output = QAudioOutput()
        self.player.setAudioOutput(self.audio_output)
        self.player.setVideoOutput(self.video_widget)
        
        # Connections
        self.player.positionChanged.connect(self._position_changed)
        self.player.durationChanged.connect(self._duration_changed)
        
        # Load video
        self.file_path = file_path
        self.player.setSource(QUrl.fromLocalFile(file_path))
        
        # Play automatically
        self.player.play()
        
    def _toggle_play(self):
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
            self.btn_play_pause.setText("Play")
        else:
            self.player.play()
            self.btn_play_pause.setText("Pause")
            
    def _position_changed(self, position):
        self.slider.setValue(position)
        self.lbl_time.setText(self._format_time(position))
        
    def _duration_changed(self, duration):
        self.slider.setRange(0, duration)
        self.lbl_duration.setText(self._format_time(duration))
        
    def _set_position(self, position):
        self.player.setPosition(position)
        
    def _open_system(self, path):
        try:
            self.player.pause()
            self.btn_play_pause.setText("Play")
            open_media_file(path)
        except Exception as e:
            QMessageBox.warning(self, "Player Error", f"Could not launch media player: {e}")
            
    def _format_time(self, ms):
        seconds = int(ms // 1000)
        minutes = seconds // 60
        seconds = seconds % 60
        return f"{minutes:02d}:{seconds:02d}"
        
    def closeEvent(self, event):
        self.player.stop()
        event.accept()


# ─── WORKER THREADS ─────────────────────────────────────────────────────────

class DownloadWorker(QThread):
    progress = pyqtSignal(int)
    log = pyqtSignal(str)
    # success, message, downloaded_file_path
    finished = pyqtSignal(bool, str, str)

    def __init__(self, url: str, output_dir: Path, ffmpeg_exe: Path, cookie_browser: str = None):
        super().__init__()
        # Clean URL: strip client-side fragment identifiers (#)
        self.url = url.split('#')[0].strip()
        self.output_dir = output_dir
        self.ffmpeg_exe = ffmpeg_exe
        self.cookie_browser = cookie_browser
        self._cancelled = False

    def run(self):
        self.log.emit(f"Initializing download for URL: {self.url}")
        
        class DownloadCancelledError(Exception):
            pass

        # Configure yt-dlp logger callback
        class YtdlLogger:
            def __init__(self, thread):
                self.thread = thread
            def debug(self, msg):
                if self.thread._cancelled:
                    raise DownloadCancelledError("Download cancelled by user.")
                if "debug" in msg.lower():
                    return
                self.thread.log.emit(msg)
            def warning(self, msg):
                if self.thread._cancelled:
                    raise DownloadCancelledError("Download cancelled by user.")
                self.thread.log.emit(f"[Warning] {msg}")
            def error(self, msg):
                if self.thread._cancelled:
                    raise DownloadCancelledError("Download cancelled by user.")
                self.thread.log.emit(f"[Error] {msg}")

        def progress_hook(d):
            if self._cancelled:
                raise DownloadCancelledError("Download cancelled by user.")
            if d['status'] == 'downloading':
                total = d.get('total_bytes') or d.get('total_bytes_estimate')
                downloaded = d.get('downloaded_bytes', 0)
                if total:
                    pct = int(downloaded / total * 100)
                    self.progress.emit(pct)
                else:
                    self.progress.emit(-1)
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
        if self.cookie_browser and self.cookie_browser.lower() != "no cookies":
            if os.path.exists(self.cookie_browser):
                ydl_opts['cookiefile'] = self.cookie_browser
            else:
                ydl_opts['cookiesfrombrowser'] = (self.cookie_browser.lower(), None, None, None)

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
            if "cancelled by user" in str(e).lower():
                self.log.emit("[Info] Download cancelled by user.")
                self.finished.emit(False, "Cancelled by user.", "")
                return
            err_msg = str(e)
            if "cookies" in err_msg.lower() or "cookie" in err_msg.lower():
                self.log.emit("[Warning] Selected browser cookies could not be loaded. Retrying download without cookies...")
                if 'cookiesfrombrowser' in ydl_opts:
                    del ydl_opts['cookiesfrombrowser']
                if 'cookiefile' in ydl_opts:
                    del ydl_opts['cookiefile']
                try:
                    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                        info = ydl.extract_info(self.url, download=True)
                        filename = ydl.prepare_filename(info)
                        filename_path = Path(filename)
                        if not filename_path.exists():
                            base_name = filename_path.stem
                            matches = list(self.output_dir.glob(f"{base_name}*"))
                            if matches:
                                filename_path = matches[0]
                        self.log.emit("Download and conversion complete!")
                        self.finished.emit(True, "Download successful!", str(filename_path))
                except Exception as e2:
                    if "cancelled by user" in str(e2).lower():
                        self.log.emit("[Info] Download cancelled by user.")
                        self.finished.emit(False, "Cancelled by user.", "")
                        return
                    logger.error(f"yt-dlp download error: {e2}")
                    self.log.emit(f"[Error] Download failed: {str(e2)}")
                    self.finished.emit(False, str(e2), "")
            else:
                logger.error(f"yt-dlp download error: {e}")
                self.log.emit(f"[Error] Download failed: {str(e)}")
                self.finished.emit(False, str(e), "")


class ProfileAnalyzerWorker(QThread):
    log = pyqtSignal(str)
    finished = pyqtSignal(list, float)  # list of entries, median views

    def __init__(self, url: str, cookie_browser: str = None):
        super().__init__()
        # Clean URL: strip client-side fragment identifiers (#), query parameters (?), and trailing slashes
        self.url = url.split('#')[0].split('?')[0].rstrip('/')
        self.cookie_browser = cookie_browser

    def run(self):
        self.log.emit(f"Analyzing account profile feed: {self.url} ...")
        ydl_opts = {
            'extract_flat': True,
            'quiet': True,
            'playlist_items': '1-30',
        }
        if self.cookie_browser and self.cookie_browser.lower() != "no cookies":
            if os.path.exists(self.cookie_browser):
                ydl_opts['cookiefile'] = self.cookie_browser
            else:
                ydl_opts['cookiesfrombrowser'] = (self.cookie_browser.lower(), None, None, None)
        
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(self.url, download=False)
                entries = info.get('entries', [])
        except Exception as e:
            err_msg = str(e)
            if "cookies" in err_msg.lower() or "cookie" in err_msg.lower():
                self.log.emit("[Warning] Selected browser cookies could not be loaded. Retrying analysis without cookies...")
                if 'cookiesfrombrowser' in ydl_opts:
                    del ydl_opts['cookiesfrombrowser']
                if 'cookiefile' in ydl_opts:
                    del ydl_opts['cookiefile']
                try:
                    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                        info = ydl.extract_info(self.url, download=False)
                        entries = info.get('entries', [])
                except Exception as e2:
                    logger.error(f"Profile analyzer error: {e2}")
                    self.log.emit(f"[Error] Analysis failed: {str(e2)}")
                    self.finished.emit([], 0.0)
                    return
            else:
                logger.error(f"Profile analyzer error: {e}")
                self.log.emit(f"[Error] Analysis failed: {str(e)}")
                self.finished.emit([], 0.0)
                return

        try:
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
            logger.error(f"Profile analyzer data processing error: {e}")
            self.log.emit(f"[Error] Analysis processing failed: {str(e)}")
            self.finished.emit([], 0.0)


class VideoAnalyzerWorker(QThread):
    log = pyqtSignal(str)
    finished = pyqtSignal(bool, float, str, str)  # success, duration, title, transcription

    def __init__(self, ffmpeg_exe: Path, input_path: str):
        super().__init__()
        self.ffmpeg_exe = ffmpeg_exe
        self.input_path = input_path

    def run(self):
        self.log.emit("Starting intelligent hook content analysis...")
        
        # 1. Extract audio from first 10 seconds to a temporary wav file
        temp_wav = Path(self.input_path).parent / "temp_hook_audio.wav"
        if temp_wav.exists():
            try:
                temp_wav.unlink()
            except Exception:
                pass
                
        self.log.emit("Extracting video soundtrack for speech-to-text...")
        cmd_audio = [
            str(self.ffmpeg_exe),
            "-y",
            "-ss", "0",
            "-t", "10",
            "-i", self.input_path,
            "-acodec", "pcm_s16le",
            "-ac", "1",
            "-ar", "16000",
            str(temp_wav)
        ]
        
        try:
            subprocess.run(cmd_audio, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
        except Exception as e:
            self.log.emit(f"[Warning] Could not extract audio stream: {e}")
            
        transcription = ""
        suggested_title = ""
        
        if temp_wav.exists():
            self.log.emit("Transcribing hook vocal audio using SpeechRecognition...")
            import speech_recognition as sr
            r = sr.Recognizer()
            try:
                with sr.AudioFile(str(temp_wav)) as source:
                    audio = r.record(source)
                transcription = r.recognize_google(audio)
                self.log.emit(f"Transcribed Hook Text: \"{transcription}\"")
                # Format a title from the first 6 words
                words = transcription.split()
                if words:
                    title_words = words[:6]
                    suggested_title = " ".join(title_words).strip().title()
                    if len(words) > 6:
                        suggested_title += "..."
            except Exception as e:
                self.log.emit(f"[Info] Speech transcription skipped: {e}")
            finally:
                try:
                    temp_wav.unlink()
                except Exception:
                    pass

        # 2. Detect scene changes using FFmpeg visual filter in the first 10 seconds
        self.log.emit("Detecting visual scene cuts in the first 10 seconds...")
        cmd_scene = [
            str(self.ffmpeg_exe),
            "-ss", "0",
            "-t", "10",
            "-i", self.input_path,
            "-filter:v", "select='gt(scene,0.18)',showinfo",
            "-f", "null",
            "-"
        ]
        
        suggested_duration = 4.0 # default fallback
        
        try:
            result = subprocess.run(cmd_scene, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            # Parse stderr output for showinfo pts_time
            # Output template: [Parsed_showinfo_1 @ 0x...] n:   0 pts: 120120 pts_time:4.004 ...
            pts_times = []
            for line in result.stderr.split('\n'):
                if "showinfo" in line and "pts_time:" in line:
                    match = re.search(r'pts_time:([\d\.]+)', line)
                    if match:
                        t = float(match.group(1))
                        if t > 1.5:  # ignore immediate start frame cuts
                            pts_times.append(t)
            
            if pts_times:
                # Use the first cut as the suggested hook duration
                pts_times.sort()
                suggested_duration = pts_times[0]
                self.log.emit(f"First major visual transition detected at: {suggested_duration:.2f}s")
            else:
                self.log.emit("No major visual scene transition detected. Falling back to default duration.")
        except Exception as e:
            self.log.emit(f"[Warning] Scene cut detection failed: {e}")

        # Fallback title if empty
        if not suggested_title:
            suggested_title = Path(self.input_path).stem.replace("_", " ").title()
            
        self.log.emit(f"Suggested Hook Duration: {suggested_duration:.2f}s")
        self.log.emit(f"Suggested Title: \"{suggested_title}\"")
        self.finished.emit(True, suggested_duration, suggested_title, transcription)


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
        self.selected_cookie_file = ""
        
        # Ensure hook output directory is constructed
        self.hooks_dir = self.settings.download_dir / "viral_hooks"
        self.hooks_dir.mkdir(parents=True, exist_ok=True)
        
        # Build UI Structure
        self._build_ui()

    def _build_ui(self):
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(15)

        # Header titles with Help button
        header_layout = QHBoxLayout()
        lbl_subtitle = QLabel("VIRAL HOOK FACTORY — Scraping outliers, cropping hooks, & auto-merging", self)
        lbl_subtitle.setObjectName("subtitle")
        header_layout.addWidget(lbl_subtitle)
        header_layout.addStretch()
        
        self.btn_help = QPushButton("Help / Instructions", self)
        self.btn_help.setMinimumWidth(180)
        self.btn_help.clicked.connect(self._show_help_dialog)
        header_layout.addWidget(self.btn_help)
        
        main_layout.addLayout(header_layout)

        # Stacked Tabs layout
        self.tabs = QTabWidget(self)
        
        # ─── TAB 1: MEDIA DOWNLOADER ───────────────────────
        tab_grabber = QWidget(self)
        self.tab_grabber = tab_grabber
        grabber_layout = QVBoxLayout(tab_grabber)
        grabber_layout.setSpacing(12)
        
        # Url Search Panel
        search_group = QGroupBox("DOWNLOAD SINGLE CLIP / REEL", self)
        search_vlayout = QVBoxLayout(search_group)
        
        search_grid = QHBoxLayout()
        self.edit_url = QLineEdit(self)
        self.edit_url.setPlaceholderText("Paste Instagram Reel, YouTube Video, TikTok, or Facebook URL...")
        self.btn_download = QPushButton("Download Clip", self)
        self.btn_download.clicked.connect(self._start_download)
        self.btn_cancel_download = QPushButton("Cancel", self)
        self.btn_cancel_download.setEnabled(False)
        self.btn_cancel_download.clicked.connect(self._cancel_download)
        
        search_grid.addWidget(self.edit_url)
        search_grid.addWidget(self.btn_download)
        search_grid.addWidget(self.btn_cancel_download)
        search_vlayout.addLayout(search_grid)
        
        cookies_row = QHBoxLayout()
        lbl_cookies = QLabel("Cookies:", self)
        self.combo_cookies = QComboBox(self)
        self.combo_cookies.addItems(["No Cookies", "cookies.txt (Browse...)", "Chrome", "Firefox", "Edge", "Opera", "Brave", "Vivaldi", "Safari"])
        self.combo_cookies.setToolTip("Select browser or a cookies.txt file to authenticate downloads.")
        self.combo_cookies.setMinimumWidth(120)
        self.combo_cookies.currentIndexChanged.connect(self._on_downloader_cookie_selection_changed)
        
        cookies_row.addWidget(lbl_cookies)
        cookies_row.addWidget(self.combo_cookies)
        cookies_row.addStretch()
        search_vlayout.addLayout(cookies_row)
        
        grabber_layout.addWidget(search_group)

        # Progress elements
        self.progress_download = QProgressBar(self)
        self.progress_download.setValue(0)
        grabber_layout.addWidget(self.progress_download)
        
        # Downloaded Path Row
        path_row = QHBoxLayout()
        path_row.addWidget(QLabel("Downloaded Path:", self))
        self.edit_downloaded_path = QLineEdit(self)
        self.edit_downloaded_path.setReadOnly(True)
        self.edit_downloaded_path.setPlaceholderText("Path will appear here after download...")
        self.btn_copy_path = QPushButton("Copy Path", self)
        self.btn_copy_path.clicked.connect(self._copy_downloaded_path)
        self.btn_open_download_folder = QPushButton("📂 Open Folder", self)
        self.btn_open_download_folder.clicked.connect(lambda: self._open_folder_of_path(self.edit_downloaded_path.text()))
        path_row.addWidget(self.edit_downloaded_path)
        path_row.addWidget(self.btn_copy_path)
        path_row.addWidget(self.btn_open_download_folder)
        grabber_layout.addLayout(path_row)
        
        self.log_downloader = QPlainTextEdit(self)
        self.log_downloader.setReadOnly(True)
        self.log_downloader.setPlaceholderText("Downloader output logs...")
        grabber_layout.addWidget(self.log_downloader)
        
        self.tabs.addTab(tab_grabber, "Media Downloader")

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
        self.btn_open_crop_folder = QPushButton("📂 Open Folder", self)
        self.btn_open_crop_folder.clicked.connect(lambda: self._open_folder_of_path(self.edit_crop_source.text()))
        self.btn_analyze_video = QPushButton("🔍 Analyze Clip", self)
        self.btn_analyze_video.clicked.connect(self._analyze_source_video)
        
        config_grid.addWidget(self.edit_crop_source, 0, 1)
        config_grid.addWidget(self.btn_browse_crop, 0, 2)
        
        h_lay_crop_btns = QHBoxLayout()
        h_lay_crop_btns.addWidget(self.btn_open_crop_folder)
        h_lay_crop_btns.addWidget(self.btn_analyze_video)
        config_grid.addLayout(h_lay_crop_btns, 0, 3)
        
        config_grid.addWidget(QLabel("Hook Start (seconds)", self), 1, 0)
        self.spin_crop_start = QDoubleSpinBox(self)
        self.spin_crop_start.setRange(0.0, 3600.0)
        self.spin_crop_start.setSingleStep(0.5)
        self.spin_crop_start.setValue(0.0)
        config_grid.addWidget(self.spin_crop_start, 1, 1, 1, 3)
        
        config_grid.addWidget(QLabel("Hook End (seconds)", self), 2, 0)
        self.spin_crop_end = QDoubleSpinBox(self)
        self.spin_crop_end.setRange(0.1, 3600.0)
        self.spin_crop_end.setSingleStep(0.5)
        self.spin_crop_end.setValue(3.0)
        config_grid.addWidget(self.spin_crop_end, 2, 1, 1, 3)
        
        config_grid.addWidget(QLabel("Output Aspect Ratio", self), 3, 0)
        self.combo_crop_ratio = QComboBox(self)
        self.combo_crop_ratio.addItems(["Original Aspect Ratio", "Crop to Vertical (9:16)"])
        config_grid.addWidget(self.combo_crop_ratio, 3, 1, 1, 3)
        
        config_grid.addWidget(QLabel("Hook Title / Name", self), 4, 0)
        self.edit_crop_title = QLineEdit(self)
        self.edit_crop_title.setPlaceholderText("Enter hook descriptive name (e.g., Marketing Hook Alpha)...")
        config_grid.addWidget(self.edit_crop_title, 4, 1, 1, 3)
        
        cropper_layout.addWidget(config_group)
        
        self.chk_delete_source = QCheckBox("Delete original video file after cropping (requires user confirmation)", self)
        self.chk_delete_source.setChecked(False)
        cropper_layout.addWidget(self.chk_delete_source)
        
        self.btn_crop_start = QPushButton("Crop & Export to Library", self)
        self.btn_crop_start.clicked.connect(self._start_crop)
        cropper_layout.addWidget(self.btn_crop_start)
        
        self.log_cropper = QPlainTextEdit(self)
        self.log_cropper.setReadOnly(True)
        self.log_cropper.setPlaceholderText("Cropping output logs...")
        cropper_layout.addWidget(self.log_cropper)
        
        self.tab_cropper = tab_cropper
        self.tabs.addTab(tab_cropper, "Hook Cropper")

        # ─── TAB 3: HOOK MERGER ────────────────────────────
        tab_merger = QWidget(self)
        merger_layout = QVBoxLayout(tab_merger)
        merger_layout.setSpacing(12)
        
        merger_group = QGroupBox("MANUAL HOOK MERGER TOOL (Prepend Hook to Video)", self)
        merger_grid = QGridLayout(merger_group)
        merger_grid.setSpacing(10)
        
        merger_grid.addWidget(QLabel("Selected Hook File", self), 0, 0)
        self.edit_merge_hook = QLineEdit(self)
        self.edit_merge_hook.setReadOnly(True)
        self.btn_open_merge_hook_folder = QPushButton("📂 Open Folder", self)
        self.btn_open_merge_hook_folder.clicked.connect(lambda: self._open_folder_of_path(self.edit_merge_hook.text()))
        merger_grid.addWidget(self.edit_merge_hook, 0, 1)
        merger_grid.addWidget(self.btn_open_merge_hook_folder, 0, 2)
        
        merger_grid.addWidget(QLabel("Target Video File", self), 1, 0)
        self.edit_merge_video = QLineEdit(self)
        self.btn_browse_merge_video = QPushButton("Browse...", self)
        self.btn_browse_merge_video.clicked.connect(self._browse_merge_video_file)
        self.btn_open_merge_video_folder = QPushButton("📂 Open Folder", self)
        self.btn_open_merge_video_folder.clicked.connect(lambda: self._open_folder_of_path(self.edit_merge_video.text()))
        
        merger_grid.addWidget(self.edit_merge_video, 1, 1)
        h_lay_merge_video = QHBoxLayout()
        h_lay_merge_video.addWidget(self.btn_browse_merge_video)
        h_lay_merge_video.addWidget(self.btn_open_merge_video_folder)
        merger_grid.addLayout(h_lay_merge_video, 1, 2)
        
        merger_grid.addWidget(QLabel("Output File Path", self), 2, 0)
        self.edit_merge_output = QLineEdit(self)
        self.btn_browse_merge_output = QPushButton("Browse...", self)
        self.btn_browse_merge_output.clicked.connect(self._browse_merge_output_file)
        self.btn_open_merge_output_folder = QPushButton("📂 Open Folder", self)
        self.btn_open_merge_output_folder.clicked.connect(lambda: self._open_folder_of_path(self.edit_merge_output.text()))
        
        merger_grid.addWidget(self.edit_merge_output, 2, 1)
        h_lay_merge_output = QHBoxLayout()
        h_lay_merge_output.addWidget(self.btn_browse_merge_output)
        h_lay_merge_output.addWidget(self.btn_open_merge_output_folder)
        merger_grid.addLayout(h_lay_merge_output, 2, 2)
        
        self.btn_merge_manual = QPushButton("Merge Hook with Video", self)
        self.btn_merge_manual.clicked.connect(self._start_manual_merge)
        merger_grid.addWidget(self.btn_merge_manual, 3, 0, 1, 3)
        
        merger_layout.addWidget(merger_group)
        
        self.progress_merger = QProgressBar(self)
        self.progress_merger.setValue(0)
        merger_layout.addWidget(self.progress_merger)
        
        self.log_merger = QPlainTextEdit(self)
        self.log_merger.setReadOnly(True)
        self.log_merger.setPlaceholderText("Merge console output logs...")
        merger_layout.addWidget(self.log_merger)
        
        self.tab_merger = tab_merger
        self.tabs.addTab(tab_merger, "Hook Merger")
        
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
        self.btn_cancel_download.setEnabled(True)
        self.progress_download.setRange(0, 0)
        self.progress_download.setValue(0)
        self.log_downloader.clear()
        
        if hasattr(self.parent(), 'telemetry'):
            self.parent().telemetry.report_download(url)
        
        ffmpeg_exe = get_ffmpeg_path()
        cookie_browser = self.combo_cookies.currentText()
        if cookie_browser.startswith("File: ") and hasattr(self, 'selected_cookie_file') and self.selected_cookie_file:
            cookie_browser = self.selected_cookie_file
            
        self.dl_worker = DownloadWorker(url, self.settings.download_dir, ffmpeg_exe, cookie_browser=cookie_browser)
        self.dl_worker.log.connect(self.log_downloader.appendPlainText)
        self.dl_worker.progress.connect(self._on_download_progress)
        self.dl_worker.finished.connect(self._on_download_finished)
        self.dl_worker.start()

    def _on_downloader_cookie_selection_changed(self, index):
        if self.combo_cookies.itemText(index) == "cookies.txt (Browse...)":
            f_path, _ = QFileDialog.getOpenFileName(self, "Select cookies.txt file", "", "Text Files (*.txt)")
            if f_path:
                self.selected_cookie_file = f_path
                self.combo_cookies.setItemText(index, f"File: {Path(f_path).name}")
                self.combo_cookies.setToolTip(f"Using cookies file: {f_path}")
            else:
                self.combo_cookies.setCurrentIndex(0)

    def _on_download_progress(self, val: int):
        if val < 0:
            self.progress_download.setRange(0, 0)
        else:
            self.progress_download.setRange(0, 100)
            self.progress_download.setValue(val)

    def _cancel_download(self):
        if hasattr(self, 'dl_worker') and self.dl_worker.isRunning():
            self.dl_worker._cancelled = True
            self.log_downloader.appendPlainText("[Info] Cancelling download...")
            self.btn_cancel_download.setEnabled(False)

    def _on_download_finished(self, success: bool, msg: str, file_path: str):
        self.btn_download.setEnabled(True)
        self.btn_analyze.setEnabled(True)
        self.btn_cancel_download.setEnabled(False)
        self.progress_download.setRange(0, 100)
        if success:
            self.progress_download.setValue(100)
            self.edit_downloaded_path.setText(file_path)
            QMessageBox.information(self, "Success", f"Download Completed!\nFile saved to: {file_path}")
            # Auto-fill crop source input field and switch to Cropper Tab
            self.edit_crop_source.setText(file_path)
            self.edit_crop_title.setText(Path(file_path).stem)
            self.tabs.setCurrentIndex(1)
        else:
            self.progress_download.setValue(0)
            self.edit_downloaded_path.clear()
            if "cancelled by user" in msg.lower():
                QMessageBox.warning(self, "Cancelled", "Download was cancelled by the user.")
            else:
                QMessageBox.critical(self, "Failed", f"Download failed: {msg}")

    def _copy_downloaded_path(self):
        path = self.edit_downloaded_path.text().strip()
        if path:
            from PyQt6.QtWidgets import QApplication
            QApplication.clipboard().setText(path)
            QMessageBox.information(self, "Copied", "Path copied to clipboard!")

    # CROPPER Tab Actions
    def _browse_crop_file(self):
        f_path, _ = QFileDialog.getOpenFileName(
            self, "Select Source Video", str(self.settings.download_dir), "Video Files (*.mp4 *.mkv *.avi *.mov)"
        )
        if f_path:
            self.edit_crop_source.setText(f_path)
            self.edit_crop_title.setText(Path(f_path).stem)

    def _show_help_dialog(self):
        from dola_automation.info_dialogs import HookFactoryHelpDialog
        dlg = HookFactoryHelpDialog(self)
        dlg.exec()

    def enforce_plan_limits(self, plan_name: str):
        self.tabs.blockSignals(True)
        while self.tabs.count() > 0:
            self.tabs.removeTab(0)
            
        if plan_name in ['1-Day Trial', 'Creator Plan']:
            # Only add Media Downloader
            self.tabs.addTab(self.tab_grabber, "Media Downloader")
        else:
            # Add all tabs for Studio Pro
            self.tabs.addTab(self.tab_grabber, "Media Downloader")
            self.tabs.addTab(self.tab_cropper, "Hook Cropper")
            self.tabs.addTab(self.tab_merger, "Hook Merger")
            
        self.tabs.blockSignals(False)

    def _analyze_source_video(self):
        src_path = self.edit_crop_source.text().strip()
        if not src_path or not os.path.exists(src_path):
            QMessageBox.warning(self, "Invalid Source", "Please select a valid source video file first.")
            return
            
        self.btn_analyze_video.setEnabled(False)
        self.btn_crop_start.setEnabled(False)
        self.log_cropper.clear()
        self.log_cropper.appendPlainText("Analyzing video content...")
        
        ffmpeg_exe = get_ffmpeg_path()
        self.analyzer_worker = VideoAnalyzerWorker(ffmpeg_exe, src_path)
        self.analyzer_worker.log.connect(self.log_cropper.appendPlainText)
        self.analyzer_worker.finished.connect(self._on_video_analysis_finished)
        self.analyzer_worker.start()
        
    def _on_video_analysis_finished(self, success, duration, title, transcription):
        self.btn_analyze_video.setEnabled(True)
        self.btn_crop_start.setEnabled(True)
        
        if success:
            self.spin_crop_end.setValue(duration)
            self.edit_crop_title.setText(title)
            if transcription:
                QMessageBox.information(
                    self, "Analysis Complete",
                    f"Hook analysis completed successfully!\n\n"
                    f"Suggested Duration: {duration:.2f}s (based on visual transition)\n"
                    f"Transcribed Text: \"{transcription}\"\n\n"
                    f"Set crop end time and title accordingly."
                )
            else:
                QMessageBox.information(
                    self, "Analysis Complete",
                    f"Visual scene analysis completed!\n\n"
                    f"Suggested Duration: {duration:.2f}s\n"
                    f"Set crop end time accordingly (speech recognition had no results)."
                )
        else:
            QMessageBox.warning(self, "Analysis Failed", "Failed to analyze video hook content.")

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
            
            # Signal is emitted, which main window handles to switch to Hook Library and refresh it

            # Check if user wants to delete original source
            if self.chk_delete_source.isChecked():
                src_path = self.edit_crop_source.text().strip()
                if os.path.exists(src_path):
                    confirm = QMessageBox.question(
                        self, "Delete Original Video?",
                        f"Crop successful!\n\nWould you like to delete the original source video file:\n{src_path}?",
                        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                        QMessageBox.StandardButton.No
                    )
                    if confirm == QMessageBox.StandardButton.Yes:
                        try:
                            os.unlink(src_path)
                            self.log_cropper.appendPlainText("Original source video file deleted successfully.")
                            self.edit_crop_source.clear()
                        except Exception as e:
                            self.log_cropper.appendPlainText(f"[Warning] Could not delete original file: {e}")
        else:
            QMessageBox.critical(self, "Failed", f"Crop failed: {msg}")

    # LIBRARY Tab Actions
    def _load_saved_hooks(self):
        self.table_hooks.setRowCount(0)
        
        # Clear existing cards in grid_cards
        while self.grid_cards.count():
            item = self.grid_cards.takeAt(0)
            widget = item.widget()
            if widget:
                widget.deleteLater()
                
        try:
            hooks = self.db.list_viral_hooks()
            self.table_hooks.setRowCount(len(hooks))
            
            for row_idx, r in enumerate(hooks):
                # Update hidden table
                self.table_hooks.setItem(row_idx, 0, QTableWidgetItem(str(r['id'])))
                self.table_hooks.setItem(row_idx, 1, QTableWidgetItem(r['title']))
                self.table_hooks.setItem(row_idx, 2, QTableWidgetItem(f"{r['duration']:.1f}s"))
                self.table_hooks.setItem(row_idx, 3, QTableWidgetItem(r['aspect_ratio']))
                self.table_hooks.setItem(row_idx, 4, QTableWidgetItem(r['created_at'][:19].replace('T', ' ')))
                self.table_hooks.setItem(row_idx, 5, QTableWidgetItem(r['file_path']))
                
                # Generate thumbnail if not exists
                video_path = r['file_path']
                thumb_path = Path(video_path).with_suffix('.jpg')
                
                if os.path.exists(video_path) and not thumb_path.exists():
                    try:
                        ffmpeg_exe = get_ffmpeg_path()
                        # Extract single frame at 1 second
                        cmd = [
                            str(ffmpeg_exe),
                            "-y",
                            "-ss", "00:00:01",
                            "-i", video_path,
                            "-vframes", "1",
                            "-s", "160x100",
                            str(thumb_path)
                        ]
                        subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=2)
                    except Exception as ex:
                        logger.error(f"Failed to generate thumbnail for {video_path}: {ex}")
                
                # Create a card frame
                card = QFrame(self.scroll_content)
                card.setObjectName("card")
                card.setFixedWidth(180)
                card.setStyleSheet("QFrame#card { background: rgba(18, 34, 24, 0.4); border: 1px solid rgba(46, 74, 56, 0.5); border-radius: 10px; padding: 10px; } QFrame#card:hover { border-color: #2ecc71; background: rgba(24, 46, 31, 0.5); }")
                card_layout = QVBoxLayout(card)
                card_layout.setSpacing(8)
                
                # Thumbnail image
                lbl_thumb = QLabel(card)
                lbl_thumb.setFixedSize(160, 100)
                lbl_thumb.setScaledContents(True)
                lbl_thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
                lbl_thumb.setCursor(Qt.CursorShape.PointingHandCursor)
                lbl_thumb.mousePressEvent = lambda event, p=video_path: self._play_in_app(p)
                
                if thumb_path.exists():
                    lbl_thumb.setPixmap(QPixmap(str(thumb_path)))
                else:
                    # Generic visual fallback
                    lbl_thumb.setText("🎬 Click to Play")
                    lbl_thumb.setStyleSheet("background-color: #0c1a12; color: rgba(255,255,255,0.4); font-weight: bold; border-radius: 6px; border: 1px dashed rgba(46, 74, 56, 0.5);")
                
                card_layout.addWidget(lbl_thumb, 0, Qt.AlignmentFlag.AlignCenter)
                
                # Title
                lbl_title = QLabel(r['title'], card)
                lbl_title.setWordWrap(True)
                lbl_title.setStyleSheet("font-weight: bold; font-size: 12px; color: #F0FDF4;")
                lbl_title.setToolTip(r['title'])
                lbl_title.setFixedHeight(36) # Keep title height consistent
                card_layout.addWidget(lbl_title)
                
                # Info tag (duration + aspect ratio)
                lbl_info = QLabel(f"⏱️ {r['duration']:.1f}s  |  📱 {r['aspect_ratio']}", card)
                lbl_info.setStyleSheet("font-size: 10px; color: rgba(240, 253, 244, 0.6);")
                card_layout.addWidget(lbl_info)
                
                # Path Row
                path_row = QHBoxLayout()
                path_row.setSpacing(2)
                
                lbl_path = QLineEdit(card)
                lbl_path.setText(video_path)
                lbl_path.setReadOnly(True)
                lbl_path.setStyleSheet("font-size: 9px; color: rgba(240,253,244,0.4); background: transparent; border: none;")
                lbl_path.setToolTip(video_path)
                
                btn_copy_card_path = QPushButton(card)
                btn_copy_card_path.setIcon(self.style().standardIcon(self.style().StandardPixmap.SP_DialogSaveButton))
                btn_copy_card_path.setToolTip("Copy Path")
                btn_copy_card_path.setFixedSize(16, 16)
                btn_copy_card_path.clicked.connect(lambda checked, p=video_path: self._copy_card_path(p))
                
                btn_open_card_folder = QPushButton(card)
                btn_open_card_folder.setIcon(self.style().standardIcon(self.style().StandardPixmap.SP_DialogOpenButton))
                btn_open_card_folder.setToolTip("Open Folder")
                btn_open_card_folder.setFixedSize(16, 16)
                btn_open_card_folder.clicked.connect(lambda checked, p=video_path: self._open_folder_of_path(p))
                
                path_row.addWidget(lbl_path)
                path_row.addWidget(btn_copy_card_path)
                path_row.addWidget(btn_open_card_folder)
                card_layout.addLayout(path_row)
                
                # Action Buttons layout
                btn_layout = QHBoxLayout()
                btn_layout.setSpacing(4)
                
                btn_select = QPushButton("Merge", card)
                btn_select.setStyleSheet("padding: 4px 6px; font-size: 9px; font-weight: bold; background-color: #122218; border: 1px solid #2ecc71;")
                btn_select.clicked.connect(lambda checked, idx=row_idx, path=video_path, t=r['title']: self._on_card_select(idx, path, t))
                
                btn_preview = QPushButton("Play", card)
                btn_preview.setStyleSheet("padding: 4px 6px; font-size: 9px;")
                btn_preview.clicked.connect(lambda checked, p=video_path: self._play_in_app(p))
                
                btn_delete = QPushButton("Delete", card)
                btn_delete.setStyleSheet("padding: 4px 6px; font-size: 9px; color: #ef4444;")
                btn_delete.clicked.connect(lambda checked, i=r['id'], t=r['title'], p=video_path: self._delete_direct(i, t, p))
                
                btn_layout.addWidget(btn_select)
                btn_layout.addWidget(btn_preview)
                btn_layout.addWidget(btn_delete)
                card_layout.addLayout(btn_layout)
                
                # Calculate grid position (5 cards per row)
                grid_row = row_idx // 5
                grid_col = row_idx % 5
                self.grid_cards.addWidget(card, grid_row, grid_col, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
                
        except Exception as e:
            logger.error(f"Error loading saved hooks: {e}")

    def _on_card_select(self, row_idx, file_path, hook_title):
        self.table_hooks.selectRow(row_idx)
        self.edit_merge_hook.setText(file_path)
        self.log_merger.appendPlainText(f"Selected hook for merging: {hook_title}")
        self.tabs.setCurrentIndex(3) # Switch to Merger tab

    def _play_preview_direct(self, file_path):
        try:
            open_media_file(file_path)
        except Exception as e:
            QMessageBox.warning(self, "Player Error", f"Could not launch media player: {e}")

    def _play_in_app(self, file_path):
        if not os.path.exists(file_path):
            QMessageBox.warning(self, "File Not Found", f"The hook file could not be found: {file_path}")
            return
        dlg = VideoPlayerDialog(file_path, title=f"Previewing Hook: {Path(file_path).name}", parent=self)
        dlg.exec()



    def _open_folder_of_path(self, path):
        path = path.strip()
        if path:
            try:
                open_in_file_manager(path)
            except Exception as e:
                QMessageBox.warning(self, "Folder Error", f"Could not open folder: {e}")

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


class ProfileOutliersWidget(QWidget):
    load_url_to_downloader = pyqtSignal(str)

    def __init__(self, parent, db, settings):
        super().__init__(parent)
        self.parent_window = parent
        self.db = db
        self.settings = settings
        self.analyzer_worker = None
        self.selected_cookie_file = ""
        self._build_ui()

    def _build_ui(self):
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(15, 15, 15, 15)
        main_layout.setSpacing(12)

        title_lbl = QLabel("PROFILE OUTLIERS & PERFORMANCE ANALYZER", self)
        title_lbl.setStyleSheet("font-size: 18px; font-weight: bold; color: #ffffff;")
        main_layout.addWidget(title_lbl)

        # Niche channel search panel
        self.analyzer_group = QGroupBox("NICHE CHANNEL / PROFILE FEED ANALYZER", self)
        analyzer_grid = QVBoxLayout(self.analyzer_group)
        
        analyzer_input_row = QHBoxLayout()
        self.edit_profile_url = QLineEdit(self)
        self.edit_profile_url.setPlaceholderText("Enter channel/account profile feed URL (e.g. YouTube / Instagram URL)...")
        
        lbl_cookies = QLabel("Cookies:", self)
        self.combo_cookies = QComboBox(self)
        self.combo_cookies.addItems(["No Cookies", "cookies.txt (Browse...)", "Chrome", "Firefox", "Edge", "Opera", "Brave", "Vivaldi", "Safari"])
        self.combo_cookies.setToolTip("Select browser or a cookies.txt file to read login cookies from (essential for restricted pages).")
        self.combo_cookies.setMinimumWidth(110)
        self.combo_cookies.currentIndexChanged.connect(self._on_cookie_selection_changed)
        
        self.btn_analyze = QPushButton("Analyze Account (Outliers)", self)
        self.btn_analyze.clicked.connect(self._analyze_profile)
        
        analyzer_input_row.addWidget(self.edit_profile_url)
        analyzer_input_row.addWidget(lbl_cookies)
        analyzer_input_row.addWidget(self.combo_cookies)
        analyzer_input_row.addWidget(self.btn_analyze)
        analyzer_grid.addLayout(analyzer_input_row)
        
        # Analysis Table
        self.table_analysis = QTableWidget(self)
        self.table_analysis.setColumnCount(6)
        self.table_analysis.setHorizontalHeaderLabels(["Outlier Alert", "Title", "Views", "Outlier Rate", "Duration", "Video URL"])
        self.table_analysis.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.table_analysis.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table_analysis.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table_analysis.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table_analysis.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table_analysis.doubleClicked.connect(self._on_analyzer_row_double_click)
        analyzer_grid.addWidget(self.table_analysis)
        
        lbl_hint = QLabel("💡 Tip: Double-click any row to load its URL into the Hook Factory Downloader automatically.", self)
        lbl_hint.setStyleSheet("color: rgba(255,255,255,0.5); font-style: italic; font-size: 11px;")
        analyzer_grid.addWidget(lbl_hint)
        main_layout.addWidget(self.analyzer_group)

        self.progress_analyzer = QProgressBar(self)
        self.progress_analyzer.setValue(0)
        self.progress_analyzer.setTextVisible(False)
        main_layout.addWidget(self.progress_analyzer)

        self.log_analyzer = QPlainTextEdit(self)
        self.log_analyzer.setReadOnly(True)
        self.log_analyzer.setPlaceholderText("Profile analyzer output logs...")
        main_layout.addWidget(self.log_analyzer)

    def _analyze_profile(self):
        url = self.edit_profile_url.text().strip()
        if not url:
            QMessageBox.warning(self, "Invalid URL", "Please enter an account profile feed URL.")
            return
            
        self.btn_analyze.setEnabled(False)
        self.log_analyzer.clear()
        self.progress_analyzer.setValue(0)
        self.table_analysis.setRowCount(0)
        
        cookie_browser = self.combo_cookies.currentText()
        if cookie_browser.startswith("File: ") and hasattr(self, 'selected_cookie_file') and self.selected_cookie_file:
            cookie_browser = self.selected_cookie_file
            
        self.analyzer_worker = ProfileAnalyzerWorker(url, cookie_browser=cookie_browser)
        self.analyzer_worker.log.connect(self.log_analyzer.appendPlainText)
        self.analyzer_worker.finished.connect(self._on_analysis_finished)
        
        self.progress_analyzer.setRange(0, 0)
        self.analyzer_worker.start()

    def _on_cookie_selection_changed(self, index):
        if self.combo_cookies.itemText(index) == "cookies.txt (Browse...)":
            f_path, _ = QFileDialog.getOpenFileName(self, "Select cookies.txt file", "", "Text Files (*.txt)")
            if f_path:
                self.selected_cookie_file = f_path
                self.combo_cookies.setItemText(index, f"File: {Path(f_path).name}")
                self.combo_cookies.setToolTip(f"Using cookies file: {f_path}")
            else:
                self.combo_cookies.setCurrentIndex(0)

    def _on_analysis_finished(self, entries: list, median_views: float):
        self.btn_analyze.setEnabled(True)
        self.progress_analyzer.setRange(0, 100)
        self.progress_analyzer.setValue(100)
        
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
            rate_str = "N/A"
            if views is not None:
                try:
                    v_val = int(views)
                    views_str = f"{v_val:,}"
                    if median_views > 0:
                        rate_val = v_val / median_views
                        rate_str = f"{rate_val:.1f}x"
                        if v_val >= 1.5 * median_views:
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
                font = outlier_item.font()
                font.setBold(True)
                outlier_item.setFont(font)
                
            self.table_analysis.setItem(row_idx, 0, outlier_item)
            self.table_analysis.setItem(row_idx, 1, QTableWidgetItem(title))
            self.table_analysis.setItem(row_idx, 2, QTableWidgetItem(views_str))
            self.table_analysis.setItem(row_idx, 3, QTableWidgetItem(rate_str))
            self.table_analysis.setItem(row_idx, 4, QTableWidgetItem(duration_str))
            self.table_analysis.setItem(row_idx, 5, QTableWidgetItem(v_url))
            
        self.log_analyzer.appendPlainText(f"Analysis loaded! Populated {len(entries)} items. Outliers highlighted in table.")

    def _on_analyzer_row_double_click(self, index):
        row = index.row()
        url_item = self.table_analysis.item(row, 5)
        if url_item:
            v_url = url_item.text().strip()
            if v_url:
                self.load_url_to_downloader.emit(v_url)


class HookLibraryWidget(QWidget):
    select_hook_for_merging = pyqtSignal(str, str)

    def __init__(self, parent, db, settings):
        super().__init__(parent)
        self.parent_window = parent
        self.db = db
        self.settings = settings
        
        # Ensure hook output directory is constructed
        self.hooks_dir = self.settings.download_dir / "viral_hooks"
        self.hooks_dir.mkdir(parents=True, exist_ok=True)
        
        self._build_ui()
        self._load_saved_hooks()

    def _build_ui(self):
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(15, 15, 15, 15)
        main_layout.setSpacing(12)

        # Header titles
        header_layout = QHBoxLayout()
        title_lbl = QLabel("SAVED HOOK LIBRARY & MARKETPLACE PREP", self)
        title_lbl.setStyleSheet("font-size: 18px; font-weight: bold; color: #ffffff;")
        header_layout.addWidget(title_lbl)
        header_layout.addStretch()
        
        self.btn_refresh_lib = QPushButton("Refresh Library", self)
        self.btn_refresh_lib.clicked.connect(self._load_saved_hooks)
        header_layout.addWidget(self.btn_refresh_lib)
        main_layout.addLayout(header_layout)

        # Hidden table of hooks (maintained for backward compatibility with settings dropdown and index selection)
        self.table_hooks = QTableWidget(self)
        self.table_hooks.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table_hooks.setColumnCount(6)
        self.table_hooks.setHorizontalHeaderLabels(["ID", "Hook Name", "Duration", "Format", "Created At", "File Path"])
        self.table_hooks.setVisible(False)
        main_layout.addWidget(self.table_hooks)

        # Visual scrollable area for card grid
        self.scroll_area = QScrollArea(self)
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setMinimumHeight(450)
        self.scroll_area.setStyleSheet("QScrollArea { border: none; background-color: transparent; }")
        
        self.scroll_content = QWidget(self)
        self.scroll_content.setStyleSheet("background-color: transparent;")
        self.grid_cards = QGridLayout(self.scroll_content)
        self.grid_cards.setSpacing(15)
        self.grid_cards.setContentsMargins(0, 0, 0, 0)
        self.scroll_area.setWidget(self.scroll_content)
        main_layout.addWidget(self.scroll_area)

    def _load_saved_hooks(self):
        self.table_hooks.setRowCount(0)
        
        # Clear existing cards in grid_cards
        while self.grid_cards.count():
            item = self.grid_cards.takeAt(0)
            widget = item.widget()
            if widget:
                widget.deleteLater()
                
        try:
            hooks = self.db.list_viral_hooks()
            self.table_hooks.setRowCount(len(hooks))
            
            for row_idx, r in enumerate(hooks):
                # Update hidden table
                self.table_hooks.setItem(row_idx, 0, QTableWidgetItem(str(r['id'])))
                self.table_hooks.setItem(row_idx, 1, QTableWidgetItem(r['title']))
                self.table_hooks.setItem(row_idx, 2, QTableWidgetItem(f"{r['duration']:.1f}s"))
                self.table_hooks.setItem(row_idx, 3, QTableWidgetItem(r['aspect_ratio']))
                self.table_hooks.setItem(row_idx, 4, QTableWidgetItem(r['created_at'][:19].replace('T', ' ')))
                self.table_hooks.setItem(row_idx, 5, QTableWidgetItem(r['file_path']))
                
                # Generate thumbnail if not exists
                video_path = r['file_path']
                thumb_path = Path(video_path).with_suffix('.jpg')
                
                if os.path.exists(video_path) and not thumb_path.exists():
                    try:
                        ffmpeg_exe = get_ffmpeg_path()
                        # Extract single frame at 1 second
                        cmd = [
                            str(ffmpeg_exe),
                            "-y",
                            "-ss", "00:00:01",
                            "-i", video_path,
                            "-vframes", "1",
                            "-s", "160x100",
                            str(thumb_path)
                        ]
                        subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=2)
                    except Exception as ex:
                        logger.error(f"Failed to generate thumbnail for {video_path}: {ex}")
                
                # Create a card frame
                card = QFrame(self.scroll_content)
                card.setObjectName("card")
                card.setFixedWidth(180)
                card.setStyleSheet("QFrame#card { background: rgba(18, 34, 24, 0.4); border: 1px solid rgba(46, 74, 56, 0.5); border-radius: 10px; padding: 10px; } QFrame#card:hover { border-color: #2ecc71; background: rgba(24, 46, 31, 0.5); }")
                card_layout = QVBoxLayout(card)
                card_layout.setSpacing(8)
                
                # Thumbnail image
                lbl_thumb = QLabel(card)
                lbl_thumb.setFixedSize(160, 100)
                lbl_thumb.setScaledContents(True)
                lbl_thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
                lbl_thumb.setCursor(Qt.CursorShape.PointingHandCursor)
                lbl_thumb.mousePressEvent = lambda event, p=video_path: self._play_in_app(p)
                
                if thumb_path.exists():
                    lbl_thumb.setPixmap(QPixmap(str(thumb_path)))
                else:
                    lbl_thumb.setText("🎬 Click to Play")
                    lbl_thumb.setStyleSheet("background-color: #0c1a12; color: rgba(255,255,255,0.4); font-weight: bold; border-radius: 6px; border: 1px dashed rgba(46, 74, 56, 0.5);")
                
                card_layout.addWidget(lbl_thumb, 0, Qt.AlignmentFlag.AlignCenter)
                
                # Title
                lbl_title = QLabel(r['title'], card)
                lbl_title.setWordWrap(True)
                lbl_title.setStyleSheet("font-weight: bold; font-size: 12px; color: #F0FDF4;")
                lbl_title.setToolTip(r['title'])
                lbl_title.setFixedHeight(36)
                card_layout.addWidget(lbl_title)
                
                # Info tag (duration + aspect ratio)
                lbl_info = QLabel(f"⏱️ {r['duration']:.1f}s  |  📱 {r['aspect_ratio']}", card)
                lbl_info.setStyleSheet("font-size: 10px; color: rgba(240, 253, 244, 0.6);")
                card_layout.addWidget(lbl_info)
                
                # Path Row
                path_row = QHBoxLayout()
                path_row.setSpacing(2)
                
                lbl_path = QLineEdit(card)
                lbl_path.setText(video_path)
                lbl_path.setReadOnly(True)
                lbl_path.setStyleSheet("font-size: 9px; color: rgba(240,253,244,0.4); background: transparent; border: none;")
                lbl_path.setToolTip(video_path)
                
                btn_copy_card_path = QPushButton(card)
                btn_copy_card_path.setIcon(self.style().standardIcon(self.style().StandardPixmap.SP_DialogSaveButton))
                btn_copy_card_path.setToolTip("Copy Path")
                btn_copy_card_path.setFixedSize(16, 16)
                btn_copy_card_path.clicked.connect(lambda checked, p=video_path: self._copy_card_path(p))
                
                btn_open_card_folder = QPushButton(card)
                btn_open_card_folder.setIcon(self.style().standardIcon(self.style().StandardPixmap.SP_DialogOpenButton))
                btn_open_card_folder.setToolTip("Open Folder")
                btn_open_card_folder.setFixedSize(16, 16)
                btn_open_card_folder.clicked.connect(lambda checked, p=video_path: self._open_folder_of_path(p))
                
                path_row.addWidget(lbl_path)
                path_row.addWidget(btn_copy_card_path)
                path_row.addWidget(btn_open_card_folder)
                card_layout.addLayout(path_row)
                
                # Action Buttons layout
                btn_layout = QHBoxLayout()
                btn_layout.setSpacing(4)
                
                btn_select = QPushButton("Merge", card)
                btn_select.setStyleSheet("padding: 4px 6px; font-size: 9px; font-weight: bold; background-color: #122218; border: 1px solid #2ecc71;")
                btn_select.clicked.connect(lambda checked, path=video_path, t=r['title']: self.select_hook_for_merging.emit(path, t))
                
                btn_preview = QPushButton("Play", card)
                btn_preview.setStyleSheet("padding: 4px 6px; font-size: 9px;")
                btn_preview.clicked.connect(lambda checked, p=video_path: self._play_in_app(p))
                
                btn_delete = QPushButton("Delete", card)
                btn_delete.setStyleSheet("padding: 4px 6px; font-size: 9px; color: #ef4444;")
                btn_delete.clicked.connect(lambda checked, i=r['id'], t=r['title'], p=video_path: self._delete_direct(i, t, p))
                
                btn_layout.addWidget(btn_select)
                btn_layout.addWidget(btn_preview)
                btn_layout.addWidget(btn_delete)
                card_layout.addLayout(btn_layout)
                
                # Calculate grid position (5 cards per row)
                grid_row = row_idx // 5
                grid_col = row_idx % 5
                self.grid_cards.addWidget(card, grid_row, grid_col, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
                
        except Exception as e:
            logger.error(f"Error loading saved hooks: {e}")

    def _play_in_app(self, video_path):
        if not os.path.exists(video_path):
            QMessageBox.warning(self, "Missing File", f"The video file no longer exists at path:\n{video_path}")
            return
            
        dialog = VideoPlayerDialog(video_path, parent=self)
        dialog.exec()

    def _copy_card_path(self, path):
        from PyQt6.QtWidgets import QApplication
        QApplication.clipboard().setText(path)
        QMessageBox.information(self, "Copied", "Path copied to clipboard!")

    def _open_folder_of_path(self, path):
        if not path:
            return
        p = Path(path)
        folder = p.parent if p.is_file() else p
        if folder.exists():
            open_media_file(str(folder))
        else:
            QMessageBox.warning(self, "Folder Not Found", "The target path or folder does not exist.")

    def _delete_direct(self, hook_id, title, video_path):
        confirm = QMessageBox.question(
            self, "Confirm Delete",
            f"Are you sure you want to permanently delete the hook '{title}' from your library and delete its file?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        if confirm == QMessageBox.StandardButton.Yes:
            try:
                # Delete from database
                self.db.delete_viral_hook(hook_id)
                # Delete filesystem files (video and thumbnail)
                p = Path(video_path)
                if p.exists():
                    p.unlink()
                thumb = p.with_suffix('.jpg')
                if thumb.exists():
                    thumb.unlink()
                    
                # Reload library view
                self._load_saved_hooks()
                QMessageBox.information(self, "Deleted", f"Hook '{title}' has been successfully deleted.")
            except Exception as e:
                QMessageBox.critical(self, "Error", f"Failed to delete hook: {e}")

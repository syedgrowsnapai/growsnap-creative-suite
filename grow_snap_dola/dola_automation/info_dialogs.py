from PyQt6.QtWidgets import QDialog, QVBoxLayout, QLabel, QTextBrowser, QPushButton, QHBoxLayout, QLineEdit, QMessageBox, QFrame, QFormLayout, QCheckBox, QProgressBar
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QTimer
from PyQt6.QtGui import QIcon, QPixmap
from dola_automation.styles import APP_STYLE, GradientLabel
import sys
import os
import shutil
import platform
import zipfile
import tempfile
import urllib.request
from pathlib import Path

def get_resource_path(relative_path: str) -> Path:
    base_path = Path(__file__).parent.resolve()
    if hasattr(sys, '_MEIPASS'):
        base_path = Path(sys._MEIPASS) / 'dola_automation'
    return base_path / relative_path

class InstructionsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("GrowSnap AI — Instructions")
        self.setFixedSize(550, 550)
        self.setStyleSheet(APP_STYLE)
        
        icon_path = get_resource_path("resources/icon.png")
        if icon_path.exists():
            self.setWindowIcon(QIcon(str(icon_path)))
            
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        
        title = QLabel("Instructions & Operating Guide", self)
        title.setStyleSheet("font-size: 16px; font-weight: bold; color: #2ecc71;")
        layout.addWidget(title)
        
        browser = QTextBrowser(self)
        browser.setHtml("""
        <h3>How to run GrowSnap AI:</h3>
        <ol>
            <li><b>Prepare your CSV:</b> Your CSV should contain the columns: 
            <br/><code>Brand, Emotion, Video Title, Scene 1 (0-10s), Scene 2 (10-20s), Scene 3 (20-30s), Scene 4 (30-40s), CTA Keyword, Long Caption</code>.</li>
            <li><b>Reference Images:</b> You can map reference images sequentially by picking image files or a folder. Reference images will be uploaded per scene.</li>
            <li><b>Configure Settings:</b> Adjust aspect ratio (9:16 or 16:9), threads, and watermark removal options (blur or crop).</li>
            <li><b>Start Batch:</b> Click <b>Start batch</b>.
                <ul>
                    <li><b>Phase 1: Submission:</b> It opens browser instances sequentially to submit each scene's prompt, saving the generated chat URLs.</li>
                    <li><b>Phase 2: Downloading:</b> After all scenes are submitted, it prompts you with a timer. When download starts, it opens the chat sessions to extract, download, and auto-remove watermarks.</li>
                    <li><b>Scene Concatenation:</b> Once all scenes for a given "Video Title" are downloaded, the software losslessly merges them using FFmpeg into a single master video file.</li>
                </ul>
            </li>
        </ol>
        
        <h3>Recommended Settings (Autopilot):</h3>
        <ul>
            <li><b>Threads:</b> <code>1</code> (Prevents rate-limiting and browser overload)</li>
            <li><b>New browser per video:</b> <code>Enabled</code> (Enforces fresh, clean submission contexts)</li>
            <li><b>Headless mode:</b> <code>Disabled (Unchecked)</code> (Recommended so you can monitor progress and handle captchas if needed)</li>
            <li><b>Submit & Close:</b> <code>Enabled</code> (Recommended for bulk runs; submits all prompts quickly first, then downloads them later)</li>
            <li><b>Launch Delay:</b> <code>5 - 10s</code> (Provides time for Chrome instances to boot cleanly)</li>
            <li><b>Paste Delay:</b> <code>2 - 3s</code> (Ensures prompt text boxes are fully focused before typing)</li>
            <li><b>Wait (Submit Delay):</b> <code>15s</code> (Crucial for Dola to register the prompt and redirect to a chat URL)</li>
            <li><b>Timeout:</b> <code>500s</code> (Allows ample time for video generation on Dola's server)</li>
            <li><b>Auto DL Delay:</b> <code>5 min</code> (Staggers downloading to match generation speed)</li>
            <li><b>Watermark Method:</b> <code>Blur</code> or <code>Crop</code> (Removes the Dola logo dynamically)</li>
        </ul>
        
        <h3>Simultaneous Sessions & Hardware Specs:</h3>
        <p>You can configure the thread count in the Settings tab to run multiple browser sessions in parallel. Note that each chromium browser session consumes around 200MB - 350MB of RAM and requires bandwidth to load pages and stream video assets. We recommend the following configurations:</p>
        <ul>
            <li><b>1 - 4 Threads:</b> Suitable for average computers or basic internet connections (50 - 100 Mbps).</li>
            <li><b>5 - 12 Threads:</b> Recommended for modern mid-tier PCs (8GB+ free RAM) with standard high-speed broadband (100 - 250 Mbps).</li>
            <li><b>12 - 30+ Threads:</b> Recommended for high-end workstation configurations (16GB+ free RAM) and high-speed fiber internet (300+ Mbps).</li>
        </ul>
        
        <p><i>Note: The first time the browser opens, log into your Dola account if required. The session cookies will be saved in your job folder so downloads can run automatically.</i></p>
        """)
        layout.addWidget(browser)
        
        btn_close = QPushButton("Close", self)
        btn_close.clicked.connect(self.accept)
        layout.addWidget(btn_close)

class IssuesDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("GrowSnap AI — Troubleshooting")
        self.setFixedSize(500, 350)
        self.setStyleSheet(APP_STYLE)
        
        icon_path = get_resource_path("resources/icon.png")
        if icon_path.exists():
            self.setWindowIcon(QIcon(str(icon_path)))
            
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        
        title = QLabel("Common Issues & Fixes", self)
        title.setStyleSheet("font-size: 16px; font-weight: bold; color: #D97706;")
        layout.addWidget(title)
        
        browser = QTextBrowser(self)
        browser.setHtml("""
        <h3>Troubleshooting Checklist:</h3>
        <ul>
            <li><b>Browser login required on every run:</b> Verify that you completed at least one submission successfully so that <code>auth_state.json</code> gets written to your Documents directory.</li>
            <li><b>Popup modal blocks clicking Video tab:</b> The software automatically dismisses standard popups. If it gets stuck, you can set the browser to "Headed" mode in settings, log in/close the modal manually, and the scheduler will proceed.</li>
            <li><b>FFmpeg concat failing:</b> Ensure that <code>ffmpeg.exe</code> is installed on your system or located in the application's root directory. Check the log file in the <code>logs/</code> directory for the exact FFmpeg error dump.</li>
            <li><b>Browser closes too quickly:</b> Increase the "Launch Delay" or "Paste Delay" spinner values in the Settings tab.</li>
        </ul>
        """)
        layout.addWidget(browser)
        
        btn_close = QPushButton("Close", self)
        btn_close.clicked.connect(self.accept)
        layout.addWidget(btn_close)

class SupportDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("GrowSnap AI — Developer Support")
        self.setFixedSize(400, 200)
        self.setStyleSheet(APP_STYLE)
        
        icon_path = get_resource_path("resources/icon.png")
        if icon_path.exists():
            self.setWindowIcon(QIcon(str(icon_path)))
            
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        
        title = QLabel("Developer Support & Feedback", self)
        title.setStyleSheet("font-size: 14px; font-weight: bold; color: #2ecc71; text-align: center;")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)
        
        label = QLabel("For custom features, bug fixes, or enterprise deployments, contact our support line directly via WhatsApp.", self)
        label.setWordWrap(True)
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(label)
        
        btn_close = QPushButton("Got it", self)
        btn_close.clicked.connect(self.accept)
        layout.addWidget(btn_close)

class ActivationDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("GrowSnap AI — Software Activation")
        self.setFixedSize(520, 470)
        self.setStyleSheet(APP_STYLE)
        
        icon_path = get_resource_path("resources/icon.png")
        if icon_path.exists():
            self.setWindowIcon(QIcon(str(icon_path)))
            
        self._build_ui()
        self._load_stored_details()
        
    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        layout.setContentsMargins(25, 20, 25, 20)
        
        # Logo image at the top center
        lbl_logo = QLabel(self)
        logo_path = get_resource_path("resources/icon.png")
        if logo_path.exists():
            pixmap = QPixmap(str(logo_path))
            scaled_pixmap = pixmap.scaled(80, 80, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
            lbl_logo.setPixmap(scaled_pixmap)
        lbl_logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(lbl_logo)
        
        lbl_title = GradientLabel("GrowSnap AI", self)
        lbl_title.setObjectName("title")
        lbl_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(lbl_title)
        
        self.lbl_sub = QLabel("Please enter your registered email and license activation key.", self)
        self.lbl_sub.setStyleSheet("color: #F0FDF4; font-size: 13px; font-weight: 500;")
        self.lbl_sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.lbl_sub)
        
        form_frame = QFrame(self)
        form_frame.setStyleSheet("background: rgba(18, 34, 24, 0.5); border: 1px solid rgba(46, 74, 56, 0.4); border-radius: 8px;")
        form_layout = QFormLayout(form_frame)
        form_layout.setContentsMargins(15, 15, 15, 15)
        form_layout.setSpacing(10)
        
        self.txt_email = QLineEdit(self)
        self.txt_email.setPlaceholderText("your.email@example.com")
        self.txt_email.setStyleSheet("background: #0c1a12; border: 1px solid rgba(46, 74, 56, 0.5); color: #F0FDF4;")
        
        self.txt_key = QLineEdit(self)
        self.txt_key.setPlaceholderText("Enter activation key...")
        self.txt_key.setStyleSheet("background: #0c1a12; border: 1px solid rgba(46, 74, 56, 0.5); color: #F0FDF4;")
        
        # Hardware ID display for node-locking
        from dola_automation.licensing import get_hardware_id
        self.txt_hwid = QLineEdit(self)
        self.txt_hwid.setReadOnly(True)
        self.txt_hwid.setText(get_hardware_id())
        self.txt_hwid.setStyleSheet("background: #09120c; border: 1px dashed rgba(46, 74, 56, 0.7); color: #2ecc71; font-weight: bold; font-family: monospace;")
        
        lbl_email_hdr = QLabel("Email Address:", self)
        lbl_email_hdr.setStyleSheet("font-weight: 700; color: rgba(255, 255, 255, 0.85);")
        lbl_key_hdr = QLabel("Activation Key:", self)
        lbl_key_hdr.setStyleSheet("font-weight: 700; color: rgba(255, 255, 255, 0.85);")
        lbl_hwid_hdr = QLabel("Your Hardware ID:", self)
        lbl_hwid_hdr.setStyleSheet("font-weight: 700; color: rgba(255, 255, 255, 0.85);")
        
        form_layout.addRow(lbl_email_hdr, self.txt_email)
        form_layout.addRow(lbl_key_hdr, self.txt_key)
        form_layout.addRow(lbl_hwid_hdr, self.txt_hwid)
        layout.addWidget(form_frame)
        
        lbl_pricing = QLabel("Need an activation key or free 1-Day Trial? Contact support.", self)
        lbl_pricing.setStyleSheet("color: rgba(240, 253, 244, 0.8); font-size: 12px; font-weight: 500;")
        lbl_pricing.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(lbl_pricing)
        
        btn_layout = QHBoxLayout()
        self.btn_upgrade = QPushButton("Upgrade / Get Trial", self)
        self.btn_upgrade.clicked.connect(self._upgrade_plan)
        
        self.btn_activate = QPushButton("Activate", self)
        self.btn_activate.setObjectName("primary")
        self.btn_activate.clicked.connect(self._activate)
        
        self.btn_exit = QPushButton("Exit", self)
        self.btn_exit.setObjectName("danger")
        self.btn_exit.clicked.connect(self.reject)
        
        btn_layout.addWidget(self.btn_upgrade)
        btn_layout.addWidget(self.btn_activate)
        btn_layout.addWidget(self.btn_exit)
        layout.addLayout(btn_layout)
        
    def _upgrade_plan(self):
        import webbrowser
        import urllib.parse
        from dola_automation.licensing import get_hardware_id
        msg = urllib.parse.quote(f"Hi! I'm interested in purchasing a license / free trial for GrowSnap AI.\nMy Hardware ID is: {get_hardware_id()}")
        webbrowser.open(f"https://wa.me/923138694809?text={msg}")
        
    def _activate(self):
        email = self.txt_email.text().strip()
        key = self.txt_key.text().strip()
        
        if not email or not key:
            QMessageBox.warning(self, "Input Error", "Please fill in both email and key fields.")
            return
            
        from dola_automation.licensing import verify_key, save_license
        is_valid, msg, details = verify_key(email, key)
        if is_valid:
            plan = details.get('plan', 'Activated Plan')
            expiry = details.get('expiry', '')
            hw = details.get('hardware', '')
            
            save_license(email, key, plan, expiry, hw)
            QMessageBox.information(self, "Activated", f"Activation successful!\n\nPlan: {plan}\nExpires: {expiry}\n\nThank you for using GrowSnap AI!")
            self.accept()
        else:
            QMessageBox.critical(self, "Activation Failed", f"Activation failed:\n{msg}")
            
    def _load_stored_details(self):
        from dola_automation.licensing import get_license_file_path, verify_key
        import json
        lic_file = get_license_file_path()
        if lic_file.exists():
            try:
                with open(lic_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                email = data.get('email', '')
                key = data.get('key', '')
                if email:
                    self.txt_email.setText(email)
                if key:
                    self.txt_key.setText(key)
                
                if email and key:
                    is_valid, msg, details = verify_key(email, key)
                    if not is_valid:
                        self.lbl_sub.setText(f"❌ License Error: {msg}")
                        self.lbl_sub.setStyleSheet("color: #ef4444; font-size: 12px; font-weight: bold;")
            except Exception:
                pass

class ThreadsWarningDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("GrowSnap AI — High Concurrency Warning")
        self.setFixedSize(550, 520)
        self.setStyleSheet(APP_STYLE)
        
        icon_path = get_resource_path("resources/icon.png")
        if icon_path.exists():
            self.setWindowIcon(QIcon(str(icon_path)))
            
        self._build_ui()
        
    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(15)
        layout.setContentsMargins(20, 20, 20, 20)
        
        title = QLabel("⚠️ Caution: High Concurrency Settings Warning", self)
        title.setStyleSheet("font-size: 16px; font-weight: bold; color: #D97706;")
        layout.addWidget(title)
        
        browser = QTextBrowser(self)
        browser.setHtml("""
        <p style='color: #F0FDF4; font-size: 13px;'>Running multiple simultaneous browser instances is highly resource intensive. Each thread launches an isolated Chromium page session which consumes substantial memory (RAM), processor time (CPU), and network bandwidth.</p>
        
        <h4 style='color: #2ecc71; margin-bottom: 5px; margin-top: 10px;'>Recommended Minimum Hardware & Network Configurations:</h4>
        <table border='1' cellpadding='6' style='border-color: rgba(46, 74, 56, 0.4); border-collapse: collapse; width: 100%; color: #F0FDF4; font-size: 12px;'>
            <tr style='background: rgba(18, 34, 24, 0.6);'>
                <th>Concurrent Sessions</th>
                <th>Recommended Minimum Specifications</th>
                <th>Required Internet Speed</th>
            </tr>
            <tr>
                <td><b>5 Threads simultaneously</b></td>
                <td>4-Core modern CPU, 8 GB Free System RAM</td>
                <td>100 - 150 Mbps stable connection</td>
            </tr>
            <tr>
                <td><b>10 Threads simultaneously</b></td>
                <td>8-Core modern CPU, 16 GB Free System RAM</td>
                <td>200 - 250 Mbps stable connection</td>
            </tr>
            <tr>
                <td><b>16 Threads simultaneously</b></td>
                <td>12-Core+ high-end CPU, 32 GB Free System RAM</td>
                <td>300+ Mbps Fiber-optic connection</td>
            </tr>
        </table>
        
        <h4 style='color: #e74c3c; margin-bottom: 5px; margin-top: 15px;'>⚠️ Risks and Issues of Ignoring These Settings:</h4>
        <p style='color: #f39c12; margin-top: 0px; font-size: 12px;'>Ignoring these minimum requirements may severely slow down your operating system, cause your system to freeze, or result in the following issues:</p>
        <ul style='color: #F0FDF4; margin-top: 5px; font-size: 12px; line-height: 1.4;'>
            <li><b>System Slowdowns & Lag:</b> CPU saturation will cause mouse cursor stuttering, interface lags, and slow down other open applications.</li>
            <li><b>Total System Freeze/Crash:</b> Memory (RAM) exhaustion can lead to your laptop completely freezing, requiring a physical hard reboot.</li>
            <li><b>Browser Crashes (OOM):</b> Playwright/Chromium browser instances will crash mid-run due to Out-Of-Memory errors.</li>
            <li><b>Network Timeout Failures:</b> Saturating your internet bandwidth leads to web page loading timeouts, resulting in failed prompt submissions or broken downloads.</li>
            <li><b>Dola Rate Limiting & Account Locks:</b> Running too many parallel sessions from a single IP address triggers Dola's security/spam filters, causing failed request errors or temporary locks.</li>
        </ul>
        """)
        layout.addWidget(browser)
        
        # Checkbox for confirmation
        self.chk_confirm = QCheckBox("I confirm that my system meets the minimum hardware & internet speed specifications.", self)
        self.chk_confirm.setStyleSheet("color: #F0FDF4; font-size: 11px;")
        self.chk_confirm.stateChanged.connect(self._on_check_changed)
        layout.addWidget(self.chk_confirm)
        
        btn_layout = QHBoxLayout()
        self.btn_confirm = QPushButton("Confirm & Proceed", self)
        self.btn_confirm.setObjectName("primary")
        self.btn_confirm.setEnabled(False)  # Disabled until checked
        self.btn_confirm.clicked.connect(self.accept)
        
        self.btn_cancel = QPushButton("Cancel", self)
        self.btn_cancel.setObjectName("danger")
        self.btn_cancel.clicked.connect(self.reject)
        
        btn_layout.addWidget(self.btn_cancel)
        btn_layout.addWidget(self.btn_confirm)
        layout.addLayout(btn_layout)
        
    def _on_check_changed(self, state):
        self.btn_confirm.setEnabled(self.chk_confirm.isChecked())

class UpdateDialog(QDialog):
    def __init__(self, update_data, is_mandatory=False, parent=None):
        super().__init__(parent)
        self.update_data = update_data
        self.is_mandatory = is_mandatory
        self.downloader = None
        
        self.setWindowTitle("GrowSnap AI — Update Available")
        self.setFixedSize(500, 390)
        self.setStyleSheet(APP_STYLE)
        
        icon_path = get_resource_path("resources/icon.png")
        if icon_path.exists():
            self.setWindowIcon(QIcon(str(icon_path)))
            
        self._build_ui()
        
    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(15)
        layout.setContentsMargins(20, 20, 20, 20)
        
        title_text = "⚠️ Mandatory Update Required" if self.is_mandatory else "🚀 New Update Available"
        self.lbl_title = QLabel(title_text, self)
        color = "#ef4444" if self.is_mandatory else "#2ecc71"
        self.lbl_title.setStyleSheet(f"font-size: 18px; font-weight: bold; color: {color};")
        self.lbl_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.lbl_title)
        
        self.lbl_ver = QLabel(f"Version {self.update_data.get('version', 'unknown')} is available.", self)
        self.lbl_ver.setStyleSheet("color: rgba(255, 255, 255, 0.85); font-size: 13px;")
        self.lbl_ver.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.lbl_ver)
        
        lbl_notes_hdr = QLabel("Release Notes / Upgrades:", self)
        lbl_notes_hdr.setStyleSheet("font-weight: bold; color: rgba(255, 255, 255, 0.6); font-size: 11px;")
        layout.addWidget(lbl_notes_hdr)
        
        self.txt_notes = QTextBrowser(self)
        self.txt_notes.setHtml(self.update_data.get("release_notes", "No release notes.").replace("\n", "<br/>"))
        self.txt_notes.setStyleSheet("background: #0c1a12; border: 1px solid rgba(46, 74, 56, 0.5); color: #F0FDF4;")
        layout.addWidget(self.txt_notes)
        
        self.progress_bar = QProgressBar(self)
        self.progress_bar.setValue(0)
        self.progress_bar.setVisible(False)
        layout.addWidget(self.progress_bar)
        
        self.lbl_status = QLabel("", self)
        self.lbl_status.setStyleSheet("color: #2ecc71; font-size: 11px;")
        self.lbl_status.setVisible(False)
        layout.addWidget(self.lbl_status)
        
        btn_layout = QHBoxLayout()
        self.btn_update = QPushButton("Install Update", self)
        self.btn_update.setObjectName("primary")
        self.btn_update.clicked.connect(self._start_download)
        
        if self.is_mandatory:
            self.btn_cancel = QPushButton("Exit Application", self)
            self.btn_cancel.setObjectName("danger")
            self.btn_cancel.clicked.connect(self.reject)
        else:
            self.btn_cancel = QPushButton("Update Later", self)
            self.btn_cancel.clicked.connect(self.reject)
            
        btn_layout.addWidget(self.btn_cancel)
        btn_layout.addWidget(self.btn_update)
        layout.addLayout(btn_layout)
        
    def _start_download(self):
        self.btn_update.setEnabled(False)
        self.btn_cancel.setEnabled(False)
        
        self.progress_bar.setVisible(True)
        self.lbl_status.setVisible(True)
        self.lbl_status.setText("Connecting to update server...")
        
        from dola_automation.updater import UpdateDownloader
        url = self.update_data.get("download_url", "")
        
        self.downloader = UpdateDownloader(url, self)
        self.downloader.progress.connect(self._on_progress)
        self.downloader.completed.connect(self._on_completed)
        self.downloader.failed.connect(self._on_failed)
        self.downloader.start()
        
    def _on_progress(self, percent):
        self.progress_bar.setValue(percent)
        self.lbl_status.setText(f"Downloading update: {percent}%")
        
    def _on_completed(self, file_path):
        self.lbl_status.setText("Download complete! Launching installer...")
        from dola_automation.updater import launch_installer
        success = launch_installer(file_path)
        if success:
            self.accept()
        else:
            self._on_failed("Failed to launch the downloaded installer package.")
            
    def _on_failed(self, err_msg):
        QMessageBox.critical(self, "Update Failed", f"Failed to download or run the update:\n{err_msg}")
        self.btn_update.setEnabled(True)
        self.btn_cancel.setEnabled(True)
        self.progress_bar.setVisible(False)
        self.lbl_status.setVisible(False)
        
    def closeEvent(self, event):
        if self.downloader and self.downloader.isRunning():
            event.ignore()
        else:
            super().closeEvent(event)

class WatermarkHelpDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Watermark Removal Tool — Instructions")
        self.setFixedSize(520, 480)
        self.setStyleSheet(APP_STYLE)
        
        icon_path = get_resource_path("resources/icon.png")
        if icon_path.exists():
            self.setWindowIcon(QIcon(str(icon_path)))
            
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        
        title = QLabel("Watermark Removal Tool Guide", self)
        title.setStyleSheet("font-size: 16px; font-weight: bold; color: #2ecc71;")
        layout.addWidget(title)
        
        browser = QTextBrowser(self)
        browser.setHtml("""
        <h3>Overview</h3>
        <p>This tool is designed to remove watermarks from your videos visually losslessly. You can process a single video or batch-process an entire folder of videos.</p>
        
        <h3>Removal Methods</h3>
        <ul>
            <li><b>Blur Method (Delogo):</b> Applies a smart blur filter over a specific box. Perfect for logo watermarks.
                <ul>
                    <li><b>Recommended Preset for Portrait (9:16):</b> <code>Blur X:Y:W:H = 540:1220:170:80</code>. This targets the bottom-right watermark area.</li>
                    <li>Adjust X and Y to move the box, and W and H to change the size of the blur box.</li>
                </ul>
            </li>
            <li><b>Crop Method:</b> Shaves off a specific number of pixels from the bottom of the video.
                <ul>
                    <li><b>Recommended Preset:</b> <code>80 pixels</code>. Shaves off the bottom watermark band cleanly.</li>
                </ul>
            </li>
        </ul>
        
        <h3>How to use:</h3>
        <ol>
            <li>Select <b>Mode</b> (Folder Batch or Single Video).</li>
            <li>Choose your <b>Method</b> (Blur or Crop).</li>
            <li>Select the <b>Input</b> file or folder.</li>
            <li>Select the <b>Output</b> destination folder.</li>
            <li>Click <b>START PROCESSING</b>. The progress bar and console logs will show the real-time status.</li>
        </ol>
        """)
        layout.addWidget(browser)
        
        btn_close = QPushButton("Close", self)
        btn_close.clicked.connect(self.accept)
        layout.addWidget(btn_close)

class MergerHelpDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Video Merger — Instructions")
        self.setFixedSize(520, 420)
        self.setStyleSheet(APP_STYLE)
        
        icon_path = get_resource_path("resources/icon.png")
        if icon_path.exists():
            self.setWindowIcon(QIcon(str(icon_path)))
            
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        
        title = QLabel("Video Merger Tool Guide", self)
        title.setStyleSheet("font-size: 16px; font-weight: bold; color: #2ecc71;")
        layout.addWidget(title)
        
        browser = QTextBrowser(self)
        browser.setHtml("""
        <h3>Overview</h3>
        <p>The Video Merger lets you concatenate multiple separate video clips together sequentially. It performs a <b>lossless copy</b> merge using FFmpeg, meaning the videos are joined in seconds without losing quality or re-encoding.</p>
        
        <h3>How to use:</h3>
        <ol>
            <li>Click <b>Add Videos</b> to select the video clips you want to join. They will appear in the list.</li>
            <li>Select a video in the list and use the <b>Move Up</b> / <b>Move Down</b> buttons to re-order them into the correct sequence.</li>
            <li>Click <b>Remove Selected</b> or <b>Clear All</b> to clean up the queue.</li>
            <li>Click <b>Select Output File</b> to pick where to save the final merged video (must end in <code>.mp4</code>).</li>
            <li>Click <b>START MERGING</b>.</li>
        </ol>
        
        <h3>Important Notes:</h3>
        <ul>
            <li>All video segments must share the same resolution, frame rate, and encoding format to merge losslessly.</li>
            <li>After merging completed successfully, you will be prompted to clean up and delete the raw scene clips if desired.</li>
        </ul>
        """)
        layout.addWidget(browser)
        
        btn_close = QPushButton("Close", self)
        btn_close.clicked.connect(self.accept)
        layout.addWidget(btn_close)

class DependencySetupWorker(QThread):
    progress = pyqtSignal(int)      # Progress percentage (0-100)
    status = pyqtSignal(str)        # Status description text
    completed = pyqtSignal()        # Finished successfully
    failed = pyqtSignal(str)        # Failed with error message

    def run(self):
        try:
            # 1. CHECK AND INSTALL FFMPEG
            self.status.emit("Checking for FFmpeg...")
            self.progress.emit(10)
            
            ffmpeg_path = self._find_ffmpeg()
            if not ffmpeg_path:
                self.status.emit("FFmpeg not found. Downloading static build...")
                self._download_and_install_ffmpeg()
            
            # Double check ffmpeg was successfully installed
            ffmpeg_path = self._find_ffmpeg()
            if not ffmpeg_path:
                raise Exception("FFmpeg could not be found or installed automatically.")
                
            self.progress.emit(50)
            
            # 2. CHECK AND INSTALL PATCHRIGHT CHROMIUM
            self.status.emit("Checking for custom web browser engine...")
            if not self._is_chromium_installed():
                self.status.emit("Installing anti-detect Chromium browser (this may take 1-2 minutes)...")
                self._install_patchright_chromium()
                
            # Verify chromium is installed
            if not self._is_chromium_installed():
                raise Exception("Anti-detect browser engine could not be installed automatically.")
                
            self.progress.emit(100)
            self.status.emit("Initialization complete!")
            self.completed.emit()
            
        except Exception as e:
            self.failed.emit(str(e))

    def _find_ffmpeg(self):
        # 1. Try PATH
        val = shutil.which("ffmpeg")
        if val:
            return val
        # 2. Try resources relative to this file
        res_dir = Path(__file__).parent.resolve() / 'resources'
        ext = ".exe" if os.name == "nt" else ""
        local_ffmpeg = res_dir / f"ffmpeg{ext}"
        if local_ffmpeg.exists():
            return str(local_ffmpeg)
        return None

    def _is_chromium_installed(self):
        try:
            from patchright.sync_api import sync_playwright
            with sync_playwright() as p:
                executable = p.chromium.executable_path
                if executable and os.path.exists(executable):
                    return True
        except Exception:
            pass
        return False

    def _download_and_install_ffmpeg(self):
        sys_name = platform.system()
        if sys_name == "Windows":
            url = "https://github.com/shussain/growsnap-releases/releases/download/deps/ffmpeg-win.zip"
        elif sys_name == "Linux":
            url = "https://github.com/shussain/growsnap-releases/releases/download/deps/ffmpeg-linux.zip"
        else:
            raise Exception(f"Unsupported operating system: {sys_name}")

        temp_dir = Path(tempfile.gettempdir())
        zip_path = temp_dir / "ffmpeg_temp.zip"
        
        req = urllib.request.Request(
            url, 
            headers={'User-Agent': 'Mozilla/5.0 GrowSnapSetupEngine/1.0'}
        )
        
        with urllib.request.urlopen(req, timeout=15) as response:
            total_size = int(response.headers.get('content-length', 0))
            bytes_downloaded = 0
            block_size = 8192
            
            with open(zip_path, 'wb') as f:
                while True:
                    buffer = response.read(block_size)
                    if not buffer:
                        break
                    f.write(buffer)
                    bytes_downloaded += len(buffer)
                    if total_size > 0:
                        percent = 15 + int((bytes_downloaded / total_size) * 30)
                        self.progress.emit(percent)
                        self.status.emit(f"Downloading FFmpeg: {int((bytes_downloaded / total_size) * 100)}%")

        self.status.emit("Extracting FFmpeg...")
        res_dir = Path(__file__).parent.resolve() / 'resources'
        res_dir.mkdir(parents=True, exist_ok=True)
        
        with zipfile.ZipFile(zip_path, 'r') as zip_ref:
            for member in zip_ref.namelist():
                filename = os.path.basename(member)
                if filename in ("ffmpeg", "ffmpeg.exe"):
                    source = zip_ref.open(member)
                    target_path = res_dir / filename
                    with open(target_path, "wb") as target:
                        shutil.copyfileobj(source, target)
                    if os.name != "nt":
                        target_path.chmod(0o755)
                        
        if zip_path.exists():
            zip_path.unlink()

    def _install_patchright_chromium(self):
        self.status.emit("Downloading custom browser engine (this might take a moment)...")
        import subprocess
        import sys
        
        cmd = [sys.executable, "-m", "patchright", "install", "chromium"]
        
        creationflags = 0
        if os.name == 'nt':
            creationflags = subprocess.CREATE_NO_WINDOW
            
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            creationflags=creationflags
        )
        
        while True:
            line = process.stdout.readline()
            if not line:
                break
            line_str = line.strip()
            if line_str:
                if "Downloading" in line_str or "Percent" in line_str:
                    self.status.emit(f"Browser installation: {line_str}")
                    
        process.wait()
        if process.returncode != 0:
            try:
                from patchright.driver import main as patchright_driver_main
                patchright_driver_main(["install", "chromium"])
            except Exception as e:
                raise Exception(f"Failed to install patchright chromium: {e}")

class DependencyInstallerDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("GrowSnap Creative Suite — First Run Setup")
        self.setFixedSize(480, 240)
        self.setStyleSheet(APP_STYLE)
        
        icon_path = get_resource_path("resources/icon.png")
        if icon_path.exists():
            self.setWindowIcon(QIcon(str(icon_path)))
            
        self._build_ui()
        self.success = False
        
    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(15)
        
        title = QLabel("Initial System Calibration", self)
        title.setStyleSheet("font-size: 18px; font-weight: bold; color: #2ecc71;")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)
        
        self.lbl_status = QLabel("Initializing application settings...", self)
        self.lbl_status.setStyleSheet("color: rgba(255, 255, 255, 0.85); font-size: 12px;")
        self.lbl_status.setWordWrap(True)
        self.lbl_status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.lbl_status)
        
        self.progress_bar = QProgressBar(self)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        layout.addWidget(self.progress_bar)
        
        self.btn_cancel = QPushButton("Cancel Setup", self)
        self.btn_cancel.clicked.connect(self._on_cancel)
        layout.addWidget(self.btn_cancel)
        
    def start_installation(self):
        self.worker = DependencySetupWorker(self)
        self.worker.progress.connect(self._on_progress)
        self.worker.status.connect(self._on_status)
        self.worker.completed.connect(self._on_completed)
        self.worker.failed.connect(self._on_failed)
        self.worker.start()
        
    def _on_progress(self, val):
        self.progress_bar.setValue(val)
        
    def _on_status(self, text):
        self.lbl_status.setText(text)
        
    def _on_completed(self):
        self.success = True
        self.accept()
        
    def _on_failed(self, error_msg):
        QMessageBox.critical(self, "Setup Failed", f"Setup failed to configure dependencies:\n{error_msg}\n\nPlease check your internet connection and try running the app again.")
        self.reject()
        
    def _on_cancel(self):
        if hasattr(self, 'worker') and self.worker.isRunning():
            self.worker.terminate()
            self.worker.wait()
        self.reject()

    def closeEvent(self, event):
        self._on_cancel()
        super().closeEvent(event)

def check_and_install_dependencies() -> bool:
    """
    Checks if FFmpeg and Patchright Chromium are installed.
    If not, opens the setup wizard dialog to download and calibrate them.
    """
    ext = ".exe" if os.name == "nt" else ""
    res_dir = Path(__file__).parent.resolve() / 'resources'
    local_ffmpeg = res_dir / f"ffmpeg{ext}"
    ffmpeg_ready = shutil.which("ffmpeg") is not None or local_ffmpeg.exists()
    
    browser_ready = False
    try:
        from patchright.sync_api import sync_playwright
        with sync_playwright() as p:
            executable = p.chromium.executable_path
            if executable and os.path.exists(executable):
                browser_ready = True
    except Exception:
        pass
        
    if ffmpeg_ready and browser_ready:
        return True
        
    dialog = DependencyInstallerDialog()
    QTimer.singleShot(100, dialog.start_installation)
    dialog.exec()
    return dialog.success


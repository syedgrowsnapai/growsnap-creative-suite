import os
import json
import urllib.request
import tempfile
import subprocess
import sys
from pathlib import Path
from typing import Tuple, Optional
from PyQt6.QtCore import QThread, pyqtSignal

# Default free URL for update checking (e.g. GitHub releases, raw file, or gist)
# The developer can override this URL in the configuration
DEFAULT_UPDATE_URL = "https://raw.githubusercontent.com/syedgrowsnapai/growsnap-creative-suite/main/version.json"

def parse_version(v_str: str) -> Tuple[int, ...]:
    """Parses a version string like '1.2.3' into a tuple of integers (1, 2, 3)"""
    # Clean version string (remove 'v' or 'V' prefixes or build badges)
    cleaned = v_str.lower().replace('v', '').split('-')[0].strip()
    try:
        return tuple(map(int, cleaned.split('.')))
    except Exception:
        return (0, 0, 0)

def check_for_updates(current_version: str, update_url: str = DEFAULT_UPDATE_URL) -> Tuple[bool, dict, Optional[str]]:
    """
    Checks if a newer version is available.
    Returns (has_update, update_data, error_message)
    """
    try:
        req = urllib.request.Request(
            update_url, 
            headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) GrowSnapUpdateEngine/1.0'}
        )
        with urllib.request.urlopen(req, timeout=5) as response:
            data = json.loads(response.read().decode('utf-8'))
            
            latest_version_str = data.get("version", "1.0.0")
            latest_version = parse_version(latest_version_str)
            curr_version = parse_version(current_version)
            
            if latest_version > curr_version:
                return True, {
                    "version": latest_version_str,
                    "mandatory": bool(data.get("mandatory", False)),
                    "download_url": data.get("download_url", ""),
                    "release_notes": data.get("release_notes", "No release notes available.")
                }, None
            return False, {}, None
    except Exception as e:
        print(f"[Updater] Error checking updates: {e}", file=sys.stderr)
        return False, {}, str(e)

class UpdateDownloader(QThread):
    """
    Asynchronous QThread to download the update executable with progress reporting.
    """
    progress = pyqtSignal(int)      # Emits percentage downloaded
    completed = pyqtSignal(str)     # Emits path to downloaded file
    failed = pyqtSignal(str)        # Emits error message

    def __init__(self, download_url: str, parent=None):
        super().__init__(parent)
        self.download_url = download_url
        self._stopped = False

    def run(self):
        try:
            # Generate a secure file path in the temp directory
            temp_dir = Path(tempfile.gettempdir())
            # Sanitize output filename to prevent path traversal
            output_path = temp_dir / "growsnap_setup_latest.exe"
            
            # Prepare request
            req = urllib.request.Request(
                self.download_url,
                headers={'User-Agent': 'Mozilla/5.0 GrowSnapUpdateEngine/1.0'}
            )
            
            with urllib.request.urlopen(req, timeout=15) as response:
                total_size = int(response.headers.get('content-length', 0))
                bytes_downloaded = 0
                block_size = 8192
                
                with open(output_path, 'wb') as f:
                    while not self._stopped:
                        buffer = response.read(block_size)
                        if not buffer:
                            break
                        
                        f.write(buffer)
                        bytes_downloaded += len(buffer)
                        
                        if total_size > 0:
                            percent = int((bytes_downloaded / total_size) * 100)
                            self.progress.emit(percent)
                            
                if self._stopped:
                    if output_path.exists():
                        output_path.unlink()
                    self.failed.emit("Download cancelled.")
                    return
                    
                self.completed.emit(str(output_path))
                
        except Exception as e:
            self.failed.emit(str(e))

    def stop(self):
        self._stopped = True

def launch_installer(installer_path: str) -> bool:
    """
    Launches the downloaded installer and terminates the current Python app.
    """
    try:
        # Standard launch on Windows
        if os.name == 'nt':
            os.startfile(installer_path)
            return True
        else:
            # Linux/macOS fallback
            subprocess.Popen(["chmod", "+x", installer_path])
            subprocess.Popen([installer_path], shell=True)
            return True
    except Exception as e:
        print(f"[Updater] Error launching installer: {e}", file=sys.stderr)
        return False

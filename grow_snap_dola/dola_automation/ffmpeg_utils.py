import os
import sys
import subprocess
from pathlib import Path
from PyQt6.QtCore import QThread, pyqtSignal
import concurrent.futures
from typing import List, Tuple

def get_ffmpeg_path() -> Path:
    possible_paths = []
    
    # Check if frozen by PyInstaller
    if getattr(sys, 'frozen', False):
        if hasattr(sys, '_MEIPASS'):
            possible_paths.append(Path(sys._MEIPASS) / 'ffmpeg.exe')
            possible_paths.append(Path(sys._MEIPASS) / '_internal' / 'ffmpeg.exe')
            possible_paths.append(Path(sys._MEIPASS) / 'ffmpeg')
            
    # Check current directory and subdirs
    possible_paths.append(Path.cwd() / 'ffmpeg.exe')
    possible_paths.append(Path.cwd() / 'bin' / 'ffmpeg.exe')
    possible_paths.append(Path.cwd() / '_internal' / 'ffmpeg.exe')
    possible_paths.append(Path.cwd() / 'ffmpeg')
    
    # Check local resources subdirectory relative to this file
    this_dir = Path(__file__).parent.resolve()
    possible_paths.append(this_dir / 'resources' / 'ffmpeg.exe')
    possible_paths.append(this_dir / 'resources' / 'ffmpeg')
    
    # Check standard PATH
    path_env = os.environ.get('PATH', '')
    for p in path_env.split(os.pathsep):
        possible_paths.append(Path(p) / 'ffmpeg.exe')
        possible_paths.append(Path(p) / 'ffmpeg')
        
    for p in possible_paths:
        if p.exists() and p.is_file():
            return p
            
    # Fallback default name
    return Path('ffmpeg.exe' if os.name == 'nt' else 'ffmpeg')

def get_video_resolution(input_path: Path) -> Tuple[int, int]:
    import re
    ffmpeg_exe = get_ffmpeg_path()
    try:
        cmd = [str(ffmpeg_exe), '-i', str(input_path)]
        creationflags = 0
        if os.name == 'nt':
            creationflags = subprocess.CREATE_NO_WINDOW
        res = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            creationflags=creationflags
        )
        match = re.search(r' (\d{3,5})x(\d{3,5})[\s,]', res.stderr)
        if match:
            return int(match.group(1)), int(match.group(2))
    except Exception as e:
        print(f"Error getting video resolution: {e}")
    return 720, 1280

def get_video_duration(input_path: Path) -> float:
    import re
    ffmpeg_exe = get_ffmpeg_path()
    try:
        cmd = [str(ffmpeg_exe), '-i', str(input_path)]
        creationflags = 0
        if os.name == 'nt':
            creationflags = subprocess.CREATE_NO_WINDOW
        res = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            creationflags=creationflags
        )
        match = re.search(r'Duration:\s*(\d{2}):(\d{2}):(\d{2})\.(\d{2})', res.stderr)
        if match:
            hours = int(match.group(1))
            minutes = int(match.group(2))
            seconds = int(match.group(3))
            centiseconds = int(match.group(4))
            return hours * 3600.0 + minutes * 60.0 + seconds + centiseconds / 100.0
    except Exception as e:
        print(f"Error getting video duration: {e}")
    return 0.0

def process_video_watermark(input_path: Path, method: str, output_path: Path, 
                            blur_coords: Tuple[int, int, int, int] = (540, 1220, 170, 80), 
                            crop_pixels: int = 80) -> bool:
    """
    Renders watermarks invisible by either:
      1. Blurring using FFmpeg's delogo filter.
      2. Cropping pixels from the bottom of the frame.
    """
    ffmpeg_exe = get_ffmpeg_path()
    method_lower = method.lower()
    
    width, height = get_video_resolution(input_path)
    scale_x = width / 720.0
    scale_y = height / 1280.0
    
    orig_x, orig_y, orig_w, orig_h = blur_coords
    x = max(2, min(width - 3, int(orig_x * scale_x)))
    y = max(2, min(height - 3, int(orig_y * scale_y)))
    w = max(1, min(width - x - 2, int(orig_w * scale_x)))
    h = max(1, min(height - y - 2, int(orig_h * scale_y)))
    
    if method_lower == 'blur':
        vf_filter = f"delogo=x={x}:y={y}:w={w}:h={h}"
    elif method_lower == 'crop':
        crop_h = max(1, min(height - 1, int(crop_pixels * scale_y)))
        vf_filter = f"crop=iw:ih-{crop_h}:0:0"
    else:
        # No processing
        vf_filter = None
        
    temp_output = output_path.parent / f"temp_{output_path.name}"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    
    if vf_filter:
        cmd = [
            str(ffmpeg_exe), '-y',
            '-i', str(input_path),
            '-vf', vf_filter,
            '-c:v', 'libx264',
            '-preset', 'fast',
            '-crf', '17',
            '-c:a', 'copy',
            str(temp_output)
        ]
    else:
        cmd = [
            str(ffmpeg_exe), '-y',
            '-i', str(input_path),
            '-c:v', 'libx264',
            '-preset', 'fast',
            '-crf', '17',
            '-c:a', 'copy',
            str(temp_output)
        ]
        
    creationflags = 0
    if os.name == 'nt':
        creationflags = subprocess.CREATE_NO_WINDOW
        
    try:
        process = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            creationflags=creationflags,
            check=True
        )
        if temp_output.exists():
            if output_path.exists():
                output_path.unlink()
            temp_output.replace(output_path)
            return True
        return False
    except subprocess.CalledProcessError as e:
        print(f"FFmpeg process error: {e.stderr}")
        if temp_output.exists():
            temp_output.unlink()
        return False
    except Exception as e:
        print(f"FFmpeg running error: {e}")
        if temp_output.exists():
            temp_output.unlink()
        return False

def concatenate_videos(input_paths: List[str], output_path: str) -> bool:
    """
    Losslessly concatenates a list of scene video clips into a single final master video file using FFmpeg's concat demuxer.
    """
    ffmpeg_exe = get_ffmpeg_path()
    if not input_paths:
        return False
    
    txt_path = Path(output_path).with_name('concat_list.txt')
    try:
        with open(txt_path, 'w', encoding='utf-8') as f:
            for path in input_paths:
                p_str = str(Path(path).resolve()).replace('\\', '/')
                f.write(f"file '{p_str}'\n")
                
        cmd = [
            str(ffmpeg_exe), '-y',
            '-f', 'concat',
            '-safe', '0',
            '-i', str(txt_path),
            '-c', 'copy',
            str(output_path)
        ]
        
        creationflags = 0
        if os.name == 'nt':
            creationflags = subprocess.CREATE_NO_WINDOW
            
        res = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            creationflags=creationflags
        )
        
        if res.returncode == 0:
            return True
        else:
            print(f"FFmpeg merge/concat failed: {res.stderr}")
            return False
    except Exception as e:
        print(f"FFmpeg merge error: {e}")
        return False
    finally:
        if txt_path.exists():
            txt_path.unlink()

class ConverterWorker(QThread):
    progress = pyqtSignal(int)
    log = pyqtSignal(str)
    finished_batch = pyqtSignal()
    
    def __init__(self, input_paths: List[Path], output_dir: Path, method: str, 
                 blur_coords: Tuple[int, int, int, int], crop_pixels: int, max_threads: int = 4):
        super().__init__()
        self.input_paths = input_paths
        self.output_dir = output_dir
        self.method = method
        self.blur_coords = blur_coords
        self.crop_pixels = crop_pixels
        self.max_threads = max_threads
        self._stop = False
        
    def stop(self) -> None:
        self._stop = True
        
    def run(self) -> None:
        total = len(self.input_paths)
        if total == 0:
            self.finished_batch.emit()
            return
            
        self.log.emit(f"Starting conversion of {total} videos using method: {self.method}")
        completed = 0
        
        with concurrent.futures.ThreadPoolExecutor(max_workers=self.max_threads) as executor:
            future_to_path = {}
            for in_path in self.input_paths:
                if self._stop:
                    break
                out_path = self.output_dir / in_path.name
                future = executor.submit(
                    process_video_watermark,
                    in_path,
                    self.method,
                    out_path,
                    self.blur_coords,
                    self.crop_pixels
                )
                future_to_path[future] = in_path
                
            for future in concurrent.futures.as_completed(future_to_path):
                in_path = future_to_path[future]
                try:
                    success = future.result()
                    if success:
                        self.log.emit(f"Success: {in_path.name}")
                    else:
                        self.log.emit(f"Failed: {in_path.name}")
                except Exception as e:
                    self.log.emit(f"Error processing {in_path.name}: {e}")
                    
                completed += 1
                self.progress.emit(int((completed / total) * 100))
                
        if self._stop:
            self.log.emit("Conversion stopped by user.")
        else:
            self.log.emit("Batch conversion finished!")
        self.finished_batch.emit()

class MergerWorker(QThread):
    progress = pyqtSignal(int)
    log = pyqtSignal(str)
    finished = pyqtSignal(bool, str) # success, message
    
    def __init__(self, input_paths: List[str], output_path: str):
        super().__init__()
        self.input_paths = input_paths
        self.output_path = output_path
        
    def run(self) -> None:
        self.log.emit(f"Merging {len(self.input_paths)} files into '{self.output_path}'...")
        success = concatenate_videos(self.input_paths, self.output_path)
        if success:
            self.log.emit("Merge completed successfully!")
            self.finished.emit(True, "Merge completed successfully!")
        else:
            self.log.emit("Merge failed. Check log console.")
            self.finished.emit(False, "Merge failed.")


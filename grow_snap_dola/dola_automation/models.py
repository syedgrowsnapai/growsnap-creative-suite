from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
import csv
import io
from typing import Optional, List, Tuple

class JobStatus(Enum):
    PENDING = 'pending'
    RUNNING = 'running'
    WAITING = 'waiting'
    DOWNLOADING = 'downloading'
    COMPLETED = 'completed'
    SUBMITTED = 'submitted'
    FAILED = 'failed'
    NOT_FOUND = 'not found'
    SKIPPED = 'skipped'
    CANCELLED = 'cancelled'

@dataclass
class AutomationSettings:
    thread_count: int = 1
    one_browser_per_video: bool = True
    headless: bool = False
    submit_and_close: bool = False
    submit_close_delay_sec: int = 15
    inject_ui_downloader: bool = True
    model: str = 'SeaDance 2.0 Fast'
    duration: str = '10s'
    ratio: str = '9:16'
    generation_timeout_sec: int = 500
    poll_interval_sec: float = 3.0
    launch_delay_sec: int = 5
    paste_delay_sec: int = 2
    download_dir: Path = field(default_factory=lambda: Path.home() / 'Documents' / 'dola_downloads')
    auth_state_path: Path = field(default_factory=lambda: Path.home() / 'Documents' / 'dola_video_automation' / 'auth_state.json')
    auto_remove_watermark: bool = True
    auto_delete_scene_clips: bool = True
    prepend_viral_hook: bool = False
    selected_hook_id: int = -1
    watermark_method: str = 'Blur'
    watermark_blur_x: int = 540
    watermark_blur_y: int = 1220
    watermark_blur_w: int = 170
    watermark_blur_h: int = 80
    watermark_crop_pixels: int = 80
    generation_success_phrase: str = 'will be generated using'

@dataclass
class PromptJob:
    index: int
    prompt: str
    reference_image: Optional[Path] = None
    status: JobStatus = JobStatus.PENDING
    download_path: Optional[str] = None
    error: Optional[str] = None
    chat_url: Optional[str] = None
    session_id: Optional[int] = None
    job_id: Optional[int] = None
    caption: Optional[str] = None
    video_title: Optional[str] = None
    scene_index: Optional[int] = None
    started_at: Optional[str] = None

    @property
    def has_reference(self) -> bool:
        return self.reference_image is not None and self.reference_image.exists()

def parse_prompts(text: str) -> List[Tuple[str, str, Optional[str], Optional[int]]]:
    """
    Parses text either as the custom GrowSnap multi-scene CSV format, a standard prompt/caption CSV, 
    or plain text chunks separated by double newlines.
    
    Returns a list of tuples: (prompt, caption, video_title, scene_index)
    """
    normalized = text.replace('\r\n', '\n').strip()
    
    # Try parsing as CSV only if it looks like a valid CSV file
    is_csv = False
    # Check if first line contains common CSV headers as exact fields
    first_line = normalized.split('\n')[0] if normalized else ""
    if ',' in first_line:
        try:
            f_line = io.StringIO(first_line)
            reader = csv.reader(f_line)
            first_row = next(reader)
            first_row_cols = [col.strip().lower() for col in first_row]
            csv_keywords = {"video title", "prompt", "caption", "text", "brand", "emotion", "cta keyword"}
            if any(col in csv_keywords or col.startswith("scene") for col in first_row_cols):
                is_csv = True
        except Exception:
            pass
    
    if not is_csv and ',' in normalized and '\n' in normalized:
        # If it has commas and multiple lines, check if it's structured consistently as a CSV
        try:
            f = io.StringIO(normalized)
            reader = csv.reader(f)
            rows = list(reader)
            if len(rows) > 1:
                # Check if all non-empty rows have the same number of columns (at least 2)
                col_counts = [len(r) for r in rows if r]
                if len(col_counts) > 1 and all(count >= 2 for count in col_counts) and len(set(col_counts)) == 1:
                    is_csv = True
        except Exception:
            pass

    if is_csv:
        try:
            f = io.StringIO(normalized)
            reader = csv.reader(f)
            rows = list(reader)
            if len(rows) > 0:
                header = [col.strip().lower() for col in rows[0]]
                # Detect the multi-scene GrowSnap CSV format
                if "video title" in header or any("scene" in col for col in header):
                    video_title_idx = -1
                    scene_cols = [] # list of (col_index, scene_num)
                    caption_idx = -1
                    
                    for i, col in enumerate(header):
                        if "video title" in col:
                            video_title_idx = i
                        elif "caption" in col: # Handles both short/long caption, prefer long if present
                            if "long" in col or caption_idx == -1:
                                caption_idx = i
                        elif "scene" in col:
                            # Try to extract scene number
                            try:
                                # e.g. "scene 1 (0-10s)" -> 1
                                num = int(col.split("scene")[1].strip().split(" ")[0])
                                scene_cols.append((i, num))
                            except Exception:
                                pass
                    
                    # Sort scenes by number
                    scene_cols.sort(key=lambda x: x[1])
                    
                    results = []
                    # Process data rows
                    for r_idx, row in enumerate(rows[1:]):
                        if not row or len(row) <= max(video_title_idx, caption_idx):
                            continue
                        
                        raw_title = row[video_title_idx].strip() if video_title_idx != -1 else "Untitled Video"
                        v_title = f"{raw_title} (Row {r_idx+1})"
                        cap = row[caption_idx].strip() if caption_idx != -1 else ""
                        
                        for col_idx, scene_num in scene_cols:
                            if col_idx < len(row) and row[col_idx].strip():
                                scene_prompt = row[col_idx].strip()
                                results.append((scene_prompt, cap, v_title, scene_num))
                    
                    if results:
                        return results
                
                # CSV with headers, extract prompt/caption
                prompt_idx = -1
                cap_idx = -1
                for i, col in enumerate(header):
                    if "prompt" in col or "text" in col:
                        prompt_idx = i
                    elif "caption" in col or "desc" in col:
                        cap_idx = i
                
                if prompt_idx != -1:
                    results = []
                    for row in rows[1:]:
                        if not row:
                            continue
                        p = row[prompt_idx].strip() if len(row) > prompt_idx else ""
                        c = row[cap_idx].strip() if cap_idx != -1 and len(row) > cap_idx else ""
                        if p:
                            results.append((p, c, None, None))
                    if results:
                        return results
                
                # Fallback to standard 2-column CSV (prompt, caption) without header
                if len(rows[0]) >= 2 and not any(x in rows[0][0].lower() for x in ["prompt", "text", "scene"]):
                    results = []
                    for row in rows:
                        if len(row) >= 2:
                            results.append((row[0].strip(), row[1].strip(), None, None))
                        elif len(row) == 1:
                            results.append((row[0].strip(), "", None, None))
                    return results
        except Exception:
            pass

    # Fallback to double newline separation for plain text prompts
    results = []
    chunks = normalized.split('\n\n')
    for chunk in chunks:
        chunk = chunk.strip()
        if chunk:
            # Do NOT split by single newline to prevent truncating the prompt content.
            # Treat the entire chunk as the prompt, and caption as empty.
            results.append((chunk, "", None, None))
    return results

def align_reference_images(prompts: list, image_paths: List[Path]) -> List[Optional[Path]]:
    refs = []
    for i in range(len(prompts)):
        if i < len(image_paths):
            path = image_paths[i]
            refs.append(path if path.exists() else None)
        else:
            refs.append(None)
    return refs

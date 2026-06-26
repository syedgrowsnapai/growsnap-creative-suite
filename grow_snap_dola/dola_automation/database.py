from __future__ import annotations
import sqlite3
import datetime
from pathlib import Path
from typing import Optional, List
from dola_automation.models import JobStatus, PromptJob

def _utc_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()

UNSPECIFIED = object()

class HistoryDatabase:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        conn = self._connect()
        try:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    prompt_count INTEGER NOT NULL DEFAULT 0,
                    completed_count INTEGER NOT NULL DEFAULT 0,
                    failed_count INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    notes TEXT
                );

                CREATE TABLE IF NOT EXISTS jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id INTEGER NOT NULL,
                    job_index INTEGER NOT NULL,
                    prompt TEXT NOT NULL,
                    reference_image TEXT,
                    status TEXT NOT NULL,
                    download_path TEXT,
                    chat_url TEXT,
                    error TEXT,
                    started_at TEXT,
                    finished_at TEXT,
                    video_title TEXT,
                    scene_index INTEGER,
                    caption TEXT,
                    FOREIGN KEY (session_id) REFERENCES sessions(id)
                );

                CREATE TABLE IF NOT EXISTS downloads (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id INTEGER NOT NULL,
                    file_path TEXT NOT NULL,
                    file_size INTEGER,
                    downloaded_at TEXT NOT NULL,
                    FOREIGN KEY (job_id) REFERENCES jobs(id)
                );

                CREATE TABLE IF NOT EXISTS viral_hooks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    file_path TEXT NOT NULL,
                    duration REAL,
                    aspect_ratio TEXT,
                    platform TEXT,
                    source_url TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_jobs_session ON jobs(session_id);
                CREATE INDEX IF NOT EXISTS idx_downloads_job ON downloads(job_id);
            """)
            conn.commit()
        finally:
            conn.close()

    def create_session(self, name: str, jobs: List[PromptJob], notes: str = "") -> int:
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO sessions (name, prompt_count, created_at, notes) VALUES (?, ?, ?, ?)",
                (name, len(jobs), _utc_now(), notes)
            )
            session_id = cur.lastrowid
            
            for job in jobs:
                cur.execute("""
                    INSERT INTO jobs (session_id, job_index, prompt, reference_image, status, video_title, scene_index, caption)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    session_id,
                    job.index,
                    job.prompt,
                    str(job.reference_image) if job.reference_image else None,
                    job.status.value,
                    job.video_title,
                    job.scene_index,
                    job.caption
                ))
                job.job_id = cur.lastrowid
                job.session_id = session_id
                
            conn.commit()
            return session_id
        finally:
            conn.close()

    def load_session_jobs(self, session_id: int) -> List[PromptJob]:
        conn = self._connect()
        try:
            rows = conn.execute("""
                SELECT id, job_index, prompt, reference_image, status, download_path, chat_url, error, video_title, scene_index, caption, started_at
                FROM jobs WHERE session_id = ? ORDER BY job_index
            """, (session_id,)).fetchall()
            
            jobs = []
            for row in rows:
                ref_img = Path(row['reference_image']) if row['reference_image'] else None
                job = PromptJob(
                    index=row['job_index'],
                    prompt=row['prompt'],
                    reference_image=ref_img,
                    status=JobStatus(row['status']),
                    download_path=row['download_path'],
                    chat_url=row['chat_url'],
                    error=row['error'],
                    session_id=session_id,
                    job_id=row['id'],
                    video_title=row['video_title'],
                    scene_index=row['scene_index'],
                    caption=row['caption'],
                    started_at=row['started_at']
                )
                jobs.append(job)
            return jobs
        finally:
            conn.close()

    def get_jobs_by_ids(self, job_ids: List[int]) -> List[PromptJob]:
        if not job_ids:
            return []
        conn = self._connect()
        try:
            placeholders = ",".join("?" for _ in job_ids)
            rows = conn.execute(f"""
                SELECT id, session_id, job_index, prompt, reference_image, status, download_path, chat_url, error, video_title, scene_index, caption, started_at
                FROM jobs WHERE id IN ({placeholders})
            """, tuple(job_ids)).fetchall()
            
            jobs = []
            for row in rows:
                ref_img = Path(row['reference_image']) if row['reference_image'] else None
                job = PromptJob(
                    index=row['job_index'],
                    prompt=row['prompt'],
                    reference_image=ref_img,
                    status=JobStatus(row['status']),
                    download_path=row['download_path'],
                    chat_url=row['chat_url'],
                    error=row['error'],
                    session_id=row['session_id'],
                    job_id=row['id'],
                    video_title=row['video_title'],
                    scene_index=row['scene_index'],
                    caption=row['caption'],
                    started_at=row['started_at']
                )
                jobs.append(job)
            return jobs
        finally:
            conn.close()

    def delete_jobs_by_ids(self, job_ids: List[int]) -> None:
        if not job_ids:
            return
        conn = self._connect()
        try:
            placeholders = ",".join("?" for _ in job_ids)
            conn.execute(f"DELETE FROM jobs WHERE id IN ({placeholders})", tuple(job_ids))
            conn.commit()
        finally:
            conn.close()

    def list_sessions(self, limit: int = 50) -> List[sqlite3.Row]:
        conn = self._connect()
        try:
            return conn.execute("""
                SELECT id, name, prompt_count, completed_count, failed_count, created_at
                FROM sessions ORDER BY id DESC LIMIT ?
            """, (limit,)).fetchall()
        finally:
            conn.close()

    def update_job(self, job_id: int, status: Optional[JobStatus] = UNSPECIFIED, 
                   download_path: Optional[Path] = UNSPECIFIED, chat_url: Optional[str] = UNSPECIFIED, 
                   error: Optional[str] = UNSPECIFIED, mark_started: bool = False, 
                   mark_finished: bool = False, video_title: Optional[str] = UNSPECIFIED,
                   scene_index: Optional[int] = UNSPECIFIED, prompt: Optional[str] = UNSPECIFIED,
                   caption: Optional[str] = UNSPECIFIED) -> None:
        conn = self._connect()
        try:
            fields = []
            values = []
            if status is not UNSPECIFIED:
                fields.append("status = ?")
                values.append(status.value if status else None)
            if download_path is not UNSPECIFIED:
                fields.append("download_path = ?")
                values.append(str(download_path) if download_path else None)
            if chat_url is not UNSPECIFIED:
                fields.append("chat_url = ?")
                values.append(chat_url)
            if error is not UNSPECIFIED:
                fields.append("error = ?")
                values.append(error)
            if video_title is not UNSPECIFIED:
                fields.append("video_title = ?")
                values.append(video_title)
            if scene_index is not UNSPECIFIED:
                fields.append("scene_index = ?")
                values.append(scene_index)
            if prompt is not UNSPECIFIED:
                fields.append("prompt = ?")
                values.append(prompt)
            if caption is not UNSPECIFIED:
                fields.append("caption = ?")
                values.append(caption)
            if mark_started:
                fields.append("started_at = ?")
                values.append(_utc_now())
            if mark_finished:
                fields.append("finished_at = ?")
                values.append(_utc_now())
                
            if not fields:
                return
                
            values.append(job_id)
            query = f"UPDATE jobs SET {', '.join(fields)} WHERE id = ?"
            conn.execute(query, tuple(values))
            conn.commit()
        finally:
            conn.close()

    def record_download(self, job_id: int, file_path: Path) -> None:
        conn = self._connect()
        try:
            size = file_path.stat().st_size if file_path.exists() else None
            conn.execute("""
                INSERT INTO downloads (job_id, file_path, file_size, downloaded_at)
                VALUES (?, ?, ?, ?)
            """, (job_id, str(file_path), size, _utc_now()))
            conn.commit()
        finally:
            conn.close()

    def bump_session_counts(self, session_id: int, completed: int = 0, failed: int = 0) -> None:
        conn = self._connect()
        try:
            conn.execute("""
                UPDATE sessions 
                SET completed_count = completed_count + ?, failed_count = failed_count + ? 
                WHERE id = ?
            """, (completed, failed, session_id))
            conn.commit()
        finally:
            conn.close()

    def get_lifetime_count(self) -> int:
        conn = self._connect()
        try:
            row = conn.execute("SELECT SUM(completed_count) FROM sessions").fetchone()
            return int(row[0]) if row and row[0] is not None else 0
        finally:
            conn.close()

    def get_all_jobs_with_filters(self, status_filter: Optional[str] = None, 
                                  search_text: Optional[str] = None, 
                                  date_filter: Optional[str] = None, 
                                  limit_val: int = 100) -> List[sqlite3.Row]:
        conn = self._connect()
        try:
            query = """
                SELECT j.id, j.session_id, j.job_index, j.prompt, j.reference_image, 
                       j.status, j.download_path, j.chat_url, j.error, j.finished_at,
                       j.video_title, j.scene_index, j.caption,
                       s.name as session_name, s.created_at as session_date
                FROM jobs j
                LEFT JOIN sessions s ON j.session_id = s.id
                WHERE 1=1
            """
            params = []
            
            if status_filter and status_filter.lower() != 'all':
                query += " AND j.status = ?"
                params.append(status_filter.lower())
                
            if search_text:
                query += " AND (j.prompt LIKE ? OR j.video_title LIKE ?)"
                params.extend([f"%{search_text}%", f"%{search_text}%"])
                
            if date_filter:
                if date_filter == 'Today':
                    query += " AND date(s.created_at) = date('now')"
                elif date_filter == 'Last 7 Days':
                    query += " AND date(s.created_at) >= date('now', '-7 days')"
                elif date_filter == 'Last 30 Days':
                    query += " AND date(s.created_at) >= date('now', '-30 days')"
                    
            query += " ORDER BY j.id DESC LIMIT ?"
            params.append(limit_val)
            
            return conn.execute(query, tuple(params)).fetchall()
        finally:
            conn.close()

    def add_viral_hook(self, title: str, file_path: str, duration: float, aspect_ratio: str, platform: str = "", source_url: str = "") -> int:
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO viral_hooks (title, file_path, duration, aspect_ratio, platform, source_url, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (title, file_path, duration, aspect_ratio, platform, source_url, _utc_now()))
            conn.commit()
            return cur.lastrowid
        finally:
            conn.close()

    def list_viral_hooks(self) -> List[sqlite3.Row]:
        conn = self._connect()
        try:
            return conn.execute("SELECT * FROM viral_hooks ORDER BY id DESC").fetchall()
        finally:
            conn.close()

    def delete_viral_hook(self, hook_id: int) -> None:
        conn = self._connect()
        try:
            conn.execute("DELETE FROM viral_hooks WHERE id = ?", (hook_id,))
            conn.commit()
        finally:
            conn.close()

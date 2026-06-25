import os
import json
import uuid
import platform
import urllib.request
import urllib.parse
import threading
import multiprocessing
import datetime
from typing import Optional

SUPABASE_URL = 'https://nsshidocfmbrracnqjtz.supabase.co/rest/v1/telemetry'
SUPABASE_KEY = (
    'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Im5zc2hpZG9jZm1icnJhY25xanR6'
    'Iiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODEwMjE1ODIsImV4cCI6MjA5NjU5NzU4Mn0.5AWcNmfDSzDvPLdwAtwUB3EDnNOjEkqQL6OQfwirHt0'
)

def get_device_info() -> dict:
    info = {}
    try:
        info['device_id'] = str(uuid.getnode())
    except Exception:
        info['device_id'] = 'unknown'
        
    try:
        import getpass
        info['username'] = getpass.getuser()
    except Exception:
        try:
            info['username'] = os.getlogin()
        except Exception:
            info['username'] = 'unknown'
            
    try:
        info['os'] = f"{platform.system()} {platform.release()}"
        info['architecture'] = platform.machine()
        info['cpu_cores'] = multiprocessing.cpu_count()
    except Exception:
        info['os'] = 'unknown'
        info['architecture'] = 'unknown'
        info['cpu_cores'] = 0
    return info

class TelemetryTracker:
    def __init__(self, enabled: bool = True):
        self.enabled = enabled
        self.device_info = get_device_info()
        self.device_id = self.device_info.get('device_id', 'unknown')

    def _send_request(self, method: str, data: dict, query: Optional[str] = None):
        if not self.enabled:
            return
        url = SUPABASE_URL
        if query:
            url += query
            
        headers = {
            'apikey': SUPABASE_KEY,
            'Authorization': f'Bearer {SUPABASE_KEY}',
            'Content-Type': 'application/json',
            'Prefer': 'return=representation'
        }
        
        try:
            req_data = json.dumps(data).encode('utf-8')
            req = urllib.request.Request(url, data=req_data, headers=headers, method=method)
            with urllib.request.urlopen(req, timeout=10) as response:
                response.read()
        except urllib.error.HTTPError as e:
            print(f"Supabase telemetry HTTP error: {e.code} - {e.reason}")
        except Exception as e:
            print(f"Supabase telemetry error: {e}")

    def _async_call(self, method: str, data: dict, query: Optional[str] = None):
        if not self.enabled:
            return
        t = threading.Thread(target=self._send_request, args=(method, data, query), daemon=True)
        t.start()

    def report_job_started(self, chrome_profile: str, prompt: str) -> str:
        if not self.enabled:
            return str(uuid.uuid4())
        telemetry_id = str(uuid.uuid4())
        payload = {
            'id': telemetry_id,
            'device_id': self.device_id,
            'username': self.device_info.get('username', 'unknown'),
            'os': self.device_info.get('os', 'unknown'),
            'chrome_profile': chrome_profile,
            'prompt': prompt,
            'status': 'Running'
        }
        self._async_call('POST', payload)
        return telemetry_id

    def report_job_finished(self, telemetry_id: str, status: str):
        if not self.enabled:
            return
        payload = {
            'status': status,
            'last_updated': datetime.datetime.now(datetime.timezone.utc).isoformat()
        }
        query = f"?id=eq.{telemetry_id}"
        self._async_call('PATCH', payload, query)

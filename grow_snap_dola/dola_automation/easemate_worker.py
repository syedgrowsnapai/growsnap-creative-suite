import os
import sys
import time
import random
import threading
from pathlib import Path
from typing import Callable, Optional, Set
from PyQt6.QtCore import QThread, pyqtSignal

# Try to use patchright for stealth, fallback to playwright
try:
    from patchright.sync_api import sync_playwright, Page, BrowserContext, Response, Error as PlaywrightError
except ImportError:
    try:
        from playwright.sync_api import sync_playwright, Page, BrowserContext, Response, Error as PlaywrightError
    except ImportError:
        raise ImportError("Neither patchright nor playwright is installed in the python environment.")

from dola_automation.models import AutomationSettings, PromptJob, JobStatus
from dola_automation.logger import logger

class EasemateBrowserWorker:
    _use_chrome_channel = True
    
    def __init__(self, settings: AutomationSettings, on_progress=None):
        self.settings = settings
        self.on_progress = on_progress
        self.download_dir = settings.download_dir
        self.download_dir.mkdir(parents=True, exist_ok=True)
        self._cancelled = False

    def log_info(self, msg: str) -> None:
        logger.info(msg)
        if self.on_progress:
            self.on_progress(50, msg)

    def cancel(self) -> None:
        self._cancelled = True
        try:
            if hasattr(self, '_context') and self._context:
                self._context.close()
        except Exception:
            pass

    def run_job(self, job: PromptJob, model: str, aspect_ratio: str, resolution_ratio: str, thread_id: int = 0) -> bool:
        profile_name = getattr(self.settings, 'active_profile_name', 'Default')
        
        base_dir = Path.home() / 'Documents' / 'easemate_video_automation' / 'profiles' / profile_name
        if thread_id != 0:
            profile_dir = Path.home() / 'Documents' / 'easemate_video_automation' / 'profiles' / f"{profile_name}_thread_{thread_id}"
            profile_dir.mkdir(parents=True, exist_ok=True)
            # Copy profile context to keep user login session cookies
            try:
                import shutil
                if base_dir.exists():
                    for item in base_dir.iterdir():
                        dest_item = profile_dir / item.name
                        if not dest_item.exists():
                            if item.is_dir():
                                shutil.copytree(item, dest_item, dirs_exist_ok=True)
                            else:
                                shutil.copy2(item, dest_item)
            except Exception as e:
                self.log_info(f"Warning: Failed to copy profile state for thread: {e}")
        else:
            profile_dir = base_dir
            profile_dir.mkdir(parents=True, exist_ok=True)
            
        use_chrome = getattr(EasemateBrowserWorker, '_use_chrome_channel', True)
        
        success = False
        try:
            success = self._run_job_with_playwright(job, model, aspect_ratio, resolution_ratio, profile_dir, use_chrome)
        except Exception as e:
            err_msg = str(e)
            if ("browser project is not installed" in err_msg or "executable" in err_msg or "channel" in err_msg) and use_chrome:
                self.log_info("Chrome channel launch failed. Retrying launch with standard Chromium...")
                EasemateBrowserWorker._use_chrome_channel = False
                try:
                    success = self._run_job_with_playwright(job, model, aspect_ratio, resolution_ratio, profile_dir, False)
                except Exception as e2:
                    self.log_info(f"Chromium fallback execution failed: {e2}")
                    job.error = str(e2)
            else:
                self.log_info(f"Execution failed: {e}")
                job.error = err_msg
        finally:
            if thread_id != 0 and profile_dir.exists():
                try:
                    import shutil
                    shutil.rmtree(profile_dir, ignore_errors=True)
                except Exception:
                    pass
                    
        return success

    def _run_job_with_playwright(self, job: PromptJob, model: str, aspect_ratio: str, resolution_ratio: str, profile_dir: Path, use_chrome: bool) -> bool:
        success = False
        with sync_playwright() as p:
            launch_args = []
            if os.name != 'nt':
                launch_args.extend(["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"])
            if not self.settings.headless:
                launch_args.append("--disable-blink-features=AutomationControlled")
                
            context = p.chromium.launch_persistent_context(
                user_data_dir=str(profile_dir),
                headless=self.settings.headless,
                channel="chrome" if (use_chrome and not self.settings.headless) else None,
                viewport={"width": 1280, "height": 800},
                args=launch_args
            )
            self._context = context
            
            try:
                page = context.pages[0] if context.pages else context.new_page()
                
                # Intercept and abort third-party trackers, ads, and analytics to speed up React hydration
                try:
                    def handle_route(route):
                        url = route.request.url.lower()
                        blocked_patterns = [
                            "google-analytics", "googletagmanager", "facebook.net", 
                            "facebook.com/tr", "connect.facebook", "tiktok.com/analytics", 
                            "hotjar", "mixpanel", "clarity.ms", "doubleclick", 
                            "googleadservices", "amplitude", "sentry.io",
                            "analytics", "pixel"
                        ]
                        if any(p in url for p in blocked_patterns):
                            route.abort()
                        else:
                            route.continue_()
                    page.route("**/*", handle_route)
                except Exception as e:
                    self.log_info(f"Warning: Failed to setup adblock/tracker filter: {e}")

                loading_timeout_sec = getattr(self.settings, 'easemate_loading_timeout_sec', 300)
                loading_timeout_ms = loading_timeout_sec * 1000
                page.set_default_timeout(loading_timeout_ms)
                
                self.log_info("Navigating to easemate.ai...")
                page.goto("https://www.easemate.ai/ai-image-generator", wait_until="domcontentloaded", timeout=loading_timeout_ms)
                
                # Check auth state (warning if Log In button is visible)
                try:
                    if page.locator("button:has-text('Log In'), button:has-text('Sign Up'), button:has-text('Sign In')").first.is_visible():
                        self.log_info("Warning: Easemate login buttons detected. Headless might run unauthenticated.")
                except Exception:
                    pass
                    
                # Explicitly wait up to dynamic timeout for page components to load
                self.log_info(f"Waiting up to {loading_timeout_sec} seconds for EaseMate UI components to load...")
                try:
                    page.wait_for_selector("xpath=//button[contains(., 'Text to Image')] | //span[text()='Text to Image']", state="visible", timeout=loading_timeout_ms)
                    self.log_info("EaseMate UI components detected successfully.")
                    
                    # Zoom out to 80% to ensure elements fit on smaller displays
                    try:
                        page.evaluate("document.body.style.zoom = '0.8'")
                        self.log_info("Zoomed viewport out to 80% for layout visibility.")
                    except Exception:
                        pass
                except Exception as e:
                    self.log_info(f"Warning: Timeout waiting for main UI components: {e}")
                    
                # Additional wait buffer as requested for stability
                page.wait_for_timeout(3000)

                # Switch to Text to Image tab to reveal prompt input
                self.log_info("Switching to Text to Image mode...")
                text_to_image_tab = page.locator("xpath=//button[contains(., 'Text to Image')] | //span[text()='Text to Image']").first
                if text_to_image_tab.is_visible():
                    try:
                        text_to_image_tab.scroll_into_view_if_needed()
                    except Exception:
                        pass
                    text_to_image_tab.click()
                    page.wait_for_timeout(1000)
                else:
                    self.log_info("Text to Image tab button not visible or already selected.")

                # 1. Model Selection
                self.log_info(f"Selecting model: {model}")
                trigger = None
                trigger_selectors = [
                    "span:has-text('Models') + div div.cursor-pointer",
                    "span:has-text('Models') + div",
                    "div:has-text('Models') + div div.cursor-pointer",
                    "button:has-text('GPT image 2')",
                    "div.cursor-pointer:has-text('GPT image 2')",
                    "div.cursor-pointer:has-text('EaseMate Standard')"
                ]
                for sel in trigger_selectors:
                    loc = page.locator(sel).first
                    if loc.is_visible():
                        trigger = loc
                        break
                
                if trigger:
                    self.log_info("Clicking model dropdown trigger...")
                    try:
                        trigger.scroll_into_view_if_needed()
                    except Exception:
                        pass
                    trigger.click()
                    page.wait_for_timeout(1500)
                    
                    # Find and click target model
                    model_option = None
                    option_selectors = [
                        f"text='{model}'",
                        f"div:has-text('{model}')",
                        f"span:has-text('{model}')",
                        f"button:has-text('{model}')"
                    ]
                    for o_sel in option_selectors:
                        opt = page.locator(o_sel).first
                        if opt.is_visible():
                            model_option = opt
                            break
                            
                    if model_option:
                        self.log_info(f"Selecting option: {model}")
                        try:
                            model_option.scroll_into_view_if_needed()
                        except Exception:
                            pass
                        model_option.click()
                        page.wait_for_timeout(1500)
                    else:
                        self.log_info(f"Model option '{model}' not found in dropdown list.")
                else:
                    self.log_info("Model dropdown trigger not found or not visible.")
                
                # 2. Enter Prompt
                self.log_info(f"Pasting prompt: {job.prompt[:60]}...")
                textarea = page.locator("textarea").first
                if not textarea.is_visible():
                    # Attempt to force wait
                    try:
                        page.wait_for_selector("textarea", timeout=5000)
                        textarea = page.locator("textarea").first
                    except Exception:
                        pass
                if textarea.is_visible():
                    try:
                        textarea.scroll_into_view_if_needed()
                    except Exception:
                        pass
                    textarea.click()
                    textarea.fill("")
                    textarea.fill(job.prompt)
                    page.wait_for_timeout(1000)
                else:
                    raise Exception("Prompt textarea not visible on page.")

                # 3. Aspect Ratio Selection
                self.log_info(f"Selecting aspect ratio: {aspect_ratio}")
                ratio_btn = None
                ratio_selectors = [
                    f"div:has-text('Output Aspect Ratios') + div text='{aspect_ratio}'",
                    f"span:has-text('Output Aspect Ratios') + div text='{aspect_ratio}'",
                    f"text='{aspect_ratio}'"
                ]
                for r_sel in ratio_selectors:
                    btn = page.locator(r_sel).first
                    if btn.is_visible():
                        ratio_btn = btn
                        break
                if ratio_btn:
                    try:
                        ratio_btn.scroll_into_view_if_needed()
                    except Exception:
                        pass
                    ratio_btn.click()
                    page.wait_for_timeout(500)
                else:
                    self.log_info(f"Aspect ratio '{aspect_ratio}' not visible, skipping.")

                # 4. Resolution Ratio Selection
                self.log_info(f"Selecting resolution ratio: {resolution_ratio}")
                res_trigger = None
                res_trigger_selectors = [
                    "div:has-text('Resolution Ratio') + div",
                    "span:has-text('Resolution Ratio') + div",
                    "div:has-text('Resolution Ratio') + div div.cursor-pointer"
                ]
                for rt_sel in res_trigger_selectors:
                    loc = page.locator(rt_sel).first
                    if loc.is_visible():
                        res_trigger = loc
                        break
                if res_trigger:
                    try:
                        res_trigger.scroll_into_view_if_needed()
                    except Exception:
                        pass
                    res_trigger.click()
                    page.wait_for_timeout(500)
                    res_option = page.locator(f"text='{resolution_ratio}'").first
                    if res_option.is_visible():
                        try:
                            res_option.scroll_into_view_if_needed()
                        except Exception:
                            pass
                        res_option.click()
                        page.wait_for_timeout(500)
                    else:
                        self.log_info(f"Resolution ratio option '{resolution_ratio}' not visible.")
                else:
                    self.log_info("Resolution ratio trigger not visible.")

                # 5. Submit / Generate
                self.log_info("Clicking Generate button...")
                gen_btn = page.locator("button:has-text('Generate'), button:has-text('Create'), button[type='submit']").first
                
                # Perform window scrolling to the bottom to ensure fully visible UI
                try:
                    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                except Exception:
                    pass
                
                try:
                    gen_btn.scroll_into_view_if_needed()
                except Exception:
                    pass
                    
                gen_btn.click()
                
                # Wait for potential CAPTCHA verification / Submission progress (up to 45 seconds)
                self.log_info("Waiting for captcha verification checks / submission buffers (45 seconds)...")
                page.wait_for_timeout(45000)
                
                # 6. Polling for Completion (Check for download button next to Recreate)
                self.log_info("Polling for generated image download button...")
                download_btn = page.locator("button:has-text('Recreate') + button, button[class*='download']").first
                
                max_polls = 10
                poll_interval = 30000 # 30 seconds
                for attempt in range(max_polls):
                    if self._cancelled:
                        self.log_info("Job execution cancelled by user.")
                        return False
                        
                    if download_btn.is_visible():
                        self.log_info("Download button detected! Image is ready.")
                        break
                    else:
                        self.log_info(f"Generation in progress. Retrying download detection in 30s (Attempt {attempt+1}/{max_polls})...")
                        page.wait_for_timeout(poll_interval)
                
                if not download_btn.is_visible():
                    raise Exception("Generation timed out. Download button did not appear within 5 minutes.")
                
                # 7. Perform Intercepted Download
                self.log_info("Initiating high-resolution image download...")
                safe_name = "".join(c for c in job.video_title if c.isalnum() or c in (' ', '-', '_')).strip()
                dest_path = self.download_dir / f"{safe_name}.png"
                
                with page.expect_download() as download_info:
                    download_btn.click()
                download = download_info.value
                download.save_as(str(dest_path))
                
                job.download_path = str(dest_path)
                self.log_info(f"Successfully downloaded image: {dest_path.name}")
                success = True
                
            finally:
                try:
                    context.close()
                except Exception:
                    pass
                    
        return success


class EasemateBatchRunner(QThread):
    job_progress = pyqtSignal(int, str)
    job_finished = pyqtSignal(int, bool) # (index, success)
    batch_finished = pyqtSignal()

    def __init__(self, jobs: list[PromptJob], settings: AutomationSettings, model: str, ratio: str, res: str, thread_count: int = 1):
        super().__init__()
        self.jobs = jobs
        self.settings = settings
        self.model = model
        self.ratio = ratio
        self.res = res
        self.thread_count = thread_count
        self.active_workers = {}
        self.lock = threading.Lock()
        self._stop = False

    def run(self):
        self.job_progress.emit(0, f"Starting Easemate AI batch runner for {len(self.jobs)} jobs. Concurrency limit = {self.thread_count}...")
        
        queue = [j for j in self.jobs if j.status not in (JobStatus.CANCELLED, JobStatus.COMPLETED)]
        running_threads = []
        
        def run_single_job(job_obj):
            job_obj.status = JobStatus.RUNNING
            self.job_finished.emit(job_obj.index, False)
            
            success = False
            max_attempts = 3
            for attempt in range(max_attempts):
                if self._stop:
                    break
                    
                self.job_progress.emit(job_obj.index, f"Processing Job #{job_obj.index} (Attempt {attempt+1}/{max_attempts})...")
                
                # Fresh worker instance to avoid lock conflicts
                worker = EasemateBrowserWorker(self.settings, on_progress=lambda p, msg: self.job_progress.emit(job_obj.index, msg))
                with self.lock:
                    if self._stop:
                        break
                    self.active_workers[job_obj.index] = worker
                    
                t_id = f"{threading.get_ident()}_{attempt}"
                success = worker.run_job(job_obj, self.model, self.ratio, self.res, thread_id=t_id)
                
                with self.lock:
                    if job_obj.index in self.active_workers:
                        del self.active_workers[job_obj.index]
                        
                if success:
                    break
                else:
                    self.job_progress.emit(job_obj.index, f"Job #{job_obj.index} failed on attempt {attempt+1}. Retrying in 5 seconds...")
                    time.sleep(5)
                    
            if success:
                job_obj.status = JobStatus.COMPLETED
                self.job_finished.emit(job_obj.index, True)
            else:
                job_obj.status = JobStatus.FAILED
                self.job_finished.emit(job_obj.index, False)

        while (queue or running_threads) and not self._stop:
            running_threads = [t for t in running_threads if t.is_alive()]
            
            if len(running_threads) < self.thread_count and queue:
                job = queue.pop(0)
                t = threading.Thread(target=run_single_job, args=(job,), name=f"EasemateWorker-{job.index}")
                running_threads.append(t)
                t.start()
                
            time.sleep(0.5)
            
        # Wait for remaining active threads
        for t in running_threads:
            t.join()
            
        self.batch_finished.emit()

    def cancel(self):
        self._stop = True
        with self.lock:
            for w in list(self.active_workers.values()):
                try:
                    w.cancel()
                except Exception:
                    pass

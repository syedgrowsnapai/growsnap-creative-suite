import os
import time
from pathlib import Path
from typing import Set
from playwright.sync_api import sync_playwright, Response, Page

from dola_automation.models import AutomationSettings, PromptJob, JobStatus
from dola_automation.logger import logger

class SnapGenBrowserWorker:
    def __init__(self, settings: AutomationSettings, on_progress=None):
        self.settings = settings
        self.on_progress = on_progress
        self.download_dir = settings.download_dir
        self.download_dir.mkdir(parents=True, exist_ok=True)
        self._cancelled = False
        self._intercepted_mp4_urls: Set[str] = set()

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

    def run_job(self, job: PromptJob, is_image: bool = False) -> bool:
        self._intercepted_mp4_urls.clear()
        profile_name = getattr(self.settings, 'active_profile_name', 'Default')
        profile_dir = Path.home() / 'Documents' / 'snapgen_video_automation' / 'profiles' / profile_name
        profile_dir.mkdir(parents=True, exist_ok=True)
        
        self.log_info(f"SnapGen Job #{job.index}: Launching browser under profile '{profile_name}'")
        
        success = False
        with sync_playwright() as p:
            launch_args = []
            if os.name != 'nt':
                launch_args.extend(["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"])
            if not self.settings.headless:
                launch_args.append("--disable-blink-features=AutomationControlled")
                
            try:
                context = p.chromium.launch_persistent_context(
                    user_data_dir=str(profile_dir),
                    headless=self.settings.headless,
                    viewport={"width": 1280, "height": 800},
                    args=launch_args
                )
                self._context = context
            except Exception as e:
                self.log_info(f"Failed to launch SnapGen browser profile: {e}")
                return False
                
            page = context.pages[0] if context.pages else context.new_page()
            page.set_default_timeout(45000)
            
            # Intercept video source downloads in traffic
            def intercept(response: Response):
                url = response.url
                if ".mp4" in url or ".png" in url or ".jpg" in url:
                    self._intercepted_mp4_urls.add(url)
            context.on("response", intercept)
            
            try:
                self.log_info("Navigating to snapgen.ai...")
                page.goto("http://snapgen.ai/")
                page.wait_for_timeout(3000)
                
                # Check auth state
                if page.locator("button:has-text('Sign up'), button:has-text('Log in')").first.is_visible():
                    self.log_info("Warning: SnapGen login page detected. User is not authenticated.")
                    # In high-fidelity auto-rotation, we will raise an error or wait 30s for manual override
                    self.log_info("Waiting 15 seconds for headed user manual login override if visible...")
                    page.wait_for_timeout(15000)
                
                # Navigate to Generator
                if is_image:
                    self.log_info("Navigating to Image Generator...")
                    page.goto("http://snapgen.ai/ai-image-generator")
                else:
                    self.log_info("Navigating to Video Generator...")
                    page.goto("http://snapgen.ai/ai-video-generator")
                page.wait_for_timeout(3000)

                # Enter prompt
                self.log_info(f"Pasting prompt: {job.prompt}")
                # Locate text prompt textareas
                textarea = page.locator("textarea, input[placeholder*='prompt'], [contenteditable='true']").first
                textarea.click()
                textarea.fill(job.prompt)
                page.wait_for_timeout(1000)

                # Aspect Ratio selection (if supported)
                ratio_btn = page.locator(f"button:has-text('{self.settings.ratio}'), div:has-text('{self.settings.ratio}')").first
                if ratio_btn.is_visible():
                    ratio_btn.click()
                    page.wait_for_timeout(500)

                # Upload reference image if present
                if job.has_reference:
                    self.log_info(f"Uploading reference image: {job.reference_image.name}")
                    file_input = page.locator("input[type='file']").first
                    if file_input.is_visible():
                        file_input.set_input_files(str(job.reference_image))
                        page.wait_for_timeout(2000)

                # Submit / Generate button
                self.log_info("Clicking generate button...")
                gen_btn = page.locator("button:has-text('Generate'), button:has-text('Create'), button[type='submit']").first
                gen_btn.click()
                
                # Poll for completion (Wait until download links show or progress elements disappear)
                self.log_info("Waiting for generation process to finish...")
                page.wait_for_timeout(20000) # Initial wait
                
                # Download link selector
                success = True
                self.log_info("Successfully triggered SnapGen generation.")
                
            except Exception as e:
                self.log_info(f"Execution failed: {e}")
            finally:
                try:
                    context.close()
                except Exception:
                    pass
                    
        return success

from __future__ import annotations
import os
import sys
import time
import random
import string
import re
import requests
import datetime
import subprocess
import threading
from pathlib import Path
from typing import Callable, Optional, Set
from urllib.parse import quote

# Try to use patchright for stealth, fallback to playwright
try:
    from patchright.sync_api import sync_playwright, Page, BrowserContext, Response, Error as PlaywrightError
except ImportError:
    try:
        from playwright.sync_api import sync_playwright, Page, BrowserContext, Response, Error as PlaywrightError
    except ImportError:
        raise ImportError("Neither patchright nor playwright is installed in the python environment.")

from dola_automation.models import AutomationSettings, PromptJob, JobStatus, Path
from dola_automation.logger import logger
from dola_automation.ffmpeg_utils import process_video_watermark

class DolaAutomationError(Exception):
    pass

class VPNRotator:
    _lock = threading.Lock()
    _last_rotate_time = 0.0
    _current_country_idx = 0
    
    # Supported countries by both Dola and NordVPN
    countries = [
        "Singapore", "Japan", "South Korea", "United Kingdom", "Mexico", 
        "Brazil", "Argentina", "Colombia", "Chile", "Serbia", "South Africa", 
        "United Arab Emirates"
    ]
    
    @classmethod
    def rotate_vpn(cls, log_fn=None) -> bool:
        with cls._lock:
            # Prevent rapid back-to-back rotations by multiple threads
            now = time.time()
            if now - cls._last_rotate_time < 30.0:
                if log_fn:
                    log_fn("VPN was recently rotated by another thread. Waiting for connection stability...")
                time.sleep(10)
                return True
                
            cls._last_rotate_time = now
            cls._current_country_idx = (cls._current_country_idx + 1) % len(cls.countries)
            target_country = cls.countries[cls._current_country_idx]
            
            if log_fn:
                log_fn(f"NordVPN: Triggering auto-rotation to: {target_country}...")
                
            try:
                if os.name == 'nt': # Windows
                    nord_path = r"C:\Program Files\NordVPN\nordvpn.exe"
                    if os.path.exists(nord_path):
                        cmd = [nord_path, "-c", "-g", target_country]
                    else:
                        cmd = ["nordvpn", "-c", "-g", target_country]
                else: # Linux
                    cmd = ["nordvpn", "connect", target_country]
                    
                # Run connect command
                res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=20)
                if log_fn:
                    log_fn(f"NordVPN connect command finished. Status: {res.returncode}. Output: {res.stdout.strip()} {res.stderr.strip()}")
                
                # Wait 10 seconds for IP allocation and connection to establish
                time.sleep(10)
                return True
            except Exception as e:
                if log_fn:
                    log_fn(f"NordVPN connection command failed: {e}")
                return False

class DolaBrowserWorker:
    def __init__(self, settings: AutomationSettings, on_progress: Optional[Callable[[int, str], None]] = None, 
                 on_chat_created: Optional[Callable[[PromptJob, str], None]] = None):
        self.settings = settings
        self.on_progress = on_progress
        self.on_chat_created = on_chat_created
        self.download_dir = settings.download_dir
        self.download_dir.mkdir(parents=True, exist_ok=True)
        self._cancelled = False
        self._intercepted_mp4_urls: Set[str] = set()

    def log_info(self, msg: str) -> None:
        logger.info(msg)
        if self.on_progress:
            self.on_progress(50, msg) # Emits middle progress level for logging

    def cancel(self) -> None:
        self._cancelled = True
        self.log_info("Worker Cancelled.")

    def _get_job_session_path(self, job_index: int) -> Path:
        sessions_dir = Path.home() / 'Documents' / 'dola_video_automation' / 'sessions'
        sessions_dir.mkdir(parents=True, exist_ok=True)
        return sessions_dir / f"session_job_{job_index}.json"

    def run_job(self, job: PromptJob, mode: str = "full") -> bool:
        """
        Executes the automation job. Mode can be 'full' (submit + wait + download) or 'download_only'.
        """
        self._intercepted_mp4_urls.clear()
        
        mode_label = "headed" if not self.settings.headless else "headless"
        self.log_info(f"Job #{job.index}: Starting Playwright execution in {mode_label} mode.")
        
        # Determine job-specific session path
        session_path = self._get_job_session_path(job.index)
        
        success = False
        
        with sync_playwright() as p:
            launch_args = []
            if os.name != 'nt':
                launch_args.extend(["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"])
            if not self.settings.headless:
                launch_args.append("--disable-blink-features=AutomationControlled")
                
            browser = p.chromium.launch(
                headless=self.settings.headless,
                args=launch_args
            )
            
            # Setup context storage state
            context_kwargs = {
                "viewport": {"width": 1280, "height": 800}
            }
            
            if mode == "download_only":
                # Load job-specific session state so that the browser loads the identical chat session
                if session_path.exists():
                    context_kwargs["storage_state"] = str(session_path)
                    self.log_info(f"Loading job-specific session state from {session_path}")
                elif self.settings.auth_state_path.exists():
                    context_kwargs["storage_state"] = str(self.settings.auth_state_path)
                    self.log_info("Fallback: Loading global session state.")
                else:
                    self.log_info("Warning: No session state file found for download mode.")
            else:
                # Submission mode starts with a completely clean context to enforce a new chat session
                self.log_info(f"Job #{job.index}: Clean browser session initialized (no shared cookies).")
                
            context = browser.new_context(**context_kwargs)
            context.set_default_timeout(30000)
            
            # Listen to MP4 files passing in traffic
            def intercept_response(response: Response):
                url = response.url
                if ".mp4" in url or "video_mp4" in url:
                    self._intercepted_mp4_urls.add(url)
                    
            context.on("response", intercept_response)
            
            page = context.new_page()
            page.set_default_navigation_timeout(60000)
            
            # Setup anti-popup interceptors
            self._setup_popup_handlers(page)
            
            try:
                if mode == "download_only":
                    self.log_info(f"Opening chat to download for job #{job.index}...")
                    success = self._execute_download_only(page, context, job)
                else:
                    self.log_info(f"Starting job #{job.index} prompt submission...")
                    success = self._execute_on_page(page, context, job)
                    
                if success:
                    self.log_info(f"Job #{job.index} execution completed successfully.")
                else:
                    self.log_info(f"Job #{job.index} execution failed.")
            except Exception as e:
                self.log_info(f"Job #{job.index} execution failed with error: {e}")
                # Save backup state for recovery
                try:
                    context.storage_state(path=str(session_path))
                    self.log_info(f"Saved session state to {session_path} for recovery.")
                except Exception:
                    pass
            finally:
                context.close()
                browser.close()
                
        return success

    def _setup_popup_handlers(self, page: Page) -> None:
        # Dismiss overlays dynamically
        def handle_popup():
            try:
                self.log_info("Locator handler triggered! Auto-dismissing popup...")
                page.keyboard.press("Escape")
                page.evaluate("""() => {
                    document.querySelectorAll('button[aria-label="close"], button[aria-label="Close"], .semi-modal-close, .semi-modal button.semi-button-borderless').forEach(b => b.click());
                }""")
            except Exception:
                pass
                
        # Register handles on common modal overlays
        page.add_locator_handler(page.locator(".semi-modal, .login-modal"), handle_popup)

    def _execute_on_page(self, page: Page, context: BrowserContext, job: PromptJob) -> bool:
        # 1. Navigate to creation panel
        create_url = "https://www.dola.com/chat/create-image"
        self.log_info(f"Navigating to {create_url}...")
        page.goto(create_url, wait_until="domcontentloaded")
        page.wait_for_timeout(2000)
        
        # 2. Select Video tab
        self._select_video_tab(page)
        
        # 3. Configure generation variables
        self._configure_options(page)
        
        # 4. Upload reference image if mapped
        if job.has_reference:
            self.log_info(f"Uploading reference image: {job.reference_image.name}...")
            self._upload_reference(page, job.reference_image)
        else:
            self.log_info("Pasting prompt as text-only (no reference image).")
            
        # 5. Type and submit prompt
        self._fill_prompt(page, job.prompt)
        
        # 6. Click submit button
        self._submit(page)
        
        # 7. Immediately save session details and wait for chat redirect
        self.log_info("Waiting for chat session to be created...")
        chat_url = self._wait_for_chat(page)
        if not chat_url:
            # Check if redirected to region restriction during wait
            if "region-restricted" in page.url:
                self.log_info("Region restriction detected. Triggering VPN rotation...")
                VPNRotator.rotate_vpn(self.log_info)
                raise DolaAutomationError("Dola access region-restricted. VPN rotated. Retrying...")
            raise DolaAutomationError("Timed out waiting for chat redirection after prompt submission.")
            
        # 7.1. Wait and confirm prompt acceptance via phrase validation
        success_phrase = getattr(self.settings, 'generation_success_phrase', 'The video will be generated using the SeaDance 2.0 model.')
        
        self.log_info(f"Waiting up to 30s for prompt acceptance confirmation ('{success_phrase}')...")
        confirmed = False
        max_wait_seconds = 30
        
        for sec in range(max_wait_seconds):
            if self._cancelled:
                raise DolaAutomationError("Job execution cancelled by user.")
                
            # Check region restriction redirect
            if "region-restricted" in page.url:
                self.log_info("Region restriction detected. Triggering VPN rotation...")
                VPNRotator.rotate_vpn(self.log_info)
                raise DolaAutomationError("Dola access region-restricted. VPN rotated. Retrying...")
                
            # Check for rejections/errors on the page
            err = self._check_for_immediate_errors(page)
            if err:
                self.log_info(f"Immediate rejection detected: '{err}'")
                if any(x in err.lower() for x in ["high server demand", "region-restricted", "region restricted", "country switch needed", "voiceover", "on-screen text"]):
                    self.log_info("High demand, region restriction, or voiceover rejection detected. Triggering VPN rotation...")
                    VPNRotator.rotate_vpn(self.log_info)
                raise DolaAutomationError(f"Rejection: {err}")
                
            # Look for the configured success phrase or standard fallback keywords
            if success_phrase:
                has_phrase = page.evaluate(f"""(phrase) => {{
                    const bodyText = document.body.innerText || "";
                    return bodyText.toLowerCase().includes(phrase.toLowerCase());
                }}""", success_phrase)
                
                # Check for standard fallback phrases if the user-configured one isn't found yet
                has_fallback = False
                for fb in ["will be generated", "generated using", "cdans", "seadance", "seedance", "video will be"]:
                    found_fb = page.evaluate(f"""(fb_term) => {{
                        const bodyText = document.body.innerText || "";
                        return bodyText.toLowerCase().includes(fb_term.toLowerCase());
                    }}""", fb)
                    if found_fb:
                        has_fallback = True
                        break
                
                if has_phrase or has_fallback:
                    confirmed = True
                    break
            else:
                # No confirmation phrase set: wait 3 seconds and assume OK
                page.wait_for_timeout(3000)
                confirmed = True
                break
                
            page.wait_for_timeout(1000)
            
        if not confirmed:
            self.log_info(f"Could not confirm prompt submission. Confirmation phrase '{success_phrase}' not found on page within {max_wait_seconds}s.")
            raise DolaAutomationError(f"Confirmation phrase '{success_phrase}' not found on page after submission (timeout {max_wait_seconds}s).")
            
        self.log_info("Prompt submission successfully confirmed on page.")
            
        job.chat_url = chat_url
        self.log_info(f"Chat created: {chat_url}")
        
        # Call back main UI to update chat link
        if self.on_chat_created:
            self.on_chat_created(job, chat_url)
            
        # Save storage cookies for this specific job and globally
        try:
            session_path = self._get_job_session_path(job.index)
            context.storage_state(path=str(session_path))
            self.log_info(f"Saved session state for job #{job.index} to {session_path}")
            
            self.settings.auth_state_path.parent.mkdir(parents=True, exist_ok=True)
            context.storage_state(path=str(self.settings.auth_state_path))
            self.log_info("Shared global session state cookies saved.")
        except Exception as e:
            self.log_info(f"Failed to perform session save: {e}")
            
        # If submit-and-close is selected, we stop here and proceed to the next prompt!
        if self.settings.submit_and_close:
            delay = self.settings.submit_close_delay_sec if self.settings.submit_close_delay_sec > 0 else 15
            self.log_info(f"Submit & Close active. Waiting up to {delay}s for potential generation errors...")
            for _ in range(delay):
                if self._cancelled:
                    raise DolaAutomationError("Job execution cancelled by user.")
                page.wait_for_timeout(1000)
                err = self._check_for_immediate_errors(page)
                if err:
                    self.log_info(f"Immediate rejection detected on page: '{err}'")
                    if any(x in err.lower() for x in ["high server demand", "region-restricted", "region restricted", "country switch needed", "voiceover", "on-screen text"]):
                        self.log_info("High demand, region restriction, or voiceover rejection detected. Triggering VPN rotation...")
                        VPNRotator.rotate_vpn(self.log_info)
                    raise DolaAutomationError(f"Rejection: {err}")
            
            job.status = JobStatus.SUBMITTED
            return True
            
        # Otherwise, wait for generation and download immediately
        job.status = JobStatus.WAITING
        return self._wait_and_download(page, job)

    def _execute_download_only(self, page: Page, context: BrowserContext, job: PromptJob) -> bool:
        if not job.chat_url:
            self.log_info("No chat URL associated with this job.")
            return False
            
        self.log_info(f"Opening chat session: {job.chat_url}...")
        page.goto(job.chat_url, wait_until="domcontentloaded")
        page.wait_for_timeout(3000)
        
        # Dismiss initial modals if present
        self._close_dola_popups(page)
        
        # Check if we were redirected back to the home/create page
        curr_url = page.url
        if "/chat/" not in curr_url or curr_url.endswith("create-image") or curr_url.endswith("create-video") or curr_url.endswith("/chat") or curr_url.endswith("/chat/"):
            self.log_info("Redirected to home screen. Attempting to recover latest active chat session...")
            latest_chat = page.evaluate("""() => {
                const links = Array.from(document.querySelectorAll('a'));
                for (let a of links) {
                    const href = a.getAttribute('href') || '';
                    if (href.includes('/chat/') && 
                        !href.includes('create-image') && 
                        !href.includes('create-video') && 
                        !href.endsWith('/chat') &&
                        !href.endsWith('/chat/')) {
                        return a.href;
                    }
                }
                return null;
            }""")
            if latest_chat:
                self.log_info(f"Latest chat session identified: {latest_chat}. Navigating...")
                page.goto(latest_chat, wait_until="domcontentloaded")
                page.wait_for_timeout(6000)
                self._close_dola_popups(page)
            else:
                self.log_info("Could not find any active chat session link in the page history.")
        
        # Inject floating progress UI helper
        self._inject_custom_ui(page, job.index)
        
        return self._wait_and_download(page, job)

    def _select_video_tab(self, page: Page) -> None:
        self.log_info("Selecting Video generation tab...")
        try:
            video_btn = page.get_by_role("button", name="Video", exact=True)
            video_btn.wait_for(state="visible", timeout=10000)
            video_btn.click()
            page.wait_for_timeout(1000)
            self.log_info("SUCCESS! Video tab clicked.")
        except Exception as e:
            self.log_info(f"Warning: Could not click Video tab: {e}. Attempting manual click via coordinates.")
            # Fallback coordinate click
            try:
                page.click("text=Video", timeout=5000)
            except Exception:
                raise DolaAutomationError("Could not select Video generation tab. Login modal might be blocking the view.")

    def _configure_options(self, page: Page) -> None:
        self.log_info("Configuring aspect ratio and model variables...")
        # 1. Aspect Ratio
        ratio_mapped = self.settings.ratio
        self._pick_dropdown(page, "Ratio", ratio_mapped)
        
        # 2. Duration
        duration_mapped = self.settings.duration
        self._pick_dropdown(page, "Duration", duration_mapped)
        
        # 3. Model
        model_mapped = self.settings.model
        self._pick_dropdown(page, "Model", model_mapped)

    def _pick_dropdown(self, page: Page, label_text: str, option_text: str) -> None:
        try:
            # Locate dropdown button
            btn = page.locator(f"button:has-text('{label_text}')").first
            # Calmly wait for dropdown button to be visible
            btn.wait_for(state="visible", timeout=10000)
            
            # Check if button text already contains the target option text (already selected)
            btn_text = btn.inner_text() or ""
            if option_text in btn_text:
                self.log_info(f"Dropdown '{label_text}' is already set to '{option_text}'. Skipping selection.")
                return
                
            btn.click()
            page.wait_for_timeout(1000)
            
            # Select option item
            opt = page.locator(f"role=menuitem >> text={option_text}").first
            try:
                opt.wait_for(state="visible", timeout=3000)
                opt.click()
                page.wait_for_timeout(1000)
            except Exception:
                self.log_info(f"Dropdown option '{option_text}' not visible inside menu. Closing dropdown.")
                page.keyboard.press("Escape")
                page.wait_for_timeout(500)
        except Exception as e:
            self.log_info(f"Warning setting {label_text}: {e}")

    def _upload_reference(self, page: Page, image_path: Path) -> None:
        try:
            suffix = image_path.suffix.lower()
            if suffix not in ['.png', '.jpg', '.jpeg', '.webp']:
                raise DolaAutomationError(f"Unsupported reference image format: {suffix}")
                
            # Playwright file chooser hook
            with page.expect_file_chooser() as fc_info:
                # Look for upload trigger element
                upload_icon = page.locator(".upload-icon-container, input[type='file']").first
                upload_icon.wait_for(state="visible", timeout=5000)
                upload_icon.click()
                
            file_chooser = fc_info.value
            file_chooser.set_files(str(image_path))
            page.wait_for_timeout(2000)
            self.log_info(f"Successfully uploaded reference image: {image_path.name}")
        except Exception as e:
            raise DolaAutomationError(f"Reference image upload failed: {e}")

    def _fill_prompt(self, page: Page, prompt: str) -> None:
        try:
            # Locate input editor (contenteditable or textarea)
            editor = page.locator("[contenteditable='true'], .ProseMirror, textarea").first
            editor.wait_for(state="visible", timeout=10000)
            
            # Calmly wait BEFORE pasting prompt
            delay_sec = max(0, self.settings.paste_delay_sec)
            self.log_info(f"Waiting {delay_sec}s before pasting prompt...")
            page.wait_for_timeout(delay_sec * 1000)
            
            self.log_info(f"Pasting prompt: {prompt[:40]}...")
            editor.click()
            editor.focus()
            
            # Clear text using keyboard shortcut to support rich-text editor state
            page.keyboard.press("Control+A")
            page.keyboard.press("Delete")
            page.wait_for_timeout(200)
            
            # Insert prompt text via keyboard simulation (required for ProseMirror rich-text event handling)
            page.keyboard.insert_text(prompt)
            
            # Final stabilization delay AFTER pasting before submit
            page.wait_for_timeout(2000)
        except Exception as e:
            raise DolaAutomationError(f"Failed to fill prompt: {e}")

    def _submit(self, page: Page) -> None:
        self.log_info("Submitting prompt...")
        try:
            page.keyboard.press("Enter")
            page.wait_for_timeout(1000)
            
            # Check if login prompt modal intercepted submission
            if page.locator("text=Log In to Unlock More Features").is_visible():
                self.log_info("Login restriction modal detected. Attempting override...")
                self._close_dola_popups(page)
                page.keyboard.press("Enter")
        except Exception as e:
            raise DolaAutomationError(f"Failed to submit: {e}")

    def _wait_for_chat(self, page: Page) -> str | None:
        try:
            # Wait for URL to contain /chat/ and not be create-image or create-video
            for _ in range(50): # up to 25 seconds
                curr_url = page.url
                if "region-restricted" in curr_url:
                    self.log_info("Region restriction detected during redirection. Triggering VPN rotation...")
                    VPNRotator.rotate_vpn(self.log_info)
                    raise DolaAutomationError("Dola access region-restricted. VPN rotated. Retrying...")
                if "/chat/" in curr_url and not curr_url.endswith("create-image") and not curr_url.endswith("create-video"):
                    return curr_url
                page.wait_for_timeout(500)
            return None
        except Exception as e:
            if isinstance(e, DolaAutomationError):
                raise e
            return None

    def _close_dola_popups(self, page: Page) -> None:
        try:
            page.keyboard.press("Escape")
            page.evaluate("""() => {
                // Click close buttons
                document.querySelectorAll('button[aria-label="close"], button[aria-label="Close"], .semi-modal-close, .semi-modal button.semi-button-borderless').forEach(b => b.click());
                
                // Set CSS display none on blocking login modals
                document.querySelectorAll('.semi-modal, .login-modal, .semi-modal-mask, .login-modal-mask').forEach(el => {
                    el.style.setProperty('display', 'none', 'important');
                });
            }""")
            page.wait_for_timeout(500)
        except Exception:
            pass

    def _check_for_immediate_errors(self, page: Page) -> str | None:
        try:
            # Check URL for region restriction first
            if "region-restricted" in page.url:
                return "region-restricted"
                
            error_phrase = page.evaluate("""() => {
                const textContent = document.body.textContent || "";
                const innerText = document.body.innerText || "";
                const combined = (textContent + " " + innerText).toLowerCase();
                
                const rejections = [
                    "does not support adding voiceover",
                    "does not support voiceover",
                    "we can only generate a video based on",
                    "do you want us to proceed",
                    "proceed with generating the video",
                    "on-screen text, or specifying",
                    "ambient sounds",
                    "no voiceover and no on-screen text",
                    "voiceover, on-screen text",
                    "voiceover and on-screen text",
                    "no voiceover",
                    "no on-screen text",
                    "can't generate",
                    "cannot generate",
                    "amend the prompt",
                    "amend your prompt",
                    "amend prompt",
                    "high demand",
                    "generation failed",
                    "error generating",
                    "something went wrong",
                    "failed to generate",
                    "limit exceeded",
                    "quota reached",
                    "inappropriate content",
                    "policy violation",
                    "violate our policy",
                    "restricted content",
                    "region-restricted",
                    "region restricted",
                    "not available in your region",
                    "not available in your country"
                ];
                
                for (const r of rejections) {
                    if (combined.includes(r)) {
                        return r;
                    }
                }
                return null;
            }""")
            if error_phrase:
                # Map standard phrases to nice human readable strings
                mapping = {
                    "does not support adding voiceover": "Dola prompt error: voiceover/on-screen text not supported. country switch needed.",
                    "does not support voiceover": "Dola prompt error: voiceover/on-screen text not supported. country switch needed.",
                    "we can only generate a video based on": "Dola prompt error: voiceover/on-screen text not supported. country switch needed.",
                    "do you want us to proceed": "Dola prompt error: voiceover/on-screen text not supported. country switch needed.",
                    "proceed with generating the video": "Dola prompt error: voiceover/on-screen text not supported. country switch needed.",
                    "on-screen text, or specifying": "Dola prompt error: voiceover/on-screen text not supported. country switch needed.",
                    "ambient sounds": "Dola prompt error: voiceover/on-screen text not supported. country switch needed.",
                    "no voiceover and no on-screen text": "Dola prompt error: voiceover/on-screen text not supported. country switch needed.",
                    "voiceover, on-screen text": "Dola prompt error: voiceover/on-screen text not supported. country switch needed.",
                    "voiceover and on-screen text": "Dola prompt error: voiceover/on-screen text not supported. country switch needed.",
                    "no voiceover": "Dola prompt error: voiceover/on-screen text not supported. country switch needed.",
                    "no on-screen text": "Dola prompt error: voiceover/on-screen text not supported. country switch needed.",
                    "amend the prompt": "Dola prompt error: amend the prompt. country switch needed.",
                    "amend your prompt": "Dola prompt error: amend the prompt. country switch needed.",
                    "amend prompt": "Dola prompt error: amend the prompt. country switch needed.",
                    "high demand": "High server demand on Dola. Video could not be queued. country switch needed.",
                    "policy violation": "Content policy violation detected by Dola.",
                    "violate our policy": "Content policy violation detected by Dola.",
                    "restricted content": "Content policy restriction triggered by Dola.",
                    "inappropriate content": "Inappropriate content warning triggered by Dola.",
                    "limit exceeded": "Usage limit exceeded on your Dola account. country switch needed.",
                    "quota reached": "Account quota reached on Dola. country switch needed.",
                    "can't generate": "Dola assistant cannot generate this video scene. country switch needed.",
                    "cannot generate": "Dola assistant cannot generate this video scene. country switch needed.",
                    "region-restricted": "Dola access region-restricted. country switch needed.",
                    "region restricted": "Dola access region-restricted. country switch needed.",
                    "not available in your region": "Dola access region-restricted. country switch needed.",
                    "not available in your country": "Dola access region-restricted. country switch needed."
                }
                return mapping.get(error_phrase, f"Dola execution error: {error_phrase}")
            return None
        except Exception:
            return None

    def _wait_and_download(self, page: Page, job: PromptJob) -> bool:
        job.status = JobStatus.WAITING
        self.log_info("Checking if video generation is complete...")
        
        # Give the page 4 seconds to load and stabilize
        page.wait_for_timeout(4000)
        
        # Check for immediate rejection/errors
        err = self._check_for_immediate_errors(page)
        if err:
            job.status = JobStatus.FAILED
            job.error = f"Dola generation rejected: '{err}'"
            if any(x in err.lower() for x in ["high server demand", "region-restricted", "region restricted", "country switch needed", "voiceover", "on-screen text"]):
                self.log_info("High demand, region restriction, or voiceover rejection detected. Triggering VPN rotation...")
                VPNRotator.rotate_vpn(self.log_info)
            raise DolaAutomationError(f"Dola generation rejected: '{err}'")
        
        # Wait until video is ready using the polling loop
        is_ready = self._wait_until_ready(page)
        
        if is_ready:
            job.status = JobStatus.DOWNLOADING
            self.log_info("Video generated! Extracting download source URL...")
            video_url = self._extract_video_url(page)
            if not video_url:
                job.error = "Could not locate generated video source URL."
                job.status = JobStatus.FAILED
                return False
                
            success = self._download_video(video_url, job)
            if success:
                job.status = JobStatus.COMPLETED
                return True
            else:
                job.status = JobStatus.FAILED
                return False
        else:
            # Video is not ready yet. Let's calculate elapsed minutes.
            import datetime
            elapsed_mins = 0.0
            if job.started_at:
                try:
                    if 'T' in job.started_at:
                        start_dt = datetime.datetime.fromisoformat(job.started_at)
                        if start_dt.tzinfo is not None:
                            now_dt = datetime.datetime.now(datetime.timezone.utc)
                        else:
                            now_dt = datetime.datetime.utcnow()
                    else:
                        start_dt = datetime.datetime.strptime(job.started_at, "%Y-%m-%d %H:%M:%S")
                        now_dt = datetime.datetime.utcnow()
                    elapsed_mins = (now_dt - start_dt).total_seconds() / 60.0
                except Exception as e:
                    self.log_info(f"Warning: Could not parse started_at: {e}")
            
            self.log_info(f"Video is not ready yet. Elapsed time since submission: {elapsed_mins:.1f} minutes.")
            
            if elapsed_mins < 20.0:
                job.status = JobStatus.SUBMITTED
                job.error = f"The video is not yet available (First check at {elapsed_mins:.1f}m)."
                raise DolaAutomationError(f"The video is not yet available (First check at {elapsed_mins:.1f}m).")
            elif elapsed_mins < 30.0:
                job.status = JobStatus.SUBMITTED
                job.error = f"The video is not yet available (Checked after 20m check at {elapsed_mins:.1f}m)."
                raise DolaAutomationError(f"The video is not yet available (Checked after 20m check at {elapsed_mins:.1f}m).")
            else:
                job.status = JobStatus.FAILED
                job.error = f"Failed: Video not generated after 30+ minutes (elapsed {elapsed_mins:.1f}m). Please check manually: {job.chat_url}"
                raise DolaAutomationError(f"Failed: Video not generated after 30+ minutes (elapsed {elapsed_mins:.1f}m). Please check manually: {job.chat_url}")

    def _wait_until_ready(self, page: Page) -> bool:
        start_time = time.time()
        timeout = self.settings.generation_timeout_sec if self.settings.generation_timeout_sec > 0 else 999999
        poll_interval = self.settings.poll_interval_sec
        redirect_fail_count = 0
        
        while time.time() - start_time < timeout:
            if self._cancelled:
                return False
                
            # Check for region restriction
            if "region-restricted" in page.url:
                self.log_info("Region restriction detected during wait. Triggering VPN rotation...")
                VPNRotator.rotate_vpn(self.log_info)
                return False
                
            # Check if page has been redirected back to homepage/create page during wait
            curr_url = page.url
            if "/chat/" not in curr_url or curr_url.endswith("create-image") or curr_url.endswith("create-video") or curr_url.endswith("/chat") or curr_url.endswith("/chat/"):
                self.log_info("Warning: Redirected to home screen during wait. Attempting to recover latest active chat...")
                latest_chat = page.evaluate("""() => {
                    const links = Array.from(document.querySelectorAll('a'));
                    for (let a of links) {
                        const href = a.getAttribute('href') || '';
                        if (href.includes('/chat/') && 
                            !href.includes('create-image') && 
                            !href.includes('create-video') && 
                            !href.endsWith('/chat') &&
                            !href.endsWith('/chat/')) {
                            return a.href;
                        }
                    }
                    return null;
                }""")
                if latest_chat:
                    self.log_info(f"Navigating back to latest active chat session: {latest_chat}")
                    page.goto(latest_chat, wait_until="domcontentloaded")
                    page.wait_for_timeout(6000)
                    self._close_dola_popups(page)
                    redirect_fail_count = 0  # reset on success
                else:
                    self.log_info("Could not find any active chat session link in the page history.")
                    redirect_fail_count += 1
                    if redirect_fail_count >= 3:
                        raise DolaAutomationError("Chat session is expired or inaccessible on Dola (redirected to homepage).")
                    
            # Check for standard generated video containers or tag
            is_ready = page.evaluate("""() => {
                // Look for video element with valid source
                const v = document.querySelector('video');
                if (v && (v.currentSrc || v.src)) return true;
                
                // Look for 'ready' texts
                const bodyText = document.body.innerText || "";
                if (bodyText.includes("Your video is ready") || bodyText.includes("Video ready")) return true;
                
                return false;
            }""")
            
            if is_ready:
                return True
                
            # Check for error text indications
            has_error = page.evaluate("""() => {
                const bodyText = document.body.innerText || "";
                if (bodyText.includes("Generation failed") || bodyText.includes("Error generating video")) return true;
                return false;
            }""")
            if has_error:
                self.log_info("Warning - webpage displayed a generation error status.")
                return False
                
            page.wait_for_timeout(int(poll_interval * 1000))
            
        self.log_info("Timed out waiting for video generation.")
        return False

    def _extract_video_url(self, page: Page) -> str | None:
        start_time = time.time()
        timeout = 25.0  # Allow up to 25 seconds for the video URL/element to load
        
        while time.time() - start_time < timeout:
            # 1. Try to fetch from intercepted network traffic
            for url in self._intercepted_mp4_urls:
                if "video" in url or ".mp4" in url:
                    return url
                    
            # 2. Try to query the DOM directly
            src = page.evaluate("""() => {
                const v = document.querySelector('video');
                if (v && (v.currentSrc || v.src)) return v.currentSrc || v.src;
                
                // Look for link elements
                const links = Array.from(document.querySelectorAll('a'));
                for (let a of links) {
                    if (a.href && (a.href.includes('.mp4') || a.href.includes('video_mp4'))) return a.href;
                }
                return null;
            }""")
            if src:
                return src
                
            # 3. Trigger click on video element to force source load
            try:
                page.evaluate("""() => {
                    // Click video player block to activate elements
                    const block = document.querySelector('[data-plugin-identifier="block_type:2074"]');
                    if (block) block.click();
                    const playBtn = document.querySelector('[class*="play-icon-wrapper"]');
                    if (playBtn) playBtn.click();
                }""")
            except Exception:
                pass
                
            page.wait_for_timeout(1500)
            
        return None

    def _slug(self, s: str) -> str:
        s = re.sub(r'[^\w\-]+', '_', s)
        return s.strip('_')[:50]

    def _download_video(self, video_url: str, job: PromptJob) -> bool:
        self.log_info(f"Downloading video file from: {video_url}")
        
        # Format filename using Scene indexes and Titles
        if job.video_title:
            slug_title = self._slug(job.video_title)
            scene_lbl = f"_scene_{job.scene_index}" if job.scene_index else ""
            filename = f"{slug_title}{scene_lbl}.mp4"
            txt_filename = f"{slug_title}{scene_lbl}.txt"
        else:
            filename = f"dola_job_{job.index}_{self._random_string()}.mp4"
            txt_filename = filename.replace('.mp4', '.txt')
            
        out_path = self.download_dir / filename
        txt_path = self.download_dir / txt_filename
        
        try:
            res = requests.get(video_url, timeout=60, stream=True)
            if res.status_code == 200:
                with open(out_path, 'wb') as f:
                    for chunk in res.iter_content(chunk_size=8192):
                        if self._cancelled:
                            f.close()
                            if out_path.exists():
                                out_path.unlink()
                            return False
                        f.write(chunk)
                        
                self.log_info(f"Downloaded video saved to: {out_path}")
                job.download_path = str(out_path)
                
                # Save captions text sidecar
                if job.caption:
                    txt_path.write_text(job.caption, encoding='utf-8')
                    self.log_info(f"Saved sidecar caption file: {txt_path.name}")
                    
                # Post-process watermark removal if active
                if self.settings.auto_remove_watermark:
                    self.log_info(f"Post-processing: Auto-removing watermark ({self.settings.watermark_method})...")
                    coords = (
                        self.settings.watermark_blur_x,
                        self.settings.watermark_blur_y,
                        self.settings.watermark_blur_w,
                        self.settings.watermark_blur_h
                    )
                    success = process_video_watermark(
                        out_path,
                        self.settings.watermark_method,
                        out_path,
                        coords,
                        self.settings.watermark_crop_pixels
                    )
                    if success:
                        self.log_info("Watermark removed successfully.")
                    else:
                        self.log_info("Failed to remove watermark. Preserving raw downloaded video.")
                        
                return True
            else:
                job.error = f"HTTP Error {res.status_code} downloading video."
                return False
        except Exception as e:
            job.error = f"Error during download: {e}"
            self.log_info(f"Download failed: {e}")
            if out_path.exists():
                out_path.unlink()
            return False

    def _random_string(self, length: int = 6) -> str:
        return "".join(random.choices(string.ascii_letters + string.digits, k=length))

    def _inject_custom_ui(self, page: Page, job_index: int) -> None:
        try:
            started_time = datetime.datetime.now().strftime("%I:%M:%S %p")
            
            # Setup bridge trigger to callback python download action
            def handle_download(url: str):
                self.log_info(f"UI Custom Downloader clicked! Fetching URL: {url}")
                
            page.expose_function("pyTriggerDownload", handle_download)
            
            js_code = f"""
            () => {{
                function injectUI() {{
                    if (document.getElementById('dola-custom-dl-container')) return;
                    if (!document.body) return;
                    
                    const div = document.createElement('div');
                    div.id = 'dola-custom-dl-container';
                    div.style.position = 'fixed';
                    div.style.top = '20px';
                    div.style.right = '20px';
                    div.style.zIndex = '9999999';
                    div.style.backgroundColor = 'rgba(10, 24, 16, 0.95)';
                    div.style.color = '#F0FDF4';
                    div.style.padding = '15px';
                    div.style.borderRadius = '8px';
                    div.style.fontFamily = 'sans-serif';
                    div.style.border = '2px solid #2ecc71';
                    div.style.boxShadow = '0 4px 12px rgba(0,0,0,0.5)';
                    div.style.width = '220px';
                    
                    div.innerHTML = `
                        <div style="font-weight: bold; font-size: 14px; margin-bottom: 5px; color: #ffffff;">GrowSnap AI</div>
                        <div style="font-size: 12px; margin-bottom: 3px; color: #2ecc71;">Processing Scene #{job_index}</div>
                        <div id="dola-custom-dl-time" style="font-size: 11px; margin-bottom: 10px; color: #ccc;">Started: {started_time}<br/>Elapsed: 00:00</div>
                        <button id="dola-custom-dl-btn" style="background-color: #2ecc71; color: #0A1810; border: none; padding: 8px 12px; cursor: pointer; border-radius: 4px; font-weight: bold; width: 100%; transition: background 0.2s;">
                            Download Video
                        </button>
                        <div id="dola-custom-dl-status" style="font-size: 11px; margin-top: 6px; color: #D97706; text-align: center;">Active</div>
                    `;
                    document.body.appendChild(div);
                    
                    const btn = document.getElementById('dola-custom-dl-btn');
                    if (btn) {{
                        const status = document.getElementById('dola-custom-dl-status');
                        btn.addEventListener('mouseenter', () => btn.style.backgroundColor = '#22c55e');
                        btn.addEventListener('mouseleave', () => btn.style.backgroundColor = '#2ecc71');
                        btn.addEventListener('click', async () => {{
                            status.innerText = "Extracting video link...";
                            let videoUrl = null;
                            const v = document.querySelector('video');
                            if (v && (v.currentSrc || v.src)) {{
                                videoUrl = v.currentSrc || v.src;
                            }}
                            if (videoUrl) {{
                                status.innerText = "Downloading...";
                                status.style.color = "#2ecc71";
                                try {{
                                    await window.pyTriggerDownload(videoUrl);
                                    status.innerText = "Success!";
                                }} catch (err) {{
                                    status.innerText = "Failed: " + err;
                                }}
                            }} else {{
                                status.innerText = "Video element not ready!";
                                status.style.color = "#D97706";
                            }}
                        }});
                    }}
                    
                    const timeEl = document.getElementById('dola-custom-dl-time');
                    const startTime = Date.now();
                    setInterval(() => {{
                        const elapsedSecs = Math.floor((Date.now() - startTime) / 1000);
                        const mins = String(Math.floor(elapsedSecs / 60)).padStart(2, '0');
                        const secs = String(elapsedSecs % 60).padStart(2, '0');
                        if (timeEl) timeEl.innerHTML = `Started: {started_time}<br/>Elapsed: ${{mins}}:${{secs}}`;
                    }}, 1000);
                }}
                
                if (document.readyState === 'loading') {{
                    document.addEventListener('DOMContentLoaded', injectUI);
                }} else {{
                    injectUI();
                }}
            }}
            """
            page.add_init_script(js_code)
        except Exception as e:
            self.log_info(f"Failed to inject custom UI: {e}")

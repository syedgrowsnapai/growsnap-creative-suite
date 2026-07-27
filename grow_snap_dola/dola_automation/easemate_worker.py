from __future__ import annotations
import os
import sys
import time
import random
import string
import re
import copy
import threading
from pathlib import Path
from typing import Callable, Optional, Set, List

try:
    from patchright.sync_api import sync_playwright, Page, BrowserContext, Response, Locator, Error as PlaywrightError
except ImportError:
    try:
        from playwright.sync_api import sync_playwright, Page, BrowserContext, Response, Locator, Error as PlaywrightError
    except ImportError:
        raise ImportError("Neither patchright nor playwright is installed in the python environment.")

from PyQt6.QtCore import QThread, pyqtSignal

from dola_automation.models import AutomationSettings, PromptJob, JobStatus
from dola_automation.logger import logger
from dola_automation.database import HistoryDatabase

class VPNRotator:
    _lock = threading.Lock()
    _last_rotate_time = 0.0
    _current_country_idx = 0
    
    # Supported countries for EaseMate rotation (all major NordVPN locations)
    countries = [
        "United States", "Canada", "United Kingdom", "Germany", "France",
        "Japan", "Singapore", "Australia", "Netherlands", "Sweden",
        "Switzerland", "Spain", "Italy", "Norway", "Denmark",
        "Finland", "Ireland", "Belgium", "Austria", "New Zealand",
        "Poland", "Czech Republic", "Hungary", "Romania", "Bulgaria",
        "Greece", "Portugal", "Estonia", "Latvia", "Lithuania",
        "Slovakia", "Slovenia", "Croatia", "Iceland", "Albania",
        "South Korea", "Taiwan", "Hong Kong", "India", "Malaysia",
        "Thailand", "Vietnam", "Indonesia", "South Africa", "Brazil",
        "Mexico", "Chile", "Colombia", "Argentina", "Costa Rica",
        "Turkey", "Israel", "United Arab Emirates", "Georgia"
    ]
    
    @classmethod
    def get_last_rotate_time(cls) -> float:
        return cls._last_rotate_time

    @classmethod
    def rotate_vpn(cls, log_fn=None) -> bool:
        with cls._lock:
            # Prevent rapid back-to-back rotations by multiple threads
            now = time.time()
            if now - cls._last_rotate_time < 30.0:
                if log_fn:
                    log_fn("VPN was recently rotated. Waiting for connection stability...")
                time.sleep(10)
                return True
                
            cls._last_rotate_time = now
            import subprocess
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
                elif os.path.exists("/mnt/c/Program Files/NordVPN/nordvpn.exe"): # WSL (Windows Subsystem for Linux)
                    nord_path = "/mnt/c/Program Files/NordVPN/nordvpn.exe"
                    cmd = [nord_path, "-c", "-g", target_country]
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

class EasemateAutomationError(Exception):
    pass

class EasemateBrowserWorker:
    def __init__(self, settings: AutomationSettings, on_progress: Optional[Callable[[int, str], None]] = None, 
                 on_chat_created: Optional[Callable[[PromptJob, str], None]] = None):
        self.settings = settings
        self.on_progress = on_progress
        self.on_chat_created = on_chat_created
        self.download_dir = settings.download_dir
        self.download_dir.mkdir(parents=True, exist_ok=True)
        self._cancelled = False

    def log_info(self, msg: str) -> None:
        logger.info(msg)
        if self.on_progress:
            self.on_progress(50, msg)

    def cancel(self) -> None:
        self._cancelled = True
        self.log_info("Worker Cancelled.")
        try:
            if hasattr(self, '_context') and self._context:
                self._context.close()
        except Exception:
            pass

    def _get_job_session_path(self, job_index: int) -> Path:
        sessions_dir = Path.home() / 'Documents' / 'easemate_video_automation' / 'sessions'
        sessions_dir.mkdir(parents=True, exist_ok=True)
        return sessions_dir / f"session_job_{job_index}.json"

    def run_job(self, job: PromptJob, mode: str = "full", video_index: int = 0) -> bool:
        self.video_index = video_index
        mode_label = "headed" if not self.settings.headless else "headless"
        self.log_info(f"Job #{job.index}: Starting Playwright execution in {mode_label} mode.")
        
        # Determine profile directory path
        profile_name = getattr(self.settings, 'active_profile_name', 'Default')
        profile_dir = Path.home() / 'Documents' / 'easemate_video_automation' / 'profiles' / profile_name
        profile_dir.mkdir(parents=True, exist_ok=True)
        self.log_info(f"Using persistent browser profile: {profile_name} at {profile_dir}")
                
        # Safely remove any stale Chrome lock files from previous crashes to prevent Playwright hangs
        for lock_file in ["SingletonLock", "lockfile", "lock"]:
            lock_path = profile_dir / lock_file
            try:
                if lock_path.is_symlink() or lock_path.exists():
                    lock_path.unlink()
            except Exception:
                pass

        session_path = self._get_job_session_path(job.index)
        success = False
        
        # Connect to a fresh NordVPN location BEFORE launching the browser context to ensure guest limits are bypassed immediately
        if mode != "download_only":
            self.log_info("Pre-launch task started: Connecting to a fresh NordVPN IP region...")
            try:
                VPNRotator.rotate_vpn(self.log_info)
                
                # Wait for the network connection to stabilize
                self.log_info("Waiting for VPN network interface to stabilize and allocate IP...")
                import urllib.request
                import time
                stable = False
                for _ in range(10): # Max 20 seconds
                    try:
                        urllib.request.urlopen("https://www.easemate.ai/", timeout=3)
                        stable = True
                        break
                    except Exception:
                        time.sleep(2)
                if stable:
                    self.log_info("VPN network interface is stable and online.")
                else:
                    self.log_info("Warning: VPN network interface is slow/offline, continuing anyway...")
            except Exception as e:
                self.log_info(f"Warning: Failed to rotate NordVPN connection on startup: {e}")
                
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
                self.log_info(f"Failed to launch persistent context: {e}")
                raise e
            
            if context.pages:
                page = context.pages[0]
            else:
                page = context.new_page()
                
            page.set_default_navigation_timeout(60000)
            
            # Anti-detection Playwright stealth injection

            try:
                if mode == "download_only":
                    self.log_info(f"Opening browser to download for job #{job.index}...")
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
                try:
                    context.storage_state(path=str(session_path))
                    self.log_info(f"Saved session state to {session_path} for recovery.")
                except Exception:
                    pass
                raise e
            finally:
                try:
                    context.close()
                except Exception:
                    pass
                    
        return success

    def _execute_on_page(self, page: Page, context: BrowserContext, job: PromptJob) -> bool:
        # Load timeout settings
        loading_timeout_sec = getattr(self.settings, 'easemate_loading_timeout_sec', 300)
        loading_timeout_ms = loading_timeout_sec * 1000
        
        # Use a standard 30s default timeout for general selector operations, preventing thread freezes
        page.set_default_timeout(30000)
        
        # First load the homepage to clear cookies and storage natively
        self.log_info("Navigating to easemate.ai homepage to cleanse storage...")
        try:
            page.goto("https://www.easemate.ai/", wait_until="domcontentloaded", timeout=30000)
            
            # Clear cookies natively
            context.clear_cookies()
            
            # Clear local storage and session storage natively via JS
            try:
                page.evaluate("window.localStorage.clear(); window.sessionStorage.clear();")
            except Exception:
                pass
                
            self.log_info("Successfully cleared local storage, session storage, and cookies for easemate.ai.")
            page.wait_for_timeout(1000)
        except Exception as e:
            self.log_info(f"Warning clearing session storage/Cookies: {e}")
            
        target_url = getattr(self.settings, 'target_url', "https://www.easemate.ai/ai-image-generator")
        self.log_info(f"Navigating to target URL: {target_url}...")
        page.goto(target_url, wait_until="domcontentloaded", timeout=loading_timeout_ms)
        self.log_info("Generator page loaded.")
        
        # Force browser to focus on page DOM and dismiss suggestions / info bubbles
        try:
            self.log_info("Dismissing browser input popups...")
            page.locator("body").click()
            page.keyboard.press("Escape")
            page.wait_for_timeout(1000)
            self.log_info("Focused page body and dismissed browser input popups.")
        except Exception as e:
            self.log_info(f"Failed to focus body or send escape key: {e}")
        
        # Check auth warning
        try:
            if page.locator("button:has-text('Log In'), button:has-text('Sign Up'), button:has-text('Sign In')").first.is_visible():
                self.log_info("Warning: Easemate login buttons detected. Headless might run unauthenticated.")
        except Exception:
            pass
            
        # Check auth warning / login alert immediately on page load
        try:
            login_el = page.locator("div:has-text('Please login to use'), span:has-text('Please login to use'), div:has-text('Please log in to use'), span:has-text('Please log in to use'), *:has-text('Please login to use'), *:has-text('Please log in to use')").first
            if login_el.is_visible():
                job.error = "Please login to use"
                self.log_info("Detected EaseMate Login Prompt alert on page load. Exiting to rotate profile.")
                return False
        except Exception:
            pass

        # Wait up to dynamic timeout for page components to load
        self.log_info(f"Waiting up to {loading_timeout_sec} seconds for EaseMate UI components to load...")
        try:
            page.wait_for_selector("button:has-text('Text to Image'), span:has-text('Text to Image'), div:has-text('Text to Image')", state="visible", timeout=15000)
            self.log_info("EaseMate UI components detected successfully.")
        except Exception as e:
            self.log_info(f"Warning: Timeout waiting for main UI components: {e}")
            
        page.wait_for_timeout(3000)

        # Determine mode to run based on settings and reference image existence
        run_mode = "text"
        gen_mode_setting = getattr(self.settings, 'generation_mode', "Auto")
        
        if "Text to Image" in gen_mode_setting:
            run_mode = "text"
        elif "Image to Image" in gen_mode_setting:
            run_mode = "image"
            if not job.has_reference:
                raise Exception("Image to Image mode selected, but no valid local reference image path is provided in the CSV.")
        else: # Auto
            run_mode = "image" if job.has_reference else "text"

        # Switch to correct mode (Image to Image or Text to Image)
        if run_mode == "image":
            self.log_info("Switching to Image to Image mode...")
            try:
                # Target exact button text using get_by_role / get_by_text
                img_to_img_tab = page.get_by_role("button", name="Image to Image", exact=True).first
                if not img_to_img_tab.is_visible():
                    img_to_img_tab = page.get_by_text("Image to Image", exact=True).first
                    
                img_to_img_tab.wait_for(state="visible", timeout=15000)
                try:
                    img_to_img_tab.click(force=True, timeout=5000)
                except Exception:
                    page.evaluate("document.querySelectorAll('button, span, div').forEach(el => { if (el.innerText && el.innerText.trim() === 'Image to Image') el.click(); })")
                self.log_info("Successfully switched to Image to Image mode.")
                page.wait_for_timeout(1500)
                
                # Upload the reference image
                self.log_info(f"Uploading reference image: {job.reference_image}...")
                file_input = page.locator("input[type='file']").first
                file_input.set_input_files(str(job.reference_image))
                self.log_info("Successfully attached reference image.")
                page.wait_for_timeout(2000)
            except Exception as e:
                self.log_info(f"Warning: Image to Image tab / upload failed: {e}")
        else:
            self.log_info("Switching to Text to Image mode...")
            try:
                # Target exact button text using get_by_role / get_by_text
                text_to_image_tab = page.get_by_role("button", name="Text to Image", exact=True).first
                if not text_to_image_tab.is_visible():
                    text_to_image_tab = page.get_by_text("Text to Image", exact=True).first
                    
                text_to_image_tab.wait_for(state="visible", timeout=15000)
                try:
                    text_to_image_tab.click(force=True, timeout=5000)
                except Exception:
                    page.evaluate("document.querySelectorAll('button, span, div').forEach(el => { if (el.innerText && el.innerText.trim() === 'Text to Image') el.click(); })")
                self.log_info("Successfully switched to Text to Image mode.")
                page.wait_for_timeout(1000)
            except Exception as e:
                self.log_info(f"Warning: Text to Image tab not visible or failed to load: {e}")

        # 1. Model Selection
        model = self.settings.model
        self.log_info(f"Selecting model: {model}")
        
        if model == "GPT image 2":
            self.log_info("Model is 'GPT image 2' (default). Bypassing selection to avoid popup overlaps.")
        else:
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
                curr_model_text = trigger.inner_text().strip()
                if model.lower() in curr_model_text.lower():
                    self.log_info(f"Model is already set to '{model}'. Skipping selection.")
                else:
                    self.log_info("Clicking model dropdown trigger...")
                    try:
                        trigger.scroll_into_view_if_needed()
                    except Exception:
                        pass
                    trigger.click()
                    page.wait_for_timeout(1500)
                    
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
        
        # Click Escape first to dismiss any stray model menus covering elements
        try:
            page.keyboard.press("Escape")
            page.wait_for_timeout(500)
        except Exception:
            pass
            
        try:
            page.wait_for_selector("textarea", state="visible", timeout=10000)
            self.log_info("Detected visible textarea on page.")
        except Exception as e:
            self.log_info(f"Warning: Timeout waiting for textarea visibility: {e}")
            
        textarea = None
        try:
            all_tas = page.locator("textarea").all()
            for ta in all_tas:
                if ta.is_visible():
                    textarea = ta
                    break
        except Exception as e:
            self.log_info(f"Error checking textareas: {e}")
            
        if not textarea:
            textarea = page.locator("textarea").first
            
        if textarea and textarea.is_visible():
            try:
                textarea.scroll_into_view_if_needed()
            except Exception:
                pass
            textarea.click()
            page.wait_for_timeout(300)
            
            # Select all and delete default prompt
            try:
                page.keyboard.press("Control+A")
                page.keyboard.press("Backspace")
                page.wait_for_timeout(300)
                textarea.fill("")
            except Exception as e:
                self.log_info(f"Warning: failed to clear textarea via keyboard: {e}")
                
            textarea.fill(job.prompt)
            page.wait_for_timeout(3000) # Wait 3 seconds as requested
        else:
            raise Exception("Prompt textarea not visible on page.")

        # 3. Aspect Ratio Selection
        aspect_ratio = self.settings.ratio
        self.log_info(f"Selecting aspect ratio: {aspect_ratio}")
        ratio_btn = None
        
        try:
            # First try direct container locator
            container = page.locator("div:has-text('Output Aspect Ratios')").last
            btn = container.locator(f"div.cursor-pointer:has-text('{aspect_ratio}')").first
            if btn.is_visible():
                ratio_btn = btn
        except Exception:
            pass
            
        if not ratio_btn:
            try:
                btn = page.locator(f"div.cursor-pointer:has-text('{aspect_ratio}')").first
                if btn.is_visible():
                    ratio_btn = btn
            except Exception:
                pass

        if not ratio_btn:
            ratio_selectors = [
                f"xpath=//div[contains(., 'Output Aspect Ratios')]/following-sibling::div//*[text()='{aspect_ratio}']",
                f"xpath=//*[contains(text(), 'Output Aspect Ratios')]/..//*[text()='{aspect_ratio}']",
                f"text='{aspect_ratio}'"
            ]
            for r_sel in ratio_selectors:
                try:
                    btn = page.locator(r_sel).first
                    if btn.is_visible():
                        ratio_btn = btn
                        break
                except Exception:
                    pass
                    
        if ratio_btn:
            try:
                ratio_btn.scroll_into_view_if_needed()
            except Exception:
                pass
            ratio_btn.click()
            page.wait_for_timeout(1000)
        else:
            self.log_info(f"Aspect ratio '{aspect_ratio}' not visible, skipping.")

        # 4. Resolution Selection
        self.log_info("Resolution ratio is locked to '1K' (free tier). Skipping selection.")

        # 5. Submit / Generate
        self.log_info("Clicking Generate button...")
        gen_btn = page.locator("button:has-text('Generate'), button:has-text('Create'), button[type='submit']").first
        try:
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        except Exception:
            pass
        try:
            gen_btn.scroll_into_view_if_needed()
        except Exception:
            pass
        gen_btn.click()
        self.log_info("Generate button clicked. Submission sent.")

        # Wait 30 seconds unconditionally after submitting the request, checking for login alerts
        self.log_info("Waiting 30 seconds for submission buffers...")
        for _ in range(60): # 30 seconds (60 * 500ms)
            page.wait_for_timeout(500)
            try:
                login_el = page.locator("div:has-text('Please login to use'), span:has-text('Please login to use'), div:has-text('Please log in to use'), span:has-text('Please log in to use'), *:has-text('Please login to use'), *:has-text('Please log in to use')").first
                if login_el.is_visible():
                    job.error = "Please login to use"
                    self.log_info("Detected EaseMate Login Prompt alert post-click. Exiting to rotate profile.")
                    return False
            except Exception:
                pass

        # Save mid-flight session storage just in case we need it for download mode
        try:
            job.chat_url = page.url
            if self.on_chat_created:
                self.on_chat_created(job, job.chat_url)
            session_path = self._get_job_session_path(job.index)
            context.storage_state(path=str(session_path))
            self.log_info(f"Saved session state for job #{job.index} to {session_path}")
        except Exception as e:
            self.log_info(f"Failed to perform session save: {e}")

        if self.settings.submit_and_close:
            job.status = JobStatus.SUBMITTED
            return True

        job.status = JobStatus.WAITING
        return self._wait_and_download(page, job)

    def _execute_download_only(self, page: Page, context: BrowserContext, job: PromptJob) -> bool:
        if not job.chat_url:
            raise Exception("Job does not have a saved chat URL to download from.")
        self.log_info(f"Navigating directly to job URL: {job.chat_url}")
        
        # Load and restore session state cookies
        try:
            import json
            session_path = self._get_job_session_path(job.index)
            if session_path.exists():
                with open(session_path, 'r', encoding='utf-8') as f:
                    state = json.load(f)
                context.clear_cookies()
                context.add_cookies(state.get("cookies", []))
                self.log_info(f"Restored cookies for job #{job.index} session successfully.")
        except Exception as e:
            self.log_info(f"Warning: Failed to restore session cookies: {e}")
            
        page.goto(job.chat_url, wait_until="domcontentloaded")
        page.wait_for_timeout(5000)
        return self._wait_and_download(page, job)

    def _find_download_button_in_card(self, parent_locator) -> Optional[Locator]:
        try:
            clickables = parent_locator.locator("button, a, div.cursor-pointer, [role='button']").all()
            
            # 1. First pass: explicit download tags check (title, aria-label, class, ID)
            for c in clickables:
                title = (c.get_attribute("title") or "").lower()
                aria = (c.get_attribute("aria-label") or "").lower()
                cls = (c.get_attribute("class") or "").lower()
                
                if "download" in title or "download" in aria or "download" in cls:
                    if c.is_visible():
                        return c
                        
            # 2. Second pass: search for icon-only tags, strictly excluding standard <a> navigation links
            for c in clickables:
                tag = c.evaluate("el => el.tagName").lower()
                txt = (c.inner_text() or "").strip().lower()
                
                # Skip text-heavy actions
                if any(kw in txt for kw in ["generate", "video", "recreate", "remix"]):
                    continue
                    
                # Skip <a> tags unless they have explicit download attributes or download text
                if tag == "a":
                    has_dl_attr = c.get_attribute("download") is not None
                    if not has_dl_attr:
                        continue
                        
                if c.is_visible():
                    return c
        except Exception:
            pass
        return None

    def _wait_and_download(self, page: Page, job: PromptJob) -> bool:
        self.log_info("Waiting patiently for the image generation to complete...")
        
        # 1. Wait for "Generating..." progress text to completely disappear from the page
        generating_detected = False
        consecutive_missing = 0
        max_gen_polls = 60  # 60 * 5 seconds = 5 minutes maximum wait
        
        for gp in range(max_gen_polls):
            if self._cancelled:
                self.log_info("Job execution cancelled by user.")
                return False
                
            try:
                # Find any text element containing "generating" (case-insensitive)
                progress_loc = page.locator("div:has-text('Generating'), span:has-text('Generating'), p:has-text('Generating'), *:has-text('Generating')").first
                if progress_loc.is_visible():
                    progress_text = (progress_loc.text_content() or "").strip()
                    if progress_text and "generating" in progress_text.lower():
                        generating_detected = True
                        consecutive_missing = 0  # reset missing count
                        if len(progress_text) > 40:
                            progress_text = progress_text[:37] + "..."
                        self.log_info(f"EaseMate Status: {progress_text}")
                        # Emit to UI table row status
                        self.job_progress.emit(job.index, progress_text)
                        
                        # Wait 5 seconds and poll again
                        page.wait_for_timeout(5000)
                        continue
            except Exception:
                pass
                
            # If we haven't seen a generating message yet, wait a brief moment to let it start up
            if not generating_detected:
                if gp < 3:
                    self.log_info("Checking for startup of generation progress...")
                    page.wait_for_timeout(3000)
                    continue
                else:
                    self.log_info("No generation progress detected on startup. Proceeding to download check.")
                    break
            else:
                # We previously saw "Generating...", but now it is missing.
                # Increment consecutive missing count to handle React rendering flickers
                consecutive_missing += 1
                self.log_info(f"Generation message missing. Verification count: {consecutive_missing}/3...")
                
                # Check if the download button is actually visible now!
                download_btn_visible = False
                try:
                    clean_prompt = job.prompt.strip()
                    short_prompt = clean_prompt[:25]
                    candidates = page.locator(f"p:has-text('{short_prompt}'), div:has-text('{short_prompt}'), span:has-text('{short_prompt}')").all()
                    for cand in candidates:
                        if cand.is_visible():
                            parent = cand
                            for _ in range(5):
                                parent = parent.locator("..")
                                btn = self._find_download_button_in_card(parent)
                                if btn:
                                    download_btn_visible = True
                                    break
                            if download_btn_visible:
                                break
                except Exception:
                    pass
                    
                if download_btn_visible:
                    self.log_info("Download button is visible. Generation completed successfully.")
                    break
                elif consecutive_missing >= 3:
                    self.log_info("Generation progress text was missing for 15 seconds. Proceeding to final download search.")
                    break
                else:
                    # Wait and continue polling
                    page.wait_for_timeout(5000)
                    continue
                    
        # Wait 3 seconds for the React DOM to fully render the completed card buttons
        page.wait_for_timeout(3000)
        
        # 2. Locate the download button inside the card matching this prompt
        clean_prompt = job.prompt.strip()
        short_prompt = clean_prompt[:25]
        download_btn = None
        
        # Try to locate the download button inside the matching card
        try:
            candidates = page.locator(f"p:has-text('{short_prompt}'), div:has-text('{short_prompt}'), span:has-text('{short_prompt}')").all()
            for cand in candidates:
                if cand.is_visible():
                    parent = cand
                    for _ in range(5):
                        parent = parent.locator("..")
                        btn = self._find_download_button_in_card(parent)
                        if btn:
                            download_btn = btn
                            break
                    if download_btn:
                        break
        except Exception as e:
            self.log_info(f"Error searching card via prompt text: {e}")
            
        # Fallback: search by image alt matching the prompt
        if not download_btn:
            try:
                imgs = page.locator("img").all()
                for img in imgs:
                    alt = (img.get_attribute("alt") or "").strip().lower()
                    if alt and (short_prompt.lower() in alt or alt in clean_prompt.lower()):
                        parent = img
                        for _ in range(6):
                            parent = parent.locator("..")
                            btn = self._find_download_button_in_card(parent)
                            if btn:
                                download_btn = btn
                                break
                        if download_btn:
                            break
            except Exception:
                pass
                
        # Final fallback: pick the very first icon-only button under the first generated feed card
        if not download_btn:
            try:
                self.log_info("Download button not found in card; trying first feed card fallback...")
                feed_cards = page.locator("div.border, div.rounded-3xl, div.shadow-sm").all()
                for card in feed_cards:
                    if card.is_visible():
                        btn = self._find_download_button_in_card(card)
                        if btn:
                            download_btn = btn
                            break
            except Exception:
                pass
                
        if not download_btn:
            raise Exception("Failed to locate the download button for the generated image.")
            
        self.log_info("Initiating high-resolution image download...")
        safe_name = "".join(c for c in job.video_title if c.isalnum() or c in (' ', '-', '_')).strip()
        dest_path = self.download_dir / f"{safe_name}.png"
        
        with page.expect_download() as download_info:
            download_btn.click()
        download = download_info.value
        download.save_as(str(dest_path))
        
        job.download_path = str(dest_path)
        self.log_info(f"Successfully downloaded image: {dest_path.name}")
        return True


class EasemateBatchRunner(QThread):
    job_progress = pyqtSignal(int, str)  # job_index, message
    chat_created = pyqtSignal(int, str)  # job_index, chat_url
    job_finished = pyqtSignal(int, bool, str, str)  # job_index, success, download_path, error
    batch_finished = pyqtSignal()
    profile_rotated = pyqtSignal(str)

    def __init__(self, jobs: List[PromptJob], settings: AutomationSettings, db: HistoryDatabase, session_id: int, mode: str = "full"):
        super().__init__()
        self.jobs = jobs
        self.settings = settings
        self.db = db
        self.session_id = session_id
        self.mode = mode  # "full", "submit_only", "download_only"
        self._stop = False
        self._paused = False
        self.active_workers = {}
        self.lock = threading.Lock()

    def stop(self) -> None:
        self._stop = True
        with self.lock:
            for w in list(self.active_workers.values()):
                try:
                    w.cancel()
                except Exception:
                    pass

    def pause_resume(self) -> bool:
        self._paused = not self._paused
        return self._paused

    def run(self):
        logger.info(f"EasemateBatchRunner starting in '{self.mode}' mode with {len(self.jobs)} jobs. Concurrency limit = {self.settings.thread_count}.")
        
        # Filter jobs depending on mode
        runnable_jobs = []
        for job in self.jobs:
            if self.mode == "submit_only" and job.status == JobStatus.PENDING:
                runnable_jobs.append(job)
            elif self.mode == "download_only" and job.chat_url and job.status in [JobStatus.PENDING, JobStatus.RUNNING, JobStatus.WAITING, JobStatus.DOWNLOADING, JobStatus.SUBMITTED]:
                runnable_jobs.append(job)
            elif self.mode == "full" and job.status in [JobStatus.PENDING, JobStatus.RUNNING, JobStatus.WAITING, JobStatus.DOWNLOADING, JobStatus.SUBMITTED]:
                runnable_jobs.append(job)

        self.job_progress.emit(-1, f"EasemateBatchRunner started. Concurrency: {self.settings.thread_count}. Runnable jobs: {len(runnable_jobs)}")
        
        queue = list(runnable_jobs)
        running_threads = []
        
        def thread_target(job_obj):
            try:
                self.run_job_wrapper(job_obj)
            except Exception as e:
                logger.error(f"Error in thread execution for job #{job_obj.index}: {e}", exc_info=True)
                self.job_finished.emit(job_obj.index, False, "", str(e))
        
        while (queue or running_threads) and not self._stop:
            running_threads = [t for t in running_threads if t.is_alive()]
            
            while self._paused and not self._stop:
                time.sleep(0.5)
                running_threads = [t for t in running_threads if t.is_alive()]
                
            if self._stop:
                break
                
            if len(running_threads) < self.settings.thread_count and queue:
                job_to_run = queue.pop(0)
                t = threading.Thread(target=thread_target, args=(job_to_run,), name=f"EMWorker-{job_to_run.index}")
                running_threads.append(t)
                t.start()
                
            time.sleep(0.2)
            
        for t in running_threads:
            t.join()
            
        self.batch_finished.emit()

    def run_job_wrapper(self, job: PromptJob):
        if self._stop:
            self.job_progress.emit(job.index, "Cancelled")
            return
            
        while self._paused:
            if self._stop:
                self.job_progress.emit(job.index, "Cancelled")
                return
            time.sleep(0.5)
            
        thread_settings = copy.deepcopy(self.settings)
        if job.chat_url and self.mode != "submit_only":
            worker_mode = "download_only"
        else:
            worker_mode = "download_only" if self.mode == "download_only" else "full"
        if self.mode == "submit_only":
            thread_settings.submit_and_close = True

        import datetime
        job.started_at = datetime.datetime.utcnow().isoformat()
        self.db.update_job(job.job_id, status=JobStatus.RUNNING, mark_started=True)
        self.job_progress.emit(job.index, f"Starting job #{job.index}...")
        
        success = False
        error_msg = ""
        retries = 0
        max_retries = 3
        
        while retries < max_retries and not self._stop:
            worker = EasemateBrowserWorker(
                settings=thread_settings,
                on_progress=lambda p_val, p_msg: self.job_progress.emit(job.index, p_msg),
                on_chat_created=lambda j_obj, u_str: self._handle_chat_created(job, u_str)
            )
            
            with self.lock:
                if self._stop:
                    break
                self.active_workers[job.index] = worker
                
            video_idx = 0
            if job.chat_url:
                try:
                    shared_jobs = self.db.get_jobs_by_chat_url(job.chat_url)
                    shared_jobs = [j for j in shared_jobs if j.video_title == job.video_title]
                    job_ids = [j.job_id for j in shared_jobs]
                    if job.job_id in job_ids:
                        video_idx = job_ids.index(job.job_id)
                except Exception as e:
                    logger.warning(f"Could not calculate video index: {e}")
                    
            try:
                success = worker.run_job(job, mode=worker_mode, video_index=video_idx)
                if success:
                    break
                else:
                    error_msg = job.error or "Execution failed"
            except Exception as e:
                error_msg = str(e)
                logger.error(f"Error executing job #{job.index} (attempt {retries+1}): {e}", exc_info=True)
                
            if not success:
                lower_err = error_msg.lower()
                if any(x in lower_err for x in ["limit", "quota", "credit", "point", "login", "log in"]):
                    self.job_progress.emit(job.index, "Login/Quota warning detected. Rotating NordVPN IP region...")
                    try:
                        VPNRotator.rotate_vpn(lambda msg: self.job_progress.emit(job.index, msg))
                    except Exception as vpn_err:
                        self.job_progress.emit(job.index, f"Warning: NordVPN rotation failed: {vpn_err}")
                
            retries += 1
            if retries < max_retries and not self._stop:
                self.job_progress.emit(job.index, f"Attempt {retries} failed: {error_msg}. Retrying in 5 seconds (Attempt {retries+1}/{max_retries})...")
                for _ in range(10):
                    if self._stop:
                        break
                    time.sleep(0.5)
                    
        with self.lock:
            self.active_workers.pop(job.index, None)
            
        final_status = JobStatus.FAILED
        if success:
            if self.mode == "submit_only":
                final_status = JobStatus.SUBMITTED
            else:
                final_status = JobStatus.COMPLETED
        else:
            if self._stop:
                final_status = JobStatus.CANCELLED
            else:
                final_status = JobStatus.FAILED
                job.error = error_msg or "Unknown execution error"

        job.status = final_status
        self.db.update_job(
            job.job_id, 
            status=final_status, 
            download_path=Path(job.download_path) if job.download_path else None,
            error=job.error,
            mark_finished=True
        )
        
        if final_status == JobStatus.COMPLETED and job.download_path:
            self.db.record_download(job.job_id, Path(job.download_path))
            self.db.bump_session_counts(self.session_id, completed=1)
        elif final_status in [JobStatus.FAILED]:
            self.db.bump_session_counts(self.session_id, failed=1)

        self.job_finished.emit(job.index, success, job.download_path or "", job.error or "")

    def _handle_chat_created(self, job: PromptJob, chat_url: str):
        job.chat_url = chat_url
        self.db.update_job(job.job_id, chat_url=chat_url)
        self.chat_created.emit(job.index, chat_url)

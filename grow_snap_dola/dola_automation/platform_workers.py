import os
import time
import json
import logging
from pathlib import Path
from typing import Optional, Callable, List
try:
    from patchright.sync_api import sync_playwright, Page, Response
except ImportError:
    try:
        from playwright.sync_api import sync_playwright, Page, Response
    except ImportError:
        raise ImportError("Neither patchright nor playwright is installed in the python environment.")

logger = logging.getLogger("grow_snap.platform_workers")

class PlatformWorkerError(Exception):
    pass

def download_media_via_page(page: Page, url: str, dest_path: Path) -> bool:
    try:
        response = page.context.request.get(url)
        if response.ok:
            dest_path.write_bytes(response.body())
            return True
    except Exception as e:
        logger.error(f"Playwright download helper failed for {url}: {e}")
    return False

class BasePlatformWorker:
    def __init__(self, platform_name: str, download_dir: Path, on_progress: Optional[Callable[[str], None]] = None):
        self.platform_name = platform_name
        self.download_dir = download_dir
        self.on_progress = on_progress
        self._cancelled = False
        
        self.download_dir.mkdir(parents=True, exist_ok=True)
        self.profile_dir = Path.home() / 'Documents' / 'dola_video_automation' / 'profiles' / f"{platform_name}_profile"
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        
    def log(self, msg: str):
        logger.info(f"[{self.platform_name}] {msg}")
        if self.on_progress:
            self.on_progress(msg)

    def cancel(self):
        self._cancelled = True

    def get_storage_state_path(self) -> Path:
        return self.profile_dir / "storage_state.json"

    def has_session(self) -> bool:
        state_path = self.get_storage_state_path()
        return state_path.exists() and state_path.stat().st_size > 0

    def launch_session_config(self, target_url: str):
        """
        Launches a headed browser instance for the user to manually sign in.
        Saves storage state upon close.
        """
        self.log(f"Launching login window for {self.platform_name}...")
        with sync_playwright() as p:
            # Disable automation flags to bypass basic bot filters during login
            launch_args = ["--disable-blink-features=AutomationControlled"]
            if os.name != 'nt':
                launch_args.extend(["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"])
                
            context = p.chromium.launch_persistent_context(
                user_data_dir=str(self.profile_dir),
                headless=False,
                args=launch_args,
                viewport={"width": 1280, "height": 800}
            )
            
            page = context.new_page()
            page.goto(target_url)
            
            self.log("Browser opened. Please log in manually inside the browser.")
            self.log("Once logged in, close the browser window to save your session.")
            
            # Keep checking until the window is closed by the user
            while True:
                time.sleep(0.5)
                if not context.pages or page.is_closed():
                    break
            
            try:
                context.storage_state(path=str(self.get_storage_state_path()))
                self.log("Session state saved successfully.")
            except Exception as e:
                self.log(f"Failed to save session state: {e}")
            finally:
                context.close()

    def run_batch(self, prompts: List[str], target_url: str, headless: bool = True, auto_clean: bool = False) -> List[str]:
        """
        Base runner loop for batch prompt submission.
        """
        if not self.has_session():
            raise PlatformWorkerError("No active session found. Please configure session and log in first.")
            
        outputs = []
        self.log(f"Starting batch of {len(prompts)} prompts...")
        
        with sync_playwright() as p:
            launch_args = []
            if headless:
                launch_args.extend(["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"])
            else:
                launch_args.append("--disable-blink-features=AutomationControlled")
                
            browser = p.chromium.launch(headless=headless, args=launch_args)
            
            # Load the persistent session state
            context = browser.new_context(
                storage_state=str(self.get_storage_state_path()),
                viewport={"width": 1280, "height": 800}
            )
            
            page = context.new_page()
            
            # Media downloader interceptor
            intercepted_urls = []
            def response_handler(response: Response):
                # Sniff media resources being fetched
                url = response.url
                content_type = response.headers.get("content-type", "")
                if "video" in content_type or url.endswith(".mp4") or "dall-e" in url:
                    if url not in intercepted_urls:
                        intercepted_urls.append(url)
            page.on("response", response_handler)
            
            try:
                page.goto(target_url)
                # Verify login state
                if not self.verify_logged_in(page):
                    raise PlatformWorkerError("Session has expired. Re-authenticate.")
                    
                for idx, prompt in enumerate(prompts):
                    if self._cancelled:
                        self.log("Batch cancelled by user.")
                        break
                        
                    self.log(f"Processing Prompt {idx+1}/{len(prompts)}: '{prompt}'")
                    
                    # Call sub-class prompt submission flow
                    out_path = self.process_prompt(page, prompt, intercepted_urls)
                    if out_path:
                        self.log(f"Generated asset saved to: {out_path}")
                        
                        # Step 2: Overlay Cleaning Integration (If toggled)
                        if auto_clean:
                            out_path = self.perform_overlay_cleanup(out_path)
                            
                        outputs.append(str(out_path))
                    else:
                        self.log(f"Failed to generate asset for prompt: '{prompt}'")
            finally:
                context.close()
                browser.close()
                
        return outputs

    def verify_logged_in(self, page: Page) -> bool:
        """ Subclass overrides this to verify login is valid """
        return True

    def process_prompt(self, page: Page, prompt: str, intercepted_urls: List[str]) -> Optional[Path]:
        """ Subclass overrides this with custom prompt and download flow """
        return None

    def perform_overlay_cleanup(self, video_path: Path) -> Path:
        """
        Integrates with ffmpeg_utils to clean watermarks/logos.
        """
        self.log(f"Running automated visual overlay cleanup on {video_path.name}...")
        try:
            from dola_automation.ffmpeg_utils import blur_video_watermark
            out_name = f"cleaned_{video_path.name}"
            out_path = video_path.parent / out_name
            
            # Default blur area (Bottom right standard preset)
            blur_video_watermark(
                str(video_path),
                str(out_path),
                x=540, y=1220, w=170, h=80
            )
            if out_path.exists():
                video_path.unlink()  # Clean up original uncleaned draft
                return out_path
        except Exception as e:
            self.log(f"Overlay cleanup failed: {e}")
        return video_path

# =====================================================================
# Platform Specific Automation Scripts
# =====================================================================

class GrokWorker(BasePlatformWorker):
    def __init__(self, download_dir: Path, on_progress: Optional[Callable[[str], None]] = None):
        super().__init__("Grok", download_dir, on_progress)

    def verify_logged_in(self, page: Page) -> bool:
        try:
            # Check for Grok input field or logged in elements
            page.wait_for_selector("textarea, [contenteditable='true']", timeout=6000)
            return True
        except Exception:
            return False

    def process_prompt(self, page: Page, prompt: str, intercepted_urls: List[str]) -> Optional[Path]:
        try:
            # Locate input
            input_sel = "textarea, [contenteditable='true']"
            page.wait_for_selector(input_sel, timeout=10000)
            
            # Input prompt and submit
            page.fill(input_sel, prompt)
            page.keyboard.press("Enter")
            
            self.log("Prompt submitted. Waiting for generation to complete...")
            
            # Poll for final video player or image output
            media_sel = "video, img[src*='grok'], img[src*='blob']"
            page.wait_for_selector(media_sel, timeout=120000) # Grok generates in ~30-60s
            time.sleep(5)  # Let video buffer fully
            
            # Try to grab downloaded URL or extract intercepted HTTP assets
            if intercepted_urls:
                media_url = intercepted_urls[-1]
                self.log(f"Intercepted media resource: {media_url}")
                # Download file
                file_name = f"grok_{int(time.time())}.mp4" if ".mp4" in media_url else f"grok_{int(time.time())}.png"
                dest_path = self.download_dir / file_name
                
                # Fetch bytes
                if download_media_via_page(page, media_url, dest_path):
                    return dest_path
        except Exception as e:
            self.log(f"Grok processing error: {e}")
        return None

class ChatGPTWorker(BasePlatformWorker):
    def __init__(self, download_dir: Path, on_progress: Optional[Callable[[str], None]] = None):
        super().__init__("ChatGPT", download_dir, on_progress)

    def verify_logged_in(self, page: Page) -> bool:
        try:
            page.wait_for_selector("#prompt-textarea", timeout=6000)
            return True
        except Exception:
            return False

    def process_prompt(self, page: Page, prompt: str, intercepted_urls: List[str]) -> Optional[Path]:
        try:
            input_sel = "#prompt-textarea"
            page.wait_for_selector(input_sel, timeout=10000)
            
            page.fill(input_sel, prompt)
            page.click("button[data-testid='send-button']")
            
            self.log("Submitted to ChatGPT. Waiting for generation...")
            
            # Wait for spinner / typing to complete
            page.wait_for_selector("button[data-testid='send-button']", timeout=120000)
            time.sleep(3)
            
            # Look for DALL-E download link or image
            img_sel = "img[src*='dall-e'], a[href*='dall-e']"
            el = page.locator(img_sel).last
            if el:
                href = el.get_attribute("href") or el.get_attribute("src")
                if href:
                    file_name = f"chatgpt_{int(time.time())}.png"
                    dest_path = self.download_dir / file_name
                    if download_media_via_page(page, href, dest_path):
                        return dest_path
        except Exception as e:
            self.log(f"ChatGPT processing error: {e}")
        return None

class MetaAIWorker(BasePlatformWorker):
    def __init__(self, download_dir: Path, on_progress: Optional[Callable[[str], None]] = None):
        super().__init__("MetaAI", download_dir, on_progress)

    def verify_logged_in(self, page: Page) -> bool:
        try:
            page.wait_for_selector("textarea", timeout=6000)
            return True
        except Exception:
            return False

    def process_prompt(self, page: Page, prompt: str, intercepted_urls: List[str]) -> Optional[Path]:
        try:
            page.wait_for_selector("textarea", timeout=10000)
            page.fill("textarea", prompt)
            page.click("button[aria-label*='Submit']")
            
            self.log("Submitted to Meta AI. Waiting for media response...")
            
            # Wait for generation indicator to vanish
            time.sleep(15)  # Simple fallback delay for Meta AI response render
            
            if intercepted_urls:
                media_url = intercepted_urls[-1]
                file_name = f"metaai_{int(time.time())}.mp4" if "video" in media_url else f"metaai_{int(time.time())}.png"
                dest_path = self.download_dir / file_name
                if download_media_via_page(page, media_url, dest_path):
                    return dest_path
        except Exception as e:
            self.log(f"Meta AI processing error: {e}")
        return None

class FlowWorker(BasePlatformWorker):
    def __init__(self, download_dir: Path, on_progress: Optional[Callable[[str], None]] = None):
        super().__init__("GoogleFlow", download_dir, on_progress)

    def verify_logged_in(self, page: Page) -> bool:
        try:
            page.wait_for_selector("textarea", timeout=6000)
            return True
        except Exception:
            return False

    def process_prompt(self, page: Page, prompt: str, intercepted_urls: List[str]) -> Optional[Path]:
        try:
            page.wait_for_selector("textarea", timeout=10000)
            page.fill("textarea", prompt)
            page.click("button:has-text('Generate')")
            
            self.log("Submitted to Google Flow. Rendering Veo 3 video...")
            
            # Veo video generations can take up to 2-3 minutes
            page.wait_for_selector("video", timeout=180000)
            time.sleep(5)
            
            if intercepted_urls:
                media_url = intercepted_urls[-1]
                file_name = f"googleflow_{int(time.time())}.mp4"
                dest_path = self.download_dir / file_name
                if download_media_via_page(page, media_url, dest_path):
                    return dest_path
        except Exception as e:
            self.log(f"Google Flow processing error: {e}")
        return None

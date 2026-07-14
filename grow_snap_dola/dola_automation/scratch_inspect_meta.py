import sys, time
from pathlib import Path
from playwright.sync_api import sync_playwright

def inspect_meta():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        print("Navigating to Meta AI...")
        try:
            page.goto("https://www.meta.ai/ai-video-generator", timeout=20000)
            page.wait_for_timeout(3000)
            
            # Click Log in button
            login_btn = page.locator("button:has-text('Log in')").first
            if login_btn.is_visible():
                print("Clicking 'Log in' button...")
                login_btn.click()
                page.wait_for_timeout(3000)
                
                print("URL after login click:", page.url)
                print("Title after login click:", page.title())
                
                # Check body text in the login view
                print("\nBody text after login click:")
                print(page.evaluate("() => document.body.innerText.substring(0, 1500)"))
                
                # Check for login buttons
                print("\nButtons and Links in login view:")
                links = page.locator("a, button").all()
                for idx, lnk in enumerate(links):
                    try:
                        text = lnk.inner_text().strip()
                        html = lnk.evaluate("el => el.outerHTML")
                        if text or "facebook" in html.lower() or "instagram" in html.lower() or "meta" in html.lower():
                            print(f"[{idx}] Text: '{text}' | HTML: {html[:200]}")
                    except Exception:
                        pass
            else:
                print("Log in button not visible.")
        except Exception as e:
            print("Failed to navigate/inspect Meta AI login:", e)
        finally:
            browser.close()

if __name__ == '__main__':
    inspect_meta()

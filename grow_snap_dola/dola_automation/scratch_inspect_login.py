import sys, time
from pathlib import Path
from playwright.sync_api import sync_playwright

def inspect_login():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        print("Navigating to Dola...")
        page.goto("https://www.dola.com/chat/create-image")
        page.wait_for_timeout(3000)
        
        # Click cookie OK button if visible
        try:
            cookie_btn = page.locator("button:has-text('OK')").first
            if cookie_btn.is_visible():
                cookie_btn.click()
                print("Clicked cookie OK button.")
                page.wait_for_timeout(1000)
        except Exception as e:
            print("Failed to click cookie button:", e)
            
        # Wait for Log In button to be visible and click it
        try:
            print("Waiting for Log In button...")
            page.locator("button:has-text('Log In')").first.wait_for(state="visible", timeout=10000)
            login_btn = page.locator("button:has-text('Log In')").first
            print("Found Log In button. Clicking it...")
            login_btn.click()
            page.wait_for_timeout(3000)
        except Exception as e:
            print("Error finding/clicking Log In:", e)
            
        print("\nPage URL after Log In click:", page.url)
        
        # Dump page text to see what is displayed in the modal
        print("\nBody text after Log In click:")
        print(page.evaluate("() => document.body.innerText.substring(0, 1500)"))
        
        # Let's check for any modal dialogs
        print("\nChecking for dialog contents/iframes/oauth buttons:")
        links = page.locator("a, button, [role='button'], input").all()
        for idx, lnk in enumerate(links):
            try:
                text = lnk.inner_text().strip()
                html = lnk.evaluate("el => el.outerHTML")
                role = lnk.get_attribute("role")
                val = lnk.get_attribute("value")
                inp_type = lnk.get_attribute("type")
                placeholder = lnk.get_attribute("placeholder")
                print(f"[{idx}] Text: '{text}' | role: '{role}' | type: '{inp_type}' | value: '{val}' | placeholder: '{placeholder}' | HTML: {html[:200]}")
            except Exception:
                pass
                
        browser.close()

if __name__ == '__main__':
    inspect_login()

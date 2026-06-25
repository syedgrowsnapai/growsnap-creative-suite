import base64
import hmac
import hashlib
import json
import os
import urllib.request
from pathlib import Path
from datetime import datetime
from typing import Tuple

SECRET_KEY = b"GrowSnapAI_Ultimate_Secret_2026_SecuredKey!"

def get_license_file_path() -> Path:
    base_dir = Path.home() / 'Documents' / 'dola_video_automation'
    base_dir.mkdir(parents=True, exist_ok=True)
    return base_dir / 'license.json'

def get_hardware_id() -> str:
    """
    Generates a unique, stable hardware fingerprint for the computer (node-lock).
    Combines MAC address and CPU/platform signatures.
    """
    try:
        import uuid
        import platform
        # MAC address
        mac = str(uuid.getnode())
        # CPU/Architecture & system details
        proc = platform.processor() or "unknown_proc"
        system = platform.system() or "unknown_sys"
        
        raw_id = f"{mac}|{proc}|{system}"
        h = hashlib.sha256(raw_id.encode('utf-8')).hexdigest().upper()
        # Return format: GS-XXXX-XXXX-XXXX
        return f"GS-{h[:4]}-{h[4:8]}-{h[8:12]}"
    except Exception:
        return "GS-ERR-HWID-0000"

def get_network_date() -> datetime.date:
    """
    Fetches the true date from Google server headers to prevent local clock manipulation.
    If offline or failed, falls back to local system time.
    """
    try:
        req = urllib.request.Request("https://www.google.com", method="HEAD")
        with urllib.request.urlopen(req, timeout=4) as response:
            date_str = response.headers.get("Date")
            if date_str:
                # Format: "Tue, 23 Jun 2026 19:37:00 GMT"
                dt = datetime.strptime(date_str, "%a, %d %b %Y %H:%M:%S GMT")
                return dt.date()
    except Exception:
        pass
    return datetime.now().date()

def verify_key(email: str, key: str) -> Tuple[bool, str, dict]:
    """
    Decodes, cryptographically validates the token, and verifies hardware + time constraints.
    Returns (is_valid, message, license_details)
    """
    if not key or not email:
        return False, "Email and activation key are required.", {}
        
    try:
        # Decode the Base64 token
        decoded = base64.b64decode(key.encode('utf-8')).decode('utf-8')
        parts = decoded.split('|')
        
        # Token must have 5 parts: email | plan | expiry_date | hardware_id | signature
        if len(parts) != 5:
            return False, "Invalid activation key format. Ensure it was copied completely.", {}
            
        key_email, plan, expiry_str, key_hw, sig = parts
        
        # 1. Verify Email
        if key_email.lower().strip() != email.lower().strip():
            return False, "Activation key is registered to a different email address.", {}
            
        # 2. Verify Cryptographic Signature
        payload = f"{key_email}|{plan}|{expiry_str}|{key_hw}"
        expected_sig = hmac.new(SECRET_KEY, payload.encode('utf-8'), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected_sig):
            return False, "Activation key has an invalid cryptographic signature.", {}
            
        # 3. Verify Hardware Binding (Node Lock)
        if key_hw.strip():
            curr_hw = get_hardware_id()
            if key_hw.strip().upper() != curr_hw.upper():
                return False, f"Activation key is locked to a different computer.\nKey Hardware ID: {key_hw}\nThis Computer ID: {curr_hw}", {}
                
        # 4. Verify Time and Expiry
        try:
            expiry_date = datetime.strptime(expiry_str, "%Y-%m-%d").date()
        except ValueError:
            return False, "Invalid expiration date format encoded inside the key.", {}
            
        network_date = get_network_date()
        
        # Detect clock winding rollback
        # We check local date vs network date
        local_date = datetime.now().date()
        if abs((local_date - network_date).days) > 1:
            return False, "Time Synchronization Error: Your computer system clock is incorrect. Please synchronize it with internet time.", {}
            
        if expiry_date < network_date:
            return False, f"Activation key expired on {expiry_str}.", {}
            
        days_left = (expiry_date - network_date).days
        
        details = {
            "email": key_email,
            "plan": plan,
            "expiry": expiry_str,
            "hardware": key_hw,
            "days_left": days_left
        }
        
        return True, f"Valid Plan: {plan} ({days_left} days left)", details
        
    except Exception as e:
        return False, f"Key verification error: {str(e)}", {}

def check_license_stored() -> Tuple[bool, dict]:
    """
    Checks if a valid license is saved locally.
    Returns (is_valid, license_data)
    """
    lic_file = get_license_file_path()
    if not lic_file.exists():
        return False, {}
    try:
        with open(lic_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
        email = data.get('email', '')
        key = data.get('key', '')
        
        is_valid, msg, details = verify_key(email, key)
        if is_valid:
            data.update(details)
            data['plan_info'] = msg
            return True, data
        return False, {}
    except Exception:
        return False, {}

def save_license(email: str, key: str, plan: str, expiry: str, hardware: str) -> None:
    """
    Saves activation details to local license.json file.
    """
    lic_file = get_license_file_path()
    data = {
        "email": email.strip().lower(),
        "key": key.strip(),
        "plan": plan.strip(),
        "expiry": expiry.strip(),
        "hardware": hardware.strip().upper(),
        "activated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    }
    with open(lic_file, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=4)

def check_license_interactive() -> bool:
    """
    Validates stored license. If invalid/missing, prompts user with ActivationDialog.
    Returns True if valid license exists or is entered, False otherwise.
    """
    is_valid, data = check_license_stored()
    if is_valid:
        return True
        
    # Lazy imports to prevent circular dependencies
    from PyQt6.QtWidgets import QApplication
    from dola_automation.info_dialogs import ActivationDialog
    
    app = QApplication.instance()
    if not app:
        import sys
        app = QApplication(sys.argv)
        
    dialog = ActivationDialog()
    result = dialog.exec()
    
    return result == ActivationDialog.DialogCode.Accepted

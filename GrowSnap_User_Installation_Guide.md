# 🚀 GrowSnap Creative Suite — Simplified Installation & Setup Guide

This guide is designed for end-users to install, configure, and activate the **GrowSnap Creative Suite** on any computer in one click.

---

## 📋 Step 1: Copy and Extract the Package
1. Download or copy the **`GrowSnap_Creative_Suite.zip`** package onto your computer.
2. Move the ZIP file to a convenient folder (such as your `Documents` or `Desktop` directory).
3. Right-click the `.zip` file and select **Extract All...** (on Windows) or double-click it (on macOS/Linux) to unzip the files.

---

## 🏃 Step 2: Run the One-Click Launcher
Open the extracted folder and double-click the launcher script appropriate for your operating system:
*   **Windows**: Double-click `run_grow_snap.bat`
*   **macOS / Linux**: Double-click `run_grow_snap.sh`

> [!NOTE]
> On the first launch, the suite automatically sets up a secure, isolated Python environment and downloads all required dependencies. You do not need to install anything else manually.

---

## 🔑 Step 3: Activate Your License
1. When the application launches for the first time, a **Product Activation** window will appear.
2. Copy the unique **Hardware ID** (e.g., `GS-D2F8-E3A9-B10C`) shown on the screen.
3. Send this Hardware ID along with your email address to the administrator or support team.
4. Once you receive your custom **Activation Key**, paste it into the activation key field in the app and click **Activate**.

---

## 🛠️ Step 4: First-Run Automatic Calibration
After activation, the suite executes an automated first-run setup:
- It checks for **FFmpeg** encoders (downloading static builds in the background if they are missing).
- It verifies and installs the **Patchright Chromium** anti-detect browser driver.

Once the setup progress bar reaches 100%, the main **GrowSnap Creative Suite** dashboard will launch automatically, and you are ready to begin!

---

## 🔍 Troubleshooting Setup Issues

If the application fails to launch or gives command prompt errors:

### 1. "Python is not installed or not added to PATH"
* **Automatic Detection**: Our launcher automatically scans common default folders for Python (e.g., `%LocalAppData%\Programs\Python` and `C:\Program Files\Python`). 
* **If it still fails**: Make sure you have installed Python 3.10 or newer from the official [python.org](https://www.python.org/downloads/) page. During installation, ensure you check the box that says **"Add python.exe to PATH"**.

### 2. Running as Administrator
* **Supported**: You can safely right-click `run_grow_snap.bat` or `GrowSnap_Installer.bat` and select **"Run as Administrator"**. The script automatically corrects its working directory so that no files are misplaced in `C:\Windows\System32`.

### 3. Microsoft Store Redirects
* If double-clicking the launcher opens the Microsoft Store instead of Python, our updated launcher will automatically bypass this redirect by executing the isolated local virtual environment (`.venv`) directly.

---

## 📞 Support & Community Channels
If you experience any issues, have feedback, or want to discuss updates, join the official community:
*   **Support Portal / Chat**: [Telegram Community Chat](https://t.me/growsnap_support) or [Discord Server](https://discord.gg/growsnap)
*   **Dedicated Support Email**: `creative.support@m.growsnapai.com`

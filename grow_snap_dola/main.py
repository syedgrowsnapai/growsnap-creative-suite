import sys
import os
from pathlib import Path

# Add the current folder to sys.path to resolve dola_automation module correctly
sys.path.insert(0, str(Path(__file__).parent.resolve()))

from PyQt6.QtWidgets import QApplication, QMessageBox
from PyQt6.QtGui import QIcon
from dola_automation.main_window import MainWindow
from dola_automation.logger import logger, setup_logger
from dola_automation.updater import check_for_updates, DEFAULT_UPDATE_URL

APP_VERSION = "1.0.0"

def get_resource_path(relative_path: str) -> Path:
    """ Get absolute path to resource, works for dev and for PyInstaller """
    base_path = Path(__file__).parent.resolve() / 'dola_automation'
    if hasattr(sys, '_MEIPASS'):
        base_path = Path(sys._MEIPASS) / 'dola_automation'
    return base_path / relative_path

def register_desktop_entry():
    """
    Registers a custom .desktop launcher entry on Linux/Wayland
    to display the app icon properly in the taskbar/dock and launcher menu.
    """
    import platform
    if platform.system() != "Linux":
        return
        
    try:
        home = Path.home()
        desktop_dir = home / ".local" / "share" / "applications"
        desktop_dir.mkdir(parents=True, exist_ok=True)
        desktop_file = desktop_dir / "growsnapai.desktop"
        
        # Cleanup old desktop entry if present
        old_file = desktop_dir / "growsnap-ai.desktop"
        if old_file.exists():
            try:
                old_file.unlink()
            except Exception:
                pass
        
        proj_dir = Path(__file__).parent.parent.resolve()
        run_script = proj_dir / "run_grow_snap.sh"
        icon_png = get_resource_path("resources/icon.png")
        
        desktop_content = f"""[Desktop Entry]
Type=Application
Name=GrowSnap Creative Suite
Comment=AI Video Automation & Production Suite
Exec={run_script}
Icon={icon_png}
Terminal=false
Categories=Utility;AudioVideo;Video;
StartupWMClass=growsnapai
"""
        desktop_file.write_text(desktop_content, encoding='utf-8')
        desktop_file.chmod(0o755)
        logger.info(f"Registered Linux desktop entry at {desktop_file}")
    except Exception as e:
        logger.warning(f"Failed to register Linux desktop entry: {e}")

def main():
    setup_logger()
    logger.info("Launching GrowSnap AI...")
    # Force Windows to display the custom window icon in the taskbar when running compiled/source
    try:
        import ctypes
        myappid = 'growsnap.ai.v1.0'
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(myappid)
    except Exception:
        pass
        
    # Ensure X11/Wayland class name matches growsnapai for taskbar icon resolution
    if "-class" not in sys.argv:
        sys.argv.extend(["-class", "growsnapai", "-name", "growsnapai"])
        
    app = QApplication(sys.argv)
    app.setApplicationName("growsnapai")
    app.setApplicationDisplayName("GrowSnap Creative Suite")
    
    # Enable matching on Linux/Wayland
    try:
        app.setDesktopFileName("growsnapai.desktop")
    except AttributeError:
        pass
        
    # Auto register desktop launcher on Linux
    register_desktop_entry()
    
    # Set the global window icon for the entire application
    icon_path = get_resource_path("resources/icon.png")
    if icon_path.exists():
        app.setWindowIcon(QIcon(str(icon_path)))
    
    # 1. Run activation key check before starting anything
    from dola_automation.licensing import check_license_interactive
    if not check_license_interactive():
        logger.info("Activation validation failed or was closed. Exiting.")
        sys.exit(0)
        
    # 2. Check for mandatory updates on startup
    logger.info("Checking for startup updates...")
    has_update, update_data, error_msg = check_for_updates(APP_VERSION)
    if has_update:
        is_mandatory = update_data.get("mandatory", False)
        
        # Lazy import to prevent circular references
        from dola_automation.info_dialogs import UpdateDialog
        
        dialog = UpdateDialog(update_data, is_mandatory=is_mandatory)
        result = dialog.exec()
        
        # If mandatory update is rejected or closed without completion, terminate app
        if is_mandatory and result != UpdateDialog.DialogCode.Accepted:
            logger.info("Mandatory update rejected or failed. Exiting.")
            sys.exit(0)
    # 2.5 Run first-time background dependency checker/installer
    logger.info("Verifying system dependencies...")
    from dola_automation.info_dialogs import check_and_install_dependencies
    if not check_and_install_dependencies():
        logger.error("Dependency initialization failed. Exiting.")
        sys.exit(0)
            
    # 3. Launch Main Window
    logger.info("License and updates checked. Displaying GrowSnap AI.")
    window = MainWindow()
    window.show()
    sys.exit(app.exec())

if __name__ == "__main__":
    main()

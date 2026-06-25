import logging
import os
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

logger = logging.getLogger('DolaAutomation')

def setup_logger() -> None:
    log_dir = Path('logs')
    log_dir.mkdir(exist_ok=True)
    
    log_file = log_dir / 'automation.log'
    logger.setLevel(logging.DEBUG)
    
    # Avoid duplicate handlers if setup multiple times
    if logger.handlers:
        return
        
    formatter = logging.Formatter('%(asctime)s | %(levelname)-8s | [%(filename)s:%(lineno)d] | %(message)s')
    
    # Midnight rotating handler
    try:
        file_handler = TimedRotatingFileHandler(
            filename=str(log_file),
            when='midnight',
            interval=1,
            backupCount=30,
            encoding='utf-8'
        )
        file_handler.setFormatter(formatter)
        file_handler.setLevel(logging.DEBUG)
        logger.addHandler(file_handler)
    except Exception as e:
        print(f"Failed to create file logger: {e}")
        
    # Console logger
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    console_handler.setLevel(logging.INFO)
    logger.addHandler(console_handler)

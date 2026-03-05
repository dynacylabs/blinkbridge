"""Configuration management for BlinkBridge.

Handles loading and parsing of configuration files, and provides global
configuration variables used throughout the application.
"""
import json
import os
from pathlib import Path
from datetime import timedelta
from typing import Union


__all__ = [
    'COMMON_FFMPEG_ARGS',
    'CONFIG',
    'DELAY_RESTART',
    'RTSP_URL',
    'PATH_VIDEOS',
    'PATH_CONCAT',
    'PATH_CONFIG'
]

# Common FFmpeg arguments used across all FFmpeg operations
COMMON_FFMPEG_ARGS = ['-hide_banner', '-loglevel', 'error', '-y']

# Global configuration dictionary loaded from JSON file
CONFIG = None
# Delay between stream restart attempts (timedelta)
DELAY_RESTART = None
# RTSP server URL string
RTSP_URL = None
# Path to videos directory
PATH_VIDEOS = None
# Path to FFmpeg concat files directory
PATH_CONCAT = None
# Path to configuration files directory
PATH_CONFIG = None

def load_config_file(file_name: Union[str, Path]) -> None:
    """Load configuration from a JSON file and initialize global config variables.
    
    Args:
        file_name: Path to the configuration JSON file
        
    Raises:
        FileNotFoundError: If the config file doesn't exist
        json.JSONDecodeError: If the config file contains invalid JSON
    """
    global CONFIG, DELAY_RESTART, RTSP_URL, PATH_VIDEOS, PATH_CONCAT, PATH_CONFIG

    with open(file_name) as f:
        CONFIG = json.load(f)

    DELAY_RESTART = timedelta(seconds=CONFIG['cameras']['restart_delay_seconds'])
    RTSP_URL = f"rtsp://{CONFIG['rtsp_server']['address']}:{CONFIG['rtsp_server']['port']}"

    PATH_VIDEOS = Path(CONFIG['paths']['videos'])
    PATH_CONCAT = Path(CONFIG['paths']['concat'])
    PATH_CONFIG = Path(CONFIG['paths']['config'])

# Load configuration from environment variable or default location
config_file = os.getenv('BLINKBRIDGE_CONFIG', 'config.json')
load_config_file(config_file)
 

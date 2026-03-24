"""Configuration management for BlinkBridge.

Handles loading and parsing of configuration files, and provides global
configuration variables used throughout the application.
"""
import json
import logging
import os
import sys
from pathlib import Path
from datetime import timedelta
from typing import Union


log = logging.getLogger(__name__)


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
        KeyError: If required configuration keys are missing
        ValueError: If configuration values are invalid
    """
    global CONFIG, DELAY_RESTART, RTSP_URL, PATH_VIDEOS, PATH_CONCAT, PATH_CONFIG

    file_name = Path(file_name)
    
    # Check if config file exists
    try:
        if not file_name.exists():
            print(f"ERROR: Configuration file not found: {file_name}", file=sys.stderr)
            print(f"Please create a configuration file at {file_name.resolve()}", file=sys.stderr)
            raise FileNotFoundError(f"Configuration file not found: {file_name}")
    except OSError as e:
        print(f"ERROR: Cannot access configuration file: {e}", file=sys.stderr)
        raise
    
    # Read and parse JSON
    try:
        with open(file_name, 'r') as f:
            CONFIG = json.load(f)
    except json.JSONDecodeError as e:
        print(f"ERROR: Invalid JSON in configuration file: {e}", file=sys.stderr)
        print(f"Please check the syntax in {file_name}", file=sys.stderr)
        raise
    except IOError as e:
        print(f"ERROR: Cannot read configuration file: {e}", file=sys.stderr)
        raise
    except Exception as e:
        print(f"ERROR: Unexpected error loading configuration: {e}", file=sys.stderr)
        raise
    
    # Validate and extract required configuration
    try:
        # Validate camera configuration
        if 'cameras' not in CONFIG:
            raise KeyError("Missing 'cameras' section in configuration")
        if 'restart_delay_seconds' not in CONFIG['cameras']:
            raise KeyError("Missing 'cameras.restart_delay_seconds' in configuration")
        
        DELAY_RESTART = timedelta(seconds=CONFIG['cameras']['restart_delay_seconds'])
        
        # Validate RTSP server configuration
        if 'rtsp_server' not in CONFIG:
            raise KeyError("Missing 'rtsp_server' section in configuration")
        if 'address' not in CONFIG['rtsp_server'] or 'port' not in CONFIG['rtsp_server']:
            raise KeyError("Missing RTSP server address or port in configuration")
        
        RTSP_URL = f"rtsp://{CONFIG['rtsp_server']['address']}:{CONFIG['rtsp_server']['port']}"

        # Validate paths configuration
        if 'paths' not in CONFIG:
            raise KeyError("Missing 'paths' section in configuration")
        if 'videos' not in CONFIG['paths']:
            raise KeyError("Missing 'paths.videos' in configuration")
        if 'concat' not in CONFIG['paths']:
            raise KeyError("Missing 'paths.concat' in configuration")
        if 'config' not in CONFIG['paths']:
            raise KeyError("Missing 'paths.config' in configuration")
        
        PATH_VIDEOS = Path(CONFIG['paths']['videos'])
        PATH_CONCAT = Path(CONFIG['paths']['concat'])
        PATH_CONFIG = Path(CONFIG['paths']['config'])
        
        # Create directories if they don't exist
        try:
            PATH_VIDEOS.mkdir(parents=True, exist_ok=True)
            PATH_CONCAT.mkdir(parents=True, exist_ok=True)
            PATH_CONFIG.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            print(f"ERROR: Failed to create required directories: {e}", file=sys.stderr)
            raise
        
        # Validate other required settings
        if 'blink' not in CONFIG:
            raise KeyError("Missing 'blink' section in configuration")
        if 'login' not in CONFIG['blink']:
            raise KeyError("Missing 'blink.login' section in configuration")
        
        # Set defaults for optional settings
        CONFIG.setdefault('log_level', 'INFO')
        CONFIG.setdefault('still_video_duration', 0.5)
        CONFIG['cameras'].setdefault('enabled', [])
        CONFIG['cameras'].setdefault('disabled', [])
        CONFIG['cameras'].setdefault('max_failures', 3)
        CONFIG['cameras'].setdefault('scan_interval', 5)
        CONFIG['cameras'].setdefault('removal_scans', 3)
        CONFIG['blink'].setdefault('history_days', 90)
        CONFIG['blink'].setdefault('poll_interval', 1)
        CONFIG['blink'].setdefault('metadata_pages', 10)
        CONFIG.setdefault('ffmpeg', {})
        CONFIG['ffmpeg'].setdefault('encoder', 'auto')
        CONFIG['ffmpeg'].setdefault('vaapi_device', '/dev/dri/renderD128')
        
    except KeyError as e:
        print(f"ERROR: Configuration validation failed: {e}", file=sys.stderr)
        print(f"Please check your configuration file at {file_name}", file=sys.stderr)
        raise
    except (TypeError, ValueError) as e:
        print(f"ERROR: Invalid configuration value: {e}", file=sys.stderr)
        raise
    except Exception as e:
        print(f"ERROR: Unexpected error processing configuration: {e}", file=sys.stderr)
        raise

# Load configuration from environment variable or default location
try:
    config_file = os.getenv('BLINKBRIDGE_CONFIG', 'config.json')
    load_config_file(config_file)
except Exception as e:
    print(f"\nFATAL ERROR: Failed to load configuration: {e}\n", file=sys.stderr)
    sys.exit(1)

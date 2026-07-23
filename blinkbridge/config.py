from pathlib import Path
import logging
import json
from datetime import datetime, timedelta
from typing import Union
import os


__all__ = ['COMMON_FFMPEG_ARGS', 'CONFIG', 'DELAY_RESTART', 'RTSP_URL', 'PATH_VIDEOS', 'PATH_CONCAT', 'PATH_CONFIG']

COMMON_FFMPEG_ARGS = [
    '-hide_banner',
    '-loglevel', 'error',
    '-y',
]

CONFIG = None
DELAY_RESTART = None
RTSP_URL = None
PATH_VIDEOS = None
PATH_CONCAT = None
PATH_CONFIG = None

def load_config_file(file_name: Union[str, Path]) -> None:
    global CONFIG, DELAY_RESTART, RTSP_URL, PATH_VIDEOS, PATH_CONCAT, PATH_CONFIG

    with open(file_name) as f:
        CONFIG = json.load(f)

    DELAY_RESTART = timedelta(seconds=CONFIG['cameras']['restart_delay_seconds'])
    RTSP_URL = f'rtsp://{CONFIG['rtsp_server']['address']}:{CONFIG['rtsp_server']['port']}'

    PATH_VIDEOS = Path(CONFIG['paths']['videos'])
    PATH_CONCAT = Path(CONFIG['paths']['concat'])
    PATH_CONFIG = Path(CONFIG['paths']['config'])

    # Optional export of Frigate cameras block (snippet only)
    CONFIG.setdefault('frigate_export', {})
    CONFIG['frigate_export'].setdefault('enabled', False)
    CONFIG['frigate_export'].setdefault('output_path', str(PATH_CONFIG / 'frigate_cameras.yml'))
    CONFIG['frigate_export'].setdefault('rtsp_host', CONFIG['rtsp_server']['address'])
    CONFIG['frigate_export'].setdefault('rtsp_port', CONFIG['rtsp_server']['port'])
    CONFIG['frigate_export'].setdefault('roles', ['detect', 'record'])
    CONFIG['frigate_export'].setdefault('detect_defaults', {})
    CONFIG['frigate_export']['detect_defaults'].setdefault('width', 1280)
    CONFIG['frigate_export']['detect_defaults'].setdefault('height', 720)
    CONFIG['frigate_export']['detect_defaults'].setdefault('fps', 1)

config_file = os.getenv('BLINKBRIDGE_CONFIG', 'config.json')
load_config_file(config_file)
 

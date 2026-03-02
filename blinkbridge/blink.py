import asyncio
from collections import defaultdict
from datetime import datetime, timedelta 
import logging
from typing import Dict, Tuple, Union
from pathlib import Path
from aiohttp import ClientSession
from blinkpy.blinkpy import Blink
from blinkpy.auth import Auth
from blinkpy.helpers.util import json_load
from blinkbridge.config import *


log = logging.getLogger(__name__)


def find_most_recent_clip_url(recent_clips: dict, date: str) -> str:
    # sort data in reverse order by time
    sorted_data = sorted(recent_clips, key=lambda x: x['time'], reverse=True)

    # get the first entry that does not contain "/snapshot/"
    for entry in sorted_data:
        if '/snapshot/' not in entry['clip']:
            break
    else:
        return ''
    
    # convert to datetime
    date = datetime.fromisoformat(date.replace('Z', '+00:00'))
    entry_time = datetime.fromisoformat(entry['time'].replace('Z', '+00:00'))

    # see if entry is newer than date
    if entry_time > date:
        return entry['clip']
    
    return '' 

class CameraManager:
    def __init__(self):
        self.session = ClientSession()
        self.camera_last_record = defaultdict(lambda: None)
        self.metadata = None

    async def _login(self) -> None:
        self.blink = Blink(session=self.session)
        path_cred = PATH_CONFIG / ".cred.json"

        if not path_cred.exists():
            log.debug(f"logging into Blink with login info")
            self.blink.auth = Auth(CONFIG['blink']['login'], no_prompt=False)
        else:
            log.debug(f"logging into Blink with saved creds")
            self.blink.auth = Auth(await json_load(path_cred))

        await self.blink.start()

        if not path_cred.exists():
            log.debug(f"saving Blink creds")
            await self.blink.save(path_cred)

    async def refresh_metadata(self) -> None:
        log.debug('refreshing video metadata')
        dt_past = datetime.now() - timedelta(days=CONFIG['blink']['history_days'])
        self.metadata = await self.blink.get_videos_metadata(since=str(dt_past), stop=2)

    async def save_latest_clip(self, camera_name: str, force: bool=False) -> Union[Path, None]:
        '''
        Download and save latest videos for camera
        ''' 
        camera_name_sanitized = camera_name.lower().replace(' ', '_')
        file_name = PATH_VIDEOS / f"{camera_name_sanitized}_latest.mp4"
    
        # don't download if clip already exists
        if file_name.exists() and not force:
            log.debug(f"{camera_name}: skipping download, {file_name} exists")
            return file_name

        # skip deleted clips and camera snapshots
        media = next((m for m in self.metadata if m['device_name'] == camera_name 
                    if not m['deleted'] and m['source'] != 'snapshot'), None)

        if media is None:
            log.warning(f"{camera_name}: no clips found for camera")
            return None

        log.debug(f'{camera_name}: downloading video: {media}')
        response = await self.blink.do_http_get(media['media'])

        log.debug(f'{camera_name}: saving video to {file_name}')
        with open(file_name, 'wb') as f:
            f.write(await response.read())

        return file_name
    
    async def _save_clip(self, camera_name: str, url: str, file_name: Path) -> None:
        camera = self.blink.cameras[camera_name]
        response = await camera.get_video_clip(url)

        log.debug(f'{camera_name}: saving video to {file_name}')
        with open(file_name, 'wb') as f:
            f.write(await response.read())
    
    async def check_for_motion(self, camera_name: str) -> Union[Path, None]:
        '''
        Check if a camera has been motion detected
        '''
        await self.blink.refresh()
        camera = self.blink.cameras[camera_name]

        if not camera.attributes['motion_detected'] or self.camera_last_record[camera_name] == camera.attributes['last_record']:
            return None

        log.debug(f"{camera_name}: motion detected: {camera.attributes}")

        camera_name_sanitized = camera_name.lower().replace(' ', '_')
        file_name = PATH_VIDEOS / f"{camera_name_sanitized}_latest.mp4"

        # HACK: detect snapshot events and see if there is a recent clip in them
        if '/snapshot/' in camera.attributes['video']:
            if url := find_most_recent_clip_url(camera.attributes['recent_clips'], camera.attributes['last_record']):
                log.debug(f"{camera_name}: found recent clip in snapshot, saving to {file_name}")
                await self._save_clip(camera_name, url, file_name)
                self.camera_last_record[camera_name] = camera.attributes['last_record']
            
                return file_name

            log.debug(f"{camera_name}: no recent clip in snapshot, skipping")
            self.camera_last_record[camera_name] = camera.attributes['last_record']

            return None
        
        log.debug(f"{camera_name}: saving video to {file_name}")
        await camera.video_to_file(file_name)
        self.camera_last_record[camera_name] = camera.attributes['last_record']

        return file_name
        
    def get_cameras(self) -> iter:
        return self.blink.cameras.keys()
    
    async def start(self) -> None:
        await self._login()
        await self.refresh_metadata()
    
    async def close(self) -> None:
        """Properly close all connections and clean up resources."""
        # Close the session only once (blink.auth.session is the same as self.session)
        if hasattr(self, 'session') and self.session is not None:
            if not self.session.closed:
                await self.session.close()
                # Give the event loop time to clean up SSL transports
                # This prevents "Unclosed connector" warnings
                await asyncio.sleep(0.25)

async def test() -> None:
    cm = CameraManager()

    await cm.start()

    for camera in cm.get_cameras():
        file_name = await cm.check_for_motion(camera)

        print(file_name)

    await cm.close()

if __name__ == "__main__":
    asyncio.run(test())

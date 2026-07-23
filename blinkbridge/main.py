import asyncio
import signal
import logging
import os
from datetime import datetime, timedelta
from collections import defaultdict
from enum import Enum
from typing import Dict, Optional
from pathlib import Path
from rich.logging import RichHandler
from rich.highlighter import NullHighlighter, JSONHighlighter
from blinkbridge.stream_server import StreamServer
from blinkbridge.blink import CameraManager
from blinkbridge.config import *


log = logging.getLogger(__name__)

# Minimum poll interval enforced by BlinkPy API (seconds)
MIN_BLINK_THROTTLE = 2
# Grace period for FFmpeg processes to shutdown cleanly (seconds)
SHUTDOWN_GRACE_PERIOD = 0.2


class CameraState(Enum):
    """Operational state of a single camera stream."""
    STARTING = "starting"  # Grey screen — stream up, waiting for first clip
    LIVE     = "live"      # Streaming real clip(s)
    OFFLINE  = "offline"   # Camera / sync module unreachable
    ERROR    = "error"     # Unknown / inconsistent state

class Application:
    def __init__(self):
        self.stream_servers = {}
        self.cam_manager = None
        self.running = False
        # Per-camera operational state and starting-poll counter
        self.camera_states: Dict[str, CameraState] = {}
        self.camera_starting_polls: Dict[str, int] = defaultdict(int)

    async def start_stream(self, camera_name: str, redownload: bool=False) -> StreamServer:
        """Start a stream server for a camera using the Starting placeholder.

        The stream always begins with the Starting screen. The monitoring loop
        drives the transition to LIVE, OFFLINE, or ERROR after Blink state is
        refreshed.
        """
        starting_video = getattr(self.cam_manager, 'starting_placeholder_path', None)
        if starting_video is None:
            # Fallback: no placeholder available, try to get a real clip first
            if redownload:
                await self.cam_manager.refresh_metadata()
            log.debug(f"{camera_name}: getting latest clip (no placeholder)")
            starting_video = await self.cam_manager.save_latest_clip(camera_name, force=redownload)

        log.info(f"{camera_name}: starting stream server")
        stream_server = StreamServer(camera_name)
        stream_server.start_server(starting_video)
        self.stream_servers[camera_name] = stream_server
        self.camera_states[camera_name] = CameraState.STARTING
        self.camera_starting_polls[camera_name] = 0
        return stream_server

    async def check_for_motion(self, camera_name: str) -> bool:
        ss = self.stream_servers[camera_name]

        if not ss.is_running():
            return False 
        
        file_name_new_clip = await self.cam_manager.check_for_motion(camera_name)

        if not file_name_new_clip:
            return False

        log.info(f"{ss.stream_name}: motion detected, adding video")
        ss.add_video(file_name_new_clip)

        return True
        
    async def start(self) -> None:
        self.running = True
        self.cam_manager = CameraManager()
        await self.cam_manager.start()

        # get enabled cameras
        enabled_cameras = set(CONFIG['cameras']['enabled']) if CONFIG['cameras']['enabled'] else set(self.cam_manager.get_cameras())
        enabled_cameras = enabled_cameras - set(CONFIG['cameras']['disabled'])
        log.info(f"enabled cameras: {enabled_cameras}")      

        # create stream servers for each camera
        for camera in self.cam_manager.get_cameras():
            if camera not in enabled_cameras:
                continue
            
            ss = await self.start_stream(camera)
            ss.failure_count = 0
            ss.datetime_started = datetime.now()

        log.info(f"monitoring cameras for motion")
        while self.running:
            for camera_name in list(self.stream_servers.keys()):
                try:
                    await self._update_camera_state(camera_name)
                except Exception as e:
                    log.error(f"{camera_name}: error checking for motion: {e}")
                    ss = self.stream_servers.get(camera_name)
                    if ss:
                        ss.close()

            # check if any stream servers are stopped and restart them
            for camera_name in list(self.stream_servers.keys()):
                ss = self.stream_servers[camera_name]

                if ss.is_running():
                    ss.failure_detected = False
                    continue

                if not getattr(ss, 'failure_detected', False):
                    ss.failure_detected = True
                    log.warning(f"{camera_name}: stream stopped (failure count: {ss.failure_count + 1})")

                if ss.failure_count >= CONFIG['cameras']['max_failures'] - 1:
                    log.warning(f"{camera_name}: too many failures, disabling")
                    self.stream_servers.pop(camera_name)
                    continue

                if datetime.now() < ss.datetime_started + DELAY_RESTART:
                    continue

                ss_new = await self.start_stream(camera_name, redownload=True)
                ss_new.failure_count = ss.failure_count + 1
                ss_new.datetime_started = datetime.now()

            await asyncio.sleep(CONFIG['blink']['poll_interval'])

    async def _update_camera_state(self, camera_name: str) -> None:
        """Drive the state machine for a single camera.

        Refreshes Blink state, checks online/offline status, and transitions
        the camera through STARTING → LIVE / OFFLINE / ERROR as appropriate,
        swapping the stream content to the matching placeholder video.
        """
        ss = self.stream_servers.get(camera_name)
        if not ss or not ss.is_running():
            return

        current_state = self.camera_states.get(camera_name, CameraState.STARTING)

        new_clip: Optional[Path] = None
        try:
            new_clip = await self.cam_manager.check_for_motion(camera_name)
        except Exception as e:
            log.error(f"{camera_name}: error refreshing Blink data: {e}", exc_info=True)
            return

        is_offline = False
        if hasattr(self.cam_manager, 'is_camera_offline'):
            is_offline = self.cam_manager.is_camera_offline(camera_name)

        offline_video  = getattr(self.cam_manager, 'offline_placeholder_path', None)
        starting_video = getattr(self.cam_manager, 'starting_placeholder_path', None)
        error_video    = getattr(self.cam_manager, 'error_placeholder_path', None)

        if is_offline:
            if current_state != CameraState.OFFLINE:
                log.info(f"{camera_name}: camera offline — showing OFFLINE screen (was {current_state.value})")
                if offline_video and hasattr(ss, 'swap_to_placeholder'):
                    ss.swap_to_placeholder(offline_video)
                self.camera_states[camera_name] = CameraState.OFFLINE
                self.camera_starting_polls[camera_name] = 0
            return

        if new_clip:
            if current_state != CameraState.LIVE:
                log.info(f"{camera_name}: clip received — going LIVE (was {current_state.value})")
            ss.add_video(new_clip)
            self.camera_states[camera_name] = CameraState.LIVE
            self.camera_starting_polls[camera_name] = 0
            return

        if current_state == CameraState.LIVE:
            return

        if current_state == CameraState.OFFLINE:
            log.info(f"{camera_name}: back online — returning to STARTING")
            if starting_video and hasattr(ss, 'swap_to_placeholder'):
                ss.swap_to_placeholder(starting_video)
            self.camera_states[camera_name] = CameraState.STARTING
            self.camera_starting_polls[camera_name] = 0
            return

        if current_state == CameraState.STARTING:
            try:
                clip = await self.cam_manager.save_latest_clip(camera_name)
                if clip is not None:
                    log.info(f"{camera_name}: found historical clip — going LIVE")
                    ss.add_video(clip)
                    self.camera_states[camera_name] = CameraState.LIVE
                    self.camera_starting_polls[camera_name] = 0
                    return
            except Exception as e:
                log.warning(f"{camera_name}: error checking for historical clip: {e}")

            count = self.camera_starting_polls[camera_name] + 1
            self.camera_starting_polls[camera_name] = count
            max_polls = CONFIG['cameras']['max_failures']
            if count >= max_polls:
                log.warning(f"{camera_name}: online for {count} polls with no clip — going to ERROR")
                if error_video and hasattr(ss, 'swap_to_placeholder'):
                    ss.swap_to_placeholder(error_video)
                self.camera_states[camera_name] = CameraState.ERROR
            return

        if current_state == CameraState.ERROR:
            try:
                clip = await self.cam_manager.save_latest_clip(camera_name)
                if clip is not None:
                    log.info(f"{camera_name}: recovered from ERROR — going LIVE")
                    ss.add_video(clip)
                    self.camera_states[camera_name] = CameraState.LIVE
                    self.camera_starting_polls[camera_name] = 0
            except Exception as e:
                log.warning(f"{camera_name}: error during ERROR recovery check: {e}")

    async def close(self) -> None:
        self.running = False

        if self.cam_manager:
            await self.cam_manager.close()
        
        for ss in self.stream_servers.values():
            ss.close()

async def main() -> None:
    app = Application()
    
    # Create a cancellation event to coordinate shutdown
    shutdown_event = asyncio.Event()

    def handle_exit():
        # Signal the shutdown event when Ctrl+C is received
        shutdown_event.set()

    # Add signal handlers using loop.add_signal_handler
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, handle_exit)

    try:
        # Start the application
        start_task = asyncio.create_task(app.start())
        
        # Wait for shutdown signal
        await shutdown_event.wait()

        log.info("Shutting down...")
        
        # Cancel the start task and wait for it to complete
        start_task.cancel()
        try:
            await start_task
        except asyncio.CancelledError:
            pass

    except Exception as e:
        log.error(f"Unexpected error: {e}")
    
    finally:
        # Ensure app is closed gracefully
        await app.close()

if __name__ == "__main__":
    logging.basicConfig(
        format="%(message)s", datefmt="[%X]", handlers=[RichHandler(highlighter=NullHighlighter())]
    )
    logging.getLogger('blinkbridge').setLevel(CONFIG['log_level'])
    logging.getLogger(__name__).setLevel(CONFIG['log_level'])
    
    asyncio.run(main())


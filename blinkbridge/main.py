import asyncio
import signal
import logging
import os
from datetime import datetime, timedelta
from collections import defaultdict
from rich.logging import RichHandler
from rich.highlighter import NullHighlighter, JSONHighlighter
from blinkbridge.stream_server import StreamServer
from blinkbridge.blink import CameraManager
from blinkbridge.config import *


log = logging.getLogger(__name__)

class Application:
    def __init__(self):
        self.stream_servers = {}
        self.cam_manager = None
        self.running = False

    async def start_stream(self, camera_name: str, redownload: bool=False) -> StreamServer:
        if redownload:
            await self.cam_manager.refresh_metadata()

        log.debug(f"{camera_name}: getting latest clip")
        file_name_initial_video = await self.cam_manager.save_latest_clip(camera_name, force=redownload)

        # All cameras get a stream now (either real clip or black video placeholder)
        if file_name_initial_video is None:
            log.error(f"{camera_name}: cannot start stream (no video available)")
            return None

        if camera_name in self.cam_manager.cameras_without_clips:
            log.info(f"{camera_name}: starting stream with black placeholder (waiting for first clip)")
        else:
            log.info(f"{camera_name}: starting stream server")
        
        stream_server = StreamServer(camera_name)
        stream_server.start_server(file_name_initial_video)  
        self.stream_servers[camera_name] = stream_server

        return stream_server

    async def check_for_motion(self, camera_name: str) -> bool:
        ss = self.stream_servers[camera_name]

        if not ss.is_running():
            return False 
        
        file_name_new_clip = await self.cam_manager.check_for_motion(camera_name)

        if not file_name_new_clip:
            return False

        log.info(f"{ss.stream_name}: adding new clip to stream")
        ss.add_video(file_name_new_clip)

        return True
    
    async def check_for_first_clip(self, camera_name: str) -> bool:
        """Check if a camera without clips now has its first clip available."""
        if camera_name not in self.cam_manager.cameras_without_clips:
            return False  # Camera already has clips
        
        ss = self.stream_servers.get(camera_name)
        if not ss or not ss.is_running():
            return False
        
        await self.cam_manager.refresh_metadata()
        
        # Try to get the latest clip (without black fallback this time to check if real clip exists)
        file_name = await self.cam_manager.save_latest_clip(camera_name, force=True, use_black_fallback=False)
        
        if file_name:
            log.info(f"{camera_name}: first clip now available, upgrading from black placeholder")
            ss.add_video(file_name)
            return True
        
        return False
        
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
            if ss is None:
                log.warning(f"{camera}: failed to start stream")
                continue
            
            ss.failure_count = 0
            ss.datetime_started = datetime.now()

        log.info(f"monitoring cameras for motion (poll interval: {CONFIG['blink']['poll_interval']}s)")
        
        # Warn if poll interval is too fast
        MIN_BLINK_THROTTLE = 2  # blinkpy MIN_THROTTLE_TIME
        if CONFIG['blink']['poll_interval'] < MIN_BLINK_THROTTLE:
            log.warning(
                f"poll_interval ({CONFIG['blink']['poll_interval']}s) is less than "
                f"BlinkPy's minimum throttle time ({MIN_BLINK_THROTTLE}s). "
                f"Effective poll rate will be ~{MIN_BLINK_THROTTLE}s due to API throttling. "
                f"Consider increasing poll_interval to {MIN_BLINK_THROTTLE} or higher."
            )
        
        poll_count = 0
        last_log_time = datetime.now()
        log_interval = timedelta(seconds=30)  # Log summary every 30 seconds
        
        while self.running:
            poll_count += 1
            log.debug(f"Poll #{poll_count}: checking {len(self.stream_servers)} cameras...")
            
            # Periodic summary at INFO level
            if datetime.now() - last_log_time >= log_interval:
                active_cams = len(self.stream_servers) - len(self.cam_manager.cameras_without_clips)
                waiting_cams = len(self.cam_manager.cameras_without_clips)
                log.info(
                    f"Poll #{poll_count}: {active_cams} cameras active, "
                    f"{waiting_cams} waiting for first clip"
                )
                last_log_time = datetime.now()
            
            # check for motion on cameras that have clips
            for camera_name in self.stream_servers:
                try:
                    # For cameras without clips, check if they now have their first clip
                    if camera_name in self.cam_manager.cameras_without_clips:
                        log.debug(f"{camera_name}: checking for first clip...")
                        await self.check_for_first_clip(camera_name)
                    else:
                        # For cameras with clips, check for new motion
                        await self.check_for_motion(camera_name)
                except Exception as e:
                    log.error(f"{camera_name}: error checking for motion: {e}")
                    self.stream_servers[camera_name].close()

            # check if any stream servers are stopped and restart them
            for camera_name in list(self.stream_servers.keys()):
                ss = self.stream_servers[camera_name]

                if not ss.is_running():
                    # remove stream if too many failures
                    if ss.failure_count >= CONFIG['cameras']['max_failures'] - 1:
                        log.warning(f"{camera_name}: too many failures, disabling")
                        self.stream_servers.pop(camera_name)
                        continue

                    log.warning(f"{camera_name}: server failed {ss.failure_count + 1} time(s)")

                    # do nothing if stream was last started less certain time ago
                    if datetime.now() < ss.datetime_started + DELAY_RESTART:
                        continue

                    # create new stream server
                    ss_new = await self.start_stream(camera_name, redownload=True)
                    if ss_new is None:
                        # Still no clips available, keep the old server and try again later
                        log.debug(f"{camera_name}: still no clips available, will retry later")
                        ss.datetime_started = datetime.now()
                        continue
                    
                    ss_new.failure_count = ss.failure_count + 1
                    ss_new.datetime_started = datetime.now()

            await asyncio.sleep(CONFIG['blink']['poll_interval'])

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


"""Main application for BlinkBridge RTSP streaming service.

Manages the lifecycle of camera streams, monitors for motion detection,
and handles stream failures and restarts. Provides graceful shutdown handling.
"""
import asyncio
import logging
import signal
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Dict, Optional

from rich.highlighter import JSONHighlighter, NullHighlighter
from rich.logging import RichHandler

from blinkbridge.blink import CameraManager
from blinkbridge.config import *
from blinkbridge.stream_server import StreamServer


log = logging.getLogger(__name__)

# Minimum poll interval enforced by BlinkPy API (seconds)
MIN_BLINK_THROTTLE = 2
# How often to log summary status at INFO level (seconds)
LOG_INTERVAL_SECONDS = 30
# Grace period for FFmpeg processes to shutdown cleanly (seconds)
SHUTDOWN_GRACE_PERIOD = 0.2


class Application:
    """Main application that manages camera streams and monitors for motion.
    
    Coordinates CameraManager and StreamServer instances for each camera,
    handles motion detection polling, stream failures, and restarts.
    
    Attributes:
        stream_servers: Dict mapping camera names to StreamServer instances
        cam_manager: CameraManager instance for Blink integration
        running: Boolean flag indicating if application should continue running
    """
    
    def __init__(self) -> None:
        self.stream_servers: Dict[str, StreamServer] = {}
        self.cam_manager: Optional[CameraManager] = None
        self.running: bool = False

    async def start_stream(self, camera_name: str, redownload: bool=False) -> Optional[StreamServer]:
        """Start a stream server for a camera.
        
        Args:
            camera_name: Name of the camera
            redownload: Whether to force redownload of the latest clip (default: False)
            
        Returns:
            StreamServer instance if successful, None if failed
            
        Note:
            If no clip is available, may return a stream with black placeholder
            video for cameras that have never had clips.
        """
        if not self.running:
            log.debug(f"{camera_name}: skipping stream start (shutdown in progress)")
            return None
            
        if redownload:
            await self.cam_manager.refresh_metadata()

        log.debug(f"{camera_name}: getting latest clip")
        file_name_initial_video = await self.cam_manager.save_latest_clip(camera_name, force=redownload)

        if file_name_initial_video is None:
            log.error(f"{camera_name}: cannot start stream (no video available)")
            return None

        if not self.running:
            log.debug(f"{camera_name}: skipping stream start (shutdown in progress)")
            return None

        is_placeholder = camera_name in self.cam_manager.cameras_without_clips
        status = "black placeholder (waiting for first clip)" if is_placeholder else "real clip"
        log.info(f"{camera_name}: starting stream with {status}")
        
        try:
            stream_server = StreamServer(camera_name)
            stream_server.start_server(file_name_initial_video)
            self.stream_servers[camera_name] = stream_server
            return stream_server
        except Exception as e:
            log.error(f"{camera_name}: failed to start stream server: {e}")
            return None

    async def check_for_motion(self, camera_name: str) -> bool:
        """Check for motion on a camera and add new clip to stream if detected.
        
        Args:
            camera_name: Name of the camera to check
            
        Returns:
            True if motion was detected and clip added, False otherwise
        """
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
        """Check if a camera without clips now has its first clip available.
        
        Args:
            camera_name: Name of the camera to check
            
        Returns:
            True if first clip is now available and was added, False otherwise
            
        Note:
            Only checks cameras that are in cameras_without_clips set.
            Once a clip is found, upgrades the stream from black placeholder.
        """
        if camera_name not in self.cam_manager.cameras_without_clips:
            return False
        
        ss = self.stream_servers.get(camera_name)
        if not ss or not ss.is_running():
            return False
        
        await self.cam_manager.refresh_metadata()
        file_name = await self.cam_manager.save_latest_clip(camera_name, force=True, use_black_fallback=False)
        
        if file_name:
            log.info(f"{camera_name}: first clip now available, upgrading from black placeholder")
            ss.add_video(file_name)
            return True
        
        return False
        
    async def start(self) -> None:
        """Start the application, initialize cameras, and begin monitoring.
        
        Raises:
            LoginError: If Blink authentication fails
            TokenRefreshFailed: If Blink token refresh fails
        """
        self.running = True
        self.cam_manager = CameraManager()
        await self.cam_manager.start()

        enabled_cameras = self._get_enabled_cameras()
        log.info(f"enabled cameras: {enabled_cameras}")

        await self._initialize_camera_streams(enabled_cameras)
        
        if self.running:
            await self._monitor_cameras()
    
    def _get_enabled_cameras(self) -> set:
        """Get the set of enabled cameras from config.
        
        Returns:
            Set of camera names that should be monitored
            
        Note:
            If CONFIG['cameras']['enabled'] is empty, enables all discovered cameras.
            Always excludes cameras in CONFIG['cameras']['disabled'].
        """
        if CONFIG['cameras']['enabled']:
            enabled_cameras = set(CONFIG['cameras']['enabled'])
        else:
            enabled_cameras = set(self.cam_manager.get_cameras())
        
        return enabled_cameras - set(CONFIG['cameras']['disabled'])
    
    async def _initialize_camera_streams(self, enabled_cameras: set) -> None:
        """Create stream servers for all enabled cameras.
        
        Args:
            enabled_cameras: Set of camera names to initialize
            
        Note:
            Initializes failure tracking attributes on each StreamServer:
            - failure_count: Number of times stream has failed
            - datetime_started: When the stream was last started
        """
        for camera in self.cam_manager.get_cameras():
            if not self.running:
                log.info("Shutdown requested during startup, stopping stream creation")
                break
                
            if camera not in enabled_cameras:
                continue
            
            ss = await self.start_stream(camera)
            if ss is None:
                log.warning(f"{camera}: failed to start stream")
                continue
            
            ss.failure_count = 0
            ss.datetime_started = datetime.now()
            await asyncio.sleep(0)
    
    async def _monitor_cameras(self) -> None:
        """Main monitoring loop for camera motion detection.
        
        Continuously polls cameras for motion and manages stream health.
        Logs periodic status summaries at configured intervals.
        
        Note:
            Warns if poll_interval is less than BlinkPy's API throttle limit.
        """
        log.info(f"monitoring cameras for motion (poll interval: {CONFIG['blink']['poll_interval']}s)")
        
        if CONFIG['blink']['poll_interval'] < MIN_BLINK_THROTTLE:
            log.warning(
                f"poll_interval ({CONFIG['blink']['poll_interval']}s) is less than "
                f"BlinkPy's minimum throttle time ({MIN_BLINK_THROTTLE}s). "
                f"Effective poll rate will be ~{MIN_BLINK_THROTTLE}s due to API throttling."
            )
        
        poll_count = 0
        last_log_time = datetime.now()
        log_interval = timedelta(seconds=LOG_INTERVAL_SECONDS)
        
        while self.running:
            poll_count += 1
            log.debug(f"Poll #{poll_count}: checking {len(self.stream_servers)} cameras...")
            
            if datetime.now() - last_log_time >= log_interval:
                self._log_camera_status(poll_count)
                last_log_time = datetime.now()
            
            await self._check_cameras_for_updates()
            await self._restart_failed_streams()
            await asyncio.sleep(CONFIG['blink']['poll_interval'])
    
    def _log_camera_status(self, poll_count: int) -> None:
        """Log periodic status summary of cameras.
        
        Args:
            poll_count: Current poll iteration number
        """
        active_cams = len(self.stream_servers) - len(self.cam_manager.cameras_without_clips)
        waiting_cams = len(self.cam_manager.cameras_without_clips)
        log.info(
            f"Poll #{poll_count}: {active_cams} cameras active, "
            f"{waiting_cams} waiting for first clip"
        )
    
    async def _check_cameras_for_updates(self) -> None:
        """Check all cameras for motion or first clip availability.
        
        For cameras without clips, checks if first clip is now available.
        For cameras with clips, checks for new motion events.
        Closes streams that encounter errors.
        """
        for camera_name in list(self.stream_servers.keys()):
            if not self.running:
                break
            try:
                if camera_name in self.cam_manager.cameras_without_clips:
                    log.debug(f"{camera_name}: checking for first clip...")
                    await self.check_for_first_clip(camera_name)
                else:
                    await self.check_for_motion(camera_name)
            except Exception as e:
                log.error(f"{camera_name}: error checking for motion: {e}")
                self.stream_servers[camera_name].close()
    
    async def _restart_failed_streams(self) -> None:
        """Restart any failed stream servers.
        
        Checks each stream server's health and attempts restart if needed.
        Disables cameras that exceed maximum failure count.
        Respects restart delay between attempts.
        """
        for camera_name in list(self.stream_servers.keys()):
            if not self.running:
                break
                
            ss = self.stream_servers[camera_name]
            if ss.is_running():
                continue
            
            if ss.failure_count >= CONFIG['cameras']['max_failures'] - 1:
                log.warning(f"{camera_name}: too many failures, disabling")
                self.stream_servers.pop(camera_name)
                continue

            log.warning(f"{camera_name}: server failed {ss.failure_count + 1} time(s)")

            if datetime.now() < ss.datetime_started + DELAY_RESTART:
                continue

            ss_new = await self.start_stream(camera_name, redownload=True)
            if ss_new is None:
                log.debug(f"{camera_name}: still no clips available, will retry later")
                ss.datetime_started = datetime.now()
                continue
            
            ss_new.failure_count = ss.failure_count + 1
            ss_new.datetime_started = datetime.now()

    async def close(self) -> None:
        """Close the application and stop all streams.
        
        Stops all  stream servers, waits for graceful FFmpeg shutdown,
        and closes the camera manager connection.
        """
        log.info("Closing application and stopping all streams...")
        log.info("Note: FFmpeg 'Broken pipe' errors during shutdown are normal")
        self.running = False

        for camera_name, ss in list(self.stream_servers.items()):
            try:
                log.debug(f"{camera_name}: stopping stream")
                ss.close()
            except Exception as e:
                log.warning(f"{camera_name}: error stopping stream: {e}")
        
        await asyncio.sleep(SHUTDOWN_GRACE_PERIOD)
        
        if self.cam_manager:
            try:
                await self.cam_manager.close()
            except Exception as e:
                log.warning(f"Error closing camera manager: {e}")
        
        log.info("Application closed")

async def main() -> None:
    """Main entry point for the application.
    
    Sets up signal handlers, starts the application, and handles graceful shutdown.
    """
    app = Application()
    shutdown_event = asyncio.Event()

    def handle_exit() -> None:
        """Signal handler for SIGINT and SIGTERM."""
        log.info("Shutdown signal received...")
        app.running = False
        shutdown_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, handle_exit)

    try:
        start_task = asyncio.create_task(app.start())
        await shutdown_event.wait()
        
        start_task.cancel()
        try:
            await start_task
        except asyncio.CancelledError:
            log.debug("Start task cancelled successfully")

    except Exception as e:
        log.error(f"Unexpected error: {e}")
    finally:
        await app.close()

if __name__ == "__main__":
    logging.basicConfig(
        format="%(message)s", datefmt="[%X]", handlers=[RichHandler(highlighter=NullHighlighter())]
    )
    logging.getLogger('blinkbridge').setLevel(CONFIG['log_level'])
    logging.getLogger(__name__).setLevel(CONFIG['log_level'])
    
    asyncio.run(main())


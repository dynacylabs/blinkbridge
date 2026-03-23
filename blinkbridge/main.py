"""Main application for BlinkBridge RTSP streaming service.

Manages the lifecycle of camera streams with four states:
- INITIALIZING: Stream just created, showing initializing overlay
- WAITING: Camera online, waiting for first fresh clip
- STREAMING: Camera online, showing real video clips
- OFFLINE: Camera offline, showing offline overlay

No stale clips are used at startup. All cameras start as INITIALIZING,
transition to WAITING (online) or OFFLINE, and only accept clips
recorded after the bridge started.
"""
import asyncio
import logging
import signal
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Dict, Optional

from rich.highlighter import NullHighlighter
from rich.logging import RichHandler

from blinkbridge.blink import CameraManager
from blinkbridge.config import *
from blinkbridge.hwaccel import init_encoder
from blinkbridge.stream_server import StreamServer


log = logging.getLogger(__name__)

# Minimum poll interval enforced by BlinkPy API (seconds)
MIN_BLINK_THROTTLE = 2
# How often to log summary status at INFO level (seconds)
LOG_INTERVAL_SECONDS = 30
# Grace period for FFmpeg processes to shutdown cleanly (seconds)
SHUTDOWN_GRACE_PERIOD = 0.2


class CameraState(Enum):
    """Lifecycle states for camera streams."""
    INITIALIZING = "initializing"
    WAITING = "waiting"
    STREAMING = "streaming"
    OFFLINE = "offline"


class Application:
    """Main application that manages camera streams and monitors for motion.
    
    Coordinates CameraManager and StreamServer instances for each camera,
    handles motion detection polling, stream failures, and restarts.
    Periodically scans for camera changes and handles online/offline transitions.
    
    Camera lifecycle:
        INITIALIZING → WAITING (if online) or OFFLINE (if offline)
        WAITING → STREAMING (when fresh clip arrives)
        STREAMING → OFFLINE (if camera goes offline)
        OFFLINE → WAITING (when camera comes back online)
    """
    
    def __init__(self) -> None:
        self.stream_servers: Dict[str, StreamServer] = {}
        self.cam_manager: Optional[CameraManager] = None
        self.running: bool = False
        self.disabled_cameras: set = set()
        self.camera_states: Dict[str, CameraState] = {}
        self.bridge_start_time: Optional[datetime] = None
        # Per-camera freshness threshold — clips must be newer than this
        self.freshness_threshold: Dict[str, datetime] = {}
        # Consecutive scans a camera has been absent from API
        self.removal_counter: Dict[str, int] = {}

    def _start_stream_server(self, camera_name: str, video_path: Path) -> Optional[StreamServer]:
        """Create and start a StreamServer for a camera with a given video.
        
        Args:
            camera_name: Name of the camera
            video_path: Path to the initial video to stream
            
        Returns:
            StreamServer instance if successful, None if failed
        """
        if not self.running:
            log.debug(f"{camera_name}: skipping stream start (shutdown in progress)")
            return None
        
        try:
            stream_server = StreamServer(camera_name)
            stream_server.start_server(video_path)
            self.stream_servers[camera_name] = stream_server
            return stream_server
        except Exception as e:
            log.error(f"{camera_name}: failed to start stream server: {e}")
            return None

    async def check_for_motion(self, camera_name: str) -> bool:
        """Check for motion on a streaming camera and add new clip if detected.
        
        Returns:
            True if motion was detected and clip added, False otherwise
        """
        try:
            ss = self.stream_servers.get(camera_name)
            if not ss or not ss.is_running():
                return False
            
            file_name_new_clip = await self.cam_manager.check_for_motion(camera_name)
            if not file_name_new_clip:
                return False

            log.debug(f"{camera_name}: adding new clip to stream")
            await asyncio.to_thread(ss.add_video, file_name_new_clip)
            return True
        except Exception as e:
            log.error(f"{camera_name}: error in check_for_motion: {e}", exc_info=True)
            return False
    
    async def _check_waiting_camera(self, camera_name: str) -> bool:
        """Check if a WAITING camera now has a fresh clip available.
        
        Only accepts clips recorded after the camera's freshness threshold
        (bridge_start_time at startup, or offline_detection_time for cameras
        coming back online).
        
        Returns:
            True if fresh clip found and stream upgraded, False otherwise
        """
        try:
            ss = self.stream_servers.get(camera_name)
            if not ss or not ss.is_running():
                return False
            
            threshold = self.freshness_threshold.get(camera_name, self.bridge_start_time)
            file_name = await self.cam_manager.save_latest_clip(camera_name, since=threshold)
            
            if file_name:
                await asyncio.to_thread(ss.add_video, file_name)
                # Sync last_record so check_for_motion doesn't re-trigger on same clip
                camera = self.cam_manager.blink.cameras.get(camera_name)
                if camera:
                    self.cam_manager.camera_last_record[camera_name] = camera.last_record
                self.camera_states[camera_name] = CameraState.STREAMING
                log.info(f"{camera_name}: first fresh clip received, now STREAMING")
                return True
            
            return False
        except Exception as e:
            log.error(f"{camera_name}: error checking for first clip: {e}", exc_info=True)
            return False

    async def start(self) -> None:
        """Start the application, initialize cameras, and begin monitoring."""
        try:
            self.running = True
            self.bridge_start_time = datetime.now(timezone.utc)
            log.info("Detecting FFmpeg hardware acceleration...")
            encoder = init_encoder(CONFIG.get('ffmpeg', {}).get('encoder', 'auto'))
            log.info(f"Encoder: {encoder.name} ({'hardware' if encoder.is_hardware else 'software'})")
            self.cam_manager = CameraManager()
            await self.cam_manager.start()
        except Exception as e:
            log.error(f"Failed to initialize camera manager: {e}")
            raise

        try:
            enabled_cameras = self._get_enabled_cameras()
            log.info(f"enabled cameras: {enabled_cameras}")
        except Exception as e:
            log.error(f"Failed to get enabled cameras: {e}")
            raise

        try:
            await self._initialize_camera_streams(enabled_cameras)
        except Exception as e:
            log.error(f"Error during camera stream initialization: {e}")
            # Continue even if some streams fail to initialize
        
        if self.running:
            try:
                await self._monitor_cameras()
            except Exception as e:
                log.error(f"Error in camera monitoring loop: {e}")
                raise
    
    def _get_enabled_cameras(self) -> set:
        """Get the set of enabled cameras from config.
        
        If CONFIG['cameras']['enabled'] is empty, enables all discovered cameras.
        Always excludes cameras in CONFIG['cameras']['disabled'].
        """
        all_cameras = self.cam_manager.get_all_cameras()
        if CONFIG['cameras']['enabled']:
            enabled_cameras = set(CONFIG['cameras']['enabled'])
        else:
            enabled_cameras = set(all_cameras)
        
        disabled_config = set(CONFIG['cameras']['disabled'])
        result = enabled_cameras - disabled_config
        log.debug(f'All cameras: {all_cameras} ({len(all_cameras)}), Enabled: {result} ({len(result)})')
        return result
    
    async def _initialize_camera_streams(self, enabled_cameras: set) -> None:
        """Create stream servers for all enabled cameras.
        
        Phase 1: Start ALL cameras with 'Initializing' overlay.
        Phase 2: Check per-camera status → swap to WAITING or OFFLINE overlay.
        
        No clips are fetched at startup. Clips only arrive via motion polling.
        """
        init_video = self.cam_manager.get_initializing_video()
        if not init_video:
            log.error("Cannot generate 'Initializing' overlay — cannot start streams")
            return
        
        # Phase 1: Start ALL cameras with "Initializing" overlay
        log.info(f"Phase 1: Starting {len(enabled_cameras)} camera stream(s) with 'Initializing' overlay")
        for camera in enabled_cameras:
            if not self.running:
                return
            
            ss = self._start_stream_server(camera, init_video)
            if ss is None:
                log.warning(f"{camera}: failed to start stream")
                continue
            
            ss.failure_count = 0
            ss.datetime_started = datetime.now()
            self.camera_states[camera] = CameraState.INITIALIZING
            self.freshness_threshold[camera] = self.bridge_start_time
            await asyncio.sleep(0)
        
        # Phase 2: Check status and transition to WAITING or OFFLINE
        online_count = 0
        offline_count = 0
        now_str = datetime.now().strftime("%m/%d/%Y %H:%M")
        
        log.info("Phase 2: Checking camera status...")
        for camera in list(self.camera_states.keys()):
            if not self.running:
                return
            
            ss = self.stream_servers.get(camera)
            if not ss:
                continue
            
            if self.cam_manager.is_camera_online(camera):
                waiting_video = self.cam_manager.get_waiting_video()
                if waiting_video:
                    try:
                        ss.add_video(waiting_video)
                    except Exception as e:
                        log.error(f"{camera}: failed to swap to waiting overlay: {e}")
                self.camera_states[camera] = CameraState.WAITING
                online_count += 1
            else:
                offline_video = self.cam_manager.get_offline_video(now_str)
                if offline_video:
                    try:
                        ss.add_video(offline_video)
                    except Exception as e:
                        log.error(f"{camera}: failed to swap to offline overlay: {e}")
                self.camera_states[camera] = CameraState.OFFLINE
                offline_count += 1
        
        log.info(f"Camera status: {online_count} online (WAITING), {offline_count} offline")
    
    async def _monitor_cameras(self) -> None:
        """Main monitoring loop for camera motion detection.
        
        Continuously polls cameras for motion and manages stream health.
        Logs periodic status summaries at configured intervals.
        
        Note:
            Warns if poll_interval is less than BlinkPy's API throttle limit.
        """
        log.info(
            f"monitoring cameras for motion "
            f"(poll interval: {CONFIG['blink']['poll_interval']}s, "
            f"camera scan interval: {CONFIG['cameras']['scan_interval']}m)"
        )
        
        if CONFIG['blink']['poll_interval'] < MIN_BLINK_THROTTLE:
            log.warning(
                f"poll_interval ({CONFIG['blink']['poll_interval']}s) is less than "
                f"BlinkPy's minimum throttle time ({MIN_BLINK_THROTTLE}s). "
                f"Effective poll rate will be ~{MIN_BLINK_THROTTLE}s due to API throttling."
            )
        
        scan_interval = timedelta(minutes=CONFIG['cameras']['scan_interval'])
        
        poll_count = 0
        last_log_time = datetime.now()
        last_scan_time = datetime.now()
        log_interval = timedelta(seconds=LOG_INTERVAL_SECONDS)
        
        while self.running:
            poll_count += 1
            log.debug(f"Poll #{poll_count}: checking {len(self.stream_servers)} cameras...")
            
            if datetime.now() - last_log_time >= log_interval:
                self._log_camera_status(poll_count)
                last_log_time = datetime.now()
            
            if datetime.now() - last_scan_time >= scan_interval:
                await self._discover_new_cameras()
                last_scan_time = datetime.now()
            
            await self._check_cameras_for_updates()
            await self._restart_failed_streams()
            await asyncio.sleep(CONFIG['blink']['poll_interval'])
    
    def _log_camera_status(self, poll_count: int) -> None:
        """Log periodic status summary of cameras."""
        counts = {s: 0 for s in CameraState}
        for s in self.camera_states.values():
            counts[s] += 1
        log.debug(
            f"Poll #{poll_count}: "
            f"{counts[CameraState.STREAMING]} streaming, "
            f"{counts[CameraState.WAITING]} waiting, "
            f"{counts[CameraState.INITIALIZING]} initializing, "
            f"{counts[CameraState.OFFLINE]} offline, "
            f"{len(self.disabled_cameras)} disabled"
        )
    
    async def _check_single_camera(self, camera_name: str) -> None:
        """Check a single camera for updates, handling errors."""
        state = self.camera_states.get(camera_name)
        try:
            if state == CameraState.WAITING:
                await self._check_waiting_camera(camera_name)
            elif state == CameraState.STREAMING:
                await self.check_for_motion(camera_name)
        except Exception as e:
            log.error(f"{camera_name}: critical error checking for updates: {e}", exc_info=True)
            try:
                ss = self.stream_servers.get(camera_name)
                if ss:
                    ss.close()
            except Exception as close_err:
                log.error(f"{camera_name}: error closing stream: {close_err}")

    async def _check_cameras_for_updates(self) -> None:
        """Check cameras for motion or fresh clip availability.
        
        - WAITING cameras: check for fresh clips (newer than freshness threshold)
        - STREAMING cameras: check for new motion events
        - OFFLINE/INITIALIZING: skipped
        
        Refreshes Blink API data once, then checks all active cameras in parallel.
        """
        has_waiting = any(
            s == CameraState.WAITING for s in self.camera_states.values()
        )
        has_streaming = any(
            s == CameraState.STREAMING for s in self.camera_states.values()
        )

        # Refresh metadata once per poll cycle for WAITING cameras
        if has_waiting:
            try:
                await self.cam_manager.refresh_metadata()
            except Exception as e:
                log.error(f"Failed to refresh metadata: {e}")

        # Single blink.refresh() per poll cycle for STREAMING cameras
        if has_streaming:
            try:
                await self.cam_manager.blink.refresh()
            except Exception as e:
                log.error(f"Failed to refresh Blink data: {e}")

        # Build tasks for all active cameras and run in parallel
        tasks = []
        for camera_name in list(self.stream_servers.keys()):
            if not self.running:
                return
            state = self.camera_states.get(camera_name)
            if state in (CameraState.OFFLINE, CameraState.INITIALIZING):
                continue
            tasks.append(self._check_single_camera(camera_name))

        if tasks:
            await asyncio.gather(*tasks)
    
    async def _restart_single_stream(self, camera_name: str) -> None:
        """Restart a single failed stream server."""
        try:
            ss = self.stream_servers[camera_name]
            if ss.is_running():
                return
            
            state = self.camera_states.get(camera_name)
            
            # Non-streaming states: restart with overlay, no failure penalty
            if state in (CameraState.OFFLINE, CameraState.WAITING, CameraState.INITIALIZING):
                overlay = None
                if state == CameraState.OFFLINE:
                    overlay = self.cam_manager.get_offline_video(
                        datetime.now().strftime("%m/%d/%Y %H:%M")
                    )
                elif state == CameraState.WAITING:
                    overlay = self.cam_manager.get_waiting_video()
                elif state == CameraState.INITIALIZING:
                    overlay = self.cam_manager.get_initializing_video()
                
                if overlay:
                    ss_new = self._start_stream_server(camera_name, overlay)
                    if ss_new:
                        ss_new.failure_count = 0
                        ss_new.datetime_started = datetime.now()
                        log.info(f"{camera_name}: {state.value} stream restarted")
                return
            
            # STREAMING state: count failures, attempt re-download
            if ss.failure_count >= CONFIG['cameras']['max_failures'] - 1:
                log.warning(f"{camera_name}: max failures ({CONFIG['cameras']['max_failures']}) reached, disabling")
                self.disabled_cameras.add(camera_name)
                self.camera_states.pop(camera_name, None)
                self.stream_servers.pop(camera_name, None)
                return

            log.warning(f"{camera_name}: server failed {ss.failure_count + 1} time(s)")

            if datetime.now() < ss.datetime_started + DELAY_RESTART:
                return

            # Try to restart with a real clip
            file_name = await self.cam_manager.save_latest_clip(camera_name)
            if file_name:
                ss_new = self._start_stream_server(camera_name, file_name)
                if ss_new:
                    ss_new.failure_count = ss.failure_count + 1
                    ss_new.datetime_started = datetime.now()
                    self.camera_states[camera_name] = CameraState.STREAMING
                    log.info(f"{camera_name}: streaming restarted")
                    return
            
            # Fallback: restart as WAITING
            waiting_video = self.cam_manager.get_waiting_video()
            if waiting_video:
                ss_new = self._start_stream_server(camera_name, waiting_video)
                if ss_new:
                    ss_new.failure_count = ss.failure_count + 1
                    ss_new.datetime_started = datetime.now()
                    self.camera_states[camera_name] = CameraState.WAITING
                    log.info(f"{camera_name}: restarted as WAITING (clip unavailable)")
        except Exception as e:
            log.error(f"{camera_name}: error during stream restart: {e}", exc_info=True)

    async def _restart_failed_streams(self) -> None:
        """Restart any failed stream servers.
        
        Non-streaming states restart with the appropriate overlay (no failure penalty).
        Streaming cameras attempt clip re-download on restart.
        Restarts are processed in parallel.
        """
        # Identify failed cameras and pre-fetch metadata if any STREAMING restarts needed
        failed_cameras = [
            name for name in self.stream_servers
            if not self.stream_servers[name].is_running()
        ]
        if not failed_cameras:
            return

        has_streaming_failures = any(
            self.camera_states.get(name) == CameraState.STREAMING
            for name in failed_cameras
        )
        if has_streaming_failures:
            try:
                await self.cam_manager.refresh_metadata()
            except Exception as e:
                log.error(f"Failed to refresh metadata for stream restarts: {e}")

        tasks = [
            self._restart_single_stream(name)
            for name in failed_cameras
            if self.running
        ]
        if tasks:
            await asyncio.gather(*tasks)

    async def _discover_new_cameras(self) -> None:
        """Scan for camera changes and handle state transitions.
        
        1. Re-enumerates all cameras
        2. Tracks camera removal via removal_counter
        3. Handles online/offline transitions for existing streams
        4. Starts streams for newly discovered cameras
        5. Retries previously disabled cameras
        """
        try:
            log.debug("Scanning for camera changes...")
            await self.cam_manager.refresh_cameras()
            all_cameras = self.cam_manager.get_all_cameras()
            enabled_cameras = self._get_enabled_cameras()
            running_cameras = set(self.stream_servers.keys())
            removal_scans = CONFIG['cameras'].get('removal_scans', 3)
            
            log.debug(f'All cameras: {all_cameras} ({len(all_cameras)}), '
                      f'Running: {running_cameras} ({len(running_cameras)})')
            
            # --- Handle removal detection ---
            vanished = running_cameras - all_cameras
            for camera_name in vanished:
                self.removal_counter[camera_name] = self.removal_counter.get(camera_name, 0) + 1
                count = self.removal_counter[camera_name]
                if count >= removal_scans:
                    log.warning(f"{camera_name}: absent from API for {count} consecutive scans, removing stream")
                    ss = self.stream_servers.pop(camera_name, None)
                    if ss:
                        ss.close()
                    self.camera_states.pop(camera_name, None)
                    self.freshness_threshold.pop(camera_name, None)
                    self.removal_counter.pop(camera_name, None)
                else:
                    log.debug(f"{camera_name}: absent from API ({count}/{removal_scans} scans)")
            
            # Reset removal counter for cameras that reappeared
            for camera_name in list(self.removal_counter.keys()):
                if camera_name in all_cameras:
                    self.removal_counter.pop(camera_name, None)
            
            # --- Handle state transitions for existing streams ---
            now_str = datetime.now().strftime("%m/%d/%Y %H:%M")
            
            for camera_name in list(running_cameras & enabled_cameras & all_cameras):
                if not self.running:
                    return
                
                current_state = self.camera_states.get(camera_name)
                is_online = self.cam_manager.is_camera_online(camera_name)
                ss = self.stream_servers.get(camera_name)
                if not ss:
                    continue
                
                # Transition: was online (STREAMING/WAITING) → now offline
                if not is_online and current_state in (CameraState.STREAMING, CameraState.WAITING):
                    log.info(f"{camera_name}: went OFFLINE")
                    offline_video = self.cam_manager.get_offline_video(now_str)
                    if offline_video:
                        try:
                            ss.add_video(offline_video)
                            self.camera_states[camera_name] = CameraState.OFFLINE
                        except Exception as e:
                            log.error(f"{camera_name}: failed to swap to offline overlay: {e}")
                
                # Transition: was offline → now online
                elif is_online and current_state == CameraState.OFFLINE:
                    log.info(f"{camera_name}: came back ONLINE, transitioning to WAITING")
                    self.freshness_threshold[camera_name] = datetime.now(timezone.utc)
                    waiting_video = self.cam_manager.get_waiting_video()
                    if waiting_video:
                        try:
                            ss.add_video(waiting_video)
                            self.camera_states[camera_name] = CameraState.WAITING
                        except Exception as e:
                            log.error(f"{camera_name}: failed to swap to waiting overlay: {e}")
            
            # --- Start streams for new cameras ---
            cameras_to_start = (enabled_cameras - running_cameras) & all_cameras
            if not cameras_to_start:
                log.debug('No new cameras to start')
                return
            
            retried = cameras_to_start & self.disabled_cameras
            new = cameras_to_start - self.disabled_cameras
            if new:
                log.info(f"Found {len(new)} new camera(s): {new}")
            if retried:
                log.info(f"Retrying {len(retried)} previously disabled camera(s): {retried}")
            
            init_video = self.cam_manager.get_initializing_video()
            if not init_video:
                log.error("Cannot generate 'Initializing' overlay for new cameras")
                return
            
            for camera_name in cameras_to_start:
                if not self.running:
                    break
                
                self.disabled_cameras.discard(camera_name)
                self.freshness_threshold[camera_name] = datetime.now(timezone.utc)
                
                ss = self._start_stream_server(camera_name, init_video)
                if ss is None:
                    continue
                
                ss.failure_count = 0
                ss.datetime_started = datetime.now()
                self.camera_states[camera_name] = CameraState.INITIALIZING
                
                # Immediately resolve to WAITING or OFFLINE
                if self.cam_manager.is_camera_online(camera_name):
                    waiting_video = self.cam_manager.get_waiting_video()
                    if waiting_video:
                        try:
                            ss.add_video(waiting_video)
                            self.camera_states[camera_name] = CameraState.WAITING
                        except Exception as e:
                            log.error(f"{camera_name}: failed to set waiting overlay: {e}")
                else:
                    offline_video = self.cam_manager.get_offline_video(now_str)
                    if offline_video:
                        try:
                            ss.add_video(offline_video)
                            self.camera_states[camera_name] = CameraState.OFFLINE
                        except Exception as e:
                            log.error(f"{camera_name}: failed to set offline overlay: {e}")
                
                await asyncio.sleep(0)
        except Exception as e:
            log.error(f"Error during camera discovery: {e}", exc_info=True)

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

    try:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, handle_exit)
    except Exception as e:
        log.error(f"Failed to set up signal handlers: {e}")
        raise

    try:
        start_task = asyncio.create_task(app.start())
        await shutdown_event.wait()
        
        start_task.cancel()
        try:
            await start_task
        except asyncio.CancelledError:
            log.debug("Start task cancelled successfully")
        except Exception as e:
            log.error(f"Error in start task: {e}", exc_info=True)

    except KeyboardInterrupt:
        log.info("Keyboard interrupt received")
    except Exception as e:
        log.error(f"Unexpected error in main: {e}", exc_info=True)
    finally:
        try:
            await app.close()
        except Exception as e:
            log.error(f"Error during application cleanup: {e}", exc_info=True)

if __name__ == "__main__":
    logging.basicConfig(
        format="%(message)s", datefmt="[%X]", handlers=[RichHandler(highlighter=NullHighlighter())]
    )
    logging.getLogger('blinkbridge').setLevel(CONFIG['log_level'])
    logging.getLogger(__name__).setLevel(CONFIG['log_level'])
    
    asyncio.run(main())


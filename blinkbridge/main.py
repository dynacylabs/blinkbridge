"""Main application for BlinkBridge RTSP streaming service.

Manages the lifecycle of camera streams, monitors for motion detection,
and handles stream failures and restarts. Provides graceful shutdown handling.
"""
import asyncio
import logging
import signal
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Optional

from rich.highlighter import JSONHighlighter, NullHighlighter
from rich.logging import RichHandler

from blinkbridge.blink import CameraManager
from blinkbridge.config import *
from blinkbridge.stream_server import StreamServer
from blinkbridge.web import BlinkBridgeWebServer


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
        self.web_server: Optional[BlinkBridgeWebServer] = None
        self._monitor_task: Optional[asyncio.Task] = None

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

        file_name_initial_video = await self.cam_manager.save_latest_clip(camera_name, force=redownload)

        if file_name_initial_video is None:
            log.error(f"{camera_name}: cannot start stream (no video available)")
            return None

        if not self.running:
            log.debug(f"{camera_name}: skipping stream start (shutdown in progress)")
            return None

        log.info(f"{camera_name}: starting stream")
        
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
        try:
            ss = self.stream_servers.get(camera_name)
            if not ss:
                log.warning(f"{camera_name}: stream server not found")
                return False

            if not ss.is_running():
                log.debug(f"{camera_name}: stream server not running")
                return False
            
            file_name_new_clip = await self.cam_manager.check_for_motion(camera_name)

            if not file_name_new_clip:
                return False

            log.debug(f"{ss.stream_name}: adding new clip to stream")
            ss.add_video(file_name_new_clip)
            return True
        except Exception as e:
            log.error(f"{camera_name}: error in check_for_motion: {e}", exc_info=True)
            return False
    
    async def start(self) -> None:
        """Start the application, initialize cameras, and begin monitoring.
        
        Raises:
            LoginError: If Blink authentication fails
            TokenRefreshFailed: If Blink token refresh fails
            Exception: For other critical initialization errors
        """
        self.running = True

        # Start the web server before camera login so the /2fa endpoint is
        # reachable if Blink requires two-factor authentication.
        try:
            await self._start_web_server()
        except Exception as e:
            log.warning(f"Failed to start web server: {e}")

        try:
            self.cam_manager = CameraManager()
            if self.web_server is not None:
                self.cam_manager.twofa_provider = self.web_server.request_2fa_code
                self.cam_manager.credentials_provider = self.web_server.request_credentials
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

        try:
            self._export_frigate_camera_block()
        except Exception as e:
            log.warning(f"Failed to export Frigate camera block: {e}")

        if self.running:
            try:
                self._monitor_task = asyncio.create_task(self._monitor_cameras())
                await self._monitor_task
            except asyncio.CancelledError:
                log.debug("Monitor task cancelled")
            except Exception as e:
                log.error(f"Error in camera monitoring loop: {e}")
                raise
    
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

    def _export_frigate_camera_block(self) -> None:
        """Export a Frigate cameras YAML block for manual inclusion.

        This does not integrate with or control Frigate runtime. It only writes
        a snippet file users can paste/merge into their own Frigate config.
        """
        export_cfg = CONFIG.get('frigate_export', {})
        if not export_cfg.get('enabled', False):
            return

        if not self.cam_manager:
            raise RuntimeError("Camera manager not initialized")

        camera_names = sorted(set(self.cam_manager.get_cameras()))
        roles = list(export_cfg.get('roles', ['detect', 'record']))
        rtsp_host = str(export_cfg.get('rtsp_host', CONFIG['rtsp_server']['address']))
        rtsp_port = int(export_cfg.get('rtsp_port', CONFIG['rtsp_server']['port']))
        detect_defaults = dict(export_cfg.get('detect_defaults', {}))
        width = int(detect_defaults.get('width', 1280))
        height = int(detect_defaults.get('height', 720))
        fps = int(detect_defaults.get('fps', 1))

        lines = [
            "# Auto-generated by BlinkBridge.",
            "# Paste this block into your Frigate config under the top-level 'cameras:' key.",
            "cameras:",
        ]

        for camera_name in camera_names:
            camera_key = camera_name.replace(' ', '_').lower()
            lines.append(f"  {camera_key}:")
            lines.append("    ffmpeg:")
            lines.append("      inputs:")
            lines.append(f"        - path: rtsp://{rtsp_host}:{rtsp_port}/{camera_key}")
            lines.append("          roles:")
            for role in roles:
                lines.append(f"            - {role}")
            lines.append("    detect:")
            lines.append("      enabled: true")
            lines.append(f"      width: {width}")
            lines.append(f"      height: {height}")
            lines.append(f"      fps: {fps}")

        output_path = Path(str(export_cfg.get('output_path', PATH_CONFIG / 'frigate_cameras.yml')))
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text("\n".join(lines) + "\n")
        log.info(f"Exported Frigate camera block for {len(camera_names)} cameras to {output_path}")

    async def _start_web_server(self) -> None:
        """Create and start the optional utility web server."""
        web_cfg = CONFIG.get('web', {})
        if not web_cfg.get('enabled', False):
            return

        export_cfg = CONFIG.get('frigate_export', {})
        export_path = Path(str(export_cfg.get('output_path', PATH_CONFIG / 'frigate_cameras.yml')))

        self.web_server = BlinkBridgeWebServer(
            host=str(web_cfg.get('host', '0.0.0.0')),
            port=int(web_cfg.get('port', 8765)),
            frigate_export_path=export_path,
        )
        self.web_server.restart_callback = self.restart
        await self.web_server.start()
        log.info(f"Web server enabled at http://{web_cfg.get('host', '0.0.0.0')}:{web_cfg.get('port', 8765)}")
    
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
        log.debug(
            f"Poll #{poll_count}: {len(self.stream_servers)} cameras active"
        )
    
    async def _check_cameras_for_updates(self) -> None:
        """Check all cameras for motion events.
        
        Checks every active stream for new motion clips and adds them to the stream.
        Closes streams that encounter errors.
        """
        for camera_name in list(self.stream_servers.keys()):
            if not self.running:
                break
            try:
                await self.check_for_motion(camera_name)
            except Exception as e:
                log.error(f"{camera_name}: critical error checking for updates: {e}", exc_info=True)
                try:
                    ss = self.stream_servers.get(camera_name)
                    if ss:
                        ss.close()
                except Exception as close_err:
                    log.error(f"{camera_name}: error closing stream after update failure: {close_err}")
    
    async def _restart_failed_streams(self) -> None:
        """Restart any failed stream servers.
        
        Checks each stream server's health and attempts restart if needed.
        Disables cameras that exceed maximum failure count.
        Respects restart delay between attempts.
        """
        for camera_name in list(self.stream_servers.keys()):
            if not self.running:
                break
            
            try:
                ss = self.stream_servers[camera_name]
                if ss.is_running():
                    ss.failure_detected = False
                    continue

                # Log once when the failure is first detected.
                if not ss.failure_detected:
                    ss.failure_detected = True
                    log.warning(f"{camera_name}: stream stopped (failure count: {ss.failure_count + 1})")

                if ss.failure_count >= CONFIG['cameras']['max_failures'] - 1:
                    log.warning(f"{camera_name}: max failures ({CONFIG['cameras']['max_failures']}) reached, disabling")
                    try:
                        self.stream_servers.pop(camera_name)
                    except KeyError:
                        log.debug(f"{camera_name}: already removed from stream servers")
                    continue

                if datetime.now() < ss.datetime_started + DELAY_RESTART:
                    log.debug(f"{camera_name}: waiting for restart delay to elapse")
                    continue

                log.warning(f"{camera_name}: attempting restart (failure {ss.failure_count + 1})")
                ss.failure_detected = False  # reset so the next failure logs again

                ss_new = await self.start_stream(camera_name, redownload=True)
                if ss_new is None:
                    log.debug(f"{camera_name}: restart failed, will retry later")
                    ss.datetime_started = datetime.now()
                    continue
                
                ss_new.failure_count = ss.failure_count + 1
                ss_new.datetime_started = datetime.now()
                log.info(f"{camera_name}: stream restarted successfully")
            except Exception as e:
                log.error(f"{camera_name}: error during stream restart: {e}", exc_info=True)

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

        if self.web_server:
            try:
                await self.web_server.stop()
            except Exception as e:
                log.warning(f"Error stopping web server: {e}")

        # Remove the Frigate camera snippet so a stale file is never served
        # after the bridge goes offline.
        try:
            export_cfg = CONFIG.get('frigate_export', {})
            export_path = Path(str(export_cfg.get('output_path', PATH_CONFIG / 'frigate_cameras.yml')))
            if export_path.exists():
                export_path.unlink()
                log.debug(f"Removed Frigate export file: {export_path}")
        except Exception as e:
            log.warning(f"Failed to remove Frigate export file on shutdown: {e}")
        
        if self.cam_manager:
            try:
                await self.cam_manager.close()
            except Exception as e:
                log.warning(f"Error closing camera manager: {e}")
        
        log.info("Application closed")

    async def restart(self) -> None:
        """Restart camera streams and Blink connection without stopping the web server.

        Tears down all running streams and the camera manager, then re-initialises
        them from scratch. The web server keeps running throughout so the /restart
        endpoint remains reachable.
        """
        log.info("Restarting BlinkBridge (streams + Blink connection)...")

        # Cancel the running monitor loop
        self.running = False
        if self._monitor_task and not self._monitor_task.done():
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
            self._monitor_task = None
        # Stop all streams
        for camera_name, ss in list(self.stream_servers.items()):
            try:
                ss.close()
            except Exception as e:
                log.warning(f"{camera_name}: error stopping stream during restart: {e}")
        self.stream_servers.clear()

        # Remove stale Frigate export
        try:
            export_cfg = CONFIG.get('frigate_export', {})
            export_path = Path(str(export_cfg.get('output_path', PATH_CONFIG / 'frigate_cameras.yml')))
            if export_path.exists():
                export_path.unlink()
        except Exception as e:
            log.warning(f"Failed to remove Frigate export file during restart: {e}")

        # Close the old camera manager
        if self.cam_manager:
            try:
                await self.cam_manager.close()
            except Exception as e:
                log.warning(f"Error closing camera manager during restart: {e}")
            self.cam_manager = None

        # Re-initialise
        self.running = True
        try:
            self.cam_manager = CameraManager()
            if self.web_server is not None:
                self.cam_manager.twofa_provider = self.web_server.request_2fa_code
                self.cam_manager.credentials_provider = self.web_server.request_credentials
            await self.cam_manager.start()
        except Exception as e:
            log.error(f"Restart: failed to initialise camera manager: {e}")
            return

        try:
            enabled_cameras = self._get_enabled_cameras()
            await self._initialize_camera_streams(enabled_cameras)
        except Exception as e:
            log.error(f"Restart: error initialising camera streams: {e}")

        try:
            self._export_frigate_camera_block()
        except Exception as e:
            log.warning(f"Restart: failed to export Frigate camera block: {e}")

        log.info("Restart complete — resuming camera monitoring")
        # Start a fresh monitoring task.
        self._monitor_task = asyncio.create_task(self._monitor_cameras())

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


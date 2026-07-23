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
from blinkbridge.web import BlinkBridgeWebServer


log = logging.getLogger(__name__)

class Application:
    def __init__(self):
        self.stream_servers = {}
        self.cam_manager = None
        self.running = False
        self.web_server = None

    async def start_stream(self, camera_name: str, redownload: bool=False) -> StreamServer:
        if redownload:
            await self.cam_manager.refresh_metadata()

        log.debug(f"{camera_name}: getting latest clip")
        file_name_initial_video = await self.cam_manager.save_latest_clip(camera_name, force=redownload)

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

        log.info(f"{ss.stream_name}: motion detected, adding video")
        ss.add_video(file_name_new_clip)

        return True
        
    async def start(self) -> None:
        self.running = True

        # Start the web server before camera login so the /2fa endpoint is
        # reachable if Blink requires two-factor authentication.
        await self._start_web_server()

        self.cam_manager = CameraManager()
        if self.web_server is not None:
            self.cam_manager.twofa_provider = self.web_server.request_2fa_code
            self.cam_manager.credentials_provider = self.web_server.request_credentials
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
            # check for motion on each stream server
            for camera_name in self.stream_servers:
                try:                   
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
                    ss_new.failure_count = ss.failure_count + 1
                    ss_new.datetime_started = datetime.now()

            await asyncio.sleep(CONFIG['blink']['poll_interval'])

    async def close(self) -> None:
        self.running = False

        if self.cam_manager:
            await self.cam_manager.close()
        
        for ss in self.stream_servers.values():
            ss.close()

        if self.web_server:
            await self.web_server.stop()

    async def _start_web_server(self) -> None:
        """Create and start the optional utility web server."""
        web_cfg = CONFIG.get('web', {})
        if not web_cfg.get('enabled', False):
            return
        self.web_server = BlinkBridgeWebServer(
            host=str(web_cfg.get('host', '0.0.0.0')),
            port=int(web_cfg.get('port', 8765)),
            frigate_export_path=PATH_CONFIG / 'frigate_cameras.yml',
        )
        self.web_server.restart_callback = self.restart
        await self.web_server.start()
        log.info(f"Web server enabled at http://{web_cfg.get('host', '0.0.0.0')}:{web_cfg.get('port', 8765)}")

    async def restart(self) -> None:
        """Restart camera streams without stopping the web server."""
        log.info("Restarting BlinkBridge (streams + Blink connection)...")
        self.running = False
        for camera_name, ss in list(self.stream_servers.items()):
            try:
                ss.close()
            except Exception as e:
                log.warning(f"{camera_name}: error stopping stream during restart: {e}")
        self.stream_servers.clear()
        if self.cam_manager:
            try:
                await self.cam_manager.close()
            except Exception as e:
                log.warning(f"Error closing camera manager during restart: {e}")
            self.cam_manager = None
        self.running = True
        self.cam_manager = CameraManager()
        if self.web_server is not None:
            self.cam_manager.twofa_provider = self.web_server.request_2fa_code
            self.cam_manager.credentials_provider = self.web_server.request_credentials
        await self.cam_manager.start()
        enabled_cameras = set(CONFIG['cameras']['enabled']) if CONFIG['cameras']['enabled'] else set(self.cam_manager.get_cameras())
        enabled_cameras = enabled_cameras - set(CONFIG['cameras']['disabled'])
        for camera in self.cam_manager.get_cameras():
            if camera not in enabled_cameras:
                continue
            ss = await self.start_stream(camera)
            ss.failure_count = 0
            ss.datetime_started = datetime.now()
        log.info("Restart complete — resuming camera monitoring")

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


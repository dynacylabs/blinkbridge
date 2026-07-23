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

        self._export_frigate_camera_block()
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

        # Remove the Frigate camera snippet on shutdown so a stale file is
        # never served after the bridge goes offline.
        try:
            export_cfg = CONFIG.get('frigate_export', {})
            export_path = Path(str(export_cfg.get('output_path', PATH_CONFIG / 'frigate_cameras.yml')))
            if export_path.exists():
                export_path.unlink()
                log.debug(f"Removed Frigate export file: {export_path}")
        except Exception as e:
            log.warning(f"Failed to remove Frigate export file on shutdown: {e}")

    def _export_frigate_camera_block(self) -> None:
        """Export a Frigate cameras YAML block for manual inclusion.

        Writes a snippet users can paste into their Frigate config under the
        top-level 'cameras:' key.  Does nothing when frigate_export.enabled
        is false (the default).
        """
        export_cfg = CONFIG.get('frigate_export', {})
        if not export_cfg.get('enabled', False):
            return

        if not self.cam_manager:
            return

        camera_names = sorted(set(self.cam_manager.get_cameras()))
        roles = list(export_cfg.get('roles', ['detect', 'record']))
        rtsp_host = str(export_cfg.get('rtsp_host', CONFIG['rtsp_server']['address']))
        rtsp_port = int(export_cfg.get('rtsp_port', CONFIG['rtsp_server']['port']))
        detect = dict(export_cfg.get('detect_defaults', {}))
        width  = int(detect.get('width', 1280))
        height = int(detect.get('height', 720))
        fps    = int(detect.get('fps', 1))

        lines = [
            "# Auto-generated by BlinkBridge.",
            "# Paste this block into your Frigate config under the top-level 'cameras:' key.",
            "cameras:",
        ]
        for camera_name in camera_names:
            camera_key = camera_name.replace(' ', '_').lower()
            lines += [
                f"  {camera_key}:",
                "    ffmpeg:",
                "      inputs:",
                f"        - path: rtsp://{rtsp_host}:{rtsp_port}/{camera_key}",
                "          roles:",
            ]
            for role in roles:
                lines.append(f"            - {role}")
            lines += [
                "    detect:",
                "      enabled: true",
                f"      width: {width}",
                f"      height: {height}",
                f"      fps: {fps}",
            ]

        output_path = Path(str(export_cfg.get('output_path', PATH_CONFIG / 'frigate_cameras.yml')))
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text("\n".join(lines) + "\n")
        log.info(f"Exported Frigate camera block for {len(camera_names)} cameras to {output_path}")

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


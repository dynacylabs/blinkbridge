"""Blink camera integration and video clip management.

Provides the CameraManager class for authenticating with Blink cameras,
downloading video clips, and monitoring for motion detection events.
"""
import asyncio
import json
import logging
import subprocess
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Optional, Tuple, Union

from aiohttp import ClientSession
from blinkpy.auth import Auth, BlinkTwoFARequiredError, TokenRefreshFailed, LoginError
from blinkpy.blinkpy import Blink
from blinkpy.helpers.util import json_load
from blinkpy import api as blink_api

from blinkbridge.config import *
from blinkbridge.hwaccel import get_encoder


log = logging.getLogger(__name__)


def find_most_recent_clip_url(recent_clips: list, date: str) -> str:
    """Find the most recent non-snapshot clip URL that is newer than the given date.
    
    Args:
        recent_clips: List of recent clip dicts from Blink camera
        date: ISO format date string to compare against
        
    Returns:
        URL of the most recent clip, or empty string if none found
        
    Note:
        Filters out snapshots (which contain '/snapshot/' in the URL) and only
        returns actual video clips that are newer than the specified date.
    """
    sorted_data = sorted(recent_clips, key=lambda x: x['time'], reverse=True)

    # Find first entry that is not a snapshot
    clip_entry = next((entry for entry in sorted_data if '/snapshot/' not in entry['clip']), None)
    if not clip_entry:
        return ''
    
    # Check if entry is newer than the given date
    date = datetime.fromisoformat(date.replace('Z', '+00:00'))
    entry_time = datetime.fromisoformat(clip_entry['time'].replace('Z', '+00:00'))
    
    return clip_entry['clip'] if entry_time > date else '' 

class CameraManager:
    """Manages Blink camera connections and video clip downloads.
    
    Handles authentication, clip downloads, and motion detection for Blink
    camera systems. Discovers ALL cameras (including those on offline sync
    modules) and provides per-camera online/offline status detection.
    
    Attributes:
        session: aiohttp ClientSession for HTTP requests
        blink: BlinkPy Blink instance
        camera_last_record: Dict tracking last recorded event per camera
        metadata: List of video metadata from Blink API
        all_camera_names: Set of all camera names across all sync modules
        camera_sync_map: Dict mapping camera name to sync module name
    """
    
    def __init__(self) -> None:
        self.session: ClientSession = ClientSession()
        self.camera_last_record: Dict[str, Optional[str]] = defaultdict(lambda: None)
        self.metadata: Optional[list] = None
        # All cameras discovered from API (including offline sync modules)
        self.all_camera_names: set = set()
        # Maps camera name -> sync module name
        self.camera_sync_map: Dict[str, str] = {}
        # Overlay videos (text on black background)
        self._overlay_cache: Dict[str, Path] = {}

    async def _login(self) -> None:
        """Login to Blink using OAuth v2 authentication.
        
        Attempts to use saved credentials if available, otherwise performs
        fresh authentication. Handles 2FA if required.
        
        Raises:
            LoginError: If authentication fails
            TokenRefreshFailed: If token refresh fails
            
        Note:
            Credentials are saved to .cred.json in the config directory for reuse.
        """
        self.blink = Blink(session=self.session)
        path_cred = PATH_CONFIG / ".cred.json"

        try:
            if path_cred.exists():
                log.debug("Loading saved Blink credentials")
                try:
                    saved_data = await json_load(path_cred)
                    self.blink.auth = Auth(saved_data, no_prompt=True, session=self.session)
                except (json.JSONDecodeError, IOError) as e:
                    log.debug(f"Failed to load saved credentials: {e}")
                    log.debug("Falling back to credentials from config")
                    self.blink.auth = Auth(CONFIG['blink']['login'], no_prompt=True, session=self.session)
            else:
                log.debug("Using Blink credentials from config")
                self.blink.auth = Auth(CONFIG['blink']['login'], no_prompt=True, session=self.session)
        except Exception as e:
            log.error(f"Failed to initialize authentication: {e}")
            raise

        try:
            await self.blink.start()
            log.info("Successfully authenticated with Blink")
        except BlinkTwoFARequiredError:
            log.info("Two-factor authentication required")
            try:
                twofa_code = input("Enter your 2FA code: ")
                
                success = await self.blink.send_2fa_code(twofa_code)
                if not success:
                    raise LoginError("2FA verification failed")
                
                log.info("Successfully authenticated with Blink (2FA completed)")
            except Exception as e:
                log.error(f"2FA authentication failed: {e}")
                raise
        except (TokenRefreshFailed, LoginError) as e:
            log.error(f"Authentication failed: {e}")
            if path_cred.exists():
                try:
                    log.debug("Removing invalid saved credentials")
                    path_cred.unlink()
                except OSError as unlink_err:
                    log.warning(f"Failed to remove invalid credentials file: {unlink_err}")
            raise
        except Exception as e:
            log.error(f"Unexpected error during authentication: {e}")
            raise

        try:
            log.debug("Saving Blink credentials")
            await self.blink.save(path_cred)
        except (IOError, OSError) as e:
            log.warning(f"Failed to save credentials (will need to re-authenticate next time): {e}")

        # Diagnostic: dump raw camera_usage and homescreen APIs
        await self._log_raw_api_data()

    def _generate_overlay_video(self, text: str, cache_key: str = None,
                                width: int = 1920, height: int = 1080) -> Optional[Path]:
        """Generate a black video with centered white text overlay.
        
        Args:
            text: Text to display on the black background
            cache_key: Optional key for caching. If provided and a cached video
                       exists with this key, returns the cached path. If None,
                       always generates a new file.
            width: Video width in pixels (default: 1920)
            height: Video height in pixels (default: 1080)
            
        Returns:
            Path to the generated overlay video, or None if generation failed
        """
        
        if cache_key and cache_key in self._overlay_cache:
            cached = self._overlay_cache[cache_key]
            try:
                if cached.exists():
                    return cached
            except OSError:
                pass
        
        safe_name = cache_key or text.lower().replace(' ', '_').replace('/', '-').replace(':', '-')
        output_path = PATH_VIDEOS / f"_overlay_{safe_name}.mp4"
        
        # If not cached but file exists on disk, use it (for static overlays)
        if cache_key:
            try:
                if output_path.exists():
                    self._overlay_cache[cache_key] = output_path
                    return output_path
            except OSError:
                pass
        
        duration = CONFIG['still_video_duration']
        encoder = get_encoder()
        encode_args = encoder.build_simple_encode_args()
        vf_base = encoder.build_simple_video_filter()

        # Escape text for FFmpeg drawtext filter
        escaped_text = text.replace("'", "\\'").replace(":", "\\:")
        drawtext = (
            f"drawtext=text='{escaped_text}'"
            f":fontfile=/usr/share/fonts/freefont/FreeSans.ttf"
            f":fontsize=48:fontcolor=white"
            f":x=(w-text_w)/2:y=(h-text_h)/2"
        )
        
        if vf_base:
            vf = f"{vf_base},{drawtext}"
        else:
            vf = drawtext

        ffmpeg_cmd = [
            'ffmpeg', *COMMON_FFMPEG_ARGS,
            *encoder.init_args,
            '-f', 'lavfi', '-i', f'color=black:s={width}x{height}:d={duration}',
            '-f', 'lavfi', '-i', f'anullsrc=channel_layout=stereo:sample_rate=44100',
            *encode_args,
            '-c:a', 'aac', '-ar', '44100', '-ac', '2', '-b:a', '128k',
            '-t', str(duration), '-movflags', 'faststart',
            '-vf', vf,
            str(output_path)
        ]
        
        log.debug(f"Generating overlay video: '{text}' ({width}x{height})")
        try:
            result = subprocess.run(ffmpeg_cmd, capture_output=True, timeout=30)
        except subprocess.TimeoutExpired:
            log.error("FFmpeg timed out while generating overlay video")
            return None
        except FileNotFoundError:
            log.error("FFmpeg not found. Please ensure FFmpeg is installed and in PATH")
            return None
        except Exception as e:
            log.error(f"Unexpected error running FFmpeg: {e}")
            return None
        
        if result.returncode != 0:
            stderr = result.stderr.decode('utf-8', errors='replace') if result.stderr else 'No error output'
            log.error(f"Failed to generate overlay video (exit code {result.returncode}): {stderr}")
            return None
        
        try:
            if not output_path.exists():
                log.error(f"Overlay video was not created at {output_path}")
                return None
        except OSError as e:
            log.error(f"Error verifying overlay video creation: {e}")
            return None
        
        if cache_key:
            self._overlay_cache[cache_key] = output_path
        log.debug(f"Overlay video created at {output_path}")
        return output_path
    
    def get_initializing_video(self) -> Optional[Path]:
        """Get the 'Initializing' overlay video, generating if needed."""
        return self._generate_overlay_video("Initializing", cache_key="initializing")
    
    def get_waiting_video(self) -> Optional[Path]:
        """Get the 'Waiting' overlay video, generating if needed."""
        return self._generate_overlay_video("Waiting", cache_key="waiting")
    
    def get_offline_video(self, timestamp: str) -> Optional[Path]:
        """Get an 'Offline as of ...' overlay video.
        
        Args:
            timestamp: Formatted timestamp string (MM/DD/YYYY HH:MM)
        """
        text = f"Offline as of {timestamp}"
        # Sanitize timestamp for use as filename (slashes/colons are invalid in paths)
        safe_ts = timestamp.replace('/', '-').replace(':', '-').replace(' ', '_')
        cache_key = f"offline-{safe_ts}"
        return self._generate_overlay_video(text, cache_key=cache_key)
    
    def _detect_resolution_from_clips(self) -> Tuple[int, int]:
        """Detect resolution from clips. Returns default Blink resolution (1920x1080).
        
        Returns:
            Tuple of (width, height) in pixels
            
        Note:
            Currently returns hardcoded 1920x1080 as all Blink cameras use this resolution.
            Could be extended to detect actual resolution from clip metadata.
        """
        return (1920, 1080)
    
    async def refresh_metadata(self) -> None:
        """Refresh video metadata from Blink API.
        
        Fetches recent video clips based on CONFIG['blink']['history_days'].
        Uses CONFIG['blink']['metadata_pages'] to control pagination depth
        (~25 items per page). Updates self.metadata with the latest available clips.
        
        Raises:
            Exception: If API call fails
        """
        try:
            log.debug('refreshing video metadata')
            dt_past = datetime.now(timezone.utc) - timedelta(days=CONFIG['blink']['history_days'])
            stop = CONFIG['blink']['metadata_pages'] + 1  # BlinkPy uses range(1, stop)
            self.metadata = await self.blink.get_videos_metadata(since=str(dt_past), stop=stop)
            count = len(self.metadata) if self.metadata else 0
            log.debug(f'Retrieved {count} video metadata entries')
            if self.metadata:
                cameras_with_clips = defaultdict(int)
                for m in self.metadata:
                    if not m.get('deleted') and m.get('source') != 'snapshot':
                        cameras_with_clips[m.get('device_name', 'unknown')] += 1
                log.debug(f'Clips per camera: {dict(cameras_with_clips)}')
        except Exception as e:
            log.error(f"Failed to refresh video metadata: {e}")
            # Keep existing metadata if refresh fails
            if self.metadata is None:
                self.metadata = []
            raise

    async def save_latest_clip(self, camera_name: str, since: datetime = None) -> Optional[Path]:
        """Download and save latest clip for camera.
        
        Args:
            camera_name: Name of the camera
            since: Only return clips newer than this datetime. If None, returns
                the most recent clip regardless of age.
        
        Returns:
            Path to the video file, or None if no qualifying clip found
        """
        try:
            camera_name_sanitized = camera_name.lower().replace(' ', '_')
            file_name = PATH_VIDEOS / f"{camera_name_sanitized}_latest.mp4"
        except Exception as e:
            log.error(f"{camera_name}: error creating file path: {e}")
            return None

        try:
            media = None
            for m in (self.metadata or []):
                if m.get('device_name') != camera_name:
                    continue
                if m.get('deleted') or m.get('source') == 'snapshot':
                    continue
                if since:
                    try:
                        clip_time = datetime.fromisoformat(
                            m['created_at'].replace('Z', '+00:00')
                        )
                        # Ensure both sides are tz-aware for comparison
                        since_aware = since if since.tzinfo else since.replace(tzinfo=timezone.utc)
                        if clip_time <= since_aware:
                            continue
                    except (KeyError, ValueError, TypeError):
                        continue
                media = m
                break  # metadata is sorted newest-first
        except Exception as e:
            log.error(f"{camera_name}: error searching metadata: {e}")
            media = None

        if media is None:
            log.debug(f"{camera_name}: no qualifying clips found")
            return None

        try:
            log.debug(f'{camera_name}: downloading video: {media}')
            response = await self.blink.do_http_get(media['media'])
            
            if not response:
                log.error(f"{camera_name}: received empty response from Blink API")
                raise ValueError("Empty response from API")

            log.debug(f'{camera_name}: saving video to {file_name}')
            video_data = await response.read()
            
            if not video_data:
                log.error(f"{camera_name}: received empty video data")
                raise ValueError("Empty video data")
                
            with open(file_name, 'wb') as f:
                f.write(video_data)
            
            if not file_name.exists() or file_name.stat().st_size == 0:
                log.error(f"{camera_name}: video file not created or is empty")
                raise IOError("Failed to write video file")
            
            log.debug(f"{camera_name}: downloaded clip ({file_name.stat().st_size} bytes)")
            return file_name
        except IOError as e:
            log.error(f"{camera_name}: file I/O error downloading clip: {e}")
        except Exception as e:
            log.error(f"{camera_name}: failed to download clip: {e}")
            
        return None
    
    async def _save_clip(self, camera_name: str, url: str, file_name: Path) -> None:
        """Save a video clip from URL to file.
        
        Args:
            camera_name: Name of the camera
            url: URL of the video clip to download
            file_name: Path where the clip should be saved
            
        Raises:
            Exception: If download or save fails
        """
        try:
            camera = self.blink.cameras[camera_name]
            response = await camera.get_video_clip(url)
            
            if not response:
                raise ValueError("Empty response from get_video_clip")

            log.debug(f'{camera_name}: saving video to {file_name}')
            video_data = await response.read()
            
            if not video_data:
                raise ValueError("Empty video data received")
                
            with open(file_name, 'wb') as f:
                f.write(video_data)
                
            # Verify file was written
            if not file_name.exists() or file_name.stat().st_size == 0:
                raise IOError("Failed to write video file or file is empty")
                
            log.debug(f'{camera_name}: video saved ({file_name.stat().st_size} bytes)')
        except IOError as e:
            log.error(f"{camera_name}: file I/O error saving clip: {e}")
            raise
        except Exception as e:
            log.error(f"{camera_name}: error in _save_clip: {e}")
            raise
    
    async def check_for_motion(self, camera_name: str) -> Optional[Path]:
        """Check if camera detected motion and download new clip if available.
        
        Args:
            camera_name: Name of the camera to check
            
        Returns:
            Path to the downloaded clip file, or None if no new motion detected
            
        Note:
            Caller must have called ``blink.refresh()`` before invoking this
            method so that ``camera.attributes`` reflects current state.
            Handles both regular video clips and snapshot events. For snapshots,
            searches for the most recent actual clip in the recent_clips list.
        """
        try:
            camera = self.blink.cameras[camera_name]
        except KeyError:
            log.error(f"{camera_name}: camera not found in Blink cameras")
            return None
        except Exception as e:
            log.error(f"{camera_name}: error accessing camera: {e}")
            return None

        try:
            motion_detected = camera.attributes.get('motion_detected', False)
            last_record = camera.attributes.get('last_record', 'N/A')
            cached_last_record = self.camera_last_record[camera_name]
            
            log.debug(
                f"{camera_name}: motion_detected={motion_detected}, "
                f"last_record={last_record}, cached={cached_last_record}"
            )
        except Exception as e:
            log.error(f"{camera_name}: error reading camera attributes: {e}")
            return None

        if not motion_detected or cached_last_record == last_record:
            return None

        log.info(f"{camera_name}: motion detected (last_record: {last_record})")

        try:
            camera_name_sanitized = camera_name.lower().replace(' ', '_')
            file_name = PATH_VIDEOS / f"{camera_name_sanitized}_latest.mp4"
        except Exception as e:
            log.error(f"{camera_name}: error creating file path: {e}")
            return None

        # Handle snapshot events by finding recent clip
        try:
            if '/snapshot/' in camera.attributes.get('video', ''):
                recent_clips = camera.attributes.get('recent_clips', [])
                if url := find_most_recent_clip_url(recent_clips, camera.attributes['last_record']):
                    log.debug(f"{camera_name}: found recent clip in snapshot, saving to {file_name}")
                    try:
                        await self._save_clip(camera_name, url, file_name)
                        self.camera_last_record[camera_name] = last_record
                        log.debug(f"{camera_name}: clip saved to {file_name}")
                        return file_name
                    except Exception as e:
                        log.error(f"{camera_name}: failed to save clip from snapshot: {e}")
                        self.camera_last_record[camera_name] = last_record
                        return None

                log.debug(f"{camera_name}: no recent clip in snapshot, skipping")
                self.camera_last_record[camera_name] = last_record
                return None
        except Exception as e:
            log.error(f"{camera_name}: error processing snapshot: {e}")
            return None
        
        # Download regular video clip
        try:
            log.debug(f"{camera_name}: downloading clip to {file_name}")
            await camera.video_to_file(file_name)
            
            # Verify file was created
            if not file_name.exists() or file_name.stat().st_size == 0:
                log.error(f"{camera_name}: video file not created or is empty")
                return None
                
            self.camera_last_record[camera_name] = last_record
            log.debug(f"{camera_name}: clip saved to {file_name} ({file_name.stat().st_size} bytes)")
            return file_name
        except IOError as e:
            log.error(f"{camera_name}: file I/O error saving clip: {e}")
            return None
        except Exception as e:
            log.error(f"{camera_name}: error downloading clip: {e}")
            return None
        
    def _log_blink_diagnostics(self) -> None:
        """Log diagnostic info about Blink's internal camera/sync module state."""
        try:
            # Homescreen info
            hs = getattr(self.blink, 'homescreen', None)
            if hs:
                owls = hs.get('owls', [])
                doorbells = hs.get('doorbells', [])
                owl_names = [o.get('name', 'unknown') for o in owls] if owls else []
                doorbell_names = [d.get('name', 'unknown') for d in doorbells] if doorbells else []
                log.debug(f'Homescreen owls (Minis): {owl_names}')
                log.debug(f'Homescreen doorbells: {doorbell_names}')
            else:
                log.debug('Homescreen data: None/empty')
            
            # Sync modules — show online status, populated cameras AND raw camera_list
            sync = getattr(self.blink, 'sync', {})
            for sync_name, sync_mod in sync.items():
                sync_type = type(sync_mod).__name__
                sync_online = getattr(sync_mod, 'online', 'unknown')
                sync_available = getattr(sync_mod, 'available', 'unknown')
                sync_cams = list(sync_mod.cameras.keys()) if hasattr(sync_mod, 'cameras') else []
                raw_cam_list = getattr(sync_mod, 'camera_list', [])
                raw_cam_names = [c.get('name', 'unknown') for c in raw_cam_list] if isinstance(raw_cam_list, list) else raw_cam_list
                log.debug(
                    f'Sync "{sync_name}" ({sync_type}): online={sync_online}, '
                    f'available={sync_available}, cameras={sync_cams}, '
                    f'camera_list(raw)={raw_cam_names}'
                )
            
            # All merged cameras with types
            for cam_name, cam in self.blink.cameras.items():
                cam_type = getattr(cam, 'camera_type', 'unknown')
                product_type = getattr(cam, 'product_type', 'unknown')
                log.debug(f'Camera "{cam_name}": type={cam_type}, product={product_type}')
        except Exception as e:
            log.debug(f'Error logging diagnostics: {e}')

    async def _log_raw_api_data(self) -> None:
        """Log raw API responses for camera_usage to diagnose missing cameras."""
        try:
            response = await blink_api.request_camera_usage(self.blink)
            log.debug(f'Raw camera_usage API response: {response}')
            if response and 'networks' in response:
                for network in response['networks']:
                    network_id = network.get('network_id', '?')
                    cam_names = [c.get('name', '?') for c in network.get('cameras', [])]
                    log.debug(f'camera_usage: network {network_id}: {cam_names}')
        except Exception as e:
            log.error(f'Failed to query camera_usage API: {e}')

    def get_all_cameras(self) -> set:
        """Get ALL camera names from all sync modules, including offline ones.
        
        Reads camera_list from each sync module (populated by request_camera_usage
        and homescreen APIs regardless of sync module online status) and merges
        with blink.cameras for a complete set.
        
        Returns:
            Set of all camera names across all sync modules
        """
        all_names = set(self.blink.cameras.keys())
        self.camera_sync_map.clear()
        
        sync = getattr(self.blink, 'sync', {})
        for sync_name, sync_mod in sync.items():
            raw_list = getattr(sync_mod, 'camera_list', [])
            if isinstance(raw_list, list):
                for cam in raw_list:
                    name = cam.get('name') if isinstance(cam, dict) else None
                    if name:
                        all_names.add(name)
                        self.camera_sync_map[name] = sync_name
            # Also map cameras from blink.cameras to their sync module
            if hasattr(sync_mod, 'cameras'):
                for cam_name in sync_mod.cameras:
                    self.camera_sync_map[cam_name] = sync_name
        
        self.all_camera_names = all_names
        log.debug(f'All cameras (including offline sync modules): {all_names} ({len(all_names)} total)')
        return all_names
    
    def is_camera_online(self, camera_name: str) -> bool:
        """Check if a camera is online using set difference + homescreen status.
        
        Detection logic:
        1. Standard cameras: present in blink.cameras = online (only populated
           for cameras whose sync module is online). Absent = offline.
        2. Minis/Doorbells: always in blink.cameras regardless of status, so
           check homescreen["owls"/"doorbells"] for the "status" field.
        
        Args:
            camera_name: Name of the camera
            
        Returns:
            True if camera is online, False otherwise
        """
        if camera_name not in self.blink.cameras:
            # Not in blink.cameras = offline (standard camera on offline sync)
            return False
        
        # It's in blink.cameras. For Minis/Doorbells, check homescreen status
        # since they always get populated even when offline.
        hs = getattr(self.blink, 'homescreen', None)
        if hs:
            for owl in (hs.get('owls') or []):
                if owl.get('name') == camera_name:
                    return owl.get('status', '').lower() == 'online'
            for doorbell in (hs.get('doorbells') or []):
                if doorbell.get('name') == camera_name:
                    return doorbell.get('status', '').lower() == 'online'
        
        # Standard camera in blink.cameras = online
        return True
    
    def get_online_cameras(self) -> set:
        """Get set of camera names that are currently online."""
        return {name for name in self.all_camera_names if self.is_camera_online(name)}
    
    def get_offline_cameras(self) -> set:
        """Get set of camera names that are currently offline."""
        return {name for name in self.all_camera_names if not self.is_camera_online(name)}

    def get_cameras(self) -> set:
        """Get all available camera names.
        
        Returns:
            Set of camera name strings
        """
        return self.all_camera_names

    async def refresh_cameras(self) -> set:
        """Re-run camera discovery to find newly added cameras.
        
        Calls BlinkPy's setup_post_verify() to re-enumerate all cameras
        from the Blink API. This picks up cameras that were added or came
        online after initial startup.
        
        Returns:
            Set of all currently known camera names after refresh
        """
        try:
            log.debug('Re-scanning Blink account for cameras')
            previous_cameras = set(self.blink.cameras.keys())
            log.debug(f'Cameras before refresh: {previous_cameras}')
            await self.blink.setup_post_verify()
            current_cameras = set(self.blink.cameras.keys())
            log.debug(f'Cameras after refresh: {current_cameras}')
            new_cameras = current_cameras - previous_cameras
            removed_cameras = previous_cameras - current_cameras
            if new_cameras:
                log.info(f'Discovered {len(new_cameras)} new camera(s): {new_cameras}')
            if removed_cameras:
                log.info(f'{len(removed_cameras)} camera(s) no longer reported by API: {removed_cameras}')
            log.debug(f'Total cameras from Blink API: {len(current_cameras)}')
            self._log_blink_diagnostics()
            return current_cameras
        except Exception as e:
            log.error(f"Failed to refresh camera list: {e}")
            return set(self.blink.cameras.keys())
    
    async def start(self) -> None:
        """Initialize the camera manager.
        
        Performs authentication, refreshes metadata, builds the complete
        camera list (including offline sync modules), and generates
        overlay videos.
        
        Raises:
            LoginError: If authentication fails
            TokenRefreshFailed: If token refresh fails
        """
        try:
            await self._login()
        except Exception as e:
            log.error(f"Login failed: {e}")
            raise
        
        self._log_blink_diagnostics()
        
        # Build complete camera list from all sync modules
        self.get_all_cameras()
        
        # Pre-generate overlay videos
        try:
            if not self.get_initializing_video():
                log.warning("Failed to create 'Initializing' overlay video")
            if not self.get_waiting_video():
                log.warning("Failed to create 'Waiting' overlay video")
        except Exception as e:
            log.error(f"Error generating overlay videos: {e}")
    
    async def close(self) -> None:
        """Properly close all connections and clean up resources.
        
        Closes the aiohttp session and waits briefly for SSL cleanup.
        """
        try:
            if hasattr(self, 'session') and self.session is not None and not self.session.closed:
                await self.session.close()
                # Give the event loop time to clean up SSL transports
                await asyncio.sleep(0.25)
        except Exception as e:
            log.warning(f"Error closing session: {e}")

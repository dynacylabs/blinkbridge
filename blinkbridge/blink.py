"""Blink camera integration and video clip management.

Provides the CameraManager class for authenticating with Blink cameras,
downloading video clips, and monitoring for motion detection events.
"""
import asyncio
import json
import logging
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Optional, Tuple, Union

from aiohttp import ClientSession
from blinkpy.auth import Auth, BlinkTwoFARequiredError, TokenRefreshFailed, LoginError
from blinkpy.blinkpy import Blink
from blinkpy.helpers.util import json_load

from blinkbridge.config import *
from blinkbridge.hwaccel import get_encoder


log = logging.getLogger(__name__)


def find_most_recent_clip_url(recent_clips: dict, date: str) -> str:
    """Find the most recent non-snapshot clip URL that is newer than the given date.
    
    Args:
        recent_clips: Dictionary of recent clips from Blink camera
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
    
    Handles authentication, metadata management, clip downloads, and motion detection
    for Blink camera systems. Maintains state about which cameras have clips available
    and provides black video placeholders for cameras without recorded content.
    
    Attributes:
        session: aiohttp ClientSession for HTTP requests
        blink: BlinkPy Blink instance
        camera_last_record: Dict tracking last recorded event per camera
        metadata: List of video metadata from Blink API
        black_video_path: Path to black placeholder video
        cameras_without_clips: Set of cameras currently without clips
        cameras_ever_had_real_clip: Set of cameras that have had clips (persistent)
    """
    
    def __init__(self) -> None:
        self.session: ClientSession = ClientSession()
        self.camera_last_record: Dict[str, Optional[str]] = defaultdict(lambda: None)
        self.metadata: Optional[list] = None
        self.black_video_path: Optional[Path] = None
        self.cameras_without_clips: set = set()
        self.cameras_ever_had_real_clip: set = set()

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

    def _generate_black_video(self, width: int = 1920, height: int = 1080) -> Optional[Path]:
        """Generate a black video file to use as placeholder for cameras without clips.
        
        Args:
            width: Video width in pixels (default: 1920)
            height: Video height in pixels (default: 1080)
            
        Returns:
            Path to the generated black video file, or None if generation failed
            
        Note:
            Uses FFmpeg to create a video with black frames and silent audio.
            The duration matches CONFIG['still_video_duration'].
        """
        import subprocess
        
        black_video_path = PATH_VIDEOS / "_black_placeholder.mp4"
        
        try:
            if black_video_path.exists():
                log.debug(f"Black video already exists at {black_video_path}")
                return black_video_path
        except OSError as e:
            log.error(f"Error checking if black video exists: {e}")
            return None
        
        duration = CONFIG['still_video_duration']
        encoder = get_encoder()
        encode_args = encoder.build_simple_encode_args()
        vf = encoder.build_simple_video_filter()

        ffmpeg_cmd = [
            'ffmpeg', *COMMON_FFMPEG_ARGS,
            *encoder.init_args,
            '-f', 'lavfi', '-i', f'color=black:s={width}x{height}:d={duration}',
            '-f', 'lavfi', '-i', f'anullsrc=channel_layout=stereo:sample_rate=44100',
            *encode_args,
            '-c:a', 'aac', '-ar', '44100', '-ac', '2', '-b:a', '128k',
            '-t', str(duration), '-movflags', 'faststart',
        ]
        if vf:
            ffmpeg_cmd.extend(['-vf', vf])
        ffmpeg_cmd.append(str(black_video_path))
        
        log.debug(f"Generating black placeholder video ({width}x{height}, {duration}s)")
        try:
            result = subprocess.run(ffmpeg_cmd, capture_output=True, timeout=30)
        except subprocess.TimeoutExpired:
            log.error("FFmpeg timed out while generating black video")
            return None
        except FileNotFoundError:
            log.error("FFmpeg not found. Please ensure FFmpeg is installed and in PATH")
            return None
        except Exception as e:
            log.error(f"Unexpected error running FFmpeg: {e}")
            return None
        
        if result.returncode != 0:
            stderr = result.stderr.decode('utf-8', errors='replace') if result.stderr else 'No error output'
            log.error(f"Failed to generate black video (exit code {result.returncode}): {stderr}")
            return None
        
        try:
            if not black_video_path.exists():
                log.error(f"Black video was not created at {black_video_path}")
                return None
        except OSError as e:
            log.error(f"Error verifying black video creation: {e}")
            return None
        
        log.debug(f"Black placeholder video created at {black_video_path}")
        return black_video_path
    
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
        Updates self.metadata with the latest available clips.
        
        Raises:
            Exception: If API call fails
        """
        try:
            log.debug('refreshing video metadata')
            dt_past = datetime.now() - timedelta(days=CONFIG['blink']['history_days'])
            self.metadata = await self.blink.get_videos_metadata(since=str(dt_past), stop=2)
            log.debug(f'Retrieved {len(self.metadata) if self.metadata else 0} video metadata entries')
        except Exception as e:
            log.error(f"Failed to refresh video metadata: {e}")
            # Keep existing metadata if refresh fails
            if self.metadata is None:
                self.metadata = []
            raise

    async def save_latest_clip(self, camera_name: str, force: bool=False, use_black_fallback: bool=True) -> Optional[Path]:
        """Download and save latest clip for camera.
        
        Args:
            camera_name: Name of the camera
            force: Force re-download even if clip exists (default: False)
            use_black_fallback: Use black video if no clips available, only for 
                cameras that never had clips (default: True)
        
        Returns:
            Path to the video file, or None if unavailable and no fallback
            
        Note:
            Once a camera has had a real clip, it will never fall back to the
            black placeholder video, even if new clips temporarily unavailable.
        """
        try:
            camera_name_sanitized = camera_name.lower().replace(' ', '_')
            file_name = PATH_VIDEOS / f"{camera_name_sanitized}_latest.mp4"
        except Exception as e:
            log.error(f"{camera_name}: error creating file path: {e}")
            return None
    
        try:
            if file_name.exists() and not force:
                log.debug(f"{camera_name}: skipping download, {file_name} exists")
                if file_name != self.black_video_path:
                    self.cameras_without_clips.discard(camera_name)
                    self.cameras_ever_had_real_clip.add(camera_name)
                return file_name
        except OSError as e:
            log.warning(f"{camera_name}: error checking if file exists: {e}")

        try:
            media = next((m for m in self.metadata if m['device_name'] == camera_name 
                        if not m['deleted'] and m['source'] != 'snapshot'), None)
        except Exception as e:
            log.error(f"{camera_name}: error searching metadata: {e}")
            media = None

        if media is None:
            log.warning(f"{camera_name}: no clips found for camera")
            if use_black_fallback and self.black_video_path and camera_name not in self.cameras_ever_had_real_clip:
                log.info(f"{camera_name}: using black video placeholder (never had real clip)")
                self.cameras_without_clips.add(camera_name)
                return self.black_video_path
            elif camera_name in self.cameras_ever_had_real_clip:
                log.warning(f"{camera_name}: no new clips found, but camera has had real clips before")
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
            
            # Verify file was written
            if not file_name.exists() or file_name.stat().st_size == 0:
                log.error(f"{camera_name}: video file not created or is empty")
                raise IOError("Failed to write video file")
            
            self.cameras_without_clips.discard(camera_name)
            self.cameras_ever_had_real_clip.add(camera_name)
            log.debug(f"{camera_name}: successfully downloaded real clip ({file_name.stat().st_size} bytes)")
            return file_name
        except IOError as e:
            log.error(f"{camera_name}: file I/O error downloading clip: {e}")
        except Exception as e:
            log.error(f"{camera_name}: failed to download clip: {e}")
        
        # If download fails but camera had clips before, try to use cached file
        try:
            if camera_name in self.cameras_ever_had_real_clip and file_name.exists():
                log.warning(f"{camera_name}: using cached clip after download failure")
                return file_name
        except OSError:
            pass
            
        return None
    
    def _mark_camera_has_clip(self, camera_name: str) -> None:
        """Mark that a camera has a real clip.
        
        Args:
            camera_name: Name of the camera
            
        Note:
            This is a permanent state change - once marked, the camera will
            never fall back to black placeholder video.
        """
        self.cameras_without_clips.discard(camera_name)
        self.cameras_ever_had_real_clip.add(camera_name)
    
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
            Handles both regular video clips and snapshot events. For snapshots,
            searches for the most recent actual clip in the recent_clips list.
        """
        try:
            await self.blink.refresh()
        except Exception as e:
            log.error(f"{camera_name}: failed to refresh Blink data: {e}")
            raise
            
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
                        self._mark_camera_has_clip(camera_name)
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
                
            self._mark_camera_has_clip(camera_name)
            self.camera_last_record[camera_name] = last_record
            log.debug(f"{camera_name}: clip saved to {file_name} ({file_name.stat().st_size} bytes)")
            return file_name
        except IOError as e:
            log.error(f"{camera_name}: file I/O error saving clip: {e}")
            return None
        except Exception as e:
            log.error(f"{camera_name}: error downloading clip: {e}")
            return None
        
    def get_cameras(self) -> iter:
        """Get iterator of all available camera names.
        
        Returns:
            Iterator of camera name strings
        """
        return self.blink.cameras.keys()
    
    async def start(self) -> None:
        """Initialize the camera manager.
        
        Performs authentication, refreshes metadata, and generates the black
        video placeholder for cameras without clips.
        
        Raises:
            LoginError: If authentication fails
            TokenRefreshFailed: If token refresh fails
        """
        try:
            await self._login()
        except Exception as e:
            log.error(f"Login failed: {e}")
            raise
            
        try:
            await self.refresh_metadata()
        except Exception as e:
            log.warning(f"Failed to refresh metadata during startup: {e}")
            # Continue with empty metadata
            self.metadata = []
        
        # Generate black video placeholder
        try:
            width, height = self._detect_resolution_from_clips()
            self.black_video_path = self._generate_black_video(width, height)
            if not self.black_video_path:
                log.warning("Failed to create black video placeholder, cameras without clips will be skipped")
        except Exception as e:
            log.error(f"Error generating black video placeholder: {e}")
            self.black_video_path = None
    
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

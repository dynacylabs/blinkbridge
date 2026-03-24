"""RTSP streaming server management using FFmpeg.

Provides the StreamServer class for managing FFmpeg-based RTSP streams.
Handles video concatenation, still video creation, and stream lifecycle.
"""
import logging
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional, Union

from blinkbridge.config import *
from blinkbridge.ffmpeg import StillVideoCreator
from blinkbridge.utils import wait_until_file_open


log = logging.getLogger(__name__)

class StreamServer:
    """Manages RTSP streaming of Blink camera videos using FFmpeg.
    
    Creates and manages an FFmpeg process that streams video via RTSP protocol.
    Uses FFmpeg's concat demuxer to seamlessly loop and update videos. Maintains
    a "still video" created from the last frame of clips for smooth looping.
    
    Attributes:
        stream_name: Human-readable camera name
        stream_name_sanitized: URL-safe version of stream name
        current_still_video: Path to the current still video file
        process: FFmpeg subprocess handle
        failure_count: Number of consecutive stream failures
        datetime_started: When this stream server was last started
    """
    
    def __init__(self, stream_name: str):
        """Initialize a new stream server.
        
        Args:
            stream_name: Name of the camera/stream
        """
        self.stream_name: str = stream_name
        self.stream_name_sanitized: str = stream_name.replace(' ', '_').lower()
        self.current_still_video: Optional[Path] = None
        self.process: Optional[subprocess.Popen] = None
        self.failure_count: int = 0
        self.datetime_started: Optional[datetime] = None

    def _run_server(self) -> str:
        """Start the FFmpeg RTSP streaming process.
        
        Returns:
            RTSP URL where the stream is available
            
        Raises:
            FileNotFoundError: If FFmpeg is not found
            Exception: If subprocess creation fails
            
        Note:
            FFmpeg reads from a concat file that loops infinitely (-stream_loop -1).
            The concat file itself references another concat file that can be
            dynamically updated to add new clips.
        """
        output_url = f"{RTSP_URL}/{self.stream_name_sanitized}"
        input_concat_file = PATH_CONCAT / f"{self.stream_name_sanitized}.concat"

        if not input_concat_file.exists():
            raise FileNotFoundError(f"Concat file not found: {input_concat_file}")

        ffmpeg_args = [
            'ffmpeg', *COMMON_FFMPEG_ARGS,
            '-fflags', '+igndts+genpts',
            '-re',
            '-stream_loop', '-1',
            '-f', 'concat', '-safe', '0',
            '-i', str(input_concat_file.resolve()),
            '-flush_packets', '0',
            '-c:v', 'copy', '-c:a', 'copy',
            '-f', 'rtsp',
            '-fps_mode', 'drop',
            output_url
        ]
        
        try:
            self.process = subprocess.Popen(ffmpeg_args, stdout=sys.stdout, stderr=sys.stderr)
            log.debug(f"{self.stream_name}: FFmpeg process started (PID: {self.process.pid})")
        except FileNotFoundError:
            log.error(f"{self.stream_name}: FFmpeg not found. Please ensure FFmpeg is installed and in PATH")
            raise
        except Exception as e:
            log.error(f"{self.stream_name}: failed to start FFmpeg process: {e}")
            raise
            
        return output_url

    def _make_concat_files(self) -> Path:
        """Create the main concat file that loops the next concat file.
        
        Returns:
            Path to the created main concat file
            
        Raises:
            IOError: If file creation fails
            
        Note:
            Creates a two-level concat structure:
            - Main concat file: loops and references next.concat
            - Next concat file: contains the actual video to play (updated dynamically)
            
            The 'safe 0' option is propagated to allow absolute paths.
        """
        next_concat = PATH_CONCAT / f"{self.stream_name_sanitized}_next.concat"
        concat_file = PATH_CONCAT / f"{self.stream_name_sanitized}.concat"

        try:
            # Ensure directory exists
            PATH_CONCAT.mkdir(parents=True, exist_ok=True)
            
            with open(concat_file, 'w') as f:
                f.write("ffconcat version 1.0\n")
                # Reference next concat file twice for seamless looping
                for _ in range(2):
                    f.write(f"file '{next_concat.resolve()}'\n")
                    f.write("option safe 0\n")  # Allow absolute paths
        except IOError as e:
            log.error(f"{self.stream_name}: failed to create concat file: {e}")
            raise
        except Exception as e:
            log.error(f"{self.stream_name}: unexpected error creating concat file: {e}")
            raise

        return concat_file

    def _enqueue_clip(self, video_file_name: Union[str, Path]) -> Path:
        """Add a video clip to the next concat file.
        
        Args:
            video_file_name: Path to the video file to add to the stream
            
        Returns:
            Path to the updated next concat file
            
        Raises:
            FileNotFoundError: If video file doesn't exist
            IOError: If concat file cannot be written
            
        Note:
            Overwrites the next concat file with the new video. FFmpeg's concat
            demuxer will automatically switch to the new file when it loops.
        """
        video_file_name = Path(video_file_name)
        
        # Verify video file exists
        try:
            if not video_file_name.exists():
                raise FileNotFoundError(f"Video file not found: {video_file_name}")
        except OSError as e:
            log.error(f"{self.stream_name}: error checking video file: {e}")
            raise
        
        next_concat = PATH_CONCAT / f"{self.stream_name_sanitized}_next.concat"

        try:
            with open(next_concat, 'w') as f:
                f.write("ffconcat version 1.0\n")
                f.write(f"file '{video_file_name.resolve()}'\n")
        except IOError as e:
            log.error(f"{self.stream_name}: failed to write next concat file: {e}")
            raise
        except Exception as e:
            log.error(f"{self.stream_name}: unexpected error enqueueing clip: {e}")
            raise

        return next_concat

    def add_video(self, file_name_input_video: Union[str, Path], still_only: bool=False) -> None:
        """Add a video to the stream and create a still video from its last frame.
        
        Args:
            file_name_input_video: Path to the input video
            still_only: If True, only create still video without enqueueing the
                full clip first. Used for initial stream setup (default: False)
                
        Raises:
            Exception: If still video creation fails
            FileNotFoundError: If still video file wasn't created
            
        Note:
            Process flow:
            1. Enqueue full clip (unless still_only)
            2. Start creating still video in background
            3. Wait for FFmpeg to open the full clip (if enqueued)
            4. Wait for still video creation to complete
            5. Enqueue still video
            6. Delete previous still video
        """
        try:
            file_name_input_video = Path(file_name_input_video)
            
            # Verify input video exists
            if not file_name_input_video.exists():
                raise FileNotFoundError(f"Input video not found: {file_name_input_video}")
                
            if not still_only:
                self._enqueue_clip(file_name_input_video)
        except FileNotFoundError as e:
            log.error(f"{self.stream_name}: {e}")
            raise
        except Exception as e:
            log.error(f"{self.stream_name}: error enqueueing video: {e}")
            raise

        # Create timestamped filename for still video
        dt = datetime.now()
        next_still_video = PATH_VIDEOS / f"{self.stream_name_sanitized}_still_{dt.strftime('%Y-%m-%d_%H-%M-%S-%f')}.mp4"
        try:
            # Ensure videos directory exists
            PATH_VIDEOS.mkdir(parents=True, exist_ok=True)
            
            svc = StillVideoCreator(
                file_name_input_video,
                output_duration=CONFIG['still_video_duration'],
                file_name_still_video=next_still_video
            )
            
            if not still_only:
                log.debug(f"{self.stream_name}: waiting for new video to start")
                try:
                    if self.process is None:
                        log.warning(f"{self.stream_name}: process not started, cannot wait for video to open")
                    else:
                        wait_until_file_open(file_name_input_video, self.process.pid)
                except TimeoutError as e:
                    log.warning(f"{self.stream_name}: timeout waiting for video to open: {e}")
                    # Continue anyway - video might still work
                except Exception as e:
                    log.warning(f"{self.stream_name}: error waiting for video to open: {e}")
            
            log.debug(f'{self.stream_name}: waiting for still video creation to finish')
            svc.wait()
            
            if not next_still_video.exists():
                raise FileNotFoundError(f"Still video was not created: {next_still_video}")
            
            if next_still_video.stat().st_size == 0:
                raise ValueError(f"Still video is empty: {next_still_video}")
                
            self._enqueue_clip(next_still_video)

            if self.current_still_video and not still_only:
                try:
                    self.current_still_video.unlink()
                except OSError as e:
                    log.warning(f"{self.stream_name}: failed to delete old still video: {e}")
                except Exception as e:
                    log.warning(f"{self.stream_name}: unexpected error deleting old still video: {e}")
            
            self.current_still_video = next_still_video
        except Exception as e:
            log.error(f"{self.stream_name}: Failed to create still video from {file_name_input_video}: {e}", exc_info=True)
            try:
                if next_still_video.exists():
                    next_still_video.unlink()  # Clean up failed still video
            except Exception as cleanup_err:
                log.warning(f"{self.stream_name}: failed to cleanup still video: {cleanup_err}")
            raise
    
    def is_running(self) -> bool:
        """Check if the streaming process is still running.
        
        Returns:
            True if FFmpeg process is active, False otherwise
        """
        return self.process is not None and self.process.poll() is None
    
    def close(self) -> None:
        """Stop the streaming process gracefully.
        
        Attempts graceful termination (SIGTERM) first, then forces kill (SIGKILL)
        if the process doesn't stop within 1 second.
        """
        if not self.is_running():
            log.debug(f"{self.stream_name}: process not running, nothing to close")
            return
            
        log.debug(f"{self.stream_name}: stopping stream server")
        try:
            try:
                self.process.terminate()
            except ProcessLookupError:
                log.debug(f"{self.stream_name}: process already terminated")
                return
            except Exception as e:
                log.warning(f"{self.stream_name}: error terminating process: {e}")
                return
                
            try:
                self.process.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                try:
                    self.process.kill()
                    self.process.wait()
                except Exception as e:
                    log.warning(f"{self.stream_name}: error killing process: {e}")
        except Exception as e:
            log.warning(f"{self.stream_name}: unexpected error during shutdown: {e}")

    def start_server(self, file_name_initial_video: Union[str, Path]) -> None:
        """Initialize and start the RTSP stream server.
        
        Args:
            file_name_initial_video: Path to the first video to stream
            
        Raises:
            FileNotFoundError: If initial video or FFmpeg not found
            Exception: If server initialization fails
            
        Note:
            Creates concat files, generates initial still video, and starts
            the FFmpeg RTSP streaming process.
        """
        try:
            file_name_initial_video = Path(file_name_initial_video)
            if not file_name_initial_video.exists():
                raise FileNotFoundError(f"Initial video not found: {file_name_initial_video}")
        except OSError as e:
            log.error(f"{self.stream_name}: error accessing initial video: {e}")
            raise
        
        try:
            self._make_concat_files()
        except Exception as e:
            log.error(f"{self.stream_name}: failed to create concat files: {e}")
            raise
            
        try:
            self.add_video(file_name_initial_video, still_only=True)
        except Exception as e:
            log.error(f"{self.stream_name}: failed to add initial video: {e}")
            raise
            
        try:
            url = self._run_server()
            log.info(f"{self.stream_name}: stream ready at {url}")
        except Exception as e:
            log.error(f"{self.stream_name}: failed to start server: {e}")
            raise

    
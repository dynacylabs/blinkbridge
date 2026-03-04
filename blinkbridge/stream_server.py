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

    def _run_server(self) -> str:
        """Start the FFmpeg RTSP streaming process.
        
        Returns:
            RTSP URL where the stream is available
            
        Note:
            FFmpeg reads from a concat file that loops infinitely (-stream_loop -1).
            The concat file itself references another concat file that can be
            dynamically updated to add new clips.
        """
        output_url = f"{RTSP_URL}/{self.stream_name_sanitized}"
        input_concat_file = PATH_CONCAT / f"{self.stream_name_sanitized}.concat"

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
        
        self.process = subprocess.Popen(ffmpeg_args, stdout=sys.stdout, stderr=sys.stderr)
        return output_url

    def _make_concat_files(self) -> Path:
        """Create the main concat file that loops the next concat file.
        
        Returns:
            Path to the created main concat file
            
        Note:
            Creates a two-level concat structure:
            - Main concat file: loops and references next.concat
            - Next concat file: contains the actual video to play (updated dynamically)
            
            The 'safe 0' option is propagated to allow absolute paths.
        """
        log.debug(f"{self.stream_name}: making concat file")

        next_concat = PATH_CONCAT / f"{self.stream_name_sanitized}_next.concat"
        concat_file = PATH_CONCAT / f"{self.stream_name_sanitized}.concat"

        with open(concat_file, 'w') as f:
            f.write("ffconcat version 1.0\n")
            # Reference next concat file twice for seamless looping
            for _ in range(2):
                f.write(f"file '{next_concat.resolve()}'\n")
                f.write("option safe 0\n")  # Allow absolute paths

        return concat_file

    def _enqueue_clip(self, video_file_name: Union[str, Path]) -> Path:
        """Add a video clip to the next concat file.
        
        Args:
            video_file_name: Path to the video file to add to the stream
            
        Returns:
            Path to the updated next concat file
            
        Note:
            Overwrites the next concat file with the new video. FFmpeg's concat
            demuxer will automatically switch to the new file when it loops.
        """
        log.debug(f"{self.stream_name}: enqueueing {video_file_name}")

        video_file_name = Path(video_file_name)
        next_concat = PATH_CONCAT / f"{self.stream_name_sanitized}_next.concat"

        with open(next_concat, 'w') as f:
            f.write("ffconcat version 1.0\n")
            f.write(f"file '{video_file_name.resolve()}'\n")

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
        if not still_only:
            self._enqueue_clip(file_name_input_video)

        # Create timestamped filename for still video
        dt = datetime.now()
        next_still_video = PATH_VIDEOS / f"{self.stream_name_sanitized}_still_{dt.strftime('%Y-%m-%d_%H-%M-%S-%f')}.mp4"

        log.debug(f"{self.stream_name}: creating still video {next_still_video}")
        try:
            svc = StillVideoCreator(
                file_name_input_video,
                output_duration=CONFIG['still_video_duration'],
                file_name_still_video=next_still_video
            )
            
            if not still_only:
                log.debug(f"{self.stream_name}: waiting for new video to start")
                wait_until_file_open(file_name_input_video, self.process.pid)
            
            log.debug(f'{self.stream_name}: waiting for still video creation to finish')
            svc.wait()
            
            if not next_still_video.exists():
                raise FileNotFoundError(f"Still video was not created: {next_still_video}")
                
            self._enqueue_clip(next_still_video)

            if self.current_still_video and not still_only:
                log.debug(f'{self.stream_name}: deleting old still video {self.current_still_video}')
                self.current_still_video.unlink()
            
            self.current_still_video = next_still_video
        except Exception as e:
            log.error(f"{self.stream_name}: Failed to create still video: {e}")
            if next_still_video.exists():
                next_still_video.unlink()
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
            return
            
        log.debug(f"{self.stream_name}: stopping stream server")
        try:
            self.process.terminate()
            try:
                self.process.wait(timeout=1.0)
                log.debug(f"{self.stream_name}: stream stopped gracefully")
            except subprocess.TimeoutExpired:
                log.debug(f"{self.stream_name}: forcing stream to stop")
                self.process.kill()
                self.process.wait()
        except Exception as e:
            log.debug(f"{self.stream_name}: error during shutdown: {e}")

    def start_server(self, file_name_initial_video: Union[str, Path]) -> None:
        """Initialize and start the RTSP stream server.
        
        Args:
            file_name_initial_video: Path to the first video to stream
            
        Note:
            Creates concat files, generates initial still video, and starts
            the FFmpeg RTSP streaming process.
        """
        log.debug(f"{self.stream_name}: starting server with {file_name_initial_video}")
        self._make_concat_files()
        self.add_video(file_name_initial_video, still_only=True)
        url = self._run_server()
        log.info(f"{self.stream_name}: stream ready at {url}")

    
"""FFmpeg operations for video processing and stream parameter extraction.

Provides classes for interacting with FFmpeg and FFprobe to:
- Extract stream parameters from video files
- Extract last frames from videos
- Convert images to videos with matching parameters
- Create still videos from source footage
"""
import json
import logging
import subprocess
import sys
import threading
from pathlib import Path
from typing import Dict, Optional, Tuple, Union

from blinkbridge.config import *


log = logging.getLogger(__name__)

class StreamParameters:
    """Extract audio and video stream parameters from a video file using ffprobe.
    
    Runs ffprobe as a subprocess to extract codec information, dimensions,
    frame rates, and other parameters needed to create matching output videos.
    """
    
    def __init__(self, video_file: Union[str, Path]):
        """Initialize ffprobe subprocess.
        
        Args:
            video_file: Path to the video file to analyze
        """
        ffprobe_params = [
            'ffprobe',
            '-hide_banner',
            '-loglevel', 'fatal',
            '-show_streams',
            '-print_format', 'json',
            str(video_file)
        ]
        self.process = subprocess.Popen(ffprobe_params, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def wait(self) -> Tuple[Dict, Dict]:
        """Wait for ffprobe to complete and return audio and video stream parameters.
        
        Returns:
            Tuple of (audio_params, video_params) dictionaries. If a stream
            type is not found, returns an empty dict for that type.
            
        Raises:
            Exception: If ffprobe fails to execute or parse the video file
            
        Note:
            Numeric values in returned dicts are kept as strings to preserve
            exact values from source (e.g., "30000/1001" for frame rates).
        """
        out, err = self.process.communicate()
        
        if self.process.returncode != 0:
            error_msg = err.decode('utf-8') if err else "Unknown error"
            raise Exception(f"ffprobe failed to extract parameters: {error_msg}")
        
        js = json.loads(out.decode('utf-8'), parse_float=str, parse_int=str)
        streams = js['streams']

        stream_audio = next((s for s in streams if s['codec_name'] == 'aac'), {})
        stream_video = next((s for s in streams if s['codec_name'] == 'h264'), {})
        
        if not stream_audio:
            log.warning(f"No AAC audio stream found. Available codecs: {[s.get('codec_name') for s in streams]}")
        if not stream_video:
            log.warning(f"No H264 video stream found. Available codecs: {[s.get('codec_name') for s in streams]}")

        return stream_audio, stream_video

class VideoToLastFrame:
    """Extract the last frame from a video file as an image.
    
    Uses FFmpeg to seek near the end of a video and extract a single frame
    as a JPEG image. This frame is used to create looping still videos.
    """
    
    def __init__(self, input_video: Union[str, Path], output_image: Union[str, Path]):
        """Initialize FFmpeg subprocess to extract last frame.
        
        Args:
            input_video: Path to the source video file
            output_image: Path where the extracted frame should be saved
            
        Note:
            Seeks to 1 second before the end of the video to avoid potential
            encoding issues at the very last frame.
        """
        time_offset_from_end = 1.0

        ffmpeg_params = [
            'ffmpeg', *COMMON_FFMPEG_ARGS,
            '-sseof', str(-time_offset_from_end),
            '-i', str(input_video),
            '-update', '1',  # Update output file with each frame
            '-pix_fmt', 'yuv420p',
            '-vf', 'scale=out_range=pc',  # Ensure correct color space
            '-q:v', '1',  # Highest quality JPEG
            str(output_image)
        ]
        
        self.process = subprocess.Popen(ffmpeg_params, stdout=sys.stdout, stderr=subprocess.PIPE)

    def wait(self) -> None:
        """Wait for ffmpeg to complete extraction.
        
        Raises:
            Exception: If FFmpeg fails to extract the frame
        """
        out, err = self.process.communicate()
        
        if self.process.returncode != 0:
            raise Exception(f"ffmpeg failed to extract the last frame: {err.decode('utf-8')}")
        
class FrameToVideo:
    """Convert a static image to a video file with audio.
    
    Creates a video by looping a still image and adding silent or copied audio.
    Matches the video parameters (codec, resolution, frame rate) of the source.
    """
    
    def __init__(self, 
                 image_file_name: Union[str, Path], 
                 params_video: Dict, 
                 params_audio: Dict, 
                 output_duration: float=1, 
                 file_name_output_video: Union[str, Path]="output.mp4"):
        """Initialize FFmpeg subprocess to create video from image.
        
        Args:
            image_file_name: Path to the input image file
            params_video: Video stream parameters from StreamParameters
            params_audio: Audio stream parameters from StreamParameters
            output_duration: Duration of output video in seconds (default: 1)
            file_name_output_video: Path for output video (default: "output.mp4")
            
        Note:
            If params_audio is empty, generates silent stereo audio at 44.1kHz.
        """
        time_base_denominator = params_video['time_base'].split('/')[1]
        fps_value = params_video['r_frame_rate']
        
        if params_audio:
            audio_channels = params_audio['channels']
            audio_sample_rate = params_audio['sample_rate']
        else:
            log.info("No audio stream in source, generating silent audio track")
            audio_channels = '2'
            audio_sample_rate = '44100'
        
        ffmpeg_params = [
            'ffmpeg', *COMMON_FFMPEG_ARGS,
            '-loop', '1', '-i', str(image_file_name),
            '-f', 'lavfi', '-i', f"anullsrc=channel_layout={audio_channels}:sample_rate={audio_sample_rate}",
            '-c:v', params_video['codec_name'],
            '-pix_fmt', params_video['pix_fmt'],
            '-t', str(output_duration),
            '-vf', f"scale={params_video['width']}:{params_video['height']},fps={fps_value}",
            '-b:v', params_video['bit_rate'],
            '-profile:v', params_video['profile'],
            '-level:v', params_video['level'],
            '-movflags', 'faststart',
            '-video_track_timescale', time_base_denominator,
            '-fps_mode', 'passthrough',
            '-c:a', 'aac', '-ar', audio_sample_rate, '-ac', audio_channels,
            str(file_name_output_video)
        ]

        self.process = subprocess.Popen(ffmpeg_params, stdout=sys.stdout, stderr=subprocess.PIPE)

    def wait(self) -> None:
        """Wait for ffmpeg to complete video creation.
        
        Raises:
            Exception: If FFmpeg fails to create the video
        """
        out, err = self.process.communicate()

        if self.process.returncode != 0:
            raise Exception(f"ffmpeg failed to create the video: {err.decode('utf-8')}")

class StillVideoCreator:
    """Create a still video from the last frame of a source video (runs in background thread).
    
    Combines VideoToLastFrame, StreamParameters, and FrameToVideo to create
    a looping still video that matches the source video's parameters. Runs
    asynchronously in a separate thread.
    """
    
    def __init__(self, 
                 file_name_input_video: Union[str, Path], 
                 output_duration: float=1, 
                 file_name_still_video: Union[str, Path]="output.mp4"):
        """Initialize and start still video creation in background thread.
        
        Args:
            file_name_input_video: Path to source video file
            output_duration: Duration of output still video in seconds (default: 1)
            file_name_still_video: Path for output still video (default: "output.mp4")
            
        Note:
            The creation process happens asynchronously. Call wait() to block
            until completion or check for errors.
        """
        self.exception: Optional[Exception] = None
        self.thread = threading.Thread(
            target=self._run, 
            args=(file_name_input_video, output_duration, file_name_still_video)
        )
        self.thread.start()

    def _run(self, 
             file_name_input_video: Union[str, Path], 
             output_duration: float, 
             file_name_still_video: Union[str, Path]) -> None:
        """Background thread worker that creates the still video.
        
        Args:
            file_name_input_video: Path to source video
            output_duration: Duration in seconds
            file_name_still_video: Output path
            
        Note:
            Any exceptions are stored in self.exception for retrieval by wait().
        """
        try:
            still_image_file_name = PATH_VIDEOS / 'last_frame.jpg'
            # Extract last frame from source video
            lfg = VideoToLastFrame(file_name_input_video, still_image_file_name)
            # Get stream parameters from source
            params_audio, params_video = StreamParameters(file_name_input_video).wait()
            lfg.wait()

            if not params_video:
                raise ValueError(
                    f"Failed to extract video stream (H264) from {file_name_input_video}"
                )
            
            if not params_audio:
                log.warning(f"No audio stream (AAC) found in {file_name_input_video}, will generate silent audio")

            # Convert frame to video with matching parameters
            FrameToVideo(
                still_image_file_name, params_video, params_audio,
                output_duration=output_duration,
                file_name_output_video=file_name_still_video
            ).wait()
            
            # Clean up temporary frame image
            still_image_file_name.unlink()
        except Exception as e:
            log.error(f"Error in StillVideoCreator: {e}")
            self.exception = e
    
    def wait(self) -> None:
        """Wait for the thread to complete and raise any exceptions.
        
        Raises:
            Exception: Any exception that occurred during still video creation
        """
        self.thread.join()
        if self.exception:
            raise self.exception
    
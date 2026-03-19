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
from blinkbridge.hwaccel import get_encoder


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
            
        Raises:
            FileNotFoundError: If video file doesn't exist or ffprobe not found
            Exception: If subprocess creation fails
        """
        video_file = Path(video_file)
        
        try:
            if not video_file.exists():
                raise FileNotFoundError(f"Video file not found: {video_file}")
        except OSError as e:
            log.error(f"Error checking video file: {e}")
            raise
        
        ffprobe_params = [
            'ffprobe',
            '-hide_banner',
            '-loglevel', 'fatal',
            '-show_streams',
            '-print_format', 'json',
            str(video_file)
        ]
        
        try:
            self.process = subprocess.Popen(ffprobe_params, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except FileNotFoundError:
            log.error("ffprobe not found. Please ensure FFmpeg is installed and in PATH")
            raise
        except Exception as e:
            log.error(f"Failed to start ffprobe: {e}")
            raise

    def wait(self) -> Tuple[Dict, Dict]:
        """Wait for ffprobe to complete and return audio and video stream parameters.
        
        Returns:
            Tuple of (audio_params, video_params) dictionaries. If a stream
            type is not found, returns an empty dict for that type.
            
        Raises:
            Exception: If ffprobe fails to execute or parse the video file
            json.JSONDecodeError: If ffprobe output is not valid JSON
            
        Note:
            Numeric values in returned dicts are kept as strings to preserve
            exact values from source (e.g., "30000/1001" for frame rates).
        """
        try:
            out, err = self.process.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            log.error("ffprobe timed out while analyzing video")
            self.process.kill()
            out, err = self.process.communicate()
            raise Exception("ffprobe timed out after 30 seconds")
        except Exception as e:
            log.error(f"Error communicating with ffprobe: {e}")
            raise
        
        if self.process.returncode != 0:
            error_msg = err.decode('utf-8', errors='replace') if err else "Unknown error"
            log.error(f"ffprobe failed (exit code {self.process.returncode}): {error_msg}")
            raise Exception(f"ffprobe failed to extract parameters: {error_msg}")
        
        try:
            js = json.loads(out.decode('utf-8'), parse_float=str, parse_int=str)
        except json.JSONDecodeError as e:
            log.error(f"Failed to parse ffprobe output as JSON: {e}")
            raise
        except UnicodeDecodeError as e:
            log.error(f"Failed to decode ffprobe output: {e}")
            raise Exception("Failed to decode ffprobe output")
        
        try:
            streams = js.get('streams', [])
            if not streams:
                log.debug("No streams found in video file")
                return {}, {}

            stream_audio = next((s for s in streams if s.get('codec_name') == 'aac'), {})
            stream_video = next((s for s in streams if s.get('codec_name') == 'h264'), {})
            
            if not stream_audio:
                log.debug(f"No AAC audio stream found. Available codecs: {[s.get('codec_name') for s in streams]}")
            if not stream_video:
                log.debug(f"No H264 video stream found. Available codecs: {[s.get('codec_name') for s in streams]}")

            return stream_audio, stream_video
        except Exception as e:
            log.error(f"Error parsing stream data: {e}")
            raise

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
            
        Raises:
            FileNotFoundError: If input video doesn't exist or FFmpeg not found
            Exception: If subprocess creation fails
            
        Note:
            Seeks to 1 second before the end of the video to avoid potential
            encoding issues at the very last frame.
        """
        input_video = Path(input_video)
        output_image = Path(output_image)
        
        try:
            if not input_video.exists():
                raise FileNotFoundError(f"Input video not found: {input_video}")
        except OSError as e:
            log.error(f"Error checking input video: {e}")
            raise
            
        try:
            # Ensure output directory exists
            output_image.parent.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            log.error(f"Failed to create output directory: {e}")
            raise
        
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
        
        try:
            log.debug(f"FFmpeg command: {' '.join(ffmpeg_params)}")
            self.process = subprocess.Popen(ffmpeg_params, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except FileNotFoundError:
            log.error("FFmpeg not found. Please ensure FFmpeg is installed and in PATH")
            raise
        except Exception as e:
            log.error(f"Failed to start FFmpeg for frame extraction: {e}")
            raise

    def wait(self) -> None:
        """Wait for ffmpeg to complete extraction.
        
        Raises:
            Exception: If FFmpeg fails to extract the frame
            subprocess.TimeoutExpired: If extraction takes too long
        """
        try:
            out, err = self.process.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            log.error("FFmpeg timed out while extracting frame")
            self.process.kill()
            out, err = self.process.communicate()
            raise Exception("Frame extraction timed out after 30 seconds")
        except Exception as e:
            log.error(f"Error communicating with FFmpeg: {e}")
            raise
        
        if self.process.returncode != 0:
            stdout_msg = out.decode('utf-8', errors='replace') if out else ""
            stderr_msg = err.decode('utf-8', errors='replace') if err else ""
            error_msg = stderr_msg or stdout_msg or "Unknown error (no output captured)"
            log.error(f"FFmpeg failed to extract frame (exit code {self.process.returncode})")
            if error_msg:
                log.error(f"FFmpeg output: {error_msg}")
            raise Exception(f"ffmpeg failed to extract the last frame: {error_msg}")
        
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
            
        Raises:
            FileNotFoundError: If image file doesn't exist or FFmpeg not found
            ValueError: If required parameters are missing
            Exception: If subprocess creation fails
            
        Note:
            If params_audio is empty, generates silent stereo audio at 44.1kHz.
        """
        image_file_name = Path(image_file_name)
        file_name_output_video = Path(file_name_output_video)
        
        try:
            if not image_file_name.exists():
                raise FileNotFoundError(f"Image file not found: {image_file_name}")
        except OSError as e:
            log.error(f"Error checking image file: {e}")
            raise
            
        try:
            # Validate required video parameters
            required_video_params = ['time_base', 'r_frame_rate', 'codec_name', 'pix_fmt', 
                                    'width', 'height', 'bit_rate', 'profile', 'level']
            missing_params = [p for p in required_video_params if p not in params_video]
            if missing_params:
                raise ValueError(f"Missing required video parameters: {missing_params}")
            
            time_base_denominator = params_video['time_base'].split('/')[1]
            fps_value = params_video['r_frame_rate']
        except (KeyError, IndexError, ValueError) as e:
            log.error(f"Invalid video parameters: {e}")
            raise ValueError(f"Invalid video parameters: {e}")
        
        try:
            if params_audio:
                audio_channels = params_audio.get('channels', '2')
                audio_sample_rate = params_audio.get('sample_rate', '44100')
            else:
                log.debug("No audio stream in source, will generate silent audio track")
                audio_channels = '2'
                audio_sample_rate = '44100'
        except Exception as e:
            log.warning(f"Error processing audio parameters, using defaults: {e}")
            audio_channels = '2'
            audio_sample_rate = '44100'
        
        try:
            # Ensure output directory exists
            file_name_output_video.parent.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            log.error(f"Failed to create output directory: {e}")
            raise
        
        encoder = get_encoder()
        vf_string = encoder.build_video_filter(
            params_video['width'], params_video['height'], fps_value
        )
        encode_args = encoder.build_encode_args(params_video)

        ffmpeg_params = [
            'ffmpeg', *COMMON_FFMPEG_ARGS,
            *encoder.init_args,
            '-loop', '1', '-i', str(image_file_name),
            '-f', 'lavfi', '-i', f"anullsrc=channel_layout={audio_channels}:sample_rate={audio_sample_rate}",
            *encode_args,
            '-t', str(output_duration),
            '-vf', vf_string,
            '-movflags', 'faststart',
            '-video_track_timescale', time_base_denominator,
            '-fps_mode', 'passthrough',
            '-c:a', 'aac', '-ar', audio_sample_rate, '-ac', audio_channels,
            str(file_name_output_video)
        ]

        try:
            log.debug(f"FFmpeg command: {' '.join(ffmpeg_params)}")
            self.process = subprocess.Popen(ffmpeg_params, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except FileNotFoundError:
            log.error("FFmpeg not found. Please ensure FFmpeg is installed and in PATH")
            raise
        except Exception as e:
            log.error(f"Failed to start FFmpeg for video creation: {e}")
            raise

    def wait(self) -> None:
        """Wait for ffmpeg to complete video creation.
        
        Raises:
            Exception: If FFmpeg fails to create the video
            subprocess.TimeoutExpired: If video creation takes too long
        """
        try:
            out, err = self.process.communicate(timeout=60)
        except subprocess.TimeoutExpired:
            log.error("FFmpeg timed out while creating video")
            self.process.kill()
            out, err = self.process.communicate()
            raise Exception("Video creation timed out after 60 seconds")
        except Exception as e:
            log.error(f"Error communicating with FFmpeg: {e}")
            raise

        if self.process.returncode != 0:
            stdout_msg = out.decode('utf-8', errors='replace') if out else ""
            stderr_msg = err.decode('utf-8', errors='replace') if err else ""
            error_msg = stderr_msg or stdout_msg or "Unknown error (no output captured)"
            log.error(f"FFmpeg failed to create video (exit code {self.process.returncode})")
            if error_msg:
                log.error(f"FFmpeg output: {error_msg}")
            raise Exception(f"ffmpeg failed to create the video: {error_msg}")

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
        still_image_file_name = None
        try:
            log.debug(f"Creating still video from {file_name_input_video}")
            still_image_file_name = PATH_VIDEOS / 'last_frame.jpg'
            # Extract last frame from source video
            lfg = VideoToLastFrame(file_name_input_video, still_image_file_name)
            # Get stream parameters from source
            params_audio, params_video = StreamParameters(file_name_input_video).wait()
            
            # Log parameters for debugging
            log.debug(f"Video parameters: {params_video}")
            log.debug(f"Audio parameters: {params_audio}")
            
            # Wait for frame extraction to complete
            lfg.wait()
            
            # Verify frame was extracted
            if not still_image_file_name.exists():
                raise FileNotFoundError(f"Frame extraction failed: {still_image_file_name} not created")
            
            if still_image_file_name.stat().st_size == 0:
                raise ValueError(f"Extracted frame is empty: {still_image_file_name}")

            if not params_video:
                raise ValueError(
                    f"Failed to extract video stream (H264) from {file_name_input_video}"
                )
            
            if not params_audio:
                log.debug(f"No audio stream (AAC) found in {file_name_input_video}, will generate silent audio")

            # Convert frame to video with matching parameters
            FrameToVideo(
                still_image_file_name, params_video, params_audio,
                output_duration=output_duration,
                file_name_output_video=file_name_still_video
            ).wait()
            
            # Verify still video was created
            if not Path(file_name_still_video).exists():
                raise FileNotFoundError(f"Still video creation failed: {file_name_still_video} not created")
            
            if Path(file_name_still_video).stat().st_size == 0:
                raise ValueError(f"Still video is empty: {file_name_still_video}")
            
            # Clean up temporary frame image
            try:
                still_image_file_name.unlink()
            except Exception:
                pass  # Silently ignore cleanup failures for temp file
                
        except FileNotFoundError as e:
            log.error(f"File not found in StillVideoCreator: {e}")
            self.exception = e
        except ValueError as e:
            log.error(f"Value error in StillVideoCreator: {e}")
            self.exception = e
        except Exception as e:
            log.error(f"Error in StillVideoCreator: {e}", exc_info=True)
            self.exception = e
        finally:
            # Cleanup temporary frame on error
            if self.exception and still_image_file_name:
                try:
                    if still_image_file_name.exists():
                        still_image_file_name.unlink()
                except Exception:
                    pass  # Silently ignore cleanup failures
    
    def wait(self) -> None:
        """Wait for the thread to complete and raise any exceptions.
        
        Raises:
            Exception: Any exception that occurred during still video creation
        """
        self.thread.join()
        if self.exception:
            raise self.exception
    
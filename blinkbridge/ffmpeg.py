import subprocess
import json
from pathlib import Path
from typing import Dict, Optional, Tuple, Union
import threading
import sys
import logging
from blinkbridge.config import *


log = logging.getLogger(__name__)


def _find_system_font() -> Optional[str]:
    """Return the path to a usable TTF font, or None if none found."""
    candidates = [
        '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf',  # Ubuntu/Debian
        '/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf',            # Alpine font-dejavu
        '/usr/share/fonts/TTF/DejaVuSans-Bold.ttf',               # Arch
        '/usr/share/fonts/dejavu-sans/DejaVuSans-Bold.ttf',       # some distros
        '/usr/share/fonts/truetype/freefont/FreeSansBold.ttf',    # Ubuntu ttf-freefont
        '/usr/share/fonts/freefont/FreeSansBold.ttf',             # Alpine ttf-freefont
    ]
    for path in candidates:
        if Path(path).exists():
            return path
    return None


def generate_placeholder_video(
    output_path: Union[str, Path],
    text: str,
    bg_color: str = 'black',
    text_color: str = 'white',
    width: int = 1920,
    height: int = 1080,
    fps: int = 15,
    duration: float = 0.5,
) -> bool:
    """Generate a solid-colour placeholder video with centred text overlay.

    Args:
        output_path: Destination file path for the generated video.
        text: Text to overlay on the video.
        bg_color: Background colour name recognised by FFmpeg (e.g. 'black', 'gray').
        text_color: Text colour name recognised by FFmpeg.
        width: Video width in pixels.
        height: Video height in pixels.
        fps: Frames per second.
        duration: Video duration in seconds.

    Returns:
        True if the video was created successfully, False otherwise.
    """
    output_path = Path(output_path)
    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        log.error(f"generate_placeholder_video: cannot create output directory: {e}")
        return False

    font_path = _find_system_font()
    font_filter = ''
    if font_path:
        log.debug(f"generate_placeholder_video: using font {font_path}")
        font_size = height // 10
        safe_text = text.replace("'", "\\\\'").replace(':', '\\:')
        font_filter = (
            f",drawtext=fontfile='{font_path}':text='{safe_text}'"
            f":fontcolor={text_color}:fontsize={font_size}"
            f":x=(w-text_w)/2:y=(h-text_h)/2"
        )
    else:
        log.warning("generate_placeholder_video: no system font found, skipping text overlay")

    vf = f"color={bg_color}:s={width}x{height}:d={duration}:r={fps}{font_filter}"

    cmd = [
        'ffmpeg', *COMMON_FFMPEG_ARGS,
        '-f', 'lavfi', '-i', vf,
        '-f', 'lavfi', '-i', 'anullsrc=channel_layout=stereo:sample_rate=44100',
        '-c:v', 'libx264', '-profile:v', 'high', '-level:v', '4.1',
        '-pix_fmt', 'yuv420p', '-movflags', 'faststart',
        '-c:a', 'aac', '-ar', '44100', '-ac', '2', '-b:a', '128k',
        '-t', str(duration),
        str(output_path),
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, timeout=30)
    except subprocess.TimeoutExpired:
        log.error(f"generate_placeholder_video: FFmpeg timed out for '{text}'")
        return False
    except FileNotFoundError:
        log.error("generate_placeholder_video: FFmpeg not found in PATH")
        return False
    except Exception as e:
        log.error(f"generate_placeholder_video: unexpected error: {e}")
        return False

    if result.returncode != 0:
        stderr = result.stderr.decode('utf-8', errors='replace')
        log.error(f"generate_placeholder_video: FFmpeg failed (rc={result.returncode}): {stderr}")
        return False

    if not output_path.exists():
        log.error(f"generate_placeholder_video: output not created at {output_path}")
        return False

    log.debug(f"generate_placeholder_video: created '{text}' placeholder at {output_path}")
    return True

class StreamParameters:
    def __init__(self, video_file: Union[str, Path]):
        ffprobe_params = [
            'ffprobe',
            '-hide_banner',
            '-loglevel', 'fatal',
            '-show_streams',
            '-print_format', 'json',
            video_file
        ]

        self.process = subprocess.Popen(ffprobe_params, stdout=subprocess.PIPE)

    def wait(self) -> Tuple[Dict, Dict]:
        out, err = self.process.communicate()
        
        if self.process.returncode != 0:
            raise Exception("ffprobe failed to extract parameters: " + err.decode('utf-8'))
        
        # convert json but keep floats and ints as strings
        js = json.loads(out.decode('utf-8'), parse_float=lambda x: x, parse_int=lambda x: x)
        js = js['streams']

        stream_audio = next((s for s in js if s['codec_name'] == 'aac'), {})
        stream_video = next((s for s in js if s['codec_name'] == 'h264'), {})

        return stream_audio, stream_video

class VideoToLastFrame:
    def __init__(self, input_video: Union[str, Path], output_image: Union[str, Path]):
        time_offset_from_end = 1.0

        ffmpeg_params = [
            'ffmpeg',
            *COMMON_FFMPEG_ARGS,
            '-sseof', str(-time_offset_from_end),
            '-i', input_video,
            '-update', '1',
            '-pix_fmt', 'yuv420p',
            '-vf', 'scale=out_range=pc',  # HACK
            '-q:v', '1',
            output_image
        ]
        
        self.process = subprocess.Popen(ffmpeg_params, stdout=sys.stdout, stderr=subprocess.PIPE)

    def wait(self) -> None:
        out, err = self.process.communicate()
        
        if self.process.returncode != 0:
            raise Exception("ffmpeg failed to extract the last frame: " + err.decode('utf-8'))
        
class FrameToVideo:
    def __init__(self, 
                 image_file_name: Union[str, Path], 
                 params_video: Dict, 
                 params_audio: Dict, 
                 output_duration: float=1, 
                 file_name_output_video: Union[str, Path]="output.mp4"):
        time_base_denominator = params_video['time_base'].split('/')[1] # cut off "1/"
        fps_value = params_video['r_frame_rate']
        
        # Create the ffmpeg parameters list
        ffmpeg_params = [
            'ffmpeg',
            *COMMON_FFMPEG_ARGS,
            '-loop', '1',
            '-i', image_file_name,   
            '-f', 'lavfi',
            '-i', f"anullsrc=channel_layout={params_audio['channels']}:sample_rate={params_audio['sample_rate']}",
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
            '-c:a', 'aac',
            '-ar', params_audio['sample_rate'],
            '-ac', params_audio['channels'],
            file_name_output_video
        ]    

        # Create the video using ffmpeg
        self.process = subprocess.Popen(ffmpeg_params, stdout=sys.stdout, stderr=subprocess.PIPE)

    def wait(self) -> None:
        out, err = self.process.communicate()

        if self.process.returncode != 0:
            raise Exception(f"ffmpeg failed to create the video: {err.decode('utf-8')}")

class StillVideoCreator:
    def __init__(self, 
                 file_name_input_video: Union[str, Path], 
                 output_duration: float=1, 
                 file_name_still_video: Union[str, Path]="output.mp4"):
        self.thread = threading.Thread(target=self._run, 
                                       args=(file_name_input_video, output_duration, file_name_still_video))
        self.thread.start() 

    def _run(self, 
             file_name_input_video: Union[str, Path], 
             output_duration: float=1, 
             file_name_still_video: Union[str, Path]="output.mp4") -> None:
        still_image_file_name = PATH_VIDEOS / 'last_frame.jpg'
        lfg = VideoToLastFrame(file_name_input_video, still_image_file_name) # run in background
        params_audio, params_video = StreamParameters(file_name_input_video).wait()
        lfg.wait()

        assert all((params_audio, params_video))

        # convert to video
        FrameToVideo(still_image_file_name, params_video, params_audio,
                    output_duration=output_duration,
                    file_name_output_video=file_name_still_video).wait()
        
        # remove temporary file
        still_image_file_name.unlink()
        
    def wait(self) -> None:
        self.thread.join()
    
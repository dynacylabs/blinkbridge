import subprocess
import json
from pathlib import Path
from typing import Dict, Tuple, Union
import threading
import sys
import logging
from blinkbridge.config import *


log = logging.getLogger(__name__)

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
        
        if not stream_audio:
            log.warning(f"No AAC audio stream found in video. Available codecs: {[s.get('codec_name') for s in js]}")
        if not stream_video:
            log.warning(f"No H264 video stream found in video. Available codecs: {[s.get('codec_name') for s in js]}")

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
        
        # Use provided audio params or default to stereo 44.1kHz if no audio stream
        if params_audio:
            audio_channels = params_audio['channels']
            audio_sample_rate = params_audio['sample_rate']
        else:
            log.info("No audio stream in source, generating silent audio track")
            audio_channels = '2'
            audio_sample_rate = '44100'
        
        # Create the ffmpeg parameters list
        ffmpeg_params = [
            'ffmpeg',
            *COMMON_FFMPEG_ARGS,
            '-loop', '1',
            '-i', image_file_name,   
            '-f', 'lavfi',
            '-i', f"anullsrc=channel_layout={audio_channels}:sample_rate={audio_sample_rate}",
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
            '-ar', audio_sample_rate,
            '-ac', audio_channels,
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
        self.exception = None
        self.thread = threading.Thread(target=self._run, 
                                       args=(file_name_input_video, output_duration, file_name_still_video))
        self.thread.start() 

    def _run(self, 
             file_name_input_video: Union[str, Path], 
             output_duration: float=1, 
             file_name_still_video: Union[str, Path]="output.mp4") -> None:
        try:
            still_image_file_name = PATH_VIDEOS / 'last_frame.jpg'
            lfg = VideoToLastFrame(file_name_input_video, still_image_file_name) # run in background
            params_audio, params_video = StreamParameters(file_name_input_video).wait()
            lfg.wait()

            # Video stream is required, audio is optional
            if not params_video:
                error_msg = f"Failed to extract video stream (H264) from {file_name_input_video}. "
                log.error(error_msg)
                raise ValueError(error_msg)
            
            if not params_audio:
                log.warning(f"No audio stream (AAC) found in {file_name_input_video}, will generate silent audio")

            # convert to video
            FrameToVideo(still_image_file_name, params_video, params_audio,
                        output_duration=output_duration,
                        file_name_output_video=file_name_still_video).wait()
            
            # remove temporary file
            still_image_file_name.unlink()
        except Exception as e:
            log.error(f"Error in StillVideoCreator: {e}")
            self.exception = e
        
    def wait(self) -> None:
        self.thread.join()
        if self.exception:
            raise self.exception
    
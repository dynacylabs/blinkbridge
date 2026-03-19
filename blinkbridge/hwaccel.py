"""Hardware acceleration detection and encoder management for FFmpeg.

Provides automatic detection of available H.264 hardware encoders, with
fallback to software encoding. Supports NVENC, QSV, VAAPI, V4L2M2M,
and VideoToolbox acceleration APIs.

Note:
    Hardware encoder support is UNTESTED due to lack of GPU hardware during
    development. The detection and fallback logic follows standard FFmpeg
    patterns, and software encoding (libx264) is always the safe fallback.
    Community testing and bug reports for specific GPU/driver combinations
    are welcome.
"""
import logging
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from blinkbridge.config import COMMON_FFMPEG_ARGS, CONFIG


log = logging.getLogger(__name__)


@dataclass
class EncoderConfig:
    """Configuration for an FFmpeg H.264 encoder.

    Attributes:
        name: FFmpeg encoder name (e.g. 'h264_nvenc', 'libx264')
        is_hardware: Whether this is a hardware-accelerated encoder
        priority: Selection priority (lower = more preferred)
        init_args: FFmpeg arguments placed before inputs (device init)
        needs_hwupload: Whether the filter chain must include hwupload
        hw_pixel_format: Pixel format for hwupload filter
        supports_profile_level: Whether -profile:v and -level:v are supported
    """
    name: str
    is_hardware: bool
    priority: int
    init_args: List[str] = field(default_factory=list)
    needs_hwupload: bool = False
    hw_pixel_format: str = 'nv12'
    supports_profile_level: bool = True

    def build_video_filter(self, width: str, height: str, fps: str) -> str:
        """Build the -vf filter string for this encoder.

        Args:
            width: Output video width
            height: Output video height
            fps: Output frame rate (e.g. '30' or '30000/1001')

        Returns:
            Complete filter string for -vf argument
        """
        base_filter = f"scale={width}:{height},fps={fps}"
        if self.needs_hwupload:
            return f"{base_filter},format={self.hw_pixel_format},hwupload"
        return base_filter

    def build_encode_args(self, params_video: Dict) -> List[str]:
        """Build encoder-specific FFmpeg arguments from source video parameters.

        Args:
            params_video: Video stream parameters dict from StreamParameters

        Returns:
            List of FFmpeg arguments for the video encoder
        """
        args = ['-c:v', self.name]

        if not self.needs_hwupload:
            pix_fmt = params_video.get('pix_fmt', 'yuv420p')
            args.extend(['-pix_fmt', pix_fmt])

        if 'bit_rate' in params_video:
            args.extend(['-b:v', params_video['bit_rate']])

        if self.supports_profile_level:
            if 'profile' in params_video:
                args.extend(['-profile:v', params_video['profile']])
            if 'level' in params_video:
                args.extend(['-level:v', params_video['level']])

        return args

    def build_simple_encode_args(self, pix_fmt: str = 'yuv420p',
                                  profile: str = 'high',
                                  level: str = '4.1') -> List[str]:
        """Build encoder arguments for simple encodes (e.g. black placeholder).

        Args:
            pix_fmt: Pixel format (default: yuv420p)
            profile: H.264 profile (default: high)
            level: H.264 level (default: 4.1)

        Returns:
            List of FFmpeg arguments
        """
        args = ['-c:v', self.name]

        if not self.needs_hwupload:
            args.extend(['-pix_fmt', pix_fmt])

        if self.supports_profile_level:
            args.extend(['-profile:v', profile, '-level:v', level])

        return args

    def build_simple_video_filter(self) -> Optional[str]:
        """Build filter string for simple encodes that don't need scale/fps.

        Returns:
            Filter string if hwupload is needed, None otherwise
        """
        if self.needs_hwupload:
            return f"format={self.hw_pixel_format},hwupload"
        return None


def _make_encoder_configs() -> Dict[str, EncoderConfig]:
    """Build the encoder config dictionary, applying user config overrides."""
    vaapi_device = CONFIG.get('ffmpeg', {}).get('vaapi_device', '/dev/dri/renderD128')

    return {
        'h264_nvenc': EncoderConfig(
            name='h264_nvenc',
            is_hardware=True,
            priority=1,
        ),
        'h264_qsv': EncoderConfig(
            name='h264_qsv',
            is_hardware=True,
            priority=2,
            init_args=['-init_hw_device', f'qsv=hw:{vaapi_device}',
                       '-filter_hw_device', 'hw'],
            needs_hwupload=True,
        ),
        'h264_vaapi': EncoderConfig(
            name='h264_vaapi',
            is_hardware=True,
            priority=3,
            init_args=['-vaapi_device', vaapi_device],
            needs_hwupload=True,
            supports_profile_level=False,
        ),
        'h264_videotoolbox': EncoderConfig(
            name='h264_videotoolbox',
            is_hardware=True,
            priority=4,
            supports_profile_level=True,
        ),
        'h264_v4l2m2m': EncoderConfig(
            name='h264_v4l2m2m',
            is_hardware=True,
            priority=5,
            supports_profile_level=False,
        ),
        'libx264': EncoderConfig(
            name='libx264',
            is_hardware=False,
            priority=100,
        ),
    }


def _get_available_encoders() -> set:
    """Query FFmpeg for compiled-in H.264 encoders.

    Returns:
        Set of encoder names that FFmpeg reports as available
    """
    try:
        result = subprocess.run(
            ['ffmpeg', '-hide_banner', '-encoders'],
            capture_output=True, timeout=10
        )
        output = result.stdout.decode('utf-8', errors='replace')
        available = set()
        for name in _make_encoder_configs():
            if f' {name} ' in output:
                available.add(name)
        return available
    except Exception as e:
        log.warning(f"Failed to query FFmpeg encoders: {e}")
        return {'libx264'}


def _test_encoder(encoder: EncoderConfig) -> bool:
    """Verify an encoder works by running a minimal test encode.

    Args:
        encoder: Encoder configuration to test

    Returns:
        True if the test encode succeeded
    """
    with tempfile.NamedTemporaryFile(suffix='.mp4', delete=True) as tmp:
        cmd = ['ffmpeg', *COMMON_FFMPEG_ARGS]
        cmd.extend(encoder.init_args)
        cmd.extend(['-f', 'lavfi', '-i', 'color=black:s=64x64:d=0.1'])

        vf = encoder.build_simple_video_filter()
        if vf:
            cmd.extend(['-vf', vf])

        cmd.extend(['-c:v', encoder.name, '-frames:v', '1', tmp.name])

        try:
            result = subprocess.run(cmd, capture_output=True, timeout=15)
            if result.returncode == 0:
                return True
            stderr = result.stderr.decode('utf-8', errors='replace')
            log.debug(f"Test encode failed for {encoder.name}: {stderr[:200]}")
            return False
        except subprocess.TimeoutExpired:
            log.debug(f"Test encode timed out for {encoder.name}")
            return False
        except FileNotFoundError:
            log.debug(f"FFmpeg not found during encoder test")
            return False
        except Exception as e:
            log.debug(f"Test encode error for {encoder.name}: {e}")
            return False


def detect_encoder(preferred: str = 'auto') -> EncoderConfig:
    """Detect the best available H.264 encoder.

    Args:
        preferred: Encoder name to use, or 'auto' for automatic detection

    Returns:
        EncoderConfig for the best working encoder
    """
    encoders = _make_encoder_configs()

    if preferred != 'auto':
        if preferred in encoders:
            encoder = encoders[preferred]
            if _test_encoder(encoder):
                log.info(f"Using requested encoder: {encoder.name}")
                return encoder
            log.warning(
                f"Requested encoder '{preferred}' failed test encode, "
                f"falling back to auto-detection"
            )
        else:
            log.warning(f"Unknown encoder '{preferred}', falling back to auto-detection")

    available = _get_available_encoders()
    log.debug(f"FFmpeg reports available H.264 encoders: {available}")

    candidates = sorted(
        [encoders[name] for name in available if name in encoders],
        key=lambda e: e.priority
    )

    for encoder in candidates:
        if encoder.name == 'libx264':
            log.info("Using software encoder: libx264")
            return encoder

        log.debug(f"Testing hardware encoder: {encoder.name}...")
        if _test_encoder(encoder):
            log.info(f"Using hardware encoder: {encoder.name}")
            return encoder
        log.debug(f"Encoder {encoder.name} not available, trying next")

    log.info("No hardware encoder available, using software encoder: libx264")
    return encoders['libx264']


# Module-level selected encoder
_selected_encoder: Optional[EncoderConfig] = None


def init_encoder(preferred: str = 'auto') -> EncoderConfig:
    """Detect and cache the best encoder at application startup.

    Args:
        preferred: Encoder name or 'auto'

    Returns:
        The selected EncoderConfig
    """
    global _selected_encoder
    _selected_encoder = detect_encoder(preferred)
    return _selected_encoder


def get_encoder() -> EncoderConfig:
    """Get the currently selected encoder.

    Returns:
        Cached EncoderConfig, or libx264 if not yet initialized
    """
    if _selected_encoder is None:
        return _make_encoder_configs()['libx264']
    return _selected_encoder

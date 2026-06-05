from __future__ import annotations

from dataclasses import dataclass

from tune_server.models import AudioFormat


@dataclass
class AudioCapabilities:
    formats: set[AudioFormat]
    max_sample_rate: int
    max_bit_depth: int
    supports_gapless: bool = False
    max_channels: int = 2


# Common capability profiles
DLNA_CAPABILITIES = AudioCapabilities(
    formats={AudioFormat.FLAC, AudioFormat.WAV, AudioFormat.MP3, AudioFormat.AAC},
    max_sample_rate=192000,
    max_bit_depth=24,
    supports_gapless=True,  # via SetNextAVTransportURI
)

AIRPLAY_CAPABILITIES = AudioCapabilities(
    formats={AudioFormat.ALAC, AudioFormat.AAC},
    max_sample_rate=44100,
    max_bit_depth=16,
    supports_gapless=True,  # pre-transcode next track WAV to minimize gap
)

CHROMECAST_CAPABILITIES = AudioCapabilities(
    formats={AudioFormat.FLAC, AudioFormat.WAV, AudioFormat.MP3, AudioFormat.AAC, AudioFormat.OGG},
    max_sample_rate=96000,
    max_bit_depth=24,
    supports_gapless=True,
)

BLUOS_CAPABILITIES = AudioCapabilities(
    formats={AudioFormat.FLAC, AudioFormat.WAV, AudioFormat.MP3, AudioFormat.AAC, AudioFormat.OGG},
    max_sample_rate=192000,
    max_bit_depth=24,
    supports_gapless=False,
)

SQUEEZEBOX_CAPABILITIES = AudioCapabilities(
    formats={AudioFormat.FLAC, AudioFormat.WAV, AudioFormat.MP3, AudioFormat.AAC, AudioFormat.OGG},
    max_sample_rate=192000,
    max_bit_depth=24,
    supports_gapless=True,
)

LOCAL_CAPABILITIES = AudioCapabilities(
    formats={AudioFormat.WAV},  # sounddevice only accepts raw PCM; always decode
    max_sample_rate=384000,
    max_bit_depth=32,
    supports_gapless=True,
)


def format_from_extension(ext: str) -> AudioFormat | None:
    ext = ext.lower().lstrip(".")
    mapping = {
        "flac": AudioFormat.FLAC,
        "mp3": AudioFormat.MP3,
        "wav": AudioFormat.WAV,
        "m4a": AudioFormat.AAC,
        "aac": AudioFormat.AAC,
        "ogg": AudioFormat.OGG,
        "opus": AudioFormat.OPUS,
        "aiff": AudioFormat.AIFF,
        "aif": AudioFormat.AIFF,
        "alac": AudioFormat.ALAC,
        "wv": AudioFormat.WAVPACK,
        "ape": AudioFormat.APE,
        "wma": AudioFormat.WMA,
        "dsf": AudioFormat.DSD,
        "dff": AudioFormat.DSD,
        "dst": AudioFormat.DSD,
    }
    return mapping.get(ext)


def can_passthrough(
    source_format: AudioFormat,
    source_sample_rate: int,
    source_bit_depth: int,
    target_caps: AudioCapabilities,
    source_channels: int = 2,
) -> bool:
    if source_format not in target_caps.formats:
        return False
    if source_channels > target_caps.max_channels:
        return False
    # DSD is 1-bit at MHz rates — skip normal rate/depth checks
    if source_format == AudioFormat.DSD:
        return True
    if source_sample_rate > target_caps.max_sample_rate:
        return False
    if source_bit_depth > target_caps.max_bit_depth:
        return False
    return True


def choose_output_format(
    source_format: AudioFormat,
    target_caps: AudioCapabilities,
) -> AudioFormat:
    # Prefer lossless formats
    preference = [AudioFormat.FLAC, AudioFormat.WAV, AudioFormat.ALAC,
                  AudioFormat.AAC, AudioFormat.MP3]
    for fmt in preference:
        if fmt in target_caps.formats:
            return fmt
    # Fallback to first available
    return next(iter(target_caps.formats))


def ffmpeg_format_arg(fmt: AudioFormat) -> str:
    mapping = {
        AudioFormat.FLAC: "flac",
        AudioFormat.WAV: "wav",
        AudioFormat.MP3: "mp3",
        AudioFormat.AAC: "adts",
        AudioFormat.ALAC: "ipod",
        AudioFormat.OGG: "ogg",
        AudioFormat.OPUS: "opus",
        AudioFormat.AIFF: "aiff",
    }
    return mapping.get(fmt, "flac")


def ffmpeg_codec_arg(fmt: AudioFormat, bit_depth: int = 16) -> str:
    if fmt == AudioFormat.WAV:
        if bit_depth <= 16:
            return "pcm_s16le"
        elif bit_depth <= 24:
            return "pcm_s24le"
        return "pcm_s32le"
    if fmt == AudioFormat.AIFF:
        if bit_depth <= 16:
            return "pcm_s16be"
        elif bit_depth <= 24:
            return "pcm_s24be"
        return "pcm_s32be"
    mapping = {
        AudioFormat.FLAC: "flac",
        AudioFormat.MP3: "libmp3lame",
        AudioFormat.AAC: "aac",
        AudioFormat.ALAC: "alac",
        AudioFormat.OGG: "libvorbis",
        AudioFormat.OPUS: "libopus",
    }
    return mapping.get(fmt, "flac")


def mime_type_for_format(fmt: AudioFormat) -> str:
    mapping = {
        AudioFormat.FLAC: "audio/flac",
        AudioFormat.WAV: "audio/wav",
        AudioFormat.MP3: "audio/mpeg",
        AudioFormat.AAC: "audio/aac",
        AudioFormat.ALAC: "audio/mp4",
        AudioFormat.OGG: "audio/ogg",
        AudioFormat.OPUS: "audio/opus",
        AudioFormat.AIFF: "audio/aiff",
        AudioFormat.DSD: "application/x-dsd",
        AudioFormat.WAVPACK: "audio/x-wavpack",
        AudioFormat.APE: "audio/x-ape",
    }
    return mapping.get(fmt, "application/octet-stream")


# MIME types that indicate DSD/DSF/DFF support in DLNA sink protocols
DSD_MIME_TYPES = {
    "application/x-dsd",
    "audio/dsd",
    "audio/x-dsd",
    "audio/dsf",
    "audio/x-dsf",
    "audio/dff",
    "audio/x-dff",
    "audio/dst",
    "audio/x-dst",
    "audio/l24",
}


# Known DSD-capable device name/model patterns (case-insensitive)
# These devices support native DSF/DFF but often don't report it via GetProtocolInfo
_DSD_CAPABLE_PATTERNS = [
    "dmp-a",      # Eversolo DMP-A8, DMP-A6
    "eversolo",
    "marantz",    # Marantz AVRs/streamers (SR7009, PM-10, SA-10, etc.)
    "denon",      # Denon AVRs/streamers (AVR-X series, DNP-800NE, etc.)
    "heos",       # Denon/Marantz HEOS platform
    "oppo",       # Oppo UDP/BDP
    "cambridge",  # Cambridge Audio
    "naim",       # Naim streamers
    "linn",       # Linn DS/DSM
    "lumin",      # Lumin streamers
    "auralic",    # Auralic Aries
    "micromega",  # Micromega M-One (ESS Sabre DAC)
    "diretta",    # DirettaRendererUPnP (DSD64-DSD1024)
    "wiim",       # WiiM Ultra/Pro
    "pioneer",    # Pioneer/Onkyo network players
    "onkyo",      # Onkyo AVRs with DSD support
    "yamaha",     # Yamaha WXC/WXA/R-N series
    "teac",       # TEAC NT/UD series
    "sony",       # Sony HAP/UDA series
    "technics",   # Technics SL-G700, SA-C600, etc.
    "t+a",        # T+A DAC 8 DSD, MP series
    "esoteric",   # Esoteric network players
    "mcintosh",   # McIntosh network streamers
    "accuphase",  # Accuphase DP/DC series
    "ps audio",   # PS Audio DirectStream
    "hiby",       # HiBy Music firmware (SMSL SD-9, various HiBy streamers)
    "smsl",       # SMSL streamers/DACs running HiBy
]


def build_downmix_filter(source_channels: int, target_channels: int, custom_matrix: str = "") -> str | None:
    """Build FFmpeg -af filter for channel downmix. Returns None if no downmix needed."""
    if source_channels <= target_channels:
        return None
    if custom_matrix:
        return custom_matrix
    # ITU-R BS.775 standard 5.1→stereo downmix
    if source_channels >= 6 and target_channels == 2:
        return "pan=stereo|c0=FL+0.707*FC+0.707*BL|c1=FR+0.707*FC+0.707*BR"
    if source_channels >= 6 and target_channels == 1:
        return "pan=mono|c0=0.5*FL+0.5*FR+0.707*FC+0.354*BL+0.354*BR"
    # Generic fallback: let FFmpeg handle it via -ac
    return None


# Known multichannel-capable device name/model patterns (case-insensitive)
_MULTICHANNEL_CAPABLE_PATTERNS: dict[str, int] = {
    "marantz": 8,
    "denon": 8,
    "yamaha": 8,
    "pioneer": 8,
    "onkyo": 8,
    "nad": 8,
    "anthem": 8,
    "arcam": 8,
    "sonos arc": 6,
    "sonos beam": 6,
    "samsung": 6,
}


def detect_max_channels_from_device_info(name: str, model: str) -> int | None:
    """Heuristic: check device name/model against known multichannel-capable devices."""
    combined = f"{name} {model}".lower()
    for pattern, channels in _MULTICHANNEL_CAPABLE_PATTERNS.items():
        if pattern in combined:
            return channels
    return None


def detect_max_channels_from_sink_protocols(sink_protocols: list[str]) -> int:
    """Parse DLNA sink protocol entries for max channel count."""
    max_ch = 2
    import re
    _ch_re = re.compile(r"channels=(\d+)")
    for entry in sink_protocols:
        m = _ch_re.search(entry.lower())
        if m:
            max_ch = max(max_ch, int(m.group(1)))
    return max_ch


def detect_dsd_from_sink_protocols(sink_protocols: list[str]) -> bool:
    """Check if any DLNA sink protocol entry indicates DSD support."""
    for entry in sink_protocols:
        lower = entry.lower()
        if any(mime in lower for mime in DSD_MIME_TYPES):
            return True
        if "dsf" in lower or "dff" in lower or "dsd" in lower or "dst" in lower:
            return True
    return False


def detect_dsd_from_device_info(name: str, model: str) -> bool:
    """Heuristic: check device name/model against known DSD-capable devices."""
    combined = f"{name} {model}".lower()
    return any(pattern in combined for pattern in _DSD_CAPABLE_PATTERNS)


def dsd_mime_from_extension(file_path: str) -> str:
    """Return the appropriate MIME type for a DSD file based on extension."""
    lower = file_path.lower()
    if lower.endswith(".dff"):
        return "audio/x-dff"
    if lower.endswith(".dst"):
        return "audio/x-dst"
    return "audio/x-dsf"  # default for .dsf


def needs_transcode_for_dlna(fmt: AudioFormat) -> bool:
    """Returns True if this format needs FFmpeg transcoding before DLNA streaming.

    FLAC, WAV, MP3, AAC can be served as raw files; everything else must be transcoded.
    """
    return fmt in (AudioFormat.AIFF, AudioFormat.DSD, AudioFormat.WAVPACK, AudioFormat.APE)


def dlna_transcode_target(source_format: AudioFormat) -> AudioFormat:
    """Returns the target output format for DLNA transcoding.

    AIFF -> FLAC (lossless, widely supported by DLNA renderers)
    DSD/WavPack/APE -> WAV (universal PCM container, avoids re-encoding overhead)
    """
    if source_format == AudioFormat.AIFF:
        return AudioFormat.FLAC
    if source_format in (AudioFormat.DSD, AudioFormat.WAVPACK, AudioFormat.APE):
        return AudioFormat.WAV
    return source_format


def dsd_output_sample_rate(source_sample_rate: int) -> int:
    """Compute the appropriate PCM output sample rate for DSD sources.

    DSD64  (2.8224 MHz) -> 176400 Hz (4x 44100)
    DSD128 (5.6448 MHz) -> 352800 Hz (8x 44100)
    DSD256+ -> 352800 Hz (capped for compatibility)

    Some scanners store DSD rates divided (e.g. 2822 instead of 2822400).
    """
    if source_sample_rate >= 11_000_000:
        return 352_800  # DSD256/512
    if source_sample_rate >= 5_000_000:
        return 352_800  # DSD128
    if source_sample_rate >= 2_000_000:
        return 176_400  # DSD64
    # Scaled-down values from some scanners
    if source_sample_rate >= 5000:
        return 352_800  # DSD128-ish
    if source_sample_rate >= 2000:
        return 176_400  # DSD64-ish
    return 176_400  # safe default


def best_output_format(
    source_format: AudioFormat,
    source_sample_rate: int,
    source_bit_depth: int,
    target_caps: AudioCapabilities,
) -> AudioFormat:
    """Choose best output format for a given source and target capabilities."""
    if can_passthrough(source_format, source_sample_rate, source_bit_depth, target_caps):
        return source_format
    preference = [AudioFormat.FLAC, AudioFormat.WAV, AudioFormat.AAC, AudioFormat.MP3]
    for fmt in preference:
        if fmt in target_caps.formats:
            return fmt
    return next(iter(target_caps.formats), AudioFormat.WAV)

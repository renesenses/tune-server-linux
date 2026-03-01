from __future__ import annotations

from dataclasses import dataclass

from tune_server.models import AudioFormat


@dataclass
class AudioCapabilities:
    formats: set[AudioFormat]
    max_sample_rate: int
    max_bit_depth: int
    supports_gapless: bool = False


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
    supports_gapless=False,
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
        "wma": AudioFormat.WMA,
        "dsf": AudioFormat.DSD,
        "dff": AudioFormat.DSD,
    }
    return mapping.get(ext)


def can_passthrough(
    source_format: AudioFormat,
    source_sample_rate: int,
    source_bit_depth: int,
    target_caps: AudioCapabilities,
) -> bool:
    if source_format not in target_caps.formats:
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


def ffmpeg_codec_arg(fmt: AudioFormat) -> str:
    mapping = {
        AudioFormat.FLAC: "flac",
        AudioFormat.WAV: "pcm_s16le",
        AudioFormat.MP3: "libmp3lame",
        AudioFormat.AAC: "aac",
        AudioFormat.ALAC: "alac",
        AudioFormat.OGG: "libvorbis",
        AudioFormat.OPUS: "libopus",
        AudioFormat.AIFF: "pcm_s16be",
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
    }
    return mapping.get(fmt, "application/octet-stream")


# MIME types that indicate DSD/DSF/DFF support in DLNA sink protocols
DSD_MIME_TYPES = {
    "application/x-dsd",
    "audio/x-dsd",
    "audio/x-dsf",
    "audio/dsf",
    "audio/x-dff",
    "audio/dff",
}


# Known DSD-capable device name/model patterns (case-insensitive)
# These devices support native DSF/DFF but often don't report it via GetProtocolInfo
_DSD_CAPABLE_PATTERNS = [
    "dmp-a",      # Eversolo DMP-A8, DMP-A6
    "eversolo",
    "heos",       # Denon/Marantz HEOS
    "oppo",       # Oppo UDP/BDP
    "cambridge",  # Cambridge Audio
    "naim",       # Naim streamers
    "linn",       # Linn DS/DSM
    "lumin",      # Lumin streamers
    "auralic",    # Auralic Aries
]


def detect_dsd_from_sink_protocols(sink_protocols: list[str]) -> bool:
    """Check if any DLNA sink protocol entry indicates DSD support."""
    for entry in sink_protocols:
        # Format: "http-get:*:audio/x-dsf:*" or similar
        lower = entry.lower()
        if any(mime in lower for mime in DSD_MIME_TYPES):
            return True
        # Some renderers use generic patterns — check for dsf/dsd/dff keywords
        if "dsf" in lower or "dff" in lower:
            return True
    return False


def detect_dsd_from_device_info(name: str, model: str) -> bool:
    """Heuristic: check device name/model against known DSD-capable devices."""
    combined = f"{name} {model}".lower()
    return any(pattern in combined for pattern in _DSD_CAPABLE_PATTERNS)


def dsd_mime_from_extension(file_path: str) -> str:
    """Return the appropriate MIME type for a DSD file based on extension."""
    if file_path.lower().endswith(".dff"):
        return "audio/x-dff"
    return "audio/x-dsf"  # default for .dsf

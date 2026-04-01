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
    }
    return mapping.get(fmt, "application/octet-stream")

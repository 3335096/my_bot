from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass


SUPPORTED_STT_FORMATS = {"wav", "mp3", "aiff", "aac", "ogg"}

FORMAT_ALIASES = {
    "oga": "ogg",
    "opus": "ogg",
    "mpga": "mp3",
    "mpeg": "mp3",
    "x-wav": "wav",
}

MIME_TO_FORMAT = {
    "audio/ogg": "ogg",
    "audio/opus": "ogg",
    "audio/mpeg": "mp3",
    "audio/mp3": "mp3",
    "audio/aac": "aac",
    "audio/x-aac": "aac",
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    "audio/aiff": "aiff",
    "audio/x-aiff": "aiff",
    "audio/mp4": "aac",
    "audio/m4a": "aac",
}


@dataclass(slots=True)
class AudioPlan:
    primary_bytes: bytes
    primary_format: str
    fallback_bytes: bytes | None
    fallback_format: str | None
    normalized_with_ffmpeg: bool
    note: str


def infer_audio_format(file_path: str | None, mime_type: str | None) -> str | None:
    ext: str | None = None
    if file_path and "." in file_path:
        ext = file_path.rsplit(".", 1)[1].lower().strip()
        ext = FORMAT_ALIASES.get(ext, ext)
        if ext in SUPPORTED_STT_FORMATS:
            return ext

    if mime_type:
        mime = mime_type.lower().strip()
        detected = MIME_TO_FORMAT.get(mime)
        if detected in SUPPORTED_STT_FORMATS:
            return detected

    return ext if ext in SUPPORTED_STT_FORMATS else None


def _convert_to_wav_ffmpeg(raw_audio: bytes, source_suffix: str) -> bytes:
    ffmpeg_bin = shutil.which("ffmpeg")
    if ffmpeg_bin is None:
        raise FileNotFoundError("ffmpeg is not installed")

    with tempfile.TemporaryDirectory(prefix="audio_norm_") as tmpdir:
        input_path = os.path.join(tmpdir, f"source{source_suffix}")
        output_path = os.path.join(tmpdir, "normalized.wav")

        with open(input_path, "wb") as infile:
            infile.write(raw_audio)

        # Normalize to wav/mono/16k to maximize STT compatibility.
        cmd = [
            ffmpeg_bin,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            input_path,
            "-ac",
            "1",
            "-ar",
            "16000",
            "-f",
            "wav",
            output_path,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            stderr = result.stderr.strip() or "ffmpeg conversion failed"
            raise RuntimeError(stderr)

        with open(output_path, "rb") as outfile:
            normalized = outfile.read()

    if not normalized:
        raise RuntimeError("ffmpeg returned empty audio output")
    return normalized


async def build_audio_plan(
    raw_audio: bytes,
    *,
    file_path: str | None,
    mime_type: str | None,
) -> AudioPlan:
    source_format = infer_audio_format(file_path, mime_type)
    source_supported = source_format in SUPPORTED_STT_FORMATS if source_format else False
    source_suffix = f".{source_format}" if source_format else ".bin"

    try:
        normalized = await asyncio.to_thread(
            _convert_to_wav_ffmpeg,
            raw_audio,
            source_suffix,
        )
        fallback_bytes = raw_audio if source_supported and source_format != "wav" else None
        fallback_format = source_format if fallback_bytes is not None else None
        return AudioPlan(
            primary_bytes=normalized,
            primary_format="wav",
            fallback_bytes=fallback_bytes,
            fallback_format=fallback_format,
            normalized_with_ffmpeg=True,
            note="ffmpeg_normalized",
        )
    except FileNotFoundError:
        if source_supported and source_format is not None:
            # Fallback path when ffmpeg is unavailable: send source audio as-is.
            return AudioPlan(
                primary_bytes=raw_audio,
                primary_format=source_format,
                fallback_bytes=None,
                fallback_format=None,
                normalized_with_ffmpeg=False,
                note="ffmpeg_missing_used_source",
            )
        raise RuntimeError(
            "ffmpeg is not available and source format is unsupported for STT"
        )
    except Exception:
        if source_supported and source_format is not None:
            # If normalization failed, we still try source bytes once.
            return AudioPlan(
                primary_bytes=raw_audio,
                primary_format=source_format,
                fallback_bytes=None,
                fallback_format=None,
                normalized_with_ffmpeg=False,
                note="ffmpeg_failed_used_source",
            )
        raise RuntimeError("audio normalization failed and source format is unsupported")

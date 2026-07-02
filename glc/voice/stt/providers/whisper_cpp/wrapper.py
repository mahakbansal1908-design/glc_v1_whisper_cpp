"""whisper-cli subprocess wrapper with VAD silence trim.

No deps. Runs whisper-cli as a subprocess, trims silence >3s before
feeding audio to save inference time.

Env-var overrides (resolved lazily inside run_whisper_cpp so tests can
inject them via monkeypatch):
  GLC_WHISPER_CLI        — absolute path to the whisper-cli binary
  GLC_WHISPER_MODEL_DIR  — directory that contains a ggml-base*.bin model
  WHISPER_MODEL          — model size used to build default path (tiny/base/small/…)
  WHISPER_THREADS        — CPU threads passed to whisper-cli (default 4)
  WHISPER_SILENCE_THRESHOLD  — PCM amplitude below which a frame is silent (default 500)
  WHISPER_MIN_SILENCE_MS     — minimum silence run to trim (default 3000)
  WHISPER_VAD_THRESHOLD      — whisper.cpp -vth value (default 0.6)
  WHISPER_TIMEOUT_SECONDS    — subprocess hard timeout (default 300)
"""

from __future__ import annotations

import json
import os
import re
import shutil
import struct
import subprocess
import tempfile
from pathlib import Path

# ── configurable thresholds — read at call time so monkeypatch works ──
THREADS = int(os.getenv("WHISPER_THREADS", "4"))
SILENCE_THRESHOLD = int(os.getenv("WHISPER_SILENCE_THRESHOLD", "500"))
MIN_SILENCE_MS = int(os.getenv("WHISPER_MIN_SILENCE_MS", "3000"))
VAD_THRESHOLD = os.getenv("WHISPER_VAD_THRESHOLD", "0.6")
TIMEOUT_SECONDS = int(os.getenv("WHISPER_TIMEOUT_SECONDS", "300"))


def _resolve_cli() -> str | None:
    """Return path to whisper-cli binary. GLC_WHISPER_CLI overrides PATH lookup."""
    explicit = os.getenv("GLC_WHISPER_CLI")
    if explicit:
        return explicit if Path(explicit).is_file() else None
    return shutil.which("whisper-cli") or shutil.which("whisper.cpp")


def _resolve_model() -> Path | None:
    """Return path to the GGML model file.

    Priority:
    1. GLC_WHISPER_MODEL_DIR — tries ggml-base.bin then ggml-base.en.bin
    2. WHISPER_MODEL         — builds ~/.glc/models/whisper-<size>/ggml-<size>.bin
    """
    explicit_dir = os.getenv("GLC_WHISPER_MODEL_DIR")
    if explicit_dir:
        model_dir = Path(os.path.expanduser(explicit_dir))
        for name in ("ggml-base.bin", "ggml-base.en.bin"):
            candidate = model_dir / name
            if candidate.exists():
                return candidate
        return None

    model_size = os.getenv("WHISPER_MODEL", "base")
    model_dir = Path(os.path.expanduser(f"~/.glc/models/whisper-{model_size}"))
    candidate = model_dir / f"ggml-{model_size}.bin"
    return candidate if candidate.exists() else None


def _find_wav_chunk(audio: bytes, chunk_id: bytes) -> tuple[int, int] | None:
    """Walk RIFF chunks to find *chunk_id*. Returns (offset, size) or None."""
    if len(audio) < 12 or audio[:4] != b"RIFF":
        return None
    pos = 12
    while pos + 8 <= len(audio):
        cid = audio[pos : pos + 4]
        size = struct.unpack("<I", audio[pos + 4 : pos + 8])[0]
        if cid == chunk_id:
            return (pos + 8, size)
        pos += 8 + size + (size % 2)
    return None


def _trim_silence_wav(audio: bytes, min_silence_ms: int = 3000) -> bytes:
    """Remove contiguous silence > `min_silence_ms` from 16-bit mono WAV.

    Keeps non-silent segments, re-stitches. Returns original if not a
    valid 16-bit mono PCM WAV or if no silence found.
    """
    fmt = _find_wav_chunk(audio, b"fmt ")
    data = _find_wav_chunk(audio, b"data")
    if fmt is None or data is None:
        return audio
    fmt_off, _ = fmt
    data_off, data_sz = data
    if len(audio) < data_off + data_sz:
        data_sz = len(audio) - data_off
    if data_sz < 4:
        return audio

    bits_per = struct.unpack("<H", audio[fmt_off + 14 : fmt_off + 16])[0]
    channels = struct.unpack("<H", audio[fmt_off + 2 : fmt_off + 4])[0]
    sample_rate = struct.unpack("<I", audio[fmt_off + 4 : fmt_off + 8])[0]
    if bits_per != 16 or channels != 1:
        return audio

    raw = audio[data_off : data_off + data_sz]
    samples = memoryview(bytearray(raw)).cast("h")
    frame_ms = 30
    frame_len = int(sample_rate * frame_ms / 1000)
    silence_frames = int(min_silence_ms / frame_ms)

    is_silent = []
    for start in range(0, len(samples), frame_len):
        frame = samples[start : start + frame_len]
        energy = sum(abs(int(s)) for s in frame) / max(len(frame), 1)
        is_silent.append(energy < SILENCE_THRESHOLD)

    keep_regions: list[tuple[int, int]] = []
    i = 0
    while i < len(is_silent):
        if is_silent[i]:
            run_start = i
            while i < len(is_silent) and is_silent[i]:
                i += 1
            run_len = i - run_start
            if run_len >= silence_frames:
                continue
            keep_regions.append((run_start * frame_len, i * frame_len))
        else:
            speech_start = i
            while i < len(is_silent) and not is_silent[i]:
                i += 1
            keep_regions.append((speech_start * frame_len, i * frame_len))

    if len(keep_regions) == 1 and keep_regions[0] == (0, len(samples)):
        return audio

    header = audio[:data_off]
    out = bytearray()
    for lo, hi in keep_regions:
        out.extend(bytes(samples[lo:hi]))

    data_size = len(out)
    file_size = data_off + data_size - 8
    new_header = bytearray(header)
    new_header[4:8] = struct.pack("<I", file_size)
    new_header[data_off - 4 : data_off] = struct.pack("<I", data_size)
    return bytes(new_header) + bytes(out)


def _extract_duration_ms(data: dict) -> int:
    """Extract duration in ms from whisper.cpp JSON, handling multiple schemas."""
    segs = data.get("transcription") or data.get("segments") or []
    if not segs:
        return 0
    last = segs[-1]
    for key in ("end", "t1"):
        val = last.get(key)
        if val is not None:
            return int(val)
    offsets = last.get("offsets", {}) or {}
    to_val = offsets.get("to")
    if to_val is not None:
        return int(to_val)
    return 0


def _parse_stderr_transcript(stderr: str) -> tuple[str, int]:
    """Extract (text, duration_ms) from whisper-cli stderr transcript lines.

    When VAD is enabled or JSON sidecar is not written, whisper-cli emits
    timestamped transcript lines to stderr like::

        [00:00:00.000 --> 00:00:07.000]   some text here

    We parse every line matching that pattern, join the text, and return
    the last ``to`` timestamp as ``duration_ms``. Lines without a valid
    ``to`` timestamp are included in the text but ignored for duration.
    """
    text_parts: list[str] = []
    last_end_ms = 0
    pattern = re.compile(
        r"\[(\d{2}):(\d{2}):(\d{2})\.(\d{3})\]\s+-->\s+"
        r"(\d{2}):(\d{2}):(\d{2})\.(\d{3})\]\s+(.*)"
    )

    for line in stderr.splitlines():
        m = pattern.match(line.strip())
        if m:
            h2, m2, s2, cs2 = m.group(5), m.group(6), m.group(7), m.group(8)
            txt = m.group(9)
            text_parts.append(txt.strip())
            end_ms = int(h2) * 3600_000 + int(m2) * 60_000 + int(s2) * 1000 + int(cs2)
            last_end_ms = max(last_end_ms, end_ms)

    text = " ".join(text_parts).strip()
    return text, last_end_ms


def run_whisper_cpp(audio: bytes, mime: str, use_vad: bool = False) -> tuple[str, str, int]:
    """Invoke whisper-cli, parse its JSON sidecar or stderr, return (text, language, duration_ms)."""
    cli = _resolve_cli()
    if cli is None:
        raise RuntimeError("whisper-cli binary not found. Set GLC_WHISPER_CLI or add whisper-cli to PATH.")
    model_file = _resolve_model()
    if model_file is None:
        model_dir = os.getenv("GLC_WHISPER_MODEL_DIR") or os.getenv("WHISPER_MODEL", "base")
        raise RuntimeError(
            f"whisper base model not found (looked in {model_dir!r}). "
            "Set GLC_WHISPER_MODEL_DIR or run: daemon/install.sh --models"
        )

    if "wav" in mime and not use_vad:
        audio = _trim_silence_wav(audio, min_silence_ms=MIN_SILENCE_MS)

    suffix = ".wav" if "wav" in mime else ".bin"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
        f.write(audio)
        audio_path = Path(f.name)

    cmd = [cli, "-m", str(model_file), "-f", str(audio_path), "-oj", "-t", str(THREADS)]
    if use_vad:
        cmd.extend(["-vth", VAD_THRESHOLD])

    try:
        out = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=True,
            timeout=TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"whisper-cli timed out after {TIMEOUT_SECONDS}s") from e
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"whisper-cli failed (exit={e.returncode}): {e.stderr.strip()}") from e
    finally:
        audio_path.unlink(missing_ok=True)

    json_path = audio_path.with_suffix(audio_path.suffix + ".json")
    if json_path.exists():
        d = json.loads(json_path.read_text())
        json_path.unlink(missing_ok=True)
        segments = d.get("transcription") or d.get("segments") or []
        text = " ".join((s.get("text") or "").strip() for s in segments).strip()
        language = d.get("language") or "en"
        duration_ms = _extract_duration_ms(d)
        return text, language, duration_ms

    text, duration_ms = _parse_stderr_transcript(out.stderr)
    return text, "en", duration_ms

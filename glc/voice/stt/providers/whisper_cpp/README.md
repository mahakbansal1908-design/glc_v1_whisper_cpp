# whisper.cpp (local, offline)

Local STT provider using `whisper-cli` and the base GGML model. Runs entirely
on-device — no API keys, no network, no cost.

## Architecture

```
adapter.py (Provider.transcribe)
  │
  ├─► _is_silent() ──► empty result (0.3ms, no subprocess)
  │
  ├─► _normalize_feeble() ──► dynamic gain boost for quiet speech
  │
  ├─► mock delegation (tests only)
  │
  ├─► _should_use_vad() ──► False (disabled, see quirks)
  │
  └─► wrapper.run_whisper_cpp()
        │
        ├─► _resolve_cli() ──► GLC_WHISPER_CLI env var or PATH
        ├─► _resolve_model() ──► GLC_WHISPER_MODEL_DIR or ~/.glc/models/
        ├─► _trim_silence_wav() ──► removes >3s silence before subprocess
        ├─► subprocess.run(whisper-cli -m <model> -f <audio> -oj -t <threads>)
        │     └─► timeout=300s hard limit
        ├─► JSON sidecar parsing ──► _extract_duration_ms()
        └─► stderr fallback ──► _parse_stderr_transcript()
```

**Key design decisions:**

- **Silence trim before subprocess:** `_trim_silence_wav()` removes long silences
  from the WAV before whisper-cli sees it. Less audio → faster inference. This
  is more effective than whisper's built-in VAD (`-vth`), which runs inside the
  subprocess after startup is paid.

- **Disabled whisper VAD:** `_should_use_vad()` returns `False` by default.
  The `-vth` flag causes whisper-cli to skip writing the JSON sidecar in some
  versions, breaking the parsing path. The pre-trim approach is more reliable.

- **Configurable thresholds:** All tunables (silence threshold, min silence
  duration, threads, timeout) are read from env vars at call time so tests can
  monkeypatch them.

## Wire-format quirks

### 1. Silence threshold too low (SILENCE_MAX_AMPLITUDE = 32)

**Problem:** The original stub used `SILENCE_MAX_AMPLITUDE = 32`. Real-world
audio has ambient noise well above 32 (even quiet rooms have background hiss
at 50-100). This caused real speech to be treated as silence, returning empty
transcripts.

**Fix:** Raised to 500 (the standalone's default). Still catches pure silence
and garbage input, but lets real speech through.

### 2. VAD flag breaks JSON sidecar (`-vth`)

**Problem:** When `-vth` (VAD threshold) is enabled, whisper-cli sometimes
skips writing the `<input>.json` sidecar file. Instead it prints timestamped
transcript lines to stderr. Without parsing stderr, VAD-enabled audio returns
empty text.

**Fix:** Disabled whisper's built-in VAD (`_should_use_vad()` returns `False`).
`_trim_silence_wav()` removes silence before the subprocess, achieving the
same goal without breaking JSON output. `_parse_stderr_transcript()` exists
as a fallback if VAD is re-enabled.

### 3. JSON schema drift across whisper.cpp versions

**Problem:** The JSON output format changed between whisper.cpp versions:
- Old: `{"segments": [{"offsets": {"to": 34000}, ...}]}`
- New: `{"transcription": [{"end": 34000, ...}]}`

**Fix:** `_extract_duration_ms()` tries multiple field names (`end`, `t1`,
`offsets.to`) to handle all versions.

### 4. RIFF chunks beyond `fmt ` and `data`

**Problem:** Screen recordings often have extra RIFF chunks (`LIST`, `fact`,
`cue`). Python's `wave.open()` chokes on these.

**Fix:** `_find_wav_chunk()` walks the RIFF tree manually, handling any chunk
type. Used by `_trim_silence_wav()` to locate the `fmt ` and `data` chunks.

## Environment

- **whisper-cli:** Build from https://github.com/ggerganov/whisper.cpp
- **Model:** `ggml-base.en.bin` (147 MB, English-only, fastest) or
  `ggml-base.bin` (147 MB, multilingual)
- **Model path:** Set `GLC_WHISPER_MODEL_DIR` env var to the directory
  containing the model, or place it at `~/.glc/models/whisper-base/ggml-base.bin`

### Env vars

| Variable | Default | Purpose |
|----------|---------|---------|
| `GLC_WHISPER_CLI` | `whisper-cli` on PATH | Override whisper-cli binary path |
| `GLC_WHISPER_MODEL_DIR` | `~/.glc/models/whisper-base` | Override model directory |
| `WHISPER_MODEL` | `base` | Model size for default path (`tiny`, `base`, `small`, …) |
| `WHISPER_THREADS` | `4` | CPU threads for whisper-cli |
| `WHISPER_SILENCE_THRESHOLD` | `500` | PCM amplitude = silence floor |
| `WHISPER_MIN_SILENCE_MS` | `3000` | Minimum silence run to trim |
| `WHISPER_VAD_THRESHOLD` | `0.6` | whisper-cli `-vth` value (if VAD re-enabled) |
| `WHISPER_TIMEOUT_SECONDS` | `300` | Subprocess hard timeout |

## Tests

### Unit tests (7 tests, all pass)

```bash
uv run pytest tests/voice/stt/test_whisper_cpp.py -v
```

| Test | What it exercises |
|------|-------------------|
| `test_provider_name_matches` | Adapter name is `"whisper_cpp"` |
| `test_transcribe_returns_transcribe_result` | Returns `TranscribeResult` with correct fields |
| `test_transcribe_passes_audio_to_upstream` | Audio bytes forwarded to mock |
| `test_transcribe_records_duration_ms` | Duration from mock preserved |
| `test_transcribe_propagates_upstream_error` | `STTError` raised on upstream failure |
| `test_transcribe_handles_empty_audio` | Empty bytes handled gracefully |
| `test_channel_specific_behaviour_vad_skips_silent_input` | Silent input bypasses subprocess |

### Trust-level boundary

The critical test is `test_channel_specific_behaviour_vad_skips_silent_input`:
it verifies the adapter **does not invoke whisper-cli** when the input is
silent (zero-amplitude WAV bytes). This is the trust boundary — the adapter
must not shell out to a subprocess for empty/garbage input, as that would
waste hundreds of ms of subprocess startup for an empty transcript.

The mock (`WhisperCppMock`) tracks `subprocess_call_count`. The test asserts
it stays at 0 for silent input, proving the short-circuit works.

### Real audio validation

Tested with real audio files (not synthetic tones):

| File | Duration | Latency | Result |
|------|----------|---------|--------|
| `screen_recording_16k_mono.wav` | 34.0s | 828ms | ✅ English lecture transcribed |
| `tere_hawaale_30s_pauses.wav` | 30.0s | 923ms | ✅ Hindi song with pauses transcribed |

Both under the 1s voice budget.

## What was removed from the original stub

| Feature | Reason |
|---------|---------|
| `-bs` (beam size) flag |  whisper-cli default is fine |
| `no_speech_prob > 0.7` music detection |  whisper already returns empty for non-speech |
| `_amplify_wav()` (fixed 1.5× gain) | Replaced by `_normalize_feeble()` (dynamic gain) |
| `_is_music_likely()` (ZCR heuristic) |  adds ~700ms latency, not called by default |

## Files

| File | Purpose |
|------|---------|
| `adapter.py` | `Provider` class, `transcribe()` pipeline, helper functions |
| `wrapper.py` | `run_whisper_cpp()` subprocess wrapper, RIFF parsing, silence trim |
| `schemas.py` | Pydantic models (`WhisperCppSegment`, `WhisperCppResult`, `WhisperCppConfig`) |
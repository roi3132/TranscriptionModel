# Transcription Service

A production-minded FastAPI service that accepts an audio or video file and returns a timestamped
transcription produced by an open multilingual Whisper model from Hugging Face.

The service currently supports the complete audio and video-container workflow. The browser UI,
Docker packaging, and CI are the next planned phases. See
[`docs/architecture-plan.html`](docs/architecture-plan.html) for the ordered implementation plan.

## Current capabilities

- Upload WAV, MP3, M4A/AAC, FLAC, OGG, Opus, MP4, MOV, WebM, or MKV through
  `multipart/form-data`.
- Transcribe the first audio stream from a supported video container without a separate video path.
- Transcribe Hebrew (`he`), English (`en`), or another Whisper-supported language.
- Automatically detect the language with `language=auto`.
- Return full text, detected-language confidence, media duration, processing time, and timestamped
  segments.
- Enforce configurable file-size, media-duration, queue-timeout, and inference-concurrency limits.
- Expose liveness, readiness, and generated OpenAPI documentation.
- Avoid retaining the uploaded media or transcription after the response.

## Architecture

```text
Browser / curl
      │ multipart/form-data
      ▼
FastAPI route and API schemas
      │
      ▼
TranscriptionService
  ├── size and duration limits
  ├── language validation
  ├── concurrency semaphore
  └── worker-thread execution
      │
      ├── PyAV media inspector
      └── Transcriber interface
                │
                ▼
       faster-whisper / CTranslate2
                │
                ▼
   Systran/faster-whisper-small
```

The model is loaded once during the FastAPI lifespan. Until loading succeeds,
`/health/ready` returns `503`; `/health/live` remains independent of model state. Blocking media
inspection and inference run in worker threads rather than on the asynchronous HTTP event loop.

## Run locally

### Requirements

- Python 3.12
- [`uv`](https://docs.astral.sh/uv/)
- Internet access on the first model run

Install the locked runtime and development dependencies:

```bash
uv sync --all-groups
```

Optionally create local configuration:

```bash
cp .env.example .env
```

Run the API:

```bash
uv run uvicorn transcription_service.main:app --reload
```

The first startup downloads approximately 500 MB of model files into the Hugging Face user cache
(normally `~/.cache/huggingface/hub`), which is outside this repository. Later runs reuse the cached
model. The local `.venv/`, `.uv-cache/`, `.cache/`, `models/`, `uploads/`, and `.env` paths are also
excluded by `.gitignore`, so dependencies, model weights, uploaded media, and local configuration are
not committed.

You can verify this at any time with `git status --short --ignored`. Ignored generated files appear
with `!!`; they must not appear as staged or tracked files.

Available endpoints:

- API documentation: <http://localhost:8000/docs>
- Liveness: <http://localhost:8000/health/live>
- Readiness: <http://localhost:8000/health/ready>
- Transcription: `POST http://localhost:8000/v1/transcriptions`

Example:

```bash
curl --fail \
  -F "file=@/absolute/path/to/sample.wav" \
  -F "language=he" \
  http://localhost:8000/v1/transcriptions
```

Example response:

```json
{
  "text": "שלום, זאת בדיקה של שירות התמלול",
  "language": "he",
  "language_probability": 1.0,
  "duration_seconds": 2.39,
  "processing_time_seconds": 1.67,
  "model": "Systran/faster-whisper-small",
  "segments": [
    {
      "start": 0.0,
      "end": 2.44,
      "text": "שלום, זאת בדיקה של שירות התמלול"
    }
  ]
}
```

## Test and quality checks

The normal suite injects a fake transcriber and therefore does not download model weights:

```bash
uv run ruff format --check .
uv run ruff check .
uv run mypy src
uv run pytest --cov=transcription_service --cov-report=term-missing
```

Run an explicit real-model smoke test against one or more local samples:

```bash
uv run python scripts/smoke_media.py \
  en=/absolute/path/to/english.wav \
  he=/absolute/path/to/hebrew.m4a \
  en=/absolute/path/to/video.mp4
```

The smoke test loads the configured Hugging Face model, waits for readiness, and sends every sample
through the public HTTP endpoint. It is deliberately excluded from normal CI because it downloads
large weights and its execution time depends on the available hardware.

## Configuration

All settings use the `TRANSCRIPTION_` environment prefix. Defaults are listed in `.env.example`.

| Variable | Default | Purpose |
| --- | --- | --- |
| `TRANSCRIPTION_MODEL_ID` | `Systran/faster-whisper-small` | Hugging Face CTranslate2 model |
| `TRANSCRIPTION_MODEL_REVISION` | `536b066...` | Reproducible Hugging Face model commit |
| `TRANSCRIPTION_DEVICE` | `cpu` | CTranslate2 device: `cpu`, `cuda`, or `auto` |
| `TRANSCRIPTION_COMPUTE_TYPE` | `int8` | CPU-friendly quantized inference |
| `TRANSCRIPTION_BEAM_SIZE` | `5` | Beam-search quality/speed trade-off |
| `TRANSCRIPTION_VAD_FILTER` | `true` | Remove silence for speech-oriented recordings; disable for music |
| `TRANSCRIPTION_MAX_UPLOAD_BYTES` | `104857600` | 100 MiB file-size limit |
| `TRANSCRIPTION_MAX_MEDIA_DURATION_SECONDS` | `900` | 15-minute synchronous limit |
| `TRANSCRIPTION_MAX_CONCURRENT_TRANSCRIPTIONS` | `1` | Model inference slots per process |
| `TRANSCRIPTION_INFERENCE_QUEUE_TIMEOUT_SECONDS` | `5` | Wait before returning `model_busy` |

## Dependency compatibility note

The real-model smoke test exposed an incompatibility between the currently released
`faster-whisper==1.2.1` and PyAV 19:

- faster-whisper 1.2.1 calls `av.open(..., metadata_errors="ignore")`.
- PyAV 19 removed the `metadata_errors` argument.
- The combination fails only during real audio decoding with
  `TypeError: open() got an unexpected keyword argument 'metadata_errors'`.
- Fast tests using a fake model did not—and should not be expected to—detect this third-party
  integration issue.

For that reason, `pyproject.toml` currently constrains PyAV to `av>=14,<19`, and `uv.lock` resolves
PyAV 18.1.0. Do not remove this upper bound without running the real English and Hebrew smoke tests.
When a released faster-whisper version includes PyAV 19 compatibility, upgrade both packages in one
change, regenerate `uv.lock`, and rerun the complete fast and real-model suites.

Dependencies are bounded in `pyproject.toml` and fully pinned transitively in `uv.lock`. The model
revision is also pinned rather than following a mutable `main` branch, so application dependencies
and inference weights are independently reproducible.

## Major decisions and trade-offs

This section is a living decision record. Any material architectural change should update this
section in the same pull request as the code.

### Python and FastAPI

Python provides the best-supported Hugging Face and speech-model ecosystem. The performance-critical
inference path still runs in CTranslate2's native runtime. FastAPI provides typed contracts,
OpenAPI, dependency injection, multipart handling, and application lifecycle hooks without the
weight of a larger web framework.

Alternatives considered: C++ with whisper.cpp, Flask, and a separate Go/C++ API plus Python model
service. They are valid in larger or differently constrained systems, but add delivery and
operational complexity without improving this assignment's core workflow.

### faster-whisper instead of openai-whisper or raw Transformers

`faster-whisper` uses CTranslate2, supports CPU int8 and GPU execution, accepts file-like objects,
and consumes compatible model weights hosted on Hugging Face. It avoids implementing audio feature
extraction or generation logic ourselves.

The adapter is hidden behind a `Transcriber` protocol. Model-specific code does not leak into the
HTTP or use-case layers, and tests can substitute a deterministic fake.

### Multilingual `small` model

`Systran/faster-whisper-small` supports Hebrew and English while remaining practical for a CPU-only
reviewer environment. The `.en` checkpoint was deliberately not selected because it cannot
transcribe Hebrew. Larger models may improve accuracy but increase download size, RAM use, and
latency. The model ID remains configurable.

### Pinned model revision

The model defaults to Hugging Face revision
`536b0662742c02347bc0e980a01041f333bce120`. Following `main` would allow weights or supporting files
to change without a code or lockfile change, making failures and output differences harder to
reproduce.

### No database in the initial product

The requested workflow is upload → transcribe → return. There are no accounts, history, sharing,
audit, billing, or retention requirements. Adding PostgreSQL would introduce migrations,
transactions, cleanup rules, privacy obligations, and authorization questions without supporting a
current requirement.

The service is intentionally stateless: uploaded media is closed after the request, and neither
media nor transcript is retained. This also makes horizontal replication straightforward.

A database becomes appropriate when the product adds durable asynchronous jobs, user-owned history,
or retention. That evolution is described below.

### Synchronous, bounded requests

The first product returns the transcription in the upload response because that directly matches
the assignment. File size, media duration, model concurrency, and queue wait are bounded to prevent
uncontrolled resource consumption.

For longer recordings or production traffic, the API should evolve to:

```text
POST upload → object storage → durable queue → inference worker
       │                                      │
       └── 202 + job ID          PostgreSQL result/status

GET /v1/transcriptions/{job_id}
```

That design justifies object storage, a durable queue, and PostgreSQL together. An in-process
background task was rejected because a process restart would lose accepted work.

### One inference at a time by default

The default concurrency is one to keep CPU and model memory use predictable. Additional HTTP
requests wait up to the configured queue timeout and then receive `503 model_busy` plus
`Retry-After`. Blocking inference runs in a worker thread so health requests and other asynchronous
HTTP work remain responsive.

Run one Uvicorn worker by default. Multiple workers each load a separate model and multiply memory
usage. A future GPU deployment should normally run one worker process per assigned GPU and scale
workers explicitly.

### PyAV for media handling

PyAV uses bundled FFmpeg libraries, so the application does not shell out to a system FFmpeg
binary. The service checks the declared extension/MIME type for product policy, then uses PyAV as
the authoritative container and audio-stream validation. Client-provided filenames are never used
as server filesystem paths.

Audio and video intentionally use the same endpoint and transcription pipeline. For MP4, MOV,
WebM, and MKV, PyAV selects the first audio stream; the video frames are not decoded or copied into
a separate temporary file. A readable video without audio returns the typed `no_audio_stream`
client error. Selecting among multiple audio tracks is deferred until a product requirement defines
the desired user experience and API contract.

### Fast tests plus an explicit real-model smoke test

Unit and API tests must remain deterministic and fast, so they use a fake `Transcriber` while still
using real PyAV and synthetic audio/video containers for media integration tests. A separate opt-in
smoke test covers model download, native runtime loading, media audio-stream decoding, and actual
English/Hebrew inference. Both layers are needed: the PyAV 19 issue demonstrated that mocked tests
alone cannot validate third-party binary integration.

## Error behavior

Every handled failure uses a stable envelope:

```json
{
  "error": {
    "code": "unsupported_media",
    "message": "The uploaded media format is unsupported or could not be decoded.",
    "request_id": "..."
  }
}
```

Expected status codes include:

| Status | Meaning |
| --- | --- |
| `400` | Empty or malformed request |
| `413` | Upload or media duration exceeds configured limits |
| `415` | Unsupported or undecodable media |
| `422` | Invalid language or missing audio stream |
| `500` | Sanitized model/inference failure |
| `503` | Model unavailable or inference queue timeout |

Internal exception text, stack traces, and local paths are logged server-side and are not returned
to clients. Every response includes `X-Request-ID` for correlation.

## Current limitations and next steps

- Only the first audio stream in a video container is transcribed; the API does not yet expose audio
  track selection.
- The browser upload UI is not implemented yet. It will render transcript text with `dir="auto"`
  for correct Hebrew RTL and English LTR behavior.
- Docker and GitHub Actions are not implemented yet. Until that phase, the truthful supported run
  path is the native `uv` workflow above.
- There is no authentication or rate limiting. Do not expose this service directly to an untrusted
  public network in its current form.
- Automatic language detection and Hebrew/English code-switching are model-dependent and may be
  less accurate than selecting the dominant language explicitly.
- Voice activity detection is enabled by default for efficient speech transcription. Music-heavy
  vocals may be classified as non-speech and removed completely; set
  `TRANSCRIPTION_VAD_FILTER=false`, restart the service, and select the dominant language explicitly
  for songs. Disabling VAD processes the entire audio track and therefore increases latency.

## Updating this decision record

When making a major decision or accepting a shortcut:

1. Update the relevant README section in the same change.
2. State the requirement or observed failure that motivated it.
3. Record the chosen approach and meaningful alternatives.
4. Describe the operational or maintenance consequence.
5. Add the condition under which the decision should be revisited.

This keeps the repository understandable to reviewers without requiring access to the original
conversation.


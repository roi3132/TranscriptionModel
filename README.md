# Transcription Service

A production-minded FastAPI service that accepts an audio or video file and returns a timestamped
transcription produced by an open multilingual Whisper model from Hugging Face.

The service currently supports the complete audio and video-container workflow, including a browser
upload interface and Docker Compose packaging. See
[`docs/architecture-plan.html`](docs/architecture-plan.html) for the ordered implementation plan.

## Current capabilities

- Upload WAV, MP3, M4A/AAC, FLAC, OGG, Opus, MP4, MOV, WebM, or MKV through
  `multipart/form-data`.
- Transcribe the first audio stream from a supported video container without a separate video path.
- Transcribe Hebrew (`he`), English (`en`), or another Whisper-supported language.
- Automatically detect the language with `language=auto`.
- Choose `audio_mode=speech` for faster VAD-filtered processing or `audio_mode=music` to process the
  complete audio track.
- Choose `model_profile=faster` (default, Whisper small) or `model_profile=larger` (Whisper large-v3),
  independently of Speech/Music mode. Larger does not guarantee more accurate lyrics.
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

The default model is loaded once during the FastAPI lifespan. Until loading succeeds,
`/health/ready` returns `503`; `/health/live` remains independent of model state. Blocking media
inspection and inference run in worker threads rather than on the asynchronous HTTP event loop.
The optional larger model loads on its first request, inside the same inference concurrency limit,
and is reused afterward. Readiness describes the default model; it does not pre-download large-v3.

## Run locally

### Docker Compose

Prerequisite: Docker Desktop with the Compose plugin (or Docker Engine and Compose v2). From the
repository root, start the CPU-first service:

```bash
docker compose up --build
```

On first startup, the container downloads the pinned model (about 500 MB) into a named Docker
volume. The download is separate from the application image and is reused on later starts. Once the
model is ready, open <http://localhost:8000/>. Stop the foreground service with `Ctrl+C`; the model
cache remains available. To run detached instead, use `docker compose up --build -d`, follow logs
with `docker compose logs -f app`, and stop with `docker compose down`.

The container runs as a non-root user with a read-only root filesystem, a bounded temporary
filesystem for request processing, and only the model cache mounted persistently. The port is bound
to `127.0.0.1`, so the service is not exposed to other machines by default. Compose reads optional
overrides from `.env`; start from `cp .env.example .env` if you want to customize the model or
limits. `docker compose down` preserves the model cache. `docker compose down --volumes` removes
that cache too and should only be used when you intentionally want to download the weights again.

Run the full container readiness and browser-page smoke check with `make docker-smoke`. It builds
and starts the Compose service, waits up to 15 minutes for model readiness, checks the browser page,
and leaves the service running. `make docker-down` stops it without deleting the model cache.

### Native Python

Requirements: Python 3.12, [`uv`](https://docs.astral.sh/uv/), and internet access on the first
model run.

Install the locked runtime and development dependencies (or run `make setup`):

```bash
uv sync --frozen --all-groups
```

Optionally create local configuration:

```bash
cp .env.example .env
```

Run the API (or run `make run`):

```bash
uv run --frozen uvicorn transcription_service.main:app --reload
```

Open <http://localhost:8000/> for the browser interface. Choose an audio or video file, select
Auto-detect, Hebrew, or English, choose Speech or Music mode, and select Faster or Larger model.
The larger model may download several GB on first use and needs additional RAM. The page shows progress and
timestamped transcript segments, with copy and `.txt` download actions. You can also use the API
directly at <http://localhost:8000/docs> or with the `curl` example below.

The first startup downloads approximately 500 MB of model files into the Hugging Face user cache
(normally `~/.cache/huggingface/hub`), which is outside this repository. Later runs reuse the cached
model. The local `.venv/`, `.uv-cache/`, `.cache/`, `models/`, `uploads/`, `.env`, and supported
audio/video files are excluded by `.gitignore`, so dependencies, model weights, uploaded media, and
local configuration are not committed. `.dockerignore` applies the same media/cache exclusions to
the build context.

You can verify this at any time with `git status --short --ignored`. Ignored generated files appear
with `!!`; they must not appear as staged or tracked files.

Available endpoints:

- Browser interface: <http://localhost:8000/>
- API documentation: <http://localhost:8000/docs>
- Liveness: <http://localhost:8000/health/live>
- Readiness: <http://localhost:8000/health/ready>
- Transcription: `POST http://localhost:8000/v1/transcriptions`

Example:

```bash
curl --fail \
  -F "file=@/absolute/path/to/sample.wav" \
  -F "language=he" \
  -F "audio_mode=speech" \
  -F "model_profile=faster" \
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

The normal suite injects a fake transcriber and therefore does not download model weights. From a
clean clone, `make check test` runs the lockfile check, Ruff formatting and lint, mypy, and pytest.
The equivalent individual commands are:

```bash
uv run --frozen ruff format --check .
uv run --frozen ruff check .
uv run --frozen mypy src
uv run --frozen pytest --cov=transcription_service --cov-report=term-missing
```

Run an explicit real-model smoke test against one or more local samples:

```bash
uv run --frozen python scripts/smoke_media.py \
  en=/absolute/path/to/english.wav \
  he=/absolute/path/to/hebrew.m4a \
  en=/absolute/path/to/video.mp4
```

For a song or music-heavy recording, add `--audio-mode music`.
To exercise the larger model through the same API, add `--model-profile larger`.

The equivalent Make target accepts one or more language/path samples, for example
`make smoke SAMPLES="en=/path/to/english.wav he=/path/to/hebrew.m4a" AUDIO_MODE=music`.
Add `MODEL_PROFILE=larger` to that Make command to use large-v3.

The smoke test loads the configured Hugging Face model, waits for readiness, and sends every sample
through the public HTTP endpoint. It is deliberately excluded from normal CI because it downloads
large weights and its execution time depends on the available hardware.

## Configuration

All settings use the `TRANSCRIPTION_` environment prefix. Defaults are listed in `.env.example`.

| Variable | Default | Purpose |
| --- | --- | --- |
| `TRANSCRIPTION_MODEL_ID` | `Systran/faster-whisper-small` | Hugging Face CTranslate2 model |
| `TRANSCRIPTION_MODEL_REVISION` | `536b066...` | Reproducible Hugging Face model commit |
| `TRANSCRIPTION_LARGER_MODEL_ID` | `Systran/faster-whisper-large-v3` | Optional larger model, loaded on demand |
| `TRANSCRIPTION_LARGER_MODEL_REVISION` | `edaa852...` | Pinned large-v3 model commit |
| `TRANSCRIPTION_DEVICE` | `cpu` | CTranslate2 device: `cpu`, `cuda`, or `auto` |
| `TRANSCRIPTION_COMPUTE_TYPE` | `int8` | CPU-friendly quantized inference |
| `TRANSCRIPTION_BEAM_SIZE` | `5` | Beam-search quality/speed trade-off |
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

### Explicit larger-model option with lazy loading

The browser and API expose the allowlisted profiles `faster` and `larger`. Existing requests default
to `faster`, preserving the CPU-friendly small model. `larger` selects `Systran/faster-whisper-large-v3`
at revision `edaa852ec7e145841d8ffdb056a99866b5f0a478`. Clients cannot supply arbitrary model IDs or
revisions; operators configure those through environment variables. Both profiles remain independent
of `audio_mode`, and responses identify the model actually used.

The default model stays in memory. The larger model is downloaded/loaded on its first selected
request, in a worker thread, and then cached in memory until process shutdown. A lock prevents
duplicate concurrent initialization. Loading and inference share the existing semaphore and queue
timeout, so first-use loading occupies an inference slot. At most two model instances are retained
per process; memory use increases after selecting the larger option. Both use the same disk cache,
device, compute type, and beam-size settings. `processing_time_seconds` includes any first-use
download/loading time. Model initialization failures return a sanitized error without silently
substituting the small model; later requests can retry, and the default model remains usable.

This trades higher RAM use after first selection for fast repeated use and preserves quick startup.
Evicting models on every selection would reduce resident memory but cause repeated loading and
model-switch races. Revisit the cache policy for memory-constrained deployments or a larger model
catalog; separate inference workers are preferable at that scale.

The UI deliberately says “Larger model”, not “Higher accuracy”. On the same 10.3-second Hebrew song
clip, small took 15.2 seconds and produced garbled words; large-v3 took 21.5 seconds and returned
only “תודה רבה.” with an implausible timestamp. These are one-clip observations, not a benchmark or
quality guarantee. Singing remains best-effort, and the larger option is retained for user comparison.

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

### Speech and music modes instead of a VAD flag

The public API exposes the product-level `audio_mode` values `speech` and `music`, rather than
leaking faster-whisper's `vad_filter` implementation detail. `speech` is the default and enables
voice activity detection for lower latency on ordinary spoken recordings. `music` disables VAD and
Whisper's no-speech segment skipping, processing the complete audio track so music-heavy vocals are
not discarded as non-speech.

The browser exposes these two modes with a short explanation. The service does not retry an empty
speech result automatically. Such a retry would double the worst-case work and could make Whisper
hallucinate text for genuinely silent media. The explicit mode keeps cost and behavior predictable.

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
| `422` | Invalid language/audio mode or missing audio stream |
| `500` | Sanitized model/inference failure |
| `503` | Model unavailable or inference queue timeout |

Internal exception text, stack traces, and local paths are logged server-side and are not returned
to clients. Every response includes `X-Request-ID` for correlation.

### Same-origin, buildless browser interface

The upload experience is a single HTML page with local CSS and JavaScript served by FastAPI. It
submits to the existing API, so local development and deployment need only one process and there is
no separate frontend server, framework, bundler, or extra runtime dependency. Static assets are
packaged with the Python wheel. The page does not load fonts, scripts, or styles from third-party
CDNs, and the service still discards uploaded media and results after responding.

This intentionally small interface is appropriate for the assignment's one-request workflow. A
larger product with navigation, authentication, durable job history, or multiple frontend teams
could justify a separately deployed frontend and its added build and deployment pipeline.

### CPU-first container and persistent model cache

The Docker image installs the exact locked Python environment, runs the API as an unprivileged user,
and keeps model weights out of the image. Compose stores only the Hugging Face cache in a named
volume; uploaded media is handled in bounded temporary storage and is not written to that volume.
The container uses one Uvicorn worker so the model is loaded once and the in-process inference
semaphore remains meaningful. The port binds to localhost, and the container drops Linux
capabilities and has a read-only root filesystem.

This keeps rebuilds smaller than baking roughly 500 MB of weights into every image and keeps local
startup convenient on CPU-only machines. Alternatives include baking model weights into an image
for an offline deployment, or using a bind mount for a host-managed cache; those choices trade
larger image distribution or host-specific permissions for their respective operational benefits.
For multiple production replicas, the model cache can be independently warmed per instance; neither
media nor transcripts should be added to persistent storage without an explicit retention and
authorization requirement.

The base image is pinned to the maintained Python 3.12 Bookworm line rather than an immutable
digest, so rebuilding can pick up upstream security fixes. A release deployment that requires
bit-for-bit image reproducibility should pin the base image digest and update it through a deliberate
security-update process.

GitHub Actions runs the same locked Ruff, mypy, and fake-transcriber pytest checks as local
development. It does not download model weights or handle secrets. Third-party workflow actions are
pinned to commit SHAs to make the CI toolchain changes explicit and reviewable.

## Current limitations and next steps

- Only the first audio stream in a video container is transcribed; the API does not yet expose audio
  track selection.
- There is no authentication or rate limiting. Do not expose this service directly to an untrusted
  public network in its current form.
- Automatic language detection and Hebrew/English code-switching are model-dependent and may be
  less accurate than selecting the dominant language explicitly.
- Music mode processes the full audio track and is therefore slower than speech mode.
- Docker uses CPU inference and a single worker. A production GPU or multi-replica deployment needs
  deployment-specific resource allocation, scaling, and model-cache warming.

## Updating this decision record

When making a major decision or accepting a shortcut:

1. Update the relevant README section in the same change.
2. State the requirement or observed failure that motivated it.
3. Record the chosen approach and meaningful alternatives.
4. Describe the operational or maintenance consequence.
5. Add the condition under which the decision should be revisited.

This keeps the repository understandable to reviewers without requiring access to the original
conversation.


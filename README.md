# Transcription Service

A production-minded FastAPI service that accepts an audio or video file and returns a timestamped
transcription produced by an open multilingual Whisper model from Hugging Face.

The service supports a durable asynchronous workflow in Docker Compose and a compatibility
synchronous workflow for native development. Compose runs the API, an independent inference
worker, Redis, and PostgreSQL entirely on the local machine—no hosted service or subscription is
required. See [`docs/architecture-plan.html`](docs/architecture-plan.html) for completed Phase 6
and the ordered reliability and media roadmap.

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
- Accept uploads durably with `202 Accepted`, then expose job status and result resources.
- Keep accepted jobs and completed results across independent API and worker restarts in local
  PostgreSQL; persist queued media and the Redis queue in named Docker volumes.
- Run model inference only in an independent worker when asynchronous mode is enabled.
- Enforce configurable file-size, media-duration, queue-timeout, and inference-concurrency limits.
- Expose liveness, readiness, and generated OpenAPI documentation.
- Delete successfully processed source media. Configurable retention and result expiry arrive in
  Phase 7; `docker compose down --volumes` deliberately removes all local durable data.

## Architecture

```text
Browser / curl → bounded API upload → media volume → PostgreSQL job
                                      │                 │
                                      └→ local Redis → independent worker
                                                         │
                                                         ├→ PyAV validation
                                                         ├→ faster-whisper
                                                         └→ PostgreSQL result
```

Docker Compose enables asynchronous mode. The API persists media and job state before enqueueing,
returns promptly with `202`, and never loads Whisper. The worker owns PyAV validation, model loading,
and inference. PostgreSQL is authoritative for state and results; Redis is the durable handoff queue;
generated media keys—not client filenames—address files on a shared named volume.

Native mode keeps the original synchronous path for focused development and real-model smoke tests.
There the default model loads during API startup, blocking validation/inference runs off the HTTP
event loop, and no upload or result is retained. The two modes share the same `TranscriptionService`,
model registry, media validation, schemas, and typed errors.

Shared data and adapter protocols live in `domain.py`; model caching lives in `services/models.py`;
the use case lives in `services/transcription.py`. The review findings and remaining boundaries are
recorded in [`docs/code-review.md`](docs/code-review.md).

## Run locally

### Docker Compose

Prerequisite: Docker Desktop with the Compose plugin (or Docker Engine and Compose v2). From the
repository root, start the CPU-first service:

```bash
docker compose up --build
```

Compose starts `api`, `worker`, `postgres`, and `redis`. On first startup, the worker downloads the
pinned model (about 500 MB) into a named Docker volume. The download is separate from the image and
is reused on later starts. Once `/health/ready` succeeds, open <http://localhost:8000/>. Stop the
foreground stack with `Ctrl+C`; all named volumes remain available. To run detached instead, use
`docker compose up --build -d`, follow logs with `docker compose logs -f api worker`, and stop with
`docker compose down`.

The application containers run as a non-root user with read-only root filesystems, bounded temporary
filesystems, dropped capabilities, and `no-new-privileges`. The port is bound to `127.0.0.1`, so the
service is not exposed to other machines by default. Compose reads optional overrides from `.env`;
start from `cp .env.example .env` to customize the model or limits. Named volumes preserve model
weights, PostgreSQL state/results, Redis AOF data, and accepted media. `docker compose down --volumes`
removes all four and is intentionally destructive.

Run the container readiness and browser-page smoke check with `make docker-smoke`. It builds and
starts the stack, waits for API readiness, checks the browser page, and leaves the services running.
`make docker-down` stops them without deleting durable data.

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
- Submit transcription: `POST http://localhost:8000/v1/transcriptions`
- Job status: `GET http://localhost:8000/v1/transcriptions/{job_id}`
- Job result: `GET http://localhost:8000/v1/transcriptions/{job_id}/result`

Example:

```bash
curl --fail \
  -F "file=@/absolute/path/to/sample.wav" \
  -F "language=he" \
  -F "audio_mode=speech" \
  -F "model_profile=faster" \
  http://localhost:8000/v1/transcriptions
```

In Docker Compose, this returns `202 Accepted`, sets `Location` to the status resource, and returns
both polling URLs:

```json
{
  "id": "163df38c-bd7d-4946-b182-bcf367ffa18b",
  "status": "queued",
  "status_url": "http://localhost:8000/v1/transcriptions/163df38c-bd7d-4946-b182-bcf367ffa18b",
  "result_url": "http://localhost:8000/v1/transcriptions/163df38c-bd7d-4946-b182-bcf367ffa18b/result"
}
```

Poll `status_url` until it reports `succeeded`, then fetch `result_url`. Native mode defaults to the
original synchronous `200 OK` response:

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
      "end": 2.39,
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

Run `make package` to build the distribution wheel. CI builds it as a separate check: editable
development installs alone do not validate the files that Docker's non-editable install needs.

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

Phase 5 verification on 1 November 2026 passed Ruff, mypy, all 63 fast tests with 95% coverage,
wheel packaging, and real-model smoke tests for generated English, Hebrew, mixed-language, and
MP4-with-audio samples. Corrupt allowed media returned the sanitized `415 unsupported_media`
contract. The documented `make smoke` command was also exercised successfully. Docker Desktop on
Apple Silicon built the ARM64 image from scratch and ran the Compose-configured service. Its first
startup populated a 464 MB named model-cache volume and completed real inference; a restart using
the same volume initialized the model in under one second without downloading the weights again.
Readiness, the browser UI, OpenAPI, non-root execution, read-only root filesystem, bounded tmpfs,
dropped capabilities, and `no-new-privileges` were verified. Port `8002` was used for this check
because the project's native development server was already using the documented port `8000`.

Phase 6 verification on 1 November 2026 passed Ruff, mypy, all 68 fast tests, wheel packaging, and
the complete four-service Compose flow. A generated English M4A received `202`, moved through
`queued` to `succeeded`, and produced a real faster-whisper result. Restarting both API and worker
preserved and returned the same PostgreSQL-backed result. Redis and PostgreSQL health checks passed,
the API did not load a model, and the worker reused the existing model-cache volume. Port `8002` was
again used only to avoid the already-running native development server.

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
| `TRANSCRIPTION_MAX_CONCURRENT_UPLOADS` | `2` | Admitted upload requests, including parsing, queueing, and processing |
| `TRANSCRIPTION_MAX_MEDIA_DURATION_SECONDS` | `900` | 15-minute synchronous limit |
| `TRANSCRIPTION_MAX_CONCURRENT_TRANSCRIPTIONS` | `1` | Shared decoding, model loading, and inference slots per process |
| `TRANSCRIPTION_INFERENCE_QUEUE_TIMEOUT_SECONDS` | `5` | Wait before returning `model_busy` |
| `TRANSCRIPTION_ASYNC_JOBS_ENABLED` | `false` | Enable durable job submission; Compose sets this to `true` |
| `TRANSCRIPTION_DATABASE_URL` | Compose PostgreSQL URL | Authoritative job and result store |
| `TRANSCRIPTION_REDIS_URL` | Compose Redis URL | Durable job broker |
| `TRANSCRIPTION_REDIS_QUEUE_NAME` | `transcription:jobs` | Redis queue key |
| `TRANSCRIPTION_MEDIA_STORAGE_PATH` | `/data/media` | Generated-key media directory shared by API and worker |

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

### PostgreSQL only for the durable deployment

The requested workflow is upload → transcribe → return. There are no accounts, history, sharing,
audit, billing, or retention requirements. Adding PostgreSQL would introduce migrations,
transactions, cleanup rules, privacy obligations, and authorization questions without supporting a
current requirement.

That original synchronous mode remains intentionally stateless: uploaded media is closed after the
request, and neither media nor transcript is retained.

Durable asynchronous jobs now justify PostgreSQL. Compose stores job state and JSON results there,
while Redis handles queue delivery and a generated-key media volume bridges API and worker. These
services are local containers with named volumes, not managed subscriptions. Keeping persistence
behind `JobRepository` and `MediaStore` protocols preserves the original use case and leaves room
for S3-compatible media storage or a managed database at multi-host scale.

### Synchronous, bounded requests

The first product returns the transcription in the upload response because that directly matches
the assignment. File size, media duration, model concurrency, and queue wait are bounded to prevent
uncontrolled resource consumption.

Transport limits apply before dependencies or inference: at most two upload requests are admitted
per process by default; more receive `503 upload_busy` with `Retry-After`. Each body is limited to
the file limit plus 64 KiB of multipart overhead, including when Content-Length is absent or wrong.
Starlette parses at most one file and three form fields of up to 4 KiB each. The exact file-size
limit is also checked after parsing. Parser errors and early rejections close partial uploads.
If increasing file size or admission concurrency, also increase Compose's 256 MiB temporary
filesystem appropriately. These limits are per process, not shared across replicas.

For Compose deployments, the implemented local architecture is:

```text
POST upload → local media volume → local Redis queue → inference worker
       │                                                 │
       └── 202 + job ID                    PostgreSQL result/status

GET /v1/transcriptions/{job_id}
GET /v1/transcriptions/{job_id}/result
```

Every component runs through Docker Compose on the reviewer's machine. Redis is self-hosted and
requires no managed service or subscription. Uploaded media lives on a named Docker volume behind a
`MediaStore` interface; S3-compatible storage remains a deployment option, not a local requirement.
An in-process background task remains rejected because a process restart would lose accepted work.

### One inference at a time by default

The default pipeline concurrency is one to keep CPU and model memory use predictable. Admitted
requests wait up to the configured queue timeout and then receive `503 model_busy` plus
`Retry-After`. Media decoding, lazy loading, and inference all occupy that slot and run in a worker
thread. A cancelled caller retains its stream and slot until the native worker finishes, because a
Python task cancellation cannot safely stop a native decoder or inference thread. Processing time
includes validation, loading, and inference; it excludes upload and queue wait.

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

Duration headers are an early check, not a resource guarantee. PyAV also decodes the first audio
stream incrementally and counts samples, stopping at the configured limit even when duration
metadata is missing or under-reported. Faster-whisper decodes that validated input again for its
own preprocessing. This extra decode is an intentional CPU trade-off that bounds input before the
model allocates its full audio array, without duplicating faster-whisper's preprocessing logic.
Segment timestamps are clipped to the decoded recording duration and empty/out-of-range spans are
discarded. This fixes impossible timelines; it does not correct hallucinated words.

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
| `503` | Model unavailable, upload admission full, or inference queue timeout |

Internal exception text, stack traces, and local paths are logged server-side and are not returned
to clients. Every response includes `X-Request-ID` for correlation.

### Same-origin, buildless browser interface

The upload experience is a single HTML page with local CSS and JavaScript served by FastAPI. It
submits to the existing API and follows a `202` response by polling job status and fetching the
result. Native synchronous responses remain supported. There is no separate frontend server,
framework, bundler, or extra runtime dependency. Static assets are packaged with the Python wheel,
and the page does not load fonts, scripts, or styles from third-party CDNs.
The page obtains the transcription URL and upload limit from `/client-config`, so a configured API
prefix works without changing JavaScript. Server-side validation remains authoritative.

This intentionally small interface is appropriate for the assignment's submit-and-poll workflow. A
larger product with navigation, authentication, job history, or multiple frontend teams
could justify a separately deployed frontend and its added build and deployment pipeline.

### CPU-first local stack and explicit durable volumes

The Docker image installs the exact locked Python environment, runs application processes as an
unprivileged user, and keeps model weights out of the image. Compose uses separate named volumes for
the Hugging Face cache, queued media, PostgreSQL data, and Redis AOF persistence. The API uses one
Uvicorn worker and does not load a model; one independent inference worker loads and reuses the
default model. The port binds to localhost, and both application containers drop Linux capabilities
and have read-only root filesystems.

This keeps rebuilds smaller than baking roughly 500 MB of weights into every image and keeps local
startup convenient on CPU-only machines. Alternatives include baking model weights into an image
for an offline deployment, or using a bind mount for a host-managed cache; those choices trade
larger image distribution or host-specific permissions for their respective operational benefits.
For multiple hosts, move media behind an S3-compatible `MediaStore`, use shared managed equivalents
for PostgreSQL/Redis if desired, and warm each worker's model cache. Phase 7 adds explicit retention
before this local durable stack should be exposed as a long-running public service.

The base image is pinned to the maintained Python 3.12 Bookworm line rather than an immutable
digest, so rebuilding can pick up upstream security fixes. A release deployment that requires
bit-for-bit image reproducibility should pin the base image digest and update it through a deliberate
security-update process.

GitHub Actions runs the same locked Ruff, mypy, and fake-transcriber pytest checks as local
development. It does not download model weights or handle secrets. Third-party workflow actions are
pinned to commit SHAs to make the CI toolchain changes explicit and reviewable.

Wheel packaging relies on Hatch's normal package discovery for static files. The review caught a
duplicate `force-include` rule that broke wheel builds despite passing editable-install tests; that
rule was removed and CI now also runs `make package`.

## Implementation roadmap

Phase 1 below is implemented as repository Phase 6. Phases 2 and 3 remain ordered work. The goal is
to meet the backend-interview requirements while keeping the complete development and review
environment local and reproducible with one Docker Compose command.

### Phase 1 — Local durable asynchronous jobs (implemented)

- Split the Compose deployment into `api` and `worker` services while reusing the existing media,
  model-registry, and `Transcriber` boundaries.
- Add local Redis as the job broker, local PostgreSQL for durable job state and result metadata, and
  named volumes for PostgreSQL, Redis persistence, uploaded media, and the Hugging Face cache.
- Save an accepted upload before enqueueing it, create the job record transactionally, and return
  `202 Accepted` with a job ID and `Location` header.
- Add `GET /v1/transcriptions/{job_id}` for status and
  `GET /v1/transcriptions/{job_id}/result` for completed results.
- Use an explicit state machine: `queued`, `running`, `succeeded`, `failed`, and `expired`. Do not
  describe a worker thread in the API process as an asynchronous job worker.
- Add integration tests plus a Compose smoke test that restart API and worker and prove accepted work
  and completed results survive process restarts.

### Phase 2 — Reliable job execution and lifecycle (next)

- Add bounded retries with attempt counts, backoff, stable failure codes, and a dead-letter or
  terminal-failure policy.
- Enforce hard execution deadlines through worker-process isolation; an asyncio timeout alone cannot
  safely stop native decoding or inference.
- Support an idempotency key and a SHA-256 upload hash so repeated submissions do not create duplicate
  work. Reuse successful results only when the model revision and transcription options also match.
- Define configurable retention for source media, jobs, and results, plus a cleanup worker that marks
  expired jobs before deleting their artifacts.
- Make job claiming atomic, recover jobs abandoned by a crashed worker, and expose queue/worker
  readiness without requiring the model to run during a health probe.

### Phase 3 — Close the remaining interview-scope gaps

- Add an explicit media-normalization adapter that produces and tests 16 kHz mono audio before
  inference instead of relying only on faster-whisper's internal preprocessing.
- Define silent-input behavior in the public contract and tests. Silence should produce a deliberate
  `no_speech_detected` result or warning rather than an accidental empty response.
- Add JSON, TXT, SRT, and VTT result rendering from the same timestamped segment model.
- Add local rate limiting and document where a reverse proxy or API-key layer would enforce quotas in
  a public deployment.
- Add application-level chunking for long recordings only after the durable job pipeline works, so
  chunks can be retried and assembled without keeping an HTTP request open.

Deferred beyond these phases: diarization, translation, LLM summaries, transcript editing, billing,
PII redaction, URL ingestion, completion webhooks, managed object storage, Kubernetes, and Prometheus
metrics. These remain useful scaling or product discussions, not prerequisites for the next build.

## Current limitations and next steps

- Durable jobs currently have one delivery attempt. Bounded retries, hard process deadlines,
  idempotency/hash caching, abandoned-claim recovery, retention, cleanup, and worker-aware readiness
  are Phase 7.
- Only the first audio stream in a video container is transcribed; the API does not yet expose audio
  track selection.
- There is no authentication or rate limiting. Do not expose this service directly to an untrusted
  public network in its current form.
- Automatic language detection and Hebrew/English code-switching are model-dependent and may be
  less accurate than selecting the dominant language explicitly.
- Music mode processes the full audio track and is therefore slower than speech mode.
- Docker uses CPU inference and a single worker. A production GPU or multi-replica deployment needs
  deployment-specific resource allocation, scaling, and model-cache warming.
- Native decoding/inference has no hard execution deadline. Compose moves it out of the API but the
  Phase 6 worker still performs native work in its long-lived process. Phase 7 adds per-attempt
  subprocess isolation and hard termination; public deployment also needs ingress timeouts,
  authentication, and request-rate limits.

## Updating this decision record

When making a major decision or accepting a shortcut:

1. Update the relevant README section in the same change.
2. State the requirement or observed failure that motivated it.
3. Record the chosen approach and meaningful alternatives.
4. Describe the operational or maintenance consequence.
5. Add the condition under which the decision should be revisited.

This keeps the repository understandable to reviewers without requiring access to the original
conversation.


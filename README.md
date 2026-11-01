# Transcription Service

A production-minded FastAPI service that accepts an audio or video file and returns a timestamped
transcription produced by an open multilingual Whisper model from Hugging Face.

The service is asynchronous-only. Docker Compose runs the API, an independent inference worker,
Redis, and PostgreSQL entirely on the local machine—no hosted service or subscription is required.
See [`docs/architecture-plan.html`](docs/architecture-plan.html) for the completed Phases 6–8 plan.

## Current capabilities

- Upload WAV, MP3, M4A/AAC, FLAC, OGG, Opus, MP4, MOV, WebM, or MKV through
  `multipart/form-data`.
- Decode the first audio stream and normalize it to 16 kHz mono PCM before inference.
- Transcribe Hebrew (`he`), English (`en`), or another Whisper-supported language.
- Automatically detect the language with `language=auto`.
- Choose `audio_mode=speech` for faster VAD-filtered processing or `audio_mode=music` to process the
  complete audio track.
- Choose `model_profile=faster` (default, Whisper small) or `model_profile=larger` (Whisper large-v3),
  independently of Speech/Music mode. Larger does not guarantee more accurate lyrics.
- Return full text, detected-language confidence, media duration, processing time, and timestamped
  segments.
- Split normalized long recordings into resumable chunks, retry each chunk independently, and
  assemble text and timestamps in source order.
- Return a deliberate `no_speech_detected` warning for exact silence or an empty model result.
- Download completed results as JSON, TXT, SRT, or WebVTT from one timestamped result model.
- Accept uploads durably with `202 Accepted`, then expose job status and result resources.
- Keep accepted jobs and completed results across independent API and worker restarts in local
  PostgreSQL; persist queued media and the Redis queue in named Docker volumes.
- Run every attempt in a terminable child process with a hard deadline.
- Retry transient failures with bounded exponential backoff, recover expired worker leases, and use
  atomic PostgreSQL claims so multiple workers cannot execute the same attempt concurrently.
- Support `Idempotency-Key` and SHA-256 result reuse keyed by media, model revision, language,
  audio mode, and model profile.
- Retain failed media for 24 hours and completed JSON results for seven days by default, then mark
  jobs expired before deleting artifacts.
- Enforce configurable file-size, media-duration, queue-timeout, and inference-concurrency limits.
- Apply a process-local sliding-window upload rate limit; public deployments still require an
  authenticated ingress quota.
- Expose liveness, component-aware readiness, and generated OpenAPI documentation.
- Recover an in-progress browser job after a page reload using its durable polling URLs.
- Delete successfully processed source media immediately. `docker compose down --volumes`
  deliberately removes all local durable data.

## Architecture

```text
Browser / curl → bounded API upload → media volume → PostgreSQL job
                                      │                 │
                                      └→ local Redis → independent worker
                                                         │
                                                         ├→ PyAV 16 kHz mono chunks
                                                         ├→ chunk checkpoints
                                                         ├→ faster-whisper
                                                         └→ PostgreSQL result
```

The API hashes and persists media and job state before
enqueueing, returns promptly with `202`, and never loads Whisper. A worker supervisor atomically
claims jobs and launches a fresh child process for PyAV validation, model loading, and inference.
The supervisor can terminate that child at the configured deadline without killing the queue
consumer. PostgreSQL is authoritative for state, attempts, leases, expiry, and results; Redis is the
durable handoff queue and worker-heartbeat channel; generated media keys—not client filenames—address
files on a shared named volume.

Shared data and adapter protocols live in `domain.py`; the blocking worker pipeline lives in
`services/transcription.py`. Each isolated attempt loads only its selected model. The API never
loads Whisper or performs media decoding. Review findings are recorded in
[`docs/code-review.md`](docs/code-review.md).

## Run locally

### Docker Compose

Prerequisite: Docker Desktop with the Compose plugin (or Docker Engine and Compose v2). From the
repository root, start the CPU-first service:

```bash
docker compose up --build
```

Compose starts `api`, `worker`, `postgres`, and `redis`. The first submitted job downloads the pinned
model (about 500 MB) into a named Docker volume. The download is separate from the image and is
reused by later attempts. `/health/ready` checks the API, PostgreSQL, Redis, and a live worker
heartbeat without loading the model. Once it succeeds, open <http://localhost:8000/>. Stop the
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

### Source development

Requirements: Python 3.12 and [`uv`](https://docs.astral.sh/uv/). Docker Compose remains the runtime
because the API always requires its job store, broker, media volume, and worker.

Install the locked runtime and development dependencies (or run `make setup`):

```bash
uv sync --frozen --all-groups
```

Optionally create local configuration:

```bash
cp .env.example .env
```

Use `make run` or `docker compose up --build` to run the complete application. Running only Uvicorn
is intentionally unsupported because it could accept no durable work without the worker and local
infrastructure.

Open <http://localhost:8000/> for the browser interface. Choose an audio or video file, select
Auto-detect, Hebrew, or English, choose Speech or Music mode, and select Faster or Larger model.
The larger model may download several GB on first use and needs additional RAM. The page shows progress,
recovers the active job after reload, and offers copy plus JSON/TXT/SRT/VTT downloads. You can use the API
directly at <http://localhost:8000/docs> or with the `curl` example below.

The first worker attempt downloads approximately 500 MB of model files into the Hugging Face user cache
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
- Text/subtitle export: append `?format=txt`, `?format=srt`, or `?format=vtt`

Example:

```bash
curl --fail \
  -H "Idempotency-Key: example-upload-001" \
  -F "file=@/absolute/path/to/sample.wav" \
  -F "language=he" \
  -F "audio_mode=speech" \
  -F "model_profile=faster" \
  http://localhost:8000/v1/transcriptions
```

`Idempotency-Key` is optional. Reusing it with the same media and options returns the existing job;
reusing it for a different request returns `409 idempotency_conflict`. Even without the header, a
matching SHA-256, model revision, language, audio mode, and model profile reuses active work or an
unexpired successful result.

Every submission returns `202 Accepted`, sets `Location` to the status resource, and returns
both polling URLs:

```json
{
  "id": "163df38c-bd7d-4946-b182-bcf367ffa18b",
  "status": "queued",
  "status_url": "http://localhost:8000/v1/transcriptions/163df38c-bd7d-4946-b182-bcf367ffa18b",
  "result_url": "http://localhost:8000/v1/transcriptions/163df38c-bd7d-4946-b182-bcf367ffa18b/result"
}
```

Poll `status_url` until it reports `succeeded`, then fetch `result_url`, which returns:

```json
{
  "job_id": "163df38c-bd7d-4946-b182-bcf367ffa18b",
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
  ],
  "warnings": []
}
```

The default result representation is JSON. The same completed result can be rendered without another
inference pass:

```bash
curl --fail --output transcript.txt "$RESULT_URL?format=txt"
curl --fail --output transcript.srt "$RESULT_URL?format=srt"
curl --fail --output transcript.vtt "$RESULT_URL?format=vtt"
```

An exact-silent file succeeds with empty text and `"warnings": ["no_speech_detected"]`; silence is a
valid, explicit result rather than an infrastructure failure.

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

With the Compose stack running, execute a real-model smoke test against one or more local samples:

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

The smoke test waits for the running job service, submits each sample, polls its durable status, and
fetches the completed result. It is excluded from normal CI because it downloads large weights and
its execution time depends on the available hardware. Previous Compose verification covered real
inference, restart persistence, idempotency and hash reuse, hard deadlines, readiness expiry, and
two workers competing safely for one job.

Phase 8 passes Ruff formatting/lint, strict mypy, JavaScript syntax checking, 70 tests with 82%
coverage, and wheel packaging. Docker Desktop rebuilt the image and the API, worker, PostgreSQL,
and Redis reached readiness on port 8002.

## Configuration

All settings use the `TRANSCRIPTION_` environment prefix. Defaults are listed in `.env.example`.

| Variable | Default | Purpose |
| --- | --- | --- |
| `TRANSCRIPTION_MODEL_ID` | `Systran/faster-whisper-small` | Hugging Face CTranslate2 model |
| `TRANSCRIPTION_MODEL_REVISION` | `536b066...` | Reproducible Hugging Face model commit |
| `TRANSCRIPTION_LARGER_MODEL_ID` | `Systran/faster-whisper-large-v3` | Optional larger model selected per attempt |
| `TRANSCRIPTION_LARGER_MODEL_REVISION` | `edaa852...` | Pinned large-v3 model commit |
| `TRANSCRIPTION_DEVICE` | `cpu` | CTranslate2 device: `cpu`, `cuda`, or `auto` |
| `TRANSCRIPTION_COMPUTE_TYPE` | `int8` | CPU-friendly quantized inference |
| `TRANSCRIPTION_BEAM_SIZE` | `5` | Beam-search quality/speed trade-off |
| `TRANSCRIPTION_MAX_UPLOAD_BYTES` | `104857600` | 100 MiB file-size limit |
| `TRANSCRIPTION_MAX_CONCURRENT_UPLOADS` | `2` | Admitted upload requests, including parsing, queueing, and processing |
| `TRANSCRIPTION_MAX_MEDIA_DURATION_SECONDS` | `900` | 15-minute worker-side media limit |
| `TRANSCRIPTION_CHUNK_DURATION_SECONDS` | `300` | Normalized WAV chunk length used for resumable inference |
| `TRANSCRIPTION_CHUNK_MAX_ATTEMPTS` | `2` | Attempts for one chunk inside a job attempt |
| `TRANSCRIPTION_RATE_LIMIT_REQUESTS` | `10` | Upload submissions allowed per client/window/API process |
| `TRANSCRIPTION_RATE_LIMIT_WINDOW_SECONDS` | `60` | Sliding upload-rate window in seconds |
| `TRANSCRIPTION_DATABASE_URL` | Compose PostgreSQL URL | Authoritative job and result store |
| `TRANSCRIPTION_REDIS_URL` | Compose Redis URL | Durable job broker |
| `TRANSCRIPTION_REDIS_QUEUE_NAME` | `transcription:jobs` | Redis queue key |
| `TRANSCRIPTION_MEDIA_STORAGE_PATH` | `/data/media` | Generated-key media directory shared by API and worker |
| `TRANSCRIPTION_MAX_STORED_MEDIA_BYTES` | `5368709120` | 5 GiB local queued/failed-media budget |
| `TRANSCRIPTION_MAX_PENDING_JOBS` | `100` | Atomic queued-plus-running admission limit |
| `TRANSCRIPTION_JOB_MAX_ATTEMPTS` | `3` | Maximum attempts before terminal failure |
| `TRANSCRIPTION_JOB_RETRY_BACKOFF_SECONDS` | `5` | Initial exponential retry delay |
| `TRANSCRIPTION_JOB_TIMEOUT_SECONDS` | `1800` | Hard wall-clock deadline per child process |
| `TRANSCRIPTION_JOB_LEASE_SECONDS` | `30` | Renewable claim lease; expiry enables crash recovery |
| `TRANSCRIPTION_FAILED_MEDIA_RETENTION_SECONDS` | `86400` | Retain failed source media for diagnosis |
| `TRANSCRIPTION_RESULT_RETENTION_SECONDS` | `604800` | Keep successful JSON result before expiry |
| `TRANSCRIPTION_CLEANUP_INTERVAL_SECONDS` | `900` | Expiry and artifact-cleanup cadence |
| `TRANSCRIPTION_WORKER_POLL_SECONDS` | `2` | Queue poll and due-job dispatch cadence |
| `TRANSCRIPTION_WORKER_HEARTBEAT_TTL_SECONDS` | `15` | Readiness heartbeat lifetime |

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

### Explicit per-attempt model selection

The browser and API expose the allowlisted profiles `faster` and `larger`. Existing requests default
to `faster`, preserving the CPU-friendly small model. `larger` selects `Systran/faster-whisper-large-v3`
at revision `edaa852ec7e145841d8ffdb056a99866b5f0a478`. Clients cannot supply arbitrary model IDs or
revisions; operators configure those through environment variables. Both profiles remain independent
of `audio_mode`, and responses identify the model actually used.

The child-process entry point selects and loads exactly one configured model for its job. Disk
weights remain cached, but the model instance is released when the attempt exits. This is
intentionally slower than a permanent model process in exchange for enforceable deadlines, clean
memory reclamation, and keeping inference outside the HTTP process.

The UI deliberately says “Larger model”, not “Higher accuracy”. On the same 10.3-second Hebrew song
clip, small took 15.2 seconds and produced garbled words; large-v3 took 21.5 seconds and returned
only “תודה רבה.” with an implausible timestamp. These are one-clip observations, not a benchmark or
quality guarantee. Singing remains best-effort, and the larger option is retained for user comparison.

### PostgreSQL for durable job state

Durable asynchronous jobs now justify PostgreSQL. Compose stores job state and JSON results there,
while Redis handles queue delivery and a generated-key media volume bridges API and worker. These
services are local containers with named volumes, not managed subscriptions. Keeping persistence
behind `JobRepository` and `MediaStore` protocols preserves the original use case and leaves room
for S3-compatible media storage or a managed database at multi-host scale.

### At-least-once delivery with atomic leased claims

Redis delivers job IDs, but PostgreSQL owns the state machine. A conditional `queued → running`
update increments the attempt count and installs a renewable lease in one statement. Duplicate
Redis deliveries and competing supervisors are harmless because only one claim succeeds. A worker
crash lets the lease expire; another supervisor returns the job to `queued`, unless its attempt
budget is exhausted. A periodic database scan also redispatches due rows, closing the narrow gap
between a committed job/retry and Redis enqueueing.

Transient process/inference failures retry up to three attempts by default with exponential backoff.
Typed media and request errors are terminal immediately. The API exposes attempts, maximum attempts,
next retry time, stable terminal code/message, and expiry. Delivery is at least once: a crash after
native work but before committing its result can repeat the work, but never concurrently under a
valid lease.

### Hard deadlines through disposable child processes

An asyncio timeout cannot stop C/C++ codec or CTranslate2 work safely. The supervisor therefore runs
one attempt with `python -m transcription_service.attempt`, renews its lease while waiting, and sends
terminate/kill when the wall-clock deadline is exceeded. Timeout is a retryable `job_timeout`; after
the final attempt it becomes the durable terminal error. The trade-off is loading model state in
each child. The Hugging Face volume avoids network downloads, but repeated process startup still
adds latency. A future GPU service could preserve warm model processes only if it also provides an
equally strong external cancellation boundary.

### Idempotency, hash caching, and retention

The API hashes media while writing it once to generated-key storage. Repository advisory locks make
the decision atomic: an `Idempotency-Key` returns its original job when request identity matches and
returns `409` when it does not; otherwise an active or successful unexpired job with the same SHA-256,
model revision, language, audio mode, and model profile is reused. Failed and expired results are not
cache hits.

Successful source media and its chunk checkpoint are deleted immediately; its result expires after
seven days by default. Terminal-failure media is retained for 24 hours for local diagnosis. Cleanup first changes
the authoritative row to `expired` and clears its result, then removes any media artifact. Job
tombstones and idempotency keys remain so clients receive stable status; eventual tombstone deletion
requires a separate product policy.

### Bounded asynchronous submissions

The API persists the upload and returns a job resource rather than keeping HTTP open during model
execution. File size, media duration, pending jobs, and worker execution are bounded to prevent
uncontrolled resource consumption.

Transport limits apply before dependencies or inference: at most two upload requests are admitted
per process by default; more receive `503 upload_busy` with `Retry-After`. Each body is limited to
the file limit plus 64 KiB of multipart overhead, including when Content-Length is absent or wrong.
Starlette parses at most one file and three form fields of up to 4 KiB each. The exact file-size
limit is also checked after parsing. Parser errors and early rejections close partial uploads.
If increasing file size or admission concurrency, also increase Compose's 256 MiB temporary
filesystem appropriately. These limits are per process, not shared across replicas.

The API also admits ten upload submissions per client address per 60-second sliding window by
default and returns `429 rate_limit_exceeded` with `Retry-After` when exceeded. This limiter is
process-local and intentionally reads the socket client address rather than trusting a caller's
`X-Forwarded-For` header. For public deployment, terminate traffic at a trusted reverse proxy that
normalizes forwarding headers and enforces shared per-API-key or per-account quotas. Add API-key or
user authentication before relying on an identity-based limit; the local limiter is defense in
depth, not a multi-replica quota system.

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

### Explicit inference capacity and process isolation

Compose uses one worker supervisor by default. It atomically claims one job, then starts a child
process for the complete native pipeline. The supervisor renews both its job lease and readiness
heartbeat while waiting. At the deadline it terminates the child, records `job_timeout`, and retries
within the configured attempt budget. Each attempt reloads the model into a fresh process, so hard
termination and memory reclamation cost startup latency; the named Hugging Face cache avoids another
network download. Scale supervisors explicitly according to CPU/GPU and memory capacity. PostgreSQL
claims prevent two supervisors from running the same attempt concurrently.

### PyAV for media handling

PyAV uses bundled FFmpeg libraries, so the application does not shell out to a system FFmpeg
binary. The service checks the declared extension/MIME type for product policy, then uses PyAV as
the authoritative container and audio-stream validation. Client-provided filenames are never used
as server filesystem paths.

Audio and video intentionally use the same endpoint and transcription pipeline. For MP4, MOV,
WebM, and MKV, PyAV selects the first audio stream; the video frames are not decoded. It resamples
decoded frames to signed 16-bit, 16 kHz mono PCM and writes bounded in-memory WAV chunks. A readable
video without audio returns the typed `no_audio_stream`
client error. Selecting among multiple audio tracks is deferred until a product requirement defines
the desired user experience and API contract.

PyAV decodes the first audio stream incrementally and counts normalized samples, stopping at the
configured limit even when duration metadata is missing or under-reported. Each chunk result is
checkpointed atomically beside the generated media key. A retry skips completed chunks, retries the
failed chunk, and offsets its segment timestamps during ordered assembly. The whole normalized PCM
buffer is currently held in worker memory; an external chunk/object pipeline is the next step for
multi-hour recordings. Segment timestamps are clipped to decoded duration and empty spans are
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

Submission and result HTTP status codes include:

| Status | Meaning |
| --- | --- |
| `400` | Empty or malformed request |
| `409` | Idempotency key conflict or result requested before completion |
| `413` | Upload exceeds the configured byte limit |
| `422` | Invalid request option or missing upload |
| `429` | Local upload-rate window exceeded |
| `503` | Job infrastructure unavailable, worker heartbeat absent, or admission full |
| `507` | Configured local media-storage budget exhausted |

Media decoding, duration, language support, and inference errors happen after acceptance. They are
stored as stable `error_code` and `error_message` values on the failed job instead of being returned
from the upload request. Internal exception text, stack traces, and local paths are logged
server-side and are not returned to clients. Every response includes `X-Request-ID` for correlation.

### Same-origin, buildless browser interface

The upload experience is a single HTML page with local CSS and JavaScript served by FastAPI. It
submits to the existing API and follows a `202` response by polling job status and fetching the
result. There is no separate frontend server, framework, bundler, or extra runtime dependency.
Static assets are packaged with the Python wheel, and the page does not load fonts, scripts, or
styles from third-party CDNs.
The page obtains the transcription URL and upload limit from `/client-config`, so a configured API
prefix works without changing JavaScript. It stores only the active job's polling URLs and display
filename in browser local storage, resumes polling after reload, and clears that record at a terminal
state. Completed downloads are rendered by the API rather than reconstructed in JavaScript.
Server-side validation remains authoritative.

This intentionally small interface is appropriate for the assignment's submit-and-poll workflow. A
larger product with navigation, authentication, job history, or multiple frontend teams
could justify a separately deployed frontend and its added build and deployment pipeline.

### CPU-first local stack and explicit durable volumes

The Docker image installs the exact locked Python environment, runs application processes as an
unprivileged user, and keeps model weights out of the image. Compose uses separate named volumes for
the Hugging Face cache, queued media, PostgreSQL data, and Redis AOF persistence. The API uses one
Uvicorn worker and does not load a model; the worker supervisor starts a bounded child per attempt.
The port binds to localhost, and both application containers drop Linux capabilities and have
read-only root filesystems.

This keeps rebuilds smaller than baking roughly 500 MB of weights into every image and keeps local
startup convenient on CPU-only machines. Alternatives include baking model weights into an image
for an offline deployment, or using a bind mount for a host-managed cache; those choices trade
larger image distribution or host-specific permissions for their respective operational benefits.
For multiple hosts, move media behind an S3-compatible `MediaStore`, use shared managed equivalents
for PostgreSQL/Redis if desired, and warm each worker's model cache. Authentication, authorization,
and ingress rate limits remain required before public exposure.

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

Phases 1–3 below are implemented as repository Phases 6–8.
The goal is to meet the backend-interview requirements while keeping the complete development and
review environment local and reproducible with one Docker Compose command.

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

### Phase 2 — Reliable job execution and lifecycle (implemented)

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

### Phase 3 — Close the remaining interview-scope gaps (implemented)

- Add an explicit media-normalization adapter that produces and tests 16 kHz mono audio before
  inference instead of relying only on faster-whisper's internal preprocessing.
- Define silent-input behavior in the public contract and tests. Silence should produce a deliberate
  `no_speech_detected` result or warning rather than an accidental empty response.
- Add JSON, TXT, SRT, and VTT result rendering from the same timestamped segment model.
- Add local rate limiting and document where a reverse proxy or API-key layer would enforce quotas in
  a public deployment.
- Add application-level chunking for long recordings only after the durable job pipeline works, so
  chunks can be retried and assembled without keeping an HTTP request open.
- Recover active browser polling after reload and expose every result format through the UI.

Deferred beyond these phases: diarization, translation, LLM summaries, transcript editing, billing,
PII redaction, URL ingestion, completion webhooks, managed object storage, Kubernetes, and Prometheus
metrics. These remain useful scaling or product discussions, not prerequisites for the next build.

## Current limitations and next steps

- Job records remain after their media/result payload expires so status can report `expired` and
  idempotency keys remain stable. A later account/history product should define archival or eventual
  tombstone deletion separately.
- Only the first audio stream in a video container is transcribed; the API does not yet expose audio
  track selection.
- The implemented rate limit is process-local and address-based. Public use still requires trusted
  proxy handling plus authenticated, deployment-wide quotas.
- Hash caching is deployment-wide because there are no users yet. If authentication is added, include
  tenant/authorization scope in both the idempotency uniqueness rule and cache identity.
- Automatic language detection and Hebrew/English code-switching are model-dependent and may be
  less accurate than selecting the dominant language explicitly.
- Music mode processes the full audio track and is therefore slower than speech mode.
- Docker uses CPU inference and one supervisor by default. A production GPU or multi-replica deployment needs
  deployment-specific resource allocation, scaling, and model-cache warming.
- Public deployment still needs ingress timeouts, authentication, authorization, and request-rate
  limits around the durable job API.

## Updating this decision record

When making a major decision or accepting a shortcut:

1. Update the relevant README section in the same change.
2. State the requirement or observed failure that motivated it.
3. Record the chosen approach and meaningful alternatives.
4. Describe the operational or maintenance consequence.
5. Add the condition under which the decision should be revisited.

This keeps the repository understandable to reviewers without requiring access to the original
conversation.


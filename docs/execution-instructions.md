# Execution Instructions

This guide takes a tester from a clean checkout to a running service, a completed transcription,
and local verification. Run commands from the repository root, the directory containing
`compose.yaml`, `Dockerfile`, `pyproject.toml`, and `uv.lock`.

The commands below use a POSIX shell (macOS/Linux, or a suitable Linux environment on Windows).
Read [Trade-offs & Assumptions](trade-offs-and-assumptions.md) for scope and known limitations.

Contents: [Prerequisites](#prerequisites) · [Build and run](#build-and-run-with-docker-compose) ·
[API walkthrough](#submit-and-poll-through-the-api) · [Testing](#test-the-solution) ·
[Configuration](#configuration-reference) · [Troubleshooting](#troubleshooting) ·
[Shutdown and reset](#stop-preserve-data-or-reset).

## Prerequisites

| Workflow | Requirements |
| --- | --- |
| Build and run the complete application | Docker Desktop with Docker Compose v2, or Docker Engine with the Compose v2 plugin; a running daemon. Host Python, PostgreSQL, Redis, and FFmpeg are not needed. |
| Follow the HTTP examples | `curl` and a local audio/video sample with audible speech. A browser is sufficient for the UI workflow. |
| Run source checks and tests | Python 3.12 (the package requires `>=3.12,<3.13`) and `uv`. CI and the Dockerfile use uv 0.13.6. Make is optional; equivalent commands are provided below. |
| Check frontend syntax | Node.js for `node --check`; Node is not needed to run the UI. |
| Exercise real inference | Initial network access to Hugging Face and enough disk/RAM for the selected model. |

Initial setup also requires access to container registries and Python packages/build dependencies.
The default small model downloads roughly 500 MB; large-v3 downloads several GB. Image layers,
Python dependencies, model cache, temporary files, and job volumes require additional disk space.
The worker is capped at 6 GiB RAM and two CPUs, and the API at 1 GiB/two CPUs. Allocate enough
Docker memory for those containers plus PostgreSQL, Redis, and Docker overhead; these limits are
caps, not measured minimum hardware requirements. CPU inference speed depends on the machine.

Linux inference requires Landlock ABI 3 (kernel 6.2+) and libseccomp2, which the image installs.
Docker Desktop uses its Linux VM kernel, not the macOS/Windows host kernel. Verify its sandbox
support using the [container probe](#verify-the-inference-sandbox). The default policy is designed
for CPU inference; GPU deployment requires a separate sandbox/device policy.

## Build and run with Docker Compose

### 1. Confirm the checkout and Docker tools

```bash
pwd
docker version
docker compose version
```

Check that the current directory contains `compose.yaml`. `docker version` must reach the daemon;
start Docker Desktop or Docker Engine if it cannot.

### 2. Review or create local configuration

Default Compose values work without a `.env` file. To customize settings, copy the template once
if you do not already have local configuration:

```bash
cp -n .env.example .env
```

Edit `.env` as needed. It is ignored by Git and excluded from the image. Do not copy a local `.env`
into the submission. Keep model IDs paired with their pinned revisions.

The template includes host-style PostgreSQL/Redis addresses, while Compose explicitly supplies its
own container addresses (`postgres`, `redis`) and `/data/media`. Those entries are not a reason
to start a host database. Compose also explicitly sets the runtime environment to `production`.
Only settings interpolated or passed in `compose.yaml` change the container environment; adding
an arbitrary application setting to `.env` alone does not pass it into a container.

If port 8000 is already used, add `TRANSCRIPTION_PORT=8002` to `.env` before starting. Container
port 8000 stays the same; the host URL becomes `http://localhost:8002`.

Validate the Compose file:

```bash
docker compose config --quiet
```

### 3. Build and start the four services

```bash
docker compose build
docker compose up -d
docker compose ps
```

The combined equivalent is `docker compose up --build -d`. `api` and `worker` share the built
`transcription-service:local` image; PostgreSQL 16 and Redis 7.2 run as separate services. The
image installs the locked Python runtime dependencies and packages the static UI. No system
`ffmpeg` binary or separate frontend build is required.

The model weights are not part of the image. They are downloaded when a job first selects that
profile and retained in the `model_cache` volume. Source changes require another image build;
the checkout is not bind-mounted for hot reload.

### 4. Wait for readiness

Set the base URL for subsequent examples. Change it here if you changed the published port:

```bash
BASE_URL=http://localhost:8000
curl --fail --show-error "$BASE_URL/health/live"
curl --fail --show-error "$BASE_URL/health/ready"
```

Liveness returns `{"status":"alive"}`. Readiness returns `{"status":"ready"}` once API startup,
PostgreSQL, Redis, and the worker heartbeat are available. Connection errors or a `503` during
startup mean retry after a few seconds. Inspect logs if readiness continues to fail:

```bash
docker compose logs --tail=100 api worker postgres redis
```

Readiness does not download or load a model. The worker container intentionally has no separate
Docker healthcheck; the API readiness endpoint checks its renewable heartbeat.

If Make is installed, `make docker-smoke` builds/starts the stack, retries readiness for roughly
15 minutes, checks the browser page and readiness endpoint, and leaves services running. It does
not submit media or exercise model inference.

### 5. Transcribe through the browser

Open <http://localhost:8000/> (or your configured host port):

1. Choose a supported audio/video file with audible speech.
2. Select Auto-detect, Hebrew, or English.
3. Choose Speech for ordinary speech, or Music to process the complete audio track without VAD.
4. Leave the model on Faster for the first evaluation. Larger requires more download space/RAM.
5. Submit, wait for the queued/running job to finish, and inspect text and timestamps.
6. Copy the text or download JSON, TXT, SRT, or VTT. Reloading while a job is active resumes polling.

The default limits are 100 MiB per file and 15 minutes of decoded media. Supported extensions are
WAV, MP3, M4A, AAC, FLAC, OGG, Opus, MP4, MOV, WebM, and MKV. Only the first audio stream is selected;
a video without audio fails asynchronously. Interactive OpenAPI is at <http://localhost:8000/docs>.

## Submit and poll through the API

### 1. Upload a sample

Replace the sample path before running. For a sample path containing spaces, keep the full `-F`
argument quoted as shown. `BASE_URL` is the variable set in the readiness step.

```bash
curl --fail --show-error -i \
  -H "Idempotency-Key: tester-speech-001" \
  -F "file=@/absolute/path/to/sample.wav" \
  -F "language=en" \
  -F "audio_mode=speech" \
  -F "model_profile=faster" \
  "$BASE_URL/v1/transcriptions"
```

Use `language=he` for Hebrew or `language=auto` for detection. The idempotency header is optional;
use a new key when changing the file or options. An accepted response has HTTP `202`, a `Location`
header, and a body shaped like:

```json
{
  "id": "163df38c-bd7d-4946-b182-bcf367ffa18b",
  "status": "queued",
  "status_url": "/v1/transcriptions/163df38c-bd7d-4946-b182-bcf367ffa18b",
  "result_url": "/v1/transcriptions/163df38c-bd7d-4946-b182-bcf367ffa18b/result"
}
```

Matching active/successful work may be reused, so the returned status need not be `queued`.
Both URLs and `Location` are origin-relative paths; resolve them against `BASE_URL`. Acceptance
does not guarantee that later decoding or inference will succeed.

### 2. Poll until the job reaches a terminal state

Set `JOB_ID` to the actual `id` returned by your upload, not the illustrative UUID above:

```bash
JOB_ID=replace-with-the-returned-job-id
curl --fail --show-error "$BASE_URL/v1/transcriptions/$JOB_ID"
```

Repeat the GET every few seconds until `status` is `succeeded`, `failed`, or `expired`. While a
job is `queued`/`running`, its result is unavailable. A transient failure may return to `queued`
with `next_retry_at`; status also exposes `attempts` and `max_attempts`. On `failed`, inspect
`error_code` and `error_message`. Do not treat a successful status GET as successful inference.

### 3. Fetch the completed result and exports

After `succeeded`:

```bash
RESULT_URL="$BASE_URL/v1/transcriptions/$JOB_ID/result"
curl --fail --show-error "$RESULT_URL"
curl --fail --show-error --output transcript.txt "$RESULT_URL?format=txt"
curl --fail --show-error --output transcript.srt "$RESULT_URL?format=srt"
curl --fail --show-error --output transcript.vtt "$RESULT_URL?format=vtt"
```

These commands write exports in the current directory; change the output paths if needed.
JSON contains `job_id`, `text`, `language`, `language_probability`, `duration_seconds`,
`processing_time_seconds`, `model`, timestamped `segments`, and `warnings`. Exact silence succeeds
with empty text and `warnings: ["no_speech_detected"]`; this is an intentional result.

### Errors to expect during evaluation

Application and request-validation errors use
`{"error":{"code":"...","message":"...","request_id":"..."}}`, with `X-Request-ID` for log
correlation. Host/origin middleware rejections can return plain text; early origin rejections
occur before request-ID assignment.

| HTTP status | Typical meaning |
| --- | --- |
| `400` | Empty/malformed upload, invalid idempotency key, or rejected Host header. |
| `403` | Rejected browser origin/cross-site request. |
| `404` | Unknown or invalid job ID. |
| `408` | Total upload receive deadline exceeded. |
| `409` | Idempotency mismatch, or result requested for a job without an available successful result. |
| `413` | Upload/body exceeds the byte limit. |
| `422` | Missing file or invalid request option. |
| `429` | Local submission-rate limit; respect `Retry-After`. |
| `503` | Infrastructure/worker unavailable, upload admission full, or pending-job limit reached. |
| `507` | Local media-storage budget exhausted. |

Media decoding, duration validation, model language support, and inference happen after acceptance.
Their failures appear on the job, rather than changing the original `202` into a later HTTP error.

## Test the solution

### 1. Install the source-development environment

With uv installed, check the version and create the locked Python 3.12 environment:

```bash
uv --version
uv python install 3.12
uv sync --python 3.12 --frozen --all-groups
```

`uv python install` is unnecessary if a suitable interpreter is already available. These commands
create `.venv`; do not submit that directory. Source tools run via `uv run`, so manual activation
is not required. Docker Compose remains the supported full application runtime; starting only
Uvicorn does not start the database, broker, or independent worker.

### 2. Run formatting, lint, typing, and fast tests

```bash
make check test
```

Without Make, use the equivalent commands:

```bash
uv lock --check
uv run --frozen ruff format --check .
uv run --frozen ruff check .
uv run --frozen mypy src
uv run --frozen pytest --cov=transcription_service --cov-report=term-missing
```

Expected outcome: successful lock/format/lint/type checks and all tests passing. The most recent
local run passed 78 tests with 78% coverage. Tests use in-memory job infrastructure and a fake
transcriber, with real PyAV media normalization tests; no running Compose stack or model weights
are required. Installing the environment still requires package access on an empty cache.

To run a focused suite, for example:

```bash
uv run --frozen pytest tests/integration/test_security.py
uv run --frozen pytest tests/unit/test_media.py
```

If Node.js is available, also check frontend syntax:

```bash
node --check src/transcription_service/static/assets/app.js
```

### 3. Build the distribution wheel

```bash
make package
```

Without Make:

```bash
uv build --wheel
```

The wheel is written to `dist/`. This separately validates packaging of the application and its
static UI; passing an editable-install test alone does not prove wheel packaging works. The build
may fetch Hatchling even when the runtime environment is already installed. `dist/` is ignored.

### Verify the inference sandbox

After building the image, run the standalone probe in a disposable container using the worker's
Compose restrictions. The test script is mounted because tests are excluded from the image:

```bash
docker compose run --rm --no-deps \
  -v "$PWD/tests/sandbox_probe.py:/sandbox_probe.py:ro" \
  worker python /sandbox_probe.py
```

Expected outcome: exit code zero and JSON listing blocked filesystem, network, command execution,
and cross-thread accesses. Allowed model reads and private-workspace writes must succeed. An
unsupported Linux kernel or sandbox setup failure must be resolved before real inference works.

For a macOS source environment, the direct alternative is:

```bash
uv run --frozen python tests/sandbox_probe.py
```

Run the macOS probe directly on the host: a surrounding Seatbelt sandbox can prevent initialization.
The direct probe leaves its printed temporary directory for inspection; the disposable container
removes its own temporary files on exit. See [Security Hardening](security-hardening.md) for policy.

### Run real-model smoke tests

Start Compose and confirm readiness first. With the source development dependencies installed,
run actual samples through the public API:

```bash
uv run --frozen python scripts/smoke_media.py \
  --base-url "$BASE_URL" \
  en=/absolute/path/to/english.wav \
  he=/absolute/path/to/hebrew.m4a \
  en=/absolute/path/to/video.mp4
```

Quote an entire `LANGUAGE=PATH` argument if the path contains spaces. The script checks readiness
once, submits each sample, polls to completion, and requires nonempty text before printing its JSON
result. It exits nonzero on a failed/expired job, timeout, rejected upload, or empty transcript.
Use audible speech samples: deliberate silence is valid in the API but intentionally fails this
smoke test. Inspect the words and timestamps manually; this is not an accuracy benchmark.

For music or the larger model:

```bash
uv run --frozen python scripts/smoke_media.py \
  --base-url "$BASE_URL" \
  --audio-mode music \
  --model-profile larger \
  --timeout-seconds 3600 \
  he=/absolute/path/to/song.m4a
```

The timeout above is the client polling budget. It does not extend the server's default 1800-second
attempt deadline. A long recording or large model may require reviewed server resource/deadline
settings as well. Singing may return poor or empty text even when inference itself works.

At the default host port, Make provides this convenience target:

```bash
make smoke SAMPLES="en=/path/to/english.wav he=/path/to/hebrew.m4a" AUDIO_MODE=speech MODEL_PROFILE=faster
```

For a changed port or paths containing spaces, use the explicit script command above. The Make
target uses the script's default URL and expands samples as shell arguments. The script assumes
the default `/v1` prefix; the browser itself discovers its endpoint through `/client-config`.

Real-model tests are opt-in because they download weights and depend on hardware/runtime support.
Fast tests cannot prove native inference compatibility. For the lower-level synthetic isolated
runner check, see `tests/attempt_smoke.py` and [Security Hardening](security-hardening.md).

### Optional persistence check

After a sample succeeds, keep its `JOB_ID` and restart the application processes:

```bash
docker compose restart api worker
```

Wait for readiness again, then fetch the same status and result URLs. The unexpired successful
result should remain available from PostgreSQL. This checks ordinary process restarts; it does
not simulate host failure or establish a backup/restore guarantee.

### What CI checks

[The CI workflow](../.github/workflows/ci.yaml) installs Python 3.12 and locked dependencies, runs
`make check test`, executes the Linux sandbox probe, and builds the wheel. It does not start the
four-service stack, run real-model inference, measure accuracy, or check JavaScript syntax.

## Configuration reference

[`.env.example`](../.env.example) is the editable template;
[`Settings`](../src/transcription_service/core/config.py) defines application defaults;
[`compose.yaml`](../compose.yaml) defines which values reach containers and their resource limits.
Shell environment values used by Compose interpolation can override `.env` values. Review the
resolved configuration locally if needed; avoid sharing it if you add secrets.

| Setting | Default | Meaning |
| --- | --- | --- |
| `TRANSCRIPTION_PORT` | `8000` | Compose-only published host port, bound to `127.0.0.1`. |
| `TRANSCRIPTION_LOG_LEVEL` | `INFO` | Structured log verbosity. |
| `TRANSCRIPTION_ALLOWED_HOSTS` | `["localhost","127.0.0.1","[::1]"]` | JSON trusted Host allowlist. |
| `TRANSCRIPTION_ALLOWED_ORIGINS` | `[]` | Additional trusted browser origins; same-origin requests are allowed. |
| `TRANSCRIPTION_UPLOAD_TIMEOUT_SECONDS` | `120` | Total upload receive deadline. |
| `TRANSCRIPTION_MAX_UPLOAD_BYTES` | `104857600` | 100 MiB per-file limit, with bounded multipart overhead. |
| `TRANSCRIPTION_MAX_CONCURRENT_UPLOADS` | `2` | Admitted upload requests per API process. |
| `TRANSCRIPTION_MAX_MEDIA_DURATION_SECONDS` | `900` | Worker-side decoded duration limit. |
| `TRANSCRIPTION_CHUNK_DURATION_SECONDS` | `300` | Inference chunk duration. |
| `TRANSCRIPTION_CHUNK_MAX_ATTEMPTS` | `2` | Per-chunk inference attempts inside one job attempt. |
| `TRANSCRIPTION_RATE_LIMIT_REQUESTS` | `10` | Upload submissions per client address/window/API process. |
| `TRANSCRIPTION_RATE_LIMIT_WINDOW_SECONDS` | `60` | Sliding rate window. |
| `TRANSCRIPTION_MODEL_ID` / `TRANSCRIPTION_MODEL_REVISION` | Small model / `536b066…` | Faster profile; full revision is in `.env.example`. |
| `TRANSCRIPTION_LARGER_MODEL_ID` / `TRANSCRIPTION_LARGER_MODEL_REVISION` | Large-v3 / `edaa852…` | Larger profile; keep ID and revision paired. |
| `TRANSCRIPTION_DEVICE` | `cpu` | Runtime device; the submitted deployment/sandbox supports CPU inference. |
| `TRANSCRIPTION_COMPUTE_TYPE` | `int8` | Model compute type. |
| `TRANSCRIPTION_BEAM_SIZE` | `5` | Beam-search setting. |
| `TRANSCRIPTION_MAX_STORED_MEDIA_BYTES` | `5368709120` | 5 GiB storage budget. |
| `TRANSCRIPTION_MAX_PENDING_JOBS` | `100` | Pending queued/running admission limit. |
| `TRANSCRIPTION_JOB_MAX_ATTEMPTS` | `3` | Job attempt budget. |
| `TRANSCRIPTION_JOB_RETRY_BACKOFF_SECONDS` | `5` | Initial exponential retry delay. |
| `TRANSCRIPTION_JOB_TIMEOUT_SECONDS` | `1800` | Hard deadline per isolated attempt. |
| `TRANSCRIPTION_JOB_LEASE_SECONDS` | `30` | Renewable claim lease duration. |
| `TRANSCRIPTION_FAILED_MEDIA_RETENTION_SECONDS` | `86400` | Failed-source retention before expiry. |
| `TRANSCRIPTION_RESULT_RETENTION_SECONDS` | `604800` | Successful-result retention before expiry. |
| `TRANSCRIPTION_CLEANUP_INTERVAL_SECONDS` | `900` | Cleanup cadence; deletion is not instantaneous at expiry. |
| `TRANSCRIPTION_WORKER_POLL_SECONDS` | `2` | Queue polling/due-job dispatch cadence. |
| `TRANSCRIPTION_WORKER_HEARTBEAT_TTL_SECONDS` | `15` | Worker readiness heartbeat lifetime. |
| `TRANSCRIPTION_ATTEMPT_MEMORY_BYTES` | `17179869184` | 16 GiB Linux virtual-address-space cap; distinct from container RAM. |
| `TRANSCRIPTION_WORKER_MEMORY_LIMIT` | `6g` | Compose worker RAM cap. |
| `TRANSCRIPTION_WORKER_CPUS` | `2` | Compose worker CPU limit. |

Compose fixes the database URL to
`postgresql://transcription:transcription@postgres:5432/transcription`, Redis to
`redis://redis:6379/0`, the queue name to `transcription:jobs`, and media storage to `/data/media`.
The corresponding application settings are `TRANSCRIPTION_DATABASE_URL`, `TRANSCRIPTION_REDIS_URL`,
`TRANSCRIPTION_REDIS_QUEUE_NAME`, and `TRANSCRIPTION_MEDIA_STORAGE_PATH`; changing those for
containers requires changing the Compose environment mapping. PostgreSQL/Redis ports are not
published to the host by default.

Application containers use 512 MiB tmpfs, a 256 PID/thread cap, dropped capabilities, and a read-only
root filesystem. Raising upload/duration/model limits may also require raising tmpfs and worker
memory. Changing `.env` requires recreating containers, not merely restarting them:

```bash
docker compose up -d
```

## Troubleshooting

| Symptom | Check or action |
| --- | --- |
| Docker daemon unavailable | Start Docker, then rerun `docker version`. |
| Host port already in use | Set `TRANSCRIPTION_PORT=8002`, run `docker compose up -d`, and update `BASE_URL`. |
| Readiness stays at `503` | Inspect all four services with `docker compose ps` and logs; verify PostgreSQL/Redis health and a live worker heartbeat. |
| Readiness succeeds but the first job is slow | Inspect worker logs for model preparation/download. Readiness does not warm the model. |
| Model preparation/download fails | Check registry/Hugging Face access, disk space, and the ID/revision pair. Retry with a new submission after resolving the cause. |
| Job reports sandbox setup failure | Run the container probe and check Linux kernel/Landlock support. The policy fails closed. |
| Worker exits or job repeatedly times out | Inspect worker logs, Docker memory allocation, and file/model choice. Use Faster first; review server limits before raising them. |
| `409 idempotency_conflict` | Use a new idempotency key when the sample/options change. |
| Result GET returns `409` | Poll status; only a successful unexpired result can be downloaded. |
| Upload returns `429`/`503` | Respect `Retry-After`, check rate/admission limits, queue state, and worker availability. |
| Browser request rejected | Use the local service URL and review Host/Origin allowlists. Do not disable checks to work around a URL mismatch. |
| `uv` cannot write its default cache | Set `UV_CACHE_DIR=/tmp/transcription-uv-cache` for the command, e.g. `UV_CACHE_DIR=/tmp/transcription-uv-cache make check test`. |
| PyAV/model compatibility error | Keep the `av<19` bound with the selected faster-whisper version; see the compatibility note in [Trade-offs & Assumptions](trade-offs-and-assumptions.md#validation-boundaries-and-compatibility). |

For request failures, retain the `X-Request-ID` and inspect `docker compose logs --tail=200 api worker`.
Avoid sharing private media, transcript contents, or configuration secrets in diagnostic reports.

## Stop, preserve data, or reset

Stop and remove containers while retaining named volumes:

```bash
docker compose down
```

Subsequent `docker compose up -d` reuses model weights, database results, queued/failed media, and
Redis persistence. Successful source media is already deleted by policy; results expire after
seven days and failed media after 24 hours by default.

For an intentional clean reset only, the following command deletes all four named volumes,
including accepted media, job state/results, Redis data, and cached models. It cannot be undone
without an independent backup, and the next job will download its model again:

```bash
docker compose down --volumes
```

Before handing over a checkout, use `git status --short --ignored` to confirm `.env`, `.venv`,
`.uv-cache`, bytecode, build outputs, and local media remain ignored. Include the source, tests,
lockfile, build/runtime configuration, and all linked documentation. Generated export files from
the walkthrough are local artifacts and should not be added to the submission.

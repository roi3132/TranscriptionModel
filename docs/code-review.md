# Code and architecture review

The service now has one durable asynchronous contract. FastAPI accepts and persists uploads but
never loads Whisper or blocks HTTP on inference. An independent worker owns PyAV, faster-whisper,
and the two pinned model profiles. PostgreSQL stores job state/results, Redis provides durable
handoff, and a local named volume stores queued media.

## Roadmap status

Phases 6, 7, and 8 are implemented and verified in `docs/architecture-plan.html` and the README:

1. **Implemented:** run the API, Redis broker, PostgreSQL job store, media volume, and independent
   inference worker locally; return `202` and expose job status and result endpoints.
2. **Implemented:** retries, hard child-process deadlines, idempotency/hash deduplication, leased
   crash recovery, retention, cleanup, and component-specific readiness.
3. **Implemented:** explicit 16 kHz mono normalization, defined silent-input behavior, TXT/SRT/VTT
   exports, process-local rate limiting, resumable chunking, and browser reload recovery.

Managed cloud services are not required. Redis and PostgreSQL are self-hosted containers, and the
first `MediaStore` implementation uses a named local volume. S3-compatible storage and multi-host
scaling remain later deployment options.

## Findings fixed

| Priority | Finding | Resolution and regression evidence |
| --- | --- | --- |
| High | File-size validation ran only after multipart parsing had already buffered/spooled the whole body. | `api/uploads.py` bounds declared and streamed bytes before model dependencies, restricts multipart shape, and uses Starlette's parser cleanup. Chunked-upload tests verify partial files close and unread body data is not consumed. |
| High | Inference concurrency did not bound simultaneous upload storage. | A separate API admission limit bounds upload parsing and persistence; each supervisor runs one isolated attempt. A stalled-stream test verifies excess uploads are rejected before reading their bodies. |
| High | Standard wheel builds failed because the UI assets were included twice; editable installs hid the failure. | Removed duplicate Hatch `force-include`, built and inspected the wheel, and added `make package` to CI. |
| Medium | Missing or understated duration headers could permit oversized decoding before a post-inference check. | PyAV counts decoded audio samples incrementally in the worker child and stops at the limit. Tests cover missing and wrong duration metadata. |
| Medium | The larger-model request was initially validated against the small model's language list. | Validate once against the selected model; a regression uses models with different supported languages. |
| Medium | The browser hardcoded `/v1` despite exposing a configurable API prefix. | `/client-config` supplies the public endpoint and file limit; the integration test uploads through a custom prefix. |
| Medium | Unexpected errors could omit `X-Request-ID`, and HTTP exception handling dropped headers such as `Allow`. | Error responses always include the correlation ID and preserve HTTP headers; regressions cover unhandled errors and HTTP 405. |
| Medium | Model output could place a segment beyond the recording, as observed in the song comparison. | Clip timestamps to media duration and discard empty/outside spans. This validates the timeline without claiming to fix the words. |

## Readability changes

- `domain.py` contains shared data and adapter protocols. Adapters no longer import the use-case
  implementation just to access a dataclass or enum.
- `TranscriptionService` is a blocking child-process pipeline. The attempt entry point selects one
  configured model before constructing it; there is no second in-process queue or model registry.
- `services/jobs.py` owns submission identity and lifecycle policy; `services/execution.py` owns
  inline and isolated attempt strategies. PostgreSQL and Redis details remain in `adapters/jobs.py`.
- The worker is a supervisor. Native decoding/model work occurs in a disposable child, while the
  supervisor owns leases, retries, heartbeats, dispatch recovery, and cleanup.
- Media validation and inference now run as one blocking pipeline, with one exception boundary and
  one selected-model language check.
- Standard Starlette multipart parsing, AnyIO capacity limiting, PyAV decoding, and Hatch packaging
  remain responsible for their existing jobs; no parser, audio feature extractor, or build system
  was reimplemented.
- README startup claims now match the single lifecycle: readiness checks database, broker, and a
  renewable worker heartbeat without invoking inference.

## Boundaries retained deliberately

- Compose isolates each attempt in a terminable subprocess; this is a process boundary, not a
  complete hostile-input OS sandbox with per-job seccomp/cgroup limits.
- Pending-job admission is serialized in PostgreSQL and therefore deployment-wide; each supervisor
  runs one child at a time. Scale supervisors explicitly according to available CPU/GPU memory.
- Attempts reload process-local model state each time, trading latency for hard cancellation and
  memory reclamation while reusing the shared on-disk model cache.
- Normalization currently buffers at most the configured 15-minute PCM stream before creating
  in-memory WAV chunks. Multi-hour input should move chunk materialization behind durable object
  storage instead of raising the current limits blindly.
- The upload limiter is process-local and socket-address based. No authentication, user-owned
  history, reverse-proxy deployment, or accuracy guarantee for singing was added. Public ingress
  still needs trusted forwarding, authenticated shared quotas, and upload/idle timeouts.
- Docker Desktop on Apple Silicon built and ran the ARM64 image successfully. The Compose-configured
  container completed real inference and the named model volume survived container replacement.

## Verification

Run `make check test package` for formatting, lint, typing, fast regression tests, and wheel build.
Use the README's explicit real-model smoke command against the Compose stack, and `make docker-smoke`
for the basic container check. Fast CI must continue to avoid downloading model weights.

Phase 8 passes Ruff formatting/lint, strict mypy, JavaScript syntax checking, 70 focused tests with
82% coverage, wheel packaging, and a fresh Docker image build. The rebuilt four-service stack
reached API readiness on port 8002.

Phase 5 verification on 1 November 2026 produced the following evidence:

- Ruff formatting/lint and strict mypy passed; all 63 tests passed with 95% coverage; the wheel built.
- The cached real small model successfully transcribed generated English, Hebrew, mixed-language,
  and MP4-with-audio samples through the public endpoint.
- Corrupt content carrying an allowed `.mp3` name and MIME type returned the sanitized
  `415 unsupported_media` envelope without exposing its local path.
- No media files or obvious secret values are tracked. Request logs contain method, route, status,
  timing, model identifiers, error codes, and request IDs—not filenames or transcript text.
- The README's documented `make smoke SAMPLES=...` path completed successfully.
- Docker Desktop built the image from scratch and populated a 464 MB named model-cache volume on the
  first startup. The replacement container loaded the cached model in under one second using the
  same volume and completed real inference. Container inspection confirmed UID/GID 10001,
  read-only root filesystem, a 256 MiB `noexec,nosuid` tmpfs, all capabilities dropped, and
  `no-new-privileges`. Port 8002 was used because another local process occupied port 8000.
- Phase 6 added 68 passing fast tests and the locked Redis/psycopg clients. The four-service Compose
  stack accepted a generated M4A with `202`, completed real inference, returned the persisted result,
  and returned that same result after both API and worker restarted. PostgreSQL, Redis, media, and
  model-cache volumes remained local and no cloud account was used.
- Phase 7 raised the suite to 80 passing tests and added bounded retry/terminal policy, renewable
  atomic leases, stale-claim recovery, idempotency and SHA-256 caching, retention cleanup, hard
  child termination, storage/pending limits, and dynamic readiness. The existing Phase 6 database
  migrated in place. Real Compose checks proved cache reuse and conflict behavior, a two-attempt
  `job_timeout` under a forced 0.2-second deadline, readiness failure after worker heartbeat expiry,
  and exactly one attempt while two supervisors competed for a real job.
- Phase 8 focuses the suite at 70 tests after removing synchronous-only concurrency tests. New
  coverage proves 16 kHz mono output, explicit silent results, per-chunk retry and checkpoint resume,
  ordered timestamp assembly, process-local rate admission, every export representation, and browser
  recovery hooks. The wheel and rebuilt Compose stack passed, with all four services ready on port
  8002.

# Code and architecture review

The service now has two deliberate deployment modes: a synchronous, stateless native development
mode and a durable asynchronous Docker Compose mode. Python, PyAV, faster-whisper, pinned Hugging
Face revisions, and the two model profiles remain appropriate. PostgreSQL is now justified for job
state/results, Redis for durable handoff, and a local named volume for queued media.

## Roadmap status

The interview scope expanded after the synchronous review. Phase 6 is implemented and verified;
phases 7–8 remain ordered work in `docs/architecture-plan.html` and the README:

1. **Implemented:** run the API, Redis broker, PostgreSQL job store, media volume, and independent
   inference worker locally; return `202` and expose job status and result endpoints.
2. **Next:** add retries, hard worker deadlines, idempotency/hash deduplication, crash recovery, retention,
   cleanup, and component-specific readiness.
3. **Later:** add explicit 16 kHz mono normalization, defined silent-input behavior, TXT/SRT/VTT exports, rate
   limiting, long-file chunking, and asynchronous UI polling.

Managed cloud services are not required. Redis and PostgreSQL are self-hosted containers, and the
first `MediaStore` implementation uses a named local volume. S3-compatible storage and multi-host
scaling remain later deployment options.

## Findings fixed

| Priority | Finding | Resolution and regression evidence |
| --- | --- | --- |
| High | File-size validation ran only after multipart parsing had already buffered/spooled the whole body. | `api/uploads.py` bounds declared and streamed bytes before model dependencies, restricts multipart shape, and uses Starlette's parser cleanup. Chunked-upload tests verify partial files close and unread body data is not consumed. |
| High | Inference concurrency did not bound simultaneous upload storage or media inspection. | A separate admission limit covers the complete upload request; the inference slot now covers media decoding, loading, and inference. A stalled-stream test verifies excess requests are rejected before reading their bodies. |
| High | Standard wheel builds failed because the UI assets were included twice; editable installs hid the failure. | Removed duplicate Hatch `force-include`, built and inspected the wheel, and added `make package` to CI. |
| High | Cancelling an asyncio request could release its inference slot and close the uploaded file while its native worker continued. | Shield the worker and defer cancellation cleanup until it finishes. A regression cancels twice and checks both capacity and file ownership. |
| Medium | Missing or understated duration headers could permit oversized decoding before a post-inference check. | PyAV counts decoded audio samples incrementally and stops at the limit, inside the capacity slot. Tests cover missing and wrong duration metadata. |
| Medium | The larger-model request was initially validated against the small model's language list. | Validate once against the selected model; a regression uses models with different supported languages. |
| Medium | The browser hardcoded `/v1` despite exposing a configurable API prefix. | `/client-config` supplies the public endpoint and file limit; the integration test uploads through a custom prefix. |
| Medium | Unexpected errors could omit `X-Request-ID`, and HTTP exception handling dropped headers such as `Allow`. | Error responses always include the correlation ID and preserve HTTP headers; regressions cover unhandled errors and HTTP 405. |
| Medium | Model output could place a segment beyond the recording, as observed in the song comparison. | Clip timestamps to media duration and discard empty/outside spans. This validates the timeline without claiming to fix the words. |

## Readability changes

- `domain.py` contains shared data and adapter protocols. Adapters no longer import the use-case
  implementation just to access a dataclass or enum.
- `services/models.py` owns the bounded, thread-safe model cache. `TranscriptionService` owns the
  pipeline and capacity policy.
- Media validation and inference now run as one blocking pipeline, with one exception boundary and
  one selected-model language check.
- Standard Starlette multipart parsing, AnyIO capacity limiting, PyAV decoding, and Hatch packaging
  remain responsible for their existing jobs; no parser, audio feature extractor, or build system
  was reimplemented.
- README startup claims now match Uvicorn's lifespan behavior: the HTTP listener becomes available
  after eager initialization, and readiness reports a failed initialization afterward.

## Boundaries retained deliberately

- Native mode still shares codecs/model execution with the API process. Compose moves inference to
  an independent worker, but Phase 6 does not yet isolate each attempt. A hard wall-clock deadline
  or hostile-input sandbox needs a terminable per-attempt subprocess rather than an asyncio timeout.
- Admission and inference limits are per process. Multiple Uvicorn workers multiply both resident
  model memory and allowed work. Keep one worker per configured instance.
- Two model instances may remain resident after the larger option is first used. The small model
  remains the startup default; model eviction or separate model workers would be the next step for
  tighter memory budgets or a larger catalog.
- Duration validation intentionally decodes audio before faster-whisper decodes it again. This
  costs CPU, but rejects overlong inputs before the model's full-array preprocessing and avoids
  duplicating its resampling/feature-extraction implementation.
- No authentication, user-owned history, reverse-proxy deployment, or accuracy guarantee for
  singing was added. Public ingress needs authentication, rate limits, and upload/idle timeouts.
- Docker Desktop on Apple Silicon built and ran the ARM64 image successfully. The Compose-configured
  container completed real inference and the named model volume survived container replacement.

## Verification

Run `make check test package` for formatting, lint, typing, fast regression tests, and wheel build.
Use the README's explicit real-model smoke commands for native inference, and `make docker-smoke`
when Docker is available. Fast CI must continue to avoid downloading model weights.

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
  first startup. The replacement container reached model readiness in under one second using the
  same volume and completed real inference. Container inspection confirmed UID/GID 10001,
  read-only root filesystem, a 256 MiB `noexec,nosuid` tmpfs, all capabilities dropped, and
  `no-new-privileges`. Port 8002 was used because the native development server occupied port 8000.
- Phase 6 added 68 passing fast tests and the locked Redis/psycopg clients. The four-service Compose
  stack accepted a generated M4A with `202`, completed real inference, returned the persisted result,
  and returned that same result after both API and worker restarted. PostgreSQL, Redis, media, and
  model-cache volumes remained local and no cloud account was used.

# Code and architecture review

The service remains a synchronous, stateless FastAPI application. Python, PyAV, faster-whisper,
pinned Hugging Face revisions, the two model profiles, and the decision to omit a database remain
appropriate for the assignment. The useful changes are at resource and ownership boundaries, not
additional infrastructure.

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

- Native codecs and model execution share the API process. Python cannot forcibly stop a stuck
  native thread safely. A hard wall-clock deadline or hostile-input sandbox needs separate worker
  processes, resource limits, and a durable job design rather than an asyncio timeout.
- Admission and inference limits are per process. Multiple Uvicorn workers multiply both resident
  model memory and allowed work. Keep one worker per configured instance.
- Two model instances may remain resident after the larger option is first used. The small model
  remains the startup default; model eviction or separate model workers would be the next step for
  tighter memory budgets or a larger catalog.
- Duration validation intentionally decodes audio before faster-whisper decodes it again. This
  costs CPU, but rejects overlong inputs before the model's full-array preprocessing and avoids
  duplicating its resampling/feature-extraction implementation.
- No authentication, durable jobs/history, reverse-proxy deployment, or accuracy guarantee for
  singing was added. Public ingress needs authentication, rate limits, and upload/idle timeouts.
- The Docker CLI is unavailable in this environment. Wheel packaging and native tests are checked;
  the container startup/cache-reuse acceptance check still requires Docker.

## Verification

Run `make check test package` for formatting, lint, typing, fast regression tests, and wheel build.
Use the README's explicit real-model smoke commands for native inference, and `make docker-smoke`
when Docker is available. Fast CI must continue to avoid downloading model weights.

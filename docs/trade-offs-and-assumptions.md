# Trade-offs & Assumptions

This document records the assumptions and deliberate scope choices behind the submitted solution.
It explains what was chosen, why, what it costs, and when the choice should change. Setup and
verification commands live in [Execution Instructions](execution-instructions.md); implementation
flows live in [Service Architecture](service-architecture.html).

The shortcuts below describe the delivered evaluation scope. They are not claims that omitted
features are complete, nor claims about a measured development deadline.

Contents: [Assumptions](#evaluation-and-deployment-assumptions) ·
[Design decisions](#technology-and-architecture-decisions) ·
[Scope shortcuts](#scope-shortcuts-and-deferred-work) ·
[Validation and compatibility](#validation-boundaries-and-compatibility) ·
[Maintaining this record](#keeping-this-record-current).

## Evaluation and deployment assumptions

| Assumption | Consequence for the submission | Revisit when |
| --- | --- | --- |
| One trusted user accesses a service bound to localhost. | There are no accounts, ownership checks, or tenant boundaries; a job ID is not an authorization mechanism. | The service is exposed to other users or machines. |
| All four services run on one Docker host. | API and worker share a named media volume; PostgreSQL and Redis are local containers. | Workers run on multiple hosts or high availability becomes a requirement. |
| CPU inference is the evaluation baseline. | The default is the multilingual small model, CPU int8, and one supervisor processing one attempt at a time. | Throughput or latency targets justify GPU provisioning and a reviewed device-access policy. |
| Recordings are bounded to 100 MiB and 15 minutes by default. | Normalization and chunking fit the intended memory envelope; this is not a streaming or multi-hour transcription service. | Limits increase substantially or live transcription is requested. |
| Internet access is available for initial setup and first use of each model. | Images, Python packages, the build backend, and pinned model weights may need downloading. | An offline or air-gapped distribution is required. |
| The worker runs on a supported sandbox platform. | Linux requires Landlock ABI 3 (kernel 6.2+) and libseccomp2; macOS uses Seatbelt. Unsupported environments fail before decoding. | Another platform or GPU runtime must be supported. |
| Transcription quality is best effort. | Language detection, singing, noise, and code-switching can produce omissions or hallucinations. | Acceptance requires a measured accuracy target on a defined corpus. |
| Local Docker volumes are the persistence boundary. | Jobs survive ordinary restarts, but deleting volumes or losing the host can lose data. | Backup, disaster recovery, or independent durable storage is required. |

## Technology and architecture decisions

### Python, FastAPI, and adapter boundaries

Python keeps media handling and speech inference within the same ecosystem. FastAPI supplies typed
request/response contracts, multipart integration, lifecycle hooks, and OpenAPI. Inference runs in
CTranslate2's native runtime, so the HTTP framework is not responsible for model computation.

`Transcriber`, `MediaNormalizer`, `JobRepository`, `JobBroker`, and `MediaStore` protocols separate
the use cases from concrete infrastructure. Deterministic tests can replace those boundaries.

Alternatives considered include Flask, whisper.cpp, and a separate Go/C++ API with a Python model
service. They can fit other environments, but add integration and deployment work to this local
submission. Revisit the boundary if measured API throughput or a different inference runtime
requires it; there is no comparative framework-performance benchmark in this submission.

### faster-whisper and two pinned multilingual models

The `faster` profile selects `Systran/faster-whisper-small` at revision
`536b0662742c02347bc0e980a01041f333bce120`. The `larger` profile selects
`Systran/faster-whisper-large-v3` at revision `edaa852ec7e145841d8ffdb056a99866b5f0a478`.

The multilingual small model keeps Hebrew and English available with a practical default download
and CPU footprint. An English-only checkpoint would not meet the Hebrew requirement. The larger
profile allows comparison but costs more disk space, memory, and processing time. Clients choose
only an allowlisted profile; operators control the model ID/revision pair.

Pinned revisions make model selection repeatable independently of the Python lockfile. Exact
transcript equality across hardware and native runtime versions is not guaranteed. Revisit the
defaults using representative English/Hebrew recordings and measured latency/accuracy, not model
size alone.

### Independent worker rather than inference inside HTTP

The API persists media and job state, queues a job ID, and returns `202 Accepted`. The independent
worker owns decoding and inference; clients poll status and fetch results. This allows long work
and restart recovery without keeping an upload request open throughout inference.

The cost is four local services and a polling client instead of a single synchronous process.
An in-process background task would simplify startup but would not provide the same durability
across process restarts. Webhooks, streaming progress, and cancellation are outside the current
contract. Revisit polling if a client product needs push delivery or explicit cancellation.

### PostgreSQL is authoritative; Redis is a delivery mechanism

PostgreSQL stores state, attempts, leases, expiry, and JSON results. Redis carries job IDs and
worker heartbeats. Conditional claims and renewable leases coordinate supervisors; periodic scans
redispatch due database rows and recover expired leases. Each supervisor processes one claimed
attempt at a time.

Delivery is at least once. Repeated delivery can be rejected by the authoritative claim, but a
crash after inference and before committing the result can cause computation to run again.
The lease mechanism is not a promise of exactly-once computation under arbitrary failures.
Retryable failures use a bounded attempt budget and backoff; typed request/media errors terminate
the job. There is no separate dead-letter queue product: failure state is stored on the job.

Alternatives include a database-only polling queue or a task framework. The current small
supervisor keeps lifecycle policy explicit but also makes this repository responsible for that
policy's maintenance. Revisit the broker/framework choice if scheduling or operational needs grow.

### Fresh processes for enforceable deadlines and isolation

A Python async timeout cannot safely stop native decoder or model work. Each attempt therefore
runs in a disposable child process; the supervisor renews its lease and heartbeat, then terminates
the child's process group if the deadline is exceeded. Processes reclaim model memory on exit.

Pinned model preparation downloads weights before sandboxed inference loads them offline. The
inference child receives a private media copy and filtered configuration, with filesystem,
network, execution, and resource restrictions. The full policy is in
[Security Hardening](security-hardening.md).

The cost is repeated model loading, process startup, and a private media copy in temporary storage.
An on-disk model cache avoids repeated downloads but does not preserve a warm in-memory model.
Revisit with a warm model pool only if it retains a strong external cancellation boundary and an
appropriate sandbox. GPU support requires more than changing `TRANSCRIPTION_DEVICE`.

### PyAV normalization and resumable chunks

Audio and video use one endpoint. PyAV decodes only the first audio stream and normalizes it to
signed 16-bit, 16 kHz mono PCM. Video frames are not transcribed. Extension/MIME policy is an initial
filter; decoding is the authoritative check, and a video without audio fails as `no_audio_stream`.
PyAV uses FFmpeg libraries without a system `ffmpeg` command requirement.

Sample counting enforces the duration limit even if container metadata is absent or inaccurate.
The current implementation buffers the entire bounded normalized stream, then creates in-memory
WAV chunks (five minutes by default). A retry can use completed chunk checkpoints, although media
normalization still runs again. This reduces repeated inference; it is not streaming decoding.

Chunks are processed in order with no overlap or cross-chunk language/context reconciliation.
Text is joined, timestamps are offset, and segment ends are clipped to decoded duration. The
aggregate language is taken from the chunk with the highest language probability; it is not a
per-word language annotation. Boundaries can reduce context and accuracy, and timestamp clipping
does not fix hallucinated words. Revisit this pipeline for multi-hour input, speaker diarization,
track selection, or tighter cross-chunk consistency requirements.

### Speech/music modes and explicit silence behavior

`speech` enables voice activity detection; `music` disables VAD and no-speech segment skipping to
process the full track. This exposes an understandable choice without requiring clients to know
the model library's flags. Music mode generally does more work and may hallucinate on non-speech.

Exact digital silence succeeds with empty text and `no_speech_detected`; an empty model result
also receives that warning. The zero-sample-value check is not a general acoustic silence detector.
An empty speech result does not automatically trigger another full inference pass, which would
increase cost and could invent text for silent input.

A previous one-clip Hebrew song comparison produced poor results with both small and large-v3.
That observation motivated cautious UI wording; it is not an accuracy benchmark. Revisit defaults
with a labeled corpus if lyric quality or reliable code-switching becomes an acceptance criterion.

### Idempotency, result reuse, and lifecycle

Submission identity includes the media SHA-256, model revision, language, audio mode, and model
profile. An `Idempotency-Key` returns its original job for matching identity and rejects a mismatch
with `409`. Independently, matching active jobs or successful unexpired results can be reused.
Failed and expired jobs are not ordinary content-cache hits.

These rules currently apply across the whole deployment because there are no users. They are not
tenant isolation. If accounts are introduced, ownership must be part of lookup, key uniqueness,
and cache access before reusing results across callers.

Successful source media and checkpoints are deleted immediately. Successful results expire after
seven days; terminal-failure media is retained for 24 hours by default. Cleanup runs periodically,
marks the job expired, clears the result, and removes artifacts. These are retention policies, not
an exact deletion-time SLA. Job tombstones and idempotency associations remain in PostgreSQL;
there is no automatic tombstone archival policy. Revisit retention, backup, encryption, and deletion
requirements before handling regulated or sensitive data.

### Bounded local admission rather than distributed quotas

The API bounds upload bodies before parsing, including multipart overhead, and limits admitted
uploads to two per process by default. Receive deadlines prevent indefinitely stalled bodies.
The address-based rate limiter allows ten submissions per 60-second window per API process.
It does not treat an arbitrary `X-Forwarded-For` header as a trusted client identity.

The database separately limits pending jobs to 100 by default, and media storage has a 5 GiB budget.
Worker execution, attempt deadlines, container RAM/CPU/PIDs, and tmpfs are bounded independently.
Increasing one application setting does not automatically resize container limits. In particular,
each application container has a 512 MiB tmpfs, and normalization/model memory must fit the worker's
default 6 GiB container limit.

Local rate/admission limits are simple and useful for this evaluation, but are not shared
identity-based quotas across API replicas. Revisit with trusted forwarding, authentication, and
deployment-wide quotas before public or multi-replica operation.

### Buildless browser UI and local deployment

FastAPI serves one HTML page with packaged local CSS/JavaScript, avoiding a separate frontend build
and runtime. The UI obtains its upload endpoint and limit from `/client-config`, polls jobs, and
stores the active job's URLs/display filename in browser local storage for reload recovery. It is
not an account-backed job-history product. Static files do not depend on third-party CDNs.

Compose uses named volumes for models, media, PostgreSQL, and Redis AOF. Model weights stay outside
the image, making first-use downloads visible and allowing image rebuilds to reuse the cache.
Baking weights into an image or pre-seeding the cache is an alternative for offline delivery.
The local PostgreSQL credentials are development defaults on the internal Compose network; this
is not production secret provisioning.

Containers run as a non-root user with a read-only root filesystem, dropped capabilities, and
`no-new-privileges`. Python dependencies are locked, model revisions are pinned, and CI actions
are pinned to commit SHAs. Base/service images use tags rather than immutable digests, and the
Hatch build backend is declared separately from the application lockfile. This is reproducible
application configuration, not a bit-for-bit hermetic image build. Revisit image digests, backend
pinning, provenance, and update policy when formal release reproducibility is required.

## Scope shortcuts and deferred work

These choices keep the submission focused on durable local transcription. They are explicit
omissions, with the practical effect and follow-up recorded here.

| Shortcut or omitted feature | Current consequence | Required follow-up if scope expands |
| --- | --- | --- |
| No authentication or ownership layer | Anyone with access to the local API can submit jobs and query known job IDs; deduplication is deployment-wide. | Add identity, authorization, tenant-scoped keys/cache access, TLS, and secret management before shared/public use. |
| Process-local address quotas | Limits reset on restart and are independent per API process. | Enforce shared identity-based quotas at trusted ingress or a shared store. |
| One-host media volume | Workers require the same local storage namespace. | Implement shared/object storage and validate multi-host recovery. |
| One child per supervisor; no warm model pool | Jobs queue under load and each attempt reloads model state. | Benchmark capacity and introduce warming/scaling while retaining cancellation and isolation. |
| Whole bounded PCM buffer and non-overlapping chunks | RAM grows with allowed duration; chunk boundaries can lose context. | Stream or durably materialize chunks; evaluate overlap/context reconciliation. |
| First audio track only | Alternate-language or commentary tracks cannot be selected. | Define an audio-track API/UI and test multi-track containers. |
| No accuracy benchmark or human correction workflow | Passing tests proves behavior, not transcription quality. | Define a labeled corpus, error metrics, hardware baseline, and acceptance threshold. |
| Fake infrastructure in normal API tests | Fast tests do not exercise real SQL, Redis persistence, image behavior, or native inference. | Run the documented Compose and real-model checks for release acceptance; add infrastructure CI if required. |
| No account history, job cancellation, or push completion | Browser recovery covers one active job; clients poll and work runs to completion/deadline. | Define lifecycle/ownership semantics before adding these features. |
| Retained job tombstones; no backup automation | Metadata can grow over time; host/volume loss is outside the recovery guarantee. | Define archival, deletion, backup, and restore policy. |
| No metrics backend, autoscaling, or orchestrator | Operators use health endpoints and structured logs for this local stack. | Add service-level metrics and deployment automation when operating beyond one host. |

Diarization, translation, LLM summaries, transcript editing, billing, PII redaction, URL ingestion,
and completion webhooks are also outside the submitted contract. They were deferred to preserve
focus on file upload, asynchronous reliability, normalization, inference, and result export.

## Validation boundaries and compatibility

The fast suite substitutes deterministic inference and in-memory job adapters while exercising
request validation, real PyAV normalization in media tests, lifecycle policy, exports, and security
boundaries. The latest local run passed 78 tests with 78% coverage. Coverage is a code-execution
metric, not an accuracy score or a complete integration guarantee.

The standalone sandbox probe, container readiness smoke, and opt-in real-model script cover
different gaps. Container readiness checks the API, database, broker, and worker heartbeat; it
does not guarantee that model download/loading or a transcription will succeed. Real-model
smoke output requires human inspection and does not compare against a reference transcript.
See [Execution Instructions](execution-instructions.md#test-the-solution) for each command.

The pinned dependency set uses `faster-whisper==1.2.1` with PyAV 18.1.0. The former passes
`metadata_errors` to `av.open`, an argument removed in PyAV 19. The project therefore bounds PyAV
to `av>=14,<19`. Fake-model tests do not prove that this third-party integration remains compatible.
Upgrade both deliberately, regenerate `uv.lock`, and run actual English/Hebrew inference before
removing the bound. This documents the repository's selected versions, not the latest upstream
release state.

Historical Docker/model verification in [Code and Architecture Review](code-review.md) is prior
evidence, not a promise that every evaluator has rerun it. Logs and `X-Request-ID` support local
diagnosis; there is no promised latency, availability, or accuracy SLA.

## Keeping this record current

For a material design change or accepted shortcut, update this document in the same change:

1. State the requirement, constraint, or observed failure.
2. Record the selected approach and meaningful alternatives.
3. Explain the behavior, operational cost, and remaining limitation.
4. Identify the evidence used to validate it.
5. State the condition that would justify revisiting it.

Update [Execution Instructions](execution-instructions.md) when a command, default, prerequisite,
or verification step changes. Keep historical milestones labeled as historical.

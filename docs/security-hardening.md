# Security hardening

For complete build/run/test steps, see [Execution Instructions](execution-instructions.md).
Deployment assumptions and remaining scope limits are recorded in
[Trade-offs & Assumptions](trade-offs-and-assumptions.md).

## Browser and HTTP boundary

The API accepts localhost, 127.0.0.1, and IPv6 loopback Host headers by default.
Set `TRANSCRIPTION_ALLOWED_HOSTS` to a JSON array of explicitly trusted names when
deploying behind a proxy. Do not use a wildcard for an unauthenticated local service.
Tests additionally permit `testserver` only in the test environment.

Mutating browser requests must have the same Origin as the request URL, or an
explicitly configured `TRANSCRIPTION_ALLOWED_ORIGINS` entry. Null origins and
`Sec-Fetch-Site: cross-site` requests are rejected. Command-line clients without
browser-origin headers continue to work. CORS response headers alone would not
prevent a browser from submitting a multipart request.

Job status/result URLs and the Location header are now origin-relative paths.
API clients should resolve these paths against the service URL. The Host header
cannot inject an external polling destination. Upload bodies have a 120-second
total receive deadline; timeout handling closes partial multipart files and
releases the admission slot.

## Private files

Media storage directories are mode 0700. New uploads, result files, manifests, and
checkpoints are mode 0600 even with a permissive umask. Storage initialization also
restricts an existing media directory to 0700. The documented host directory
`data/` is excluded from both Git and Docker build contexts.

Each attempt gets a randomly named 0700 temporary directory containing a copy of
only the claimed recording, its checkpoint, a manifest, and its output. The
supervisor keeps this directory until the attempt exits, then removes it. Writers
use exclusive creation for intermediate files; existing files and symlinks are
rejected. Results are size-limited and symlink results are rejected. Checkpoints
are copied back with private permissions after the child and its descendants stop.

## Worker sandbox

The child no longer queries PostgreSQL or resolves the shared media volume. Its
environment is constructed from an allowlist and contains no PostgreSQL, Redis,
cloud, or arbitrary inherited credentials. Configuration comes from a restricted
manifest, not the repository's `.env` file.

The operator-selected pinned model is downloaded in a separate preparation process
that exits before inference starts. The fresh inference process activates the
sandbox before importing native ML libraries or loading the cached model, so their
worker threads inherit the restrictions. Only preparation retains network access
for first-use downloads. Inference loads weights offline. Runtime libraries, the
application package, and the selected model snapshot/blob files are readable; only
the private attempt directory is writable. Other media, cache tokens, database
files, and host secrets are inaccessible.

Linux requires Landlock ABI 3 (kernel 6.2 or newer) and libseccomp2, installed in the
image. Landlock restricts filesystem access; a seccomp filter blocks network
sockets, program execution, process inspection, cross-process signals, and
io_uring and filesystem metadata mutation. macOS uses the native Seatbelt sandbox with the same filesystem,
network, and execution restrictions. Unsupported systems or sandbox setup errors
fail the attempt before media decoding. Running the macOS probe inside another
Seatbelt sandbox may fail initialization; test it directly on the host.

Each attempt receives CPU-time, file-size, open-file, and core-dump limits; Linux
also enforces a 16 GiB virtual-address-space limit. Compose limits the worker to
6 GiB RAM, two CPUs, and 256 processes/threads by default, with 512 MiB of tmpfs
for private media copies. Adjust these for the selected model and hardware.
Timeout handling terminates the child's entire process group. Explicit GPU
deployments need a separately reviewed device-access sandbox policy; the default
policy is designed and tested for CPU inference.

This remains a single-user local application. Authentication, ownership checks,
TLS, trusted forwarding, and shared identity-based quotas are still required for
public or multi-user deployment. Host/origin allowlists are not authentication.

## Verification

Run `make check test package` for the normal checks. The security tests exercise
foreign Origin/Host rejection, relative URLs, upload timeout cleanup, restrictive
file permissions, symlink refusal, and private child manifests/environments.

Run `uv run --frozen python tests/sandbox_probe.py` directly on macOS, or mount
that script read-only into the production image and run it with Python. The probe
checks that another recording cannot be read or overwritten, model weights cannot
be changed, a TCP connection cannot be opened, and a shell cannot execute, while
model reads and attempt-directory writes still succeed. Its printed temporary
directory is left for inspection; an ephemeral Docker container removes it on exit.

`tests/attempt_smoke.py` is an opt-in real-model check. Mount it into the production
image as `/smoke.py`, mount the existing Hugging Face model cache at its configured
location, and run `python /smoke.py` with the Compose worker restrictions. It tests
both speech/VAD and music/full-inference paths on synthetic audio, without a database
connection or user recordings. It may download the pinned model on an empty cache.

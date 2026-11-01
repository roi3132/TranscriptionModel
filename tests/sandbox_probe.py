"""Standalone sandbox verification, runnable in the production Docker image."""

import json
import socket
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from transcription_service.core.sandbox import restrict_attempt


def main() -> None:
    # Do not use automatic TemporaryDirectory cleanup inside the restricted process:
    # the parent pytest/--rm container owns cleanup after this process exits.
    root = Path(tempfile.mkdtemp(prefix="sandbox-probe-")).resolve()
    workspace = root / "attempt"
    workspace.mkdir(mode=0o700)
    cache = root / "models"
    cache.mkdir()
    (cache / "weights").write_text("model")
    other_media = root / "other-recording"
    other_media.write_text("private")
    restrict_attempt(workspace, cache, memory_bytes=16 * 1024**3, cpu_seconds=10)

    blocked: list[str] = []
    operations = {
        "other-media-read": lambda: other_media.read_text(),
        "other-media-write": lambda: other_media.write_text("changed"),
        "other-media-chmod": lambda: other_media.chmod(0),
        "model-cache-write": lambda: (cache / "weights").write_text("changed"),
        "network": lambda: socket.create_connection(("127.0.0.1", 5432), timeout=0.2),
        "command": lambda: subprocess.run(["/bin/sh", "-c", "true"], check=True),
    }
    for name, operation in operations.items():
        try:
            operation()
        except PermissionError:
            blocked.append(name)
        else:
            raise AssertionError(f"Sandbox allowed {name}")
    assert (cache / "weights").read_text() == "model"
    (workspace / "result").write_text("allowed")
    assert (workspace / "result").read_text() == "allowed"
    with ThreadPoolExecutor(max_workers=1) as pool:
        try:
            pool.submit(other_media.read_text).result()
        except PermissionError:
            blocked.append("thread-media-read")
        else:
            raise AssertionError("Native worker threads must inherit filesystem restrictions")
    print(json.dumps({"blocked": blocked, "workspace": str(root)}))


if __name__ == "__main__":
    main()

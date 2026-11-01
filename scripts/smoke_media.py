"""Run explicit real-model smoke tests against one or more local media files."""

import argparse
import json
import mimetypes
from pathlib import Path
from time import monotonic, sleep

import httpx2

from transcription_service.domain import AudioMode, ModelProfile


def _sample(value: str) -> tuple[str, Path]:
    try:
        language, raw_path = value.split("=", maxsplit=1)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("sample must use LANGUAGE=PATH") from exc

    path = Path(raw_path).expanduser().resolve()
    if not language or not path.is_file():
        raise argparse.ArgumentTypeError(f"invalid language or missing file: {value}")
    return language.lower(), path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "sample",
        nargs="+",
        type=_sample,
        metavar="LANGUAGE=PATH",
        help="audio/video sample and explicit Whisper language code",
    )
    parser.add_argument(
        "--audio-mode",
        choices=[mode.value for mode in AudioMode],
        default=AudioMode.SPEECH.value,
        help="speech enables VAD; music processes the complete audio track",
    )
    parser.add_argument(
        "--model-profile",
        choices=[profile.value for profile in ModelProfile],
        default=ModelProfile.FASTER.value,
        help="faster uses the default model; larger loads the optional larger model",
    )
    parser.add_argument(
        "--base-url",
        default="http://localhost:8000",
        help="running asynchronous service URL",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=1800,
        help="maximum time to wait for each job",
    )
    args = parser.parse_args()

    with httpx2.Client(base_url=args.base_url, timeout=30) as client:
        ready = client.get("/health/ready")
        if ready.status_code != 200:
            raise SystemExit(f"job service did not become ready: {ready.text}")

        for language, path in args.sample:
            content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            with path.open("rb") as media:
                response = client.post(
                    "/v1/transcriptions",
                    files={"file": (path.name, media, content_type)},
                    data={
                        "language": language,
                        "audio_mode": args.audio_mode,
                        "model_profile": args.model_profile,
                    },
                )
            if response.status_code != 202:
                raise SystemExit(f"{path.name} failed: {response.status_code} {response.text}")

            accepted = response.json()
            deadline = monotonic() + args.timeout_seconds
            while True:
                status_response = client.get(accepted["status_url"])
                status_response.raise_for_status()
                status = status_response.json()
                if status["status"] == "succeeded":
                    break
                if status["status"] in {"failed", "expired"}:
                    raise SystemExit(f"{path.name} ended as {status['status']}: {status}")
                if monotonic() >= deadline:
                    raise SystemExit(f"{path.name} did not finish before the smoke-test deadline")
                sleep(1)

            result_response = client.get(accepted["result_url"])
            result_response.raise_for_status()
            payload = result_response.json()
            if not payload["text"]:
                raise SystemExit(f"{path.name} returned an empty transcription")
            print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

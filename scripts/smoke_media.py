"""Run explicit real-model smoke tests against one or more local media files."""

import argparse
import json
import mimetypes
from pathlib import Path

from fastapi.testclient import TestClient

from transcription_service.main import create_app
from transcription_service.services.transcription import AudioMode


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
    args = parser.parse_args()

    with TestClient(create_app()) as client:
        ready = client.get("/health/ready")
        if ready.status_code != 200:
            raise SystemExit(f"model did not become ready: {ready.text}")

        for language, path in args.sample:
            content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            with path.open("rb") as media:
                response = client.post(
                    "/v1/transcriptions",
                    files={"file": (path.name, media, content_type)},
                    data={"language": language, "audio_mode": args.audio_mode},
                )
            if response.status_code != 200:
                raise SystemExit(f"{path.name} failed: {response.status_code} {response.text}")

            payload = response.json()
            if not payload["text"]:
                raise SystemExit(f"{path.name} returned an empty transcription")
            print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

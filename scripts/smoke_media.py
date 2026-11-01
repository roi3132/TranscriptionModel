"""Run explicit real-model smoke tests against one or more local media files."""

import argparse
import hashlib
import json
import mimetypes
import re
import unicodedata
from pathlib import Path
from time import monotonic, sleep

import httpx2

from transcription_service.domain import AudioMode, ModelProfile


def reference_words(text: str) -> list[str]:
    """Ignore case, punctuation and combining marks, including Hebrew niqqud."""
    normalized = unicodedata.normalize("NFKC", text).casefold()
    normalized = "".join(char for char in normalized if not unicodedata.combining(char))
    return re.findall(r"[^\W_]+", normalized)


def word_errors(reference: str, hypothesis: str) -> tuple[int, int]:
    """Return Levenshtein word edits and reference length using one DP row."""
    expected, actual = reference_words(reference), reference_words(hypothesis)
    if not expected:
        raise ValueError("Reference text must contain at least one word")
    previous = list(range(len(actual) + 1))
    for index, word in enumerate(expected, start=1):
        current = [index]
        for offset, candidate in enumerate(actual, start=1):
            current.append(
                min(
                    previous[offset] + 1,
                    current[-1] + 1,
                    previous[offset - 1] + (word != candidate),
                )
            )
        previous = current
    return previous[-1], len(expected)


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
        "--reference-dir",
        type=Path,
        help="score each sample against a UTF-8 <media-stem>.txt in this directory",
    )
    parser.add_argument(
        "--report",
        type=Path,
        help="save results, model IDs, timing and optional word error rates as JSON",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=1800,
        help="maximum time to wait for each job",
    )
    args = parser.parse_args()
    if args.timeout_seconds <= 0:
        parser.error("--timeout-seconds must be positive")
    references: dict[Path, str] = {}
    if args.reference_dir:
        for _, path in args.sample:
            reference_path = args.reference_dir / f"{path.stem}.txt"
            try:
                reference = reference_path.read_text(encoding="utf-8")
                word_errors(reference, "")
            except (OSError, ValueError) as exc:
                parser.error(f"Invalid reference {reference_path}: {exc}")
            references[path] = reference

    results: list[dict[str, object]] = []

    with httpx2.Client(base_url=args.base_url, timeout=30) as client:
        ready = client.get("/health/ready")
        if ready.status_code != 200:
            raise SystemExit(f"job service did not become ready: {ready.text}")

        for language, path in args.sample:
            started_at = monotonic()
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
            with path.open("rb") as media:
                digest = hashlib.file_digest(media, "sha256").hexdigest()
            measured: dict[str, object] = {
                "sample": path.name,
                "media_sha256": digest,
                "requested_language": language,
                "audio_mode": args.audio_mode,
                "model_profile": args.model_profile,
                "wall_time_seconds": round(monotonic() - started_at, 3),
                "result": payload,
            }
            if path in references:
                edits, count = word_errors(references[path], payload["text"])
                measured.update(
                    reference=references[path],
                    word_errors=edits,
                    reference_word_count=count,
                    word_error_rate=edits / count,
                )
                print(f"{path.name}: WER {edits / count:.1%} ({edits}/{count})")
            results.append(measured)

    report: dict[str, object] = {"samples": results}
    if references:
        total_edits = sum(int(item["word_errors"]) for item in results)
        total_words = sum(int(item["reference_word_count"]) for item in results)
        report["word_error_rate"] = total_edits / total_words
        print(f"Combined WER: {total_edits / total_words:.1%} ({total_edits}/{total_words})")
    if args.report:
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    main()

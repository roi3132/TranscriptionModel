"""Render one timestamped transcription into downloadable text formats."""

from enum import StrEnum

from transcription_service.schemas.jobs import JobResultResponse


class ResultFormat(StrEnum):
    JSON = "json"
    TXT = "txt"
    SRT = "srt"
    VTT = "vtt"


def render_result(result: JobResultResponse, result_format: ResultFormat) -> str:
    if result_format is ResultFormat.TXT:
        return f"{result.text}\n"

    separator = "," if result_format is ResultFormat.SRT else "."
    cues: list[str] = []
    for index, segment in enumerate(result.segments, start=1):
        timing = f"{_timestamp(segment.start, separator)} --> {_timestamp(segment.end, separator)}"
        if result_format is ResultFormat.SRT:
            cues.append(f"{index}\n{timing}\n{segment.text}")
        else:
            cues.append(f"{timing}\n{segment.text}")

    body = "\n\n".join(cues)
    if result_format is ResultFormat.VTT:
        return f"WEBVTT\n\n{body}\n"
    return f"{body}\n" if body else ""


def _timestamp(seconds: float, separator: str) -> str:
    total_milliseconds = max(0, round(seconds * 1000))
    hours, remainder = divmod(total_milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    whole_seconds, milliseconds = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{whole_seconds:02d}{separator}{milliseconds:03d}"

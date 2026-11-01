"""Local Docker adapters for job state, queueing, and media storage."""

import json
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO, cast

import psycopg
from psycopg.rows import dict_row
from redis import Redis

from transcription_service.domain import AudioMode, ModelProfile
from transcription_service.jobs.domain import JobStatus, TranscriptionJob


class PostgresJobRepository:
    """Persist authoritative job state and results in PostgreSQL."""

    def __init__(self, database_url: str) -> None:
        self._database_url = database_url

    def _connect(self) -> psycopg.Connection[dict[str, object]]:
        return psycopg.connect(self._database_url, row_factory=dict_row)

    def migrate(self) -> None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                CREATE TABLE IF NOT EXISTS transcription_jobs (
                    id UUID PRIMARY KEY,
                    status TEXT NOT NULL,
                    media_key TEXT NOT NULL,
                    filename TEXT,
                    content_type TEXT,
                    language TEXT,
                    audio_mode TEXT NOT NULL,
                    model_profile TEXT NOT NULL,
                    result JSONB,
                    error_code TEXT,
                    error_message TEXT,
                    created_at TIMESTAMPTZ NOT NULL,
                    updated_at TIMESTAMPTZ NOT NULL
                );
                INSERT INTO schema_migrations (version) VALUES (1)
                ON CONFLICT (version) DO NOTHING;
                """
            )

    def ping(self) -> None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()

    def create(self, job: TranscriptionJob) -> None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO transcription_jobs (
                    id, status, media_key, filename, content_type, language,
                    audio_mode, model_profile, created_at, updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    job.id,
                    job.status.value,
                    job.media_key,
                    job.filename,
                    job.content_type,
                    job.language,
                    job.audio_mode.value,
                    job.model_profile.value,
                    job.created_at,
                    job.updated_at,
                ),
            )

    def get(self, job_id: str) -> TranscriptionJob | None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT * FROM transcription_jobs WHERE id = %s", (job_id,))
            row = cursor.fetchone()
        if row is None:
            return None
        return self._job_from_row(row)

    def mark_running(self, job_id: str) -> bool:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE transcription_jobs
                SET status = %s, updated_at = NOW()
                WHERE id = %s AND status = %s
                """,
                (JobStatus.RUNNING.value, job_id, JobStatus.QUEUED.value),
            )
            return cursor.rowcount == 1

    def mark_succeeded(self, job_id: str, result: dict[str, object]) -> None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE transcription_jobs
                SET status = %s, result = %s::jsonb, error_code = NULL,
                    error_message = NULL, updated_at = NOW()
                WHERE id = %s
                """,
                (JobStatus.SUCCEEDED.value, json.dumps(result), job_id),
            )

    def mark_failed(self, job_id: str, *, code: str, message: str) -> None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE transcription_jobs
                SET status = %s, error_code = %s, error_message = %s, updated_at = NOW()
                WHERE id = %s
                """,
                (JobStatus.FAILED.value, code, message, job_id),
            )

    @staticmethod
    def _job_from_row(row: dict[str, object]) -> TranscriptionJob:
        raw_result = row["result"]
        result = cast(dict[str, object] | None, raw_result)
        return TranscriptionJob(
            id=str(row["id"]),
            status=JobStatus(str(row["status"])),
            media_key=str(row["media_key"]),
            filename=cast(str | None, row["filename"]),
            content_type=cast(str | None, row["content_type"]),
            language=cast(str | None, row["language"]),
            audio_mode=AudioMode(str(row["audio_mode"])),
            model_profile=ModelProfile(str(row["model_profile"])),
            result=result,
            error_code=cast(str | None, row["error_code"]),
            error_message=cast(str | None, row["error_message"]),
            created_at=cast(datetime, row["created_at"]),
            updated_at=cast(datetime, row["updated_at"]),
        )


class RedisJobBroker:
    """Use a Redis list as the durable worker handoff queue."""

    def __init__(self, redis_url: str, queue_name: str) -> None:
        self._client: Redis = Redis.from_url(redis_url, decode_responses=True)
        self._queue_name = queue_name

    def ping(self) -> None:
        self._client.ping()

    def enqueue(self, job_id: str) -> None:
        self._client.lpush(self._queue_name, job_id)

    def dequeue(self, timeout_seconds: int) -> str | None:
        item = cast(
            list[object] | None,
            self._client.brpop(self._queue_name, timeout=timeout_seconds),
        )
        return None if item is None else str(item[1])

    def close(self) -> None:
        self._client.close()


class LocalMediaStore:
    """Store accepted uploads under generated keys on a named local volume."""

    def __init__(self, root: Path) -> None:
        self._root = root
        self._root.mkdir(parents=True, exist_ok=True)

    def save(self, key: str, source: BinaryIO) -> None:
        destination = self.path_for(key)
        temporary = destination.with_suffix(".part")
        source.seek(0)
        with temporary.open("wb") as output:
            shutil.copyfileobj(source, output, length=1024 * 1024)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, destination)
        source.seek(0)

    def path_for(self, key: str) -> Path:
        if not key or Path(key).name != key:
            raise ValueError("media key must be a generated filename")
        return self._root / key

    def delete(self, key: str) -> None:
        self.path_for(key).unlink(missing_ok=True)


def new_job(
    *,
    job_id: str,
    media_key: str,
    filename: str | None,
    content_type: str | None,
    language: str | None,
    audio_mode: AudioMode,
    model_profile: ModelProfile,
) -> TranscriptionJob:
    now = datetime.now(UTC)
    return TranscriptionJob(
        id=job_id,
        status=JobStatus.QUEUED,
        media_key=media_key,
        filename=filename,
        content_type=content_type,
        language=language,
        audio_mode=audio_mode,
        model_profile=model_profile,
        result=None,
        error_code=None,
        error_message=None,
        created_at=now,
        updated_at=now,
    )

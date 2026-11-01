"""Local Docker adapters for job state, queueing, and media storage."""

import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from typing import BinaryIO, cast

import psycopg
from psycopg.rows import dict_row
from redis import Redis

from transcription_service.domain import AudioMode, ModelProfile
from transcription_service.jobs.domain import (
    JobStatus,
    MediaStorageLimitReached,
    PendingJobLimitReached,
    StoredMedia,
    SubmissionDecision,
    SubmissionReason,
    TranscriptionJob,
)


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
                    model_revision TEXT NOT NULL DEFAULT '',
                    media_sha256 TEXT NOT NULL DEFAULT '',
                    cache_key TEXT NOT NULL DEFAULT '',
                    idempotency_key TEXT,
                    size_bytes BIGINT NOT NULL DEFAULT 0,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER NOT NULL DEFAULT 3,
                    result JSONB,
                    error_code TEXT,
                    error_message TEXT,
                    available_at TIMESTAMPTZ NOT NULL,
                    lease_expires_at TIMESTAMPTZ,
                    expires_at TIMESTAMPTZ,
                    created_at TIMESTAMPTZ NOT NULL,
                    updated_at TIMESTAMPTZ NOT NULL
                );
                ALTER TABLE transcription_jobs
                    ADD COLUMN IF NOT EXISTS model_revision TEXT NOT NULL DEFAULT '',
                    ADD COLUMN IF NOT EXISTS media_sha256 TEXT NOT NULL DEFAULT '',
                    ADD COLUMN IF NOT EXISTS cache_key TEXT NOT NULL DEFAULT '',
                    ADD COLUMN IF NOT EXISTS idempotency_key TEXT,
                    ADD COLUMN IF NOT EXISTS size_bytes BIGINT NOT NULL DEFAULT 0,
                    ADD COLUMN IF NOT EXISTS attempts INTEGER NOT NULL DEFAULT 0,
                    ADD COLUMN IF NOT EXISTS max_attempts INTEGER NOT NULL DEFAULT 3,
                    ADD COLUMN IF NOT EXISTS available_at TIMESTAMPTZ,
                    ADD COLUMN IF NOT EXISTS lease_expires_at TIMESTAMPTZ,
                    ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ;
                UPDATE transcription_jobs
                SET available_at = created_at
                WHERE available_at IS NULL;
                ALTER TABLE transcription_jobs
                    ALTER COLUMN available_at SET NOT NULL;
                CREATE UNIQUE INDEX IF NOT EXISTS transcription_jobs_idempotency_key
                    ON transcription_jobs (idempotency_key)
                    WHERE idempotency_key IS NOT NULL;
                CREATE INDEX IF NOT EXISTS transcription_jobs_dispatchable
                    ON transcription_jobs (status, available_at);
                CREATE INDEX IF NOT EXISTS transcription_jobs_expiry
                    ON transcription_jobs (expires_at)
                    WHERE expires_at IS NOT NULL;
                CREATE INDEX IF NOT EXISTS transcription_jobs_cache_key
                    ON transcription_jobs (cache_key);
                CREATE TABLE IF NOT EXISTS transcription_idempotency_keys (
                    idempotency_key TEXT PRIMARY KEY,
                    job_id UUID NOT NULL REFERENCES transcription_jobs (id),
                    cache_key TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                INSERT INTO transcription_idempotency_keys (
                    idempotency_key, job_id, cache_key, created_at
                )
                SELECT idempotency_key, id, cache_key, created_at
                FROM transcription_jobs
                WHERE idempotency_key IS NOT NULL
                ON CONFLICT (idempotency_key) DO NOTHING;
                INSERT INTO schema_migrations (version) VALUES (1), (2), (3)
                ON CONFLICT (version) DO NOTHING;
                """
            )

    def ping(self) -> None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()

    def create_or_reuse(
        self,
        job: TranscriptionJob,
        *,
        max_pending_jobs: int,
    ) -> SubmissionDecision:
        with self._connect() as connection, connection.cursor() as cursor:
            if job.idempotency_key is not None:
                cursor.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    (f"idempotency:{job.idempotency_key}",),
                )
            cursor.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (f"cache:{job.cache_key}",),
            )

            if job.idempotency_key is not None:
                cursor.execute(
                    """
                    SELECT jobs.* FROM transcription_idempotency_keys AS keys
                    JOIN transcription_jobs AS jobs ON jobs.id = keys.job_id
                    WHERE keys.idempotency_key = %s
                    """,
                    (job.idempotency_key,),
                )
                existing = cursor.fetchone()
                if existing is not None:
                    return SubmissionDecision(
                        job=self._job_from_row(existing),
                        reason=SubmissionReason.IDEMPOTENT,
                    )

            cursor.execute(
                """
                SELECT * FROM transcription_jobs
                WHERE cache_key = %s
                  AND (
                    status IN (%s, %s)
                    OR (status = %s AND (expires_at IS NULL OR expires_at > NOW()))
                  )
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (
                    job.cache_key,
                    JobStatus.QUEUED.value,
                    JobStatus.RUNNING.value,
                    JobStatus.SUCCEEDED.value,
                ),
            )
            existing = cursor.fetchone()
            if existing is not None:
                reused = self._job_from_row(existing)
                reason = (
                    SubmissionReason.CACHED_RESULT
                    if reused.status is JobStatus.SUCCEEDED
                    else SubmissionReason.ACTIVE_DUPLICATE
                )
                if job.idempotency_key is not None:
                    cursor.execute(
                        """
                        INSERT INTO transcription_idempotency_keys (
                            idempotency_key, job_id, cache_key
                        ) VALUES (%s, %s, %s)
                        """,
                        (job.idempotency_key, reused.id, reused.cache_key),
                    )
                return SubmissionDecision(job=reused, reason=reason)

            cursor.execute("SELECT pg_advisory_xact_lock(hashtextextended('job-capacity', 0))")
            cursor.execute(
                "SELECT COUNT(*) AS count FROM transcription_jobs WHERE status IN (%s, %s)",
                (JobStatus.QUEUED.value, JobStatus.RUNNING.value),
            )
            count_row = cursor.fetchone()
            active_count = 0 if count_row is None else cast(int, count_row["count"])
            if active_count >= max_pending_jobs:
                raise PendingJobLimitReached

            cursor.execute(
                """
                INSERT INTO transcription_jobs (
                    id, status, media_key, filename, content_type, language,
                    audio_mode, model_profile, model_revision, media_sha256,
                    cache_key, idempotency_key, size_bytes, attempts, max_attempts,
                    result, error_code, error_message, available_at,
                    lease_expires_at, expires_at, created_at, updated_at
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                )
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
                    job.model_revision,
                    job.media_sha256,
                    job.cache_key,
                    job.idempotency_key,
                    job.size_bytes,
                    job.attempts,
                    job.max_attempts,
                    json.dumps(job.result) if job.result is not None else None,
                    job.error_code,
                    job.error_message,
                    job.available_at,
                    job.lease_expires_at,
                    job.expires_at,
                    job.created_at,
                    job.updated_at,
                ),
            )
            if job.idempotency_key is not None:
                cursor.execute(
                    """
                    INSERT INTO transcription_idempotency_keys (
                        idempotency_key, job_id, cache_key
                    ) VALUES (%s, %s, %s)
                    """,
                    (job.idempotency_key, job.id, job.cache_key),
                )
        return SubmissionDecision(job=job, reason=SubmissionReason.CREATED)

    def get(self, job_id: str) -> TranscriptionJob | None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT * FROM transcription_jobs WHERE id = %s", (job_id,))
            row = cursor.fetchone()
        return None if row is None else self._job_from_row(row)

    def claim(self, job_id: str, *, lease_expires_at: datetime) -> TranscriptionJob | None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE transcription_jobs
                SET status = %s, attempts = attempts + 1,
                    lease_expires_at = %s, error_code = NULL,
                    error_message = NULL, updated_at = NOW()
                WHERE id = %s AND status = %s AND available_at <= NOW()
                RETURNING *
                """,
                (
                    JobStatus.RUNNING.value,
                    lease_expires_at,
                    job_id,
                    JobStatus.QUEUED.value,
                ),
            )
            row = cursor.fetchone()
        return None if row is None else self._job_from_row(row)

    def renew_lease(self, job_id: str, *, lease_expires_at: datetime) -> bool:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE transcription_jobs
                SET lease_expires_at = %s, updated_at = NOW()
                WHERE id = %s AND status = %s
                """,
                (lease_expires_at, job_id, JobStatus.RUNNING.value),
            )
            return cursor.rowcount == 1

    def mark_succeeded(
        self,
        job_id: str,
        result: dict[str, object],
        *,
        expires_at: datetime,
    ) -> None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE transcription_jobs
                SET status = %s, result = %s::jsonb, error_code = NULL,
                    error_message = NULL, lease_expires_at = NULL,
                    expires_at = %s, updated_at = NOW()
                WHERE id = %s AND status = %s
                """,
                (
                    JobStatus.SUCCEEDED.value,
                    json.dumps(result),
                    expires_at,
                    job_id,
                    JobStatus.RUNNING.value,
                ),
            )

    def mark_failed(
        self,
        job_id: str,
        *,
        code: str,
        message: str,
        expires_at: datetime,
    ) -> None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE transcription_jobs
                SET status = %s, error_code = %s, error_message = %s,
                    lease_expires_at = NULL, expires_at = %s, updated_at = NOW()
                WHERE id = %s AND status IN (%s, %s)
                """,
                (
                    JobStatus.FAILED.value,
                    code,
                    message,
                    expires_at,
                    job_id,
                    JobStatus.QUEUED.value,
                    JobStatus.RUNNING.value,
                ),
            )

    def schedule_retry(
        self,
        job_id: str,
        *,
        code: str,
        message: str,
        available_at: datetime,
    ) -> bool:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE transcription_jobs
                SET status = %s, error_code = %s, error_message = %s,
                    available_at = %s, lease_expires_at = NULL, updated_at = NOW()
                WHERE id = %s AND status = %s
                """,
                (
                    JobStatus.QUEUED.value,
                    code,
                    message,
                    available_at,
                    job_id,
                    JobStatus.RUNNING.value,
                ),
            )
            return cursor.rowcount == 1

    def recover_stale(
        self,
        *,
        now: datetime,
        failed_expires_at: datetime,
    ) -> list[str]:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE transcription_jobs
                SET status = %s, error_code = 'worker_crash',
                    error_message = 'The worker stopped before completing the job.',
                    lease_expires_at = NULL, expires_at = %s, updated_at = NOW()
                WHERE status = %s AND lease_expires_at <= %s
                  AND attempts >= max_attempts
                """,
                (
                    JobStatus.FAILED.value,
                    failed_expires_at,
                    JobStatus.RUNNING.value,
                    now,
                ),
            )
            cursor.execute(
                """
                UPDATE transcription_jobs
                SET status = %s, available_at = %s, lease_expires_at = NULL,
                    error_code = 'worker_crash',
                    error_message = 'Retrying after a worker interruption.',
                    updated_at = NOW()
                WHERE status = %s AND lease_expires_at <= %s
                  AND attempts < max_attempts
                RETURNING id
                """,
                (
                    JobStatus.QUEUED.value,
                    now,
                    JobStatus.RUNNING.value,
                    now,
                ),
            )
            return [str(row["id"]) for row in cursor.fetchall()]

    def list_dispatchable(self, *, now: datetime, limit: int) -> list[str]:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT id FROM transcription_jobs
                WHERE status = %s AND available_at <= %s
                ORDER BY available_at, created_at
                LIMIT %s
                """,
                (JobStatus.QUEUED.value, now, limit),
            )
            return [str(row["id"]) for row in cursor.fetchall()]

    def expire_due(self, *, now: datetime, limit: int) -> list[str]:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                WITH due AS (
                    SELECT id FROM transcription_jobs
                    WHERE status IN (%s, %s) AND expires_at <= %s
                    ORDER BY expires_at
                    FOR UPDATE SKIP LOCKED
                    LIMIT %s
                )
                UPDATE transcription_jobs AS jobs
                SET status = %s, result = NULL, lease_expires_at = NULL,
                    updated_at = NOW()
                FROM due
                WHERE jobs.id = due.id
                RETURNING jobs.media_key
                """,
                (
                    JobStatus.SUCCEEDED.value,
                    JobStatus.FAILED.value,
                    now,
                    limit,
                    JobStatus.EXPIRED.value,
                ),
            )
            return [str(row["media_key"]) for row in cursor.fetchall()]

    @staticmethod
    def _job_from_row(row: dict[str, object]) -> TranscriptionJob:
        return TranscriptionJob(
            id=str(row["id"]),
            status=JobStatus(str(row["status"])),
            media_key=str(row["media_key"]),
            filename=cast(str | None, row["filename"]),
            content_type=cast(str | None, row["content_type"]),
            language=cast(str | None, row["language"]),
            audio_mode=AudioMode(str(row["audio_mode"])),
            model_profile=ModelProfile(str(row["model_profile"])),
            model_revision=str(row["model_revision"]),
            media_sha256=str(row["media_sha256"]),
            cache_key=str(row["cache_key"]),
            idempotency_key=cast(str | None, row["idempotency_key"]),
            size_bytes=cast(int, row["size_bytes"]),
            attempts=cast(int, row["attempts"]),
            max_attempts=cast(int, row["max_attempts"]),
            result=cast(dict[str, object] | None, row["result"]),
            error_code=cast(str | None, row["error_code"]),
            error_message=cast(str | None, row["error_message"]),
            available_at=cast(datetime, row["available_at"]),
            lease_expires_at=cast(datetime | None, row["lease_expires_at"]),
            expires_at=cast(datetime | None, row["expires_at"]),
            created_at=cast(datetime, row["created_at"]),
            updated_at=cast(datetime, row["updated_at"]),
        )


class RedisJobBroker:
    """Use Redis for durable handoff and an expiring worker heartbeat."""

    def __init__(self, redis_url: str, queue_name: str) -> None:
        self._client: Redis = Redis.from_url(redis_url, decode_responses=True)
        self._queue_name = queue_name
        self._heartbeat_key = f"{queue_name}:worker-ready"

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

    def heartbeat(self, ttl_seconds: int) -> None:
        self._client.set(self._heartbeat_key, datetime.now(UTC).isoformat(), ex=ttl_seconds)

    def worker_is_ready(self) -> bool:
        return bool(self._client.exists(self._heartbeat_key))

    def close(self) -> None:
        self._client.close()


class LocalMediaStore:
    """Store accepted uploads under generated keys on a named local volume."""

    def __init__(self, root: Path) -> None:
        self._root = root
        self._root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._root.chmod(0o700)
        self._write_lock = Lock()

    def save(
        self,
        key: str,
        source: BinaryIO,
        *,
        max_total_bytes: int,
    ) -> StoredMedia:
        destination = self.path_for(key)
        temporary = destination.with_suffix(".part")
        digest = hashlib.sha256()
        size_bytes = 0
        source.seek(0)
        try:
            with self._write_lock:
                current_bytes = self._stored_bytes()
                descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(descriptor, "wb") as output:
                    while chunk := source.read(1024 * 1024):
                        size_bytes += len(chunk)
                        if current_bytes + size_bytes > max_total_bytes:
                            raise MediaStorageLimitReached
                        digest.update(chunk)
                        output.write(chunk)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temporary, destination)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        finally:
            source.seek(0)
        return StoredMedia(size_bytes=size_bytes, sha256=digest.hexdigest())

    def path_for(self, key: str) -> Path:
        if not key or Path(key).name != key:
            raise ValueError("media key must be a generated filename")
        return self._root / key

    def delete(self, key: str) -> None:
        media_path = self.path_for(key)
        media_path.unlink(missing_ok=True)
        checkpoint = media_path.with_suffix(".chunks.json")
        checkpoint.unlink(missing_ok=True)
        checkpoint.with_suffix(f"{checkpoint.suffix}.tmp").unlink(missing_ok=True)

    def _stored_bytes(self) -> int:
        return sum(
            item.stat().st_size
            for item in self._root.iterdir()
            if item.is_file() and item.suffix != ".part"
        )


def new_job(
    *,
    job_id: str,
    media_key: str,
    filename: str | None,
    content_type: str | None,
    language: str | None,
    audio_mode: AudioMode,
    model_profile: ModelProfile,
    model_revision: str,
    media_sha256: str,
    cache_key: str,
    idempotency_key: str | None,
    size_bytes: int,
    max_attempts: int,
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
        model_revision=model_revision,
        media_sha256=media_sha256,
        cache_key=cache_key,
        idempotency_key=idempotency_key,
        size_bytes=size_bytes,
        attempts=0,
        max_attempts=max_attempts,
        result=None,
        error_code=None,
        error_message=None,
        available_at=now,
        lease_expires_at=None,
        expires_at=None,
        created_at=now,
        updated_at=now,
    )

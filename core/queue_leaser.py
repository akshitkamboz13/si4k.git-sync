"""
Distributed Queue Leaser and State Machine for SQLite.
Ensures zero race-conditions, crash-recovery of expired leases, and exponential backoff.
"""

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from database.db import get_connection
from core.models import SyncJob, SyncStatus
from core.logger import get_logger

logger = get_logger("si4k.queue_leaser")


class QueueLeaser:
    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = db_path

    def enqueue(
        self,
        project_name: str,
        source_node: str,
        target_node: str,
        branch_name: str,
        commit_sha: str,
        target_ref: Optional[str] = None,
        operation_id: Optional[str] = None,
    ) -> SyncJob:
        if not operation_id:
            operation_id = f"op-{project_name}-{target_node}-{branch_name}-{commit_sha[:8]}-{uuid.uuid4().hex[:6]}"

        with get_connection(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO sync_queue (
                    operation_id, project_name, source_node, target_node,
                    branch_name, target_ref, commit_sha, status,
                    next_retry_at, created_at, updated_at
                ) VALUES (
                    ?, ?, ?, ?,
                    ?, ?, ?, 'queued',
                    datetime('now'), datetime('now'), datetime('now')
                )
                ON CONFLICT(operation_id) DO UPDATE SET
                    commit_sha = excluded.commit_sha,
                    status = CASE WHEN sync_queue.status = 'synced' THEN 'queued' ELSE sync_queue.status END,
                    updated_at = datetime('now')
                RETURNING *;
                """,
                (
                    operation_id,
                    project_name,
                    source_node,
                    target_node,
                    branch_name,
                    target_ref,
                    commit_sha,
                ),
            )
            row = cursor.fetchone()
            job = SyncJob.from_row(row)

            # Record audit
            cursor.execute(
                "INSERT INTO sync_audit_log (operation_id, event_type, details) VALUES (?, ?, ?)",
                (
                    operation_id,
                    "ENQUEUED",
                    json.dumps({
                        "project": project_name,
                        "source": source_node,
                        "target": target_node,
                        "branch": branch_name,
                        "sha": commit_sha,
                    }),
                ),
            )
            conn.commit()
            logger.info(f"Enqueued job #{job.id} [{operation_id}] {project_name} -> {target_node}")
            return job

    def acquire_lease(self, worker_id: str, lease_duration_sec: int = 60) -> Optional[SyncJob]:
        """
        Atomically queries the next eligible pending or expired-lease job and claims a lease.
        """
        with get_connection(self.db_path) as conn:
            cursor = conn.cursor()
            
            # Select candidate
            cursor.execute(
                """
                SELECT id FROM sync_queue
                WHERE (status = 'queued' AND next_retry_at <= datetime('now'))
                   OR (status IN ('locked', 'in_progress') AND locked_until IS NOT NULL AND locked_until < datetime('now'))
                ORDER BY created_at ASC
                LIMIT 1;
                """
            )
            candidate = cursor.fetchone()
            if not candidate:
                return None

            job_id = candidate["id"]

            # Atomic conditional claim
            cursor.execute(
                """
                UPDATE sync_queue
                SET status = 'locked',
                    locked_by = ?,
                    locked_until = datetime('now', '+' || ? || ' seconds'),
                    updated_at = datetime('now')
                WHERE id = ?
                  AND (
                      (status = 'queued' AND next_retry_at <= datetime('now'))
                      OR (status IN ('locked', 'in_progress') AND locked_until < datetime('now'))
                  )
                RETURNING *;
                """,
                (worker_id, lease_duration_sec, job_id),
            )
            claimed = cursor.fetchone()
            if claimed:
                conn.commit()
                job = SyncJob.from_row(claimed)
                logger.info(f"Worker {worker_id} acquired lease on job #{job.id} [{job.operation_id}] until {job.locked_until}")
                return job
            return None

    def heartbeat(self, job_id: int, worker_id: str, extension_sec: int = 60) -> bool:
        """Extends locked_until timestamp for a currently locked job."""
        with get_connection(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE sync_queue
                SET locked_until = datetime('now', '+' || ? || ' seconds'),
                    updated_at = datetime('now')
                WHERE id = ? AND locked_by = ? AND status IN ('locked', 'in_progress');
                """,
                (extension_sec, job_id, worker_id),
            )
            conn.commit()
            return cursor.rowcount > 0

    def mark_in_progress(self, job_id: int, worker_id: str) -> bool:
        with get_connection(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE sync_queue
                SET status = 'in_progress',
                    updated_at = datetime('now')
                WHERE id = ? AND locked_by = ?;
                """,
                (job_id, worker_id),
            )
            conn.commit()
            return cursor.rowcount > 0

    def mark_completed(self, job_id: int, worker_id: str) -> bool:
        with get_connection(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE sync_queue
                SET status = 'synced',
                    locked_by = NULL,
                    locked_until = NULL,
                    last_error = NULL,
                    updated_at = datetime('now')
                WHERE id = ? AND locked_by = ?
                RETURNING operation_id, project_name, target_node;
                """,
                (job_id, worker_id),
            )
            row = cursor.fetchone()
            if row:
                cursor.execute(
                    "INSERT INTO sync_audit_log (operation_id, event_type, details) VALUES (?, ?, ?)",
                    (row["operation_id"], "PUSH_SUCCESS", json.dumps({"target": row["target_node"]})),
                )
                conn.commit()
                logger.info(f"Job #{job_id} [{row['operation_id']}] marked SYNCED successfully.")
                return True
            return False

    def mark_failed(self, job_id: int, worker_id: str, error_message: str) -> bool:
        with get_connection(self.db_path) as conn:
            cursor = conn.cursor()
            # Fetch current retry count & max_retries
            cursor.execute("SELECT retry_count, max_retries, backoff_base_sec, operation_id FROM sync_queue WHERE id = ?", (job_id,))
            row = cursor.fetchone()
            if not row:
                return False

            retry_count = row["retry_count"]
            max_retries = row["max_retries"]
            backoff_base = row["backoff_base_sec"]
            operation_id = row["operation_id"]

            new_retry = retry_count + 1
            if new_retry >= max_retries:
                new_status = SyncStatus.DEAD_LETTER.value
                event_type = "DEAD_LETTER"
                next_retry_expr = "datetime('now', '+365 days')"
            else:
                new_status = SyncStatus.QUEUED.value
                event_type = "RETRY_SCHEDULED"
                # Exponential backoff: base * 2^retry
                delay_sec = backoff_base * (1 << retry_count)
                next_retry_expr = f"datetime('now', '+{delay_sec} seconds')"

            cursor.execute(
                f"""
                UPDATE sync_queue
                SET status = ?,
                    retry_count = ?,
                    next_retry_at = {next_retry_expr},
                    locked_by = NULL,
                    locked_until = NULL,
                    last_error = ?,
                    updated_at = datetime('now')
                WHERE id = ? AND locked_by = ?;
                """,
                (new_status, new_retry, error_message[:1000], job_id, worker_id),
            )
            
            cursor.execute(
                "INSERT INTO sync_audit_log (operation_id, event_type, details) VALUES (?, ?, ?)",
                (operation_id, event_type, json.dumps({"error": error_message, "retry": new_retry})),
            )
            conn.commit()
            logger.warning(f"Job #{job_id} failed (attempt {new_retry}/{max_retries}): {error_message} -> Status: {new_status}")
            return True

    def list_jobs(self, status: Optional[str] = None, limit: int = 50) -> List[SyncJob]:
        with get_connection(self.db_path) as conn:
            cursor = conn.cursor()
            if status:
                cursor.execute(
                    "SELECT * FROM sync_queue WHERE status = ? ORDER BY id DESC LIMIT ?",
                    (status, limit),
                )
            else:
                cursor.execute(
                    "SELECT * FROM sync_queue ORDER BY id DESC LIMIT ?",
                    (limit,),
                )
            return [SyncJob.from_row(r) for r in cursor.fetchall()]

    def queue_stats(self) -> dict:
        with get_connection(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT status, count(*) as cnt
                FROM sync_queue
                GROUP BY status;
                """
            )
            stats = {r["status"]: r["cnt"] for r in cursor.fetchall()}
            cursor.execute("SELECT count(*) as total FROM sync_queue;")
            stats["total"] = cursor.fetchone()["total"]
            return stats

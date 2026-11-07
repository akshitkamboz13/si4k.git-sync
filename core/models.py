"""
Data models and type definitions for si4k.git-sync.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Optional


class SyncStatus(str, Enum):
    QUEUED = "queued"
    LOCKED = "locked"
    IN_PROGRESS = "in_progress"
    SYNCED = "synced"
    FAILED = "failed"
    DEAD_LETTER = "dead_letter"


class PRStatus(str, Enum):
    PENDING_FETCH = "pending_fetch"
    MR_CREATED = "mr_created"
    MR_APPROVED = "mr_approved"
    MERGING = "merging"
    MERGED = "merged"
    REJECTED = "rejected"
    FAILED = "failed"


class NodeType(str, Enum):
    LAPTOP_GITLAB = "laptop_gitlab"
    SERVER_GITLAB = "server_gitlab"
    GITHUB = "github"


@dataclass
class SyncJob:
    id: int
    operation_id: str
    project_name: str
    source_node: str
    target_node: str
    branch_name: str
    commit_sha: str
    target_ref: Optional[str] = None
    status: SyncStatus = SyncStatus.QUEUED
    retry_count: int = 0
    max_retries: int = 5
    backoff_base_sec: int = 30
    next_retry_at: Optional[datetime] = None
    locked_by: Optional[str] = None
    locked_until: Optional[datetime] = None
    last_error: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    @classmethod
    def from_row(cls, row) -> "SyncJob":
        return cls(
            id=row["id"],
            operation_id=row["operation_id"],
            project_name=row["project_name"],
            source_node=row["source_node"],
            target_node=row["target_node"],
            branch_name=row["branch_name"],
            target_ref=row["target_ref"],
            commit_sha=row["commit_sha"],
            status=SyncStatus(row["status"]),
            retry_count=row["retry_count"],
            max_retries=row["max_retries"],
            backoff_base_sec=row["backoff_base_sec"],
            next_retry_at=datetime.fromisoformat(row["next_retry_at"]) if row["next_retry_at"] else None,
            locked_by=row["locked_by"],
            locked_until=datetime.fromisoformat(row["locked_until"]) if row["locked_until"] else None,
            last_error=row["last_error"],
            created_at=datetime.fromisoformat(row["created_at"]) if row["created_at"] else None,
            updated_at=datetime.fromisoformat(row["updated_at"]) if row["updated_at"] else None,
        )


@dataclass
class NodeHealth:
    node_name: str
    endpoint_url: str
    ssh_host: str
    is_reachable: bool = True
    last_ping_latency_ms: Optional[float] = None
    last_error: Optional[str] = None
    last_checked_at: Optional[datetime] = None

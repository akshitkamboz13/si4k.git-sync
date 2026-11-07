"""
Status Exporter for Home Server Web Dashboard.
Periodically exports sync queue, node health, PR, and Issue metrics
to a lightweight JSON file served by Caddy.
"""

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from core.config import SyncConfig
from core.logger import get_logger
from database.db import get_connection

logger = get_logger("si4k.status_exporter")


class StatusExporter:
    def __init__(self, config: SyncConfig, export_path: Optional[Path] = None):
        self.config = config
        if export_path:
            self.export_path = export_path
        else:
            # Check for /usr/share/caddy first, then fallback to data_dir
            caddy_dir = Path("/usr/share/caddy")
            if caddy_dir.is_dir() and os.access(caddy_dir, os.W_OK):
                self.export_path = caddy_dir / "sync-status.json"
            else:
                self.export_path = self.config.data_dir / "sync-status.json"

    def collect_status(self) -> Dict[str, Any]:
        status_data: Dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "worker_id": self.config.worker_id,
            "nodes": {},
            "queue": {
                "total": 0,
                "queued": 0,
                "locked": 0,
                "in_progress": 0,
                "synced": 0,
                "failed": 0,
                "dead_letter": 0,
            },
            "recent_jobs": [],
            "prs": {
                "total": 0,
                "open": 0,
                "merged": 0,
            },
            "issues": {
                "total": 0,
                "open": 0,
                "closed": 0,
            },
        }

        with get_connection(self.config.db_path) as conn:
            cursor = conn.cursor()

            # 1. Node Health
            cursor.execute("SELECT * FROM node_health ORDER BY node_name;")
            for r in cursor.fetchall():
                status_data["nodes"][r["node_name"]] = {
                    "is_reachable": bool(r["is_reachable"]),
                    "latency_ms": round(r["last_ping_latency_ms"], 1) if r["last_ping_latency_ms"] else None,
                    "endpoint_url": r["endpoint_url"],
                    "last_checked_at": r["last_checked_at"],
                }

            # 2. Queue Aggregates
            cursor.execute("SELECT status, count(*) as cnt FROM sync_queue GROUP BY status;")
            total_jobs = 0
            for r in cursor.fetchall():
                st = r["status"]
                cnt = r["cnt"]
                total_jobs += cnt
                if st in status_data["queue"]:
                    status_data["queue"][st] = cnt
            status_data["queue"]["total"] = total_jobs

            # 3. Recent 5 Jobs
            cursor.execute(
                """
                SELECT id, operation_id, project_name, source_node, target_node,
                       branch_name, commit_sha, status, last_error, updated_at
                FROM sync_queue
                ORDER BY id DESC LIMIT 5;
                """
            )
            for r in cursor.fetchall():
                status_data["recent_jobs"].append({
                    "id": r["id"],
                    "project": r["project_name"],
                    "source": r["source_node"],
                    "target": r["target_node"],
                    "branch": r["branch_name"],
                    "sha": r["commit_sha"][:8] if r["commit_sha"] else "",
                    "status": r["status"],
                    "updated_at": r["updated_at"],
                })

            # 4. PR Mappings
            cursor.execute(
                """
                SELECT count(*) as total,
                       sum(case when status != 'merged' then 1 else 0 end) as open_cnt,
                       sum(case when status = 'merged' then 1 else 0 end) as merged_cnt
                FROM pr_mappings;
                """
            )
            pr_row = cursor.fetchone()
            if pr_row and pr_row["total"]:
                status_data["prs"]["total"] = pr_row["total"]
                status_data["prs"]["open"] = pr_row["open_cnt"] or 0
                status_data["prs"]["merged"] = pr_row["merged_cnt"] or 0

            # 5. Issue Mappings
            cursor.execute(
                """
                SELECT count(*) as total,
                       sum(case when state = 'open' then 1 else 0 end) as open_cnt,
                       sum(case when state = 'closed' then 1 else 0 end) as closed_cnt
                FROM issue_mappings;
                """
            )
            iss_row = cursor.fetchone()
            if iss_row and iss_row["total"]:
                status_data["issues"]["total"] = iss_row["total"]
                status_data["issues"]["open"] = iss_row["open_cnt"] or 0
                status_data["issues"]["closed"] = iss_row["closed_cnt"] or 0

        return status_data

    def export(self) -> Path:
        """Atomically writes JSON status file using tempfile + rename."""
        data = self.collect_status()
        target_dir = self.export_path.parent
        target_dir.mkdir(parents=True, exist_ok=True)

        temp_file = target_dir / f".tmp-{self.export_path.name}"
        with open(temp_file, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

        temp_file.replace(self.export_path)
        logger.debug(f"Exported sync status to {self.export_path}")
        return self.export_path

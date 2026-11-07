"""
Health check monitor for Laptop GitLab, Server GitLab, and GitHub.
Periodically probes each node and updates the node_health database table.
"""

import socket
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional

from database.db import get_connection
from core.config import SyncConfig
from core.logger import get_logger

logger = get_logger("si4k.health_checker")


class HealthChecker:
    def __init__(self, config: SyncConfig):
        self.config = config

    def check_http(self, url: str, timeout: float = 3.0) -> tuple[bool, float, Optional[str]]:
        start = time.perf_counter()
        try:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "si4k-git-sync/1.0"},
                method="HEAD"
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                latency_ms = (time.perf_counter() - start) * 1000.0
                return True, latency_ms, None
        except Exception as e:
            latency_ms = (time.perf_counter() - start) * 1000.0
            # Some servers return 401/403 or 302 on HEAD/GET, which still proves reachability!
            if hasattr(e, "code") and e.code in (401, 403, 302, 301):
                return True, latency_ms, None
            return False, latency_ms, str(e)

    def check_tcp(self, host: str, port: int, timeout: float = 3.0) -> tuple[bool, float, Optional[str]]:
        start = time.perf_counter()
        try:
            with socket.create_connection((host, port), timeout=timeout):
                latency_ms = (time.perf_counter() - start) * 1000.0
                return True, latency_ms, None
        except Exception as e:
            latency_ms = (time.perf_counter() - start) * 1000.0
            return False, latency_ms, str(e)

    def probe_all(self) -> Dict[str, dict]:
        results = {}

        # 1. Server GitLab (Local container or LAN)
        # Check HTTP on 8929
        srv_url = self.config.server_gitlab_url
        srv_ok, srv_latency, srv_err = self.check_http(srv_url)
        results["server_gitlab"] = {
            "endpoint_url": srv_url,
            "ssh_host": "gitlab-server",
            "is_reachable": 1 if srv_ok else 0,
            "latency_ms": srv_latency,
            "error": srv_err,
        }

        # 2. GitHub Cloud
        gh_ok, gh_latency, gh_err = self.check_http("https://api.github.com")
        results["github"] = {
            "endpoint_url": "https://github.com",
            "ssh_host": "github.com",
            "is_reachable": 1 if gh_ok else 0,
            "latency_ms": gh_latency,
            "error": gh_err,
        }

        # 3. Laptop GitLab (May be offline / asleep)
        lap_url = self.config.laptop_gitlab_url
        lap_ok, lap_latency, lap_err = self.check_http(lap_url, timeout=2.0)
        results["laptop_gitlab"] = {
            "endpoint_url": lap_url,
            "ssh_host": "gitlab-laptop",
            "is_reachable": 1 if lap_ok else 0,
            "latency_ms": lap_latency,
            "error": lap_err,
        }

        # Persist into database
        with get_connection(self.config.db_path) as conn:
            cursor = conn.cursor()
            for node_name, data in results.items():
                cursor.execute(
                    """
                    INSERT INTO node_health (
                        node_name, endpoint_url, ssh_host, is_reachable,
                        last_ping_latency_ms, last_error, last_checked_at
                    ) VALUES (?, ?, ?, ?, ?, ?, datetime('now'))
                    ON CONFLICT(node_name) DO UPDATE SET
                        endpoint_url = excluded.endpoint_url,
                        ssh_host = excluded.ssh_host,
                        is_reachable = excluded.is_reachable,
                        last_ping_latency_ms = excluded.last_ping_latency_ms,
                        last_error = excluded.last_error,
                        last_checked_at = excluded.last_checked_at;
                    """,
                    (
                        node_name,
                        data["endpoint_url"],
                        data["ssh_host"],
                        data["is_reachable"],
                        data["latency_ms"],
                        data["error"],
                    ),
                )
            conn.commit()

        return results

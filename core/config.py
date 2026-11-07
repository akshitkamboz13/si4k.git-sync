"""
Configuration and secrets loader for si4k.git-sync.
Zero plaintext secrets in source files: loads from environment or secrets file.
"""

import os
import socket
from dataclasses import dataclass
from pathlib import Path


@dataclass
class SyncConfig:
    db_path: Path
    github_token: str
    github_username: str
    gitlab_username: str
    laptop_gitlab_url: str
    server_gitlab_url: str
    worker_id: str
    lease_duration_sec: int
    poll_interval_sec: int
    health_interval_sec: int
    data_dir: Path

    @classmethod
    def load(cls) -> "SyncConfig":
        # Check for optional env file if not already loaded
        env_candidates = [
            os.environ.get("SI4K_SYNC_ENV_FILE", ""),
            "/etc/si4k-git-sync/secrets.env",
            str(Path.home() / ".config/si4k-git-sync/secrets.env"),
        ]
        for candidate in env_candidates:
            if candidate and os.path.isfile(candidate):
                with open(candidate, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith("#") and "=" in line:
                            k, v = line.split("=", 1)
                            k = k.strip()
                            v = v.strip().strip("\"'")
                            if k not in os.environ:
                                os.environ[k] = v
                break

        data_dir = Path(os.environ.get("SI4K_SYNC_DATA_DIR", Path.home() / ".local/share/si4k-git-sync"))
        data_dir.mkdir(parents=True, exist_ok=True)
        
        db_path = Path(os.environ.get("SI4K_SYNC_DB_PATH", data_dir / "sync-state.sqlite"))
        
        worker_id = os.environ.get("SI4K_SYNC_WORKER_ID", f"{socket.gethostname()}-{os.getpid()}")
        github_token = os.environ.get("GITHUB_TOKEN", os.environ.get("GH_TOKEN", ""))
        github_user = os.environ.get("GITHUB_USER", "akshitkamboz13")
        gitlab_user = os.environ.get("GITLAB_USER", "akshitkamboz13")

        laptop_url = os.environ.get("LAPTOP_GITLAB_URL", "http://localhost:8929")
        server_url = os.environ.get("SERVER_GITLAB_URL", "http://127.0.0.1:8929")

        lease_duration = int(os.environ.get("SI4K_SYNC_LEASE_SEC", "60"))
        poll_interval = int(os.environ.get("SI4K_SYNC_POLL_SEC", "5"))
        health_interval = int(os.environ.get("SI4K_SYNC_HEALTH_SEC", "60"))

        return cls(
            db_path=db_path,
            github_token=github_token,
            github_username=github_user,
            gitlab_username=gitlab_user,
            laptop_gitlab_url=laptop_url,
            server_gitlab_url=server_url,
            worker_id=worker_id,
            lease_duration_sec=lease_duration,
            poll_interval_sec=poll_interval,
            health_interval_sec=health_interval,
            data_dir=data_dir,
        )

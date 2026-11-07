"""
Git Replicator Engine.
Performs single-target idempotent branch replication using local bare repository caches.
"""

import os
import shutil
import subprocess
from pathlib import Path
from typing import Optional

from core.config import SyncConfig
from core.models import SyncJob
from core.logger import get_logger

logger = get_logger("si4k.replicator")


class ReplicationError(Exception):
    pass


class GitReplicator:
    def __init__(self, config: SyncConfig):
        self.config = config
        self.repos_dir = self.config.data_dir / "cache_repos"
        self.repos_dir.mkdir(parents=True, exist_ok=True)

    def _resolve_remote_url(self, node_name: str, project_name: str) -> str:
        gitlab_user = self.config.gitlab_username
        gh_user = self.config.github_username

        if node_name == "server_gitlab":
            return f"git@gitlab-server:{gitlab_user}/{project_name}.git"
        elif node_name == "laptop_gitlab":
            return f"git@gitlab-laptop:{gitlab_user}/{project_name}.git"
        elif node_name == "github":
            token = self.config.github_token
            if token:
                # Masked for security in logs
                return f"https://x-access-token:{token}@github.com/{gh_user}/{project_name}.git"
            return f"git@github.com:{gh_user}/{project_name}.git"
        else:
            raise ReplicationError(f"Unknown node type: {node_name}")

    def _mask_url(self, url: str) -> str:
        if "@github.com" in url and "x-access-token:" in url:
            prefix = url.split("x-access-token:")[0]
            rest = url.split("@github.com")[1]
            return f"{prefix}x-access-token:***@github.com{rest}"
        return url

    def _run_git(self, cmd: list[str], cwd: Optional[Path] = None, env: Optional[dict] = None) -> str:
        full_env = os.environ.copy()
        if env:
            full_env.update(env)
        # Prevent git from prompting for password in terminal
        full_env["GIT_TERMINAL_PROMPT"] = "0"
        full_env["GIT_SSH_COMMAND"] = "ssh -o BatchMode=yes -o StrictHostKeyChecking=no"

        printable_cmd = [self._mask_url(arg) for arg in cmd]
        logger.debug(f"Executing: {' '.join(printable_cmd)}")

        res = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, env=full_env)
        if res.returncode != 0:
            err = res.stderr.strip() or res.stdout.strip()
            masked_err = self._mask_url(err)
            raise ReplicationError(f"Git command failed [{res.returncode}]: {masked_err}")
        return res.stdout.strip()

    def replicate(self, job: SyncJob) -> None:
        """
        Executes an idempotent push for a single job from source_node to target_node.
        """
        project = job.project_name
        branch = job.branch_name
        src_url = self._resolve_remote_url(job.source_node, project)
        target_url = self._resolve_remote_url(job.target_node, project)

        repo_cache = self.repos_dir / f"{project}.git"

        logger.info(
            f"Starting replication [{job.operation_id}]: {project} "
            f"({job.source_node} -> {job.target_node}) on branch '{branch}' @ {job.commit_sha[:8]}"
        )

        try:
            # 1. Initialize or update local bare mirror cache
            if not repo_cache.exists():
                logger.info(f"Cloning bare mirror for {project} from {job.source_node}...")
                self._run_git(["git", "clone", "--bare", src_url, str(repo_cache)])
            else:
                logger.info(f"Fetching latest changes for {project} from {job.source_node}...")
                self._run_git(
                    ["git", "fetch", src_url, f"refs/heads/{branch}:refs/heads/{branch}"],
                    cwd=repo_cache,
                )

            # 2. Push to target
            target_ref = job.target_ref or f"refs/heads/{branch}"
            refspec = f"refs/heads/{branch}:{target_ref}"
            logger.info(f"Pushing {project} to {job.target_node} ({refspec})...")
            self._run_git(
                ["git", "push", target_url, refspec],
                cwd=repo_cache,
            )
            logger.info(f"Successfully replicated {project} to {job.target_node}!")

        except ReplicationError as e:
            logger.error(f"Replication failed for [{job.operation_id}]: {e}")
            raise
        except Exception as e:
            logger.error(f"Unexpected error during replication [{job.operation_id}]: {e}")
            raise ReplicationError(str(e))

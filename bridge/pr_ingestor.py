"""
GitHub Pull Request Ingestion Service.
Detects new external PRs, safely fetches contributor branches into isolated GitLab refs,
and creates corresponding GitLab Merge Requests with full attribution.
"""

import hashlib
import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.config import SyncConfig
from core.logger import get_logger
from database.db import get_connection
from bridge.github_client import GitHubClient, GitHubAPIError
from bridge.gitlab_client import GitLabClient, GitLabAPIError

logger = get_logger("si4k.pr_ingestor")


class PRIngestor:
    def __init__(
        self,
        config: SyncConfig,
        github_client: Optional[GitHubClient] = None,
        gitlab_client: Optional[GitLabClient] = None,
    ):
        self.config = config
        self.gh = github_client or GitHubClient(config.github_token)
        # Use server GitLab URL if on server, or laptop URL
        gitlab_url = config.server_gitlab_url or config.laptop_gitlab_url
        gitlab_pat = os.environ.get("GITLAB_TOKEN", os.environ.get("GITLAB_PAT", ""))
        self.gl = gitlab_client or GitLabClient(gitlab_url, gitlab_pat)

    def _run_git(self, cmd: list[str], cwd: Optional[Path] = None) -> str:
        env = os.environ.copy()
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["GIT_SSH_COMMAND"] = "ssh -o BatchMode=yes -o StrictHostKeyChecking=no"
        res = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, env=env)
        if res.returncode != 0:
            err = res.stderr.strip() or res.stdout.strip()
            raise RuntimeError(f"Git command failed: {err}")
        return res.stdout.strip()

    def ingest_repo_prs(self, project_name: str) -> List[Dict[str, Any]]:
        owner = self.config.github_username
        repo = project_name
        logger.info(f"Checking for open PRs on GitHub: {owner}/{repo}...")

        try:
            prs = self.gh.list_open_prs(owner, repo)
        except Exception as e:
            logger.error(f"Failed to fetch GitHub PRs for {owner}/{repo}: {e}")
            return []

        logger.info(f"Found {len(prs)} open PR(s) on GitHub {owner}/{repo}.")
        results = []

        for pr in prs:
            try:
                res = self.ingest_single_pr(project_name, pr)
                if res:
                    results.append(res)
            except Exception as e:
                logger.error(f"Error ingesting PR #{pr.get('number')} on {project_name}: {e}", exc_info=True)

        return results

    def ingest_single_pr(self, project_name: str, pr: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        pr_number = pr["number"]
        pr_title = pr["title"]
        pr_body = pr.get("body") or ""
        pr_html_url = pr["html_url"]
        contributor = pr["user"]["login"]
        base_branch = pr["base"]["ref"]
        base_sha = pr["base"]["sha"]
        head_sha = pr["head"]["sha"]
        github_repo = f"{self.config.github_username}/{project_name}"

        # Generate unique idempotency key
        key_raw = f"{github_repo}:{pr_number}:{head_sha}".encode("utf-8")
        idempotency_key = hashlib.sha256(key_raw).hexdigest()

        # Check existing mapping in database
        with get_connection(self.config.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM pr_mappings WHERE project_name = ? AND github_pr_number = ?",
                (project_name, pr_number),
            )
            existing = cursor.fetchone()

        if existing and existing["head_sha"] == head_sha:
            logger.debug(f"PR #{pr_number} for {project_name} already ingested @ {head_sha[:8]}. Skipping.")
            return None

        logger.info(f"Ingesting PR #{pr_number} by @{contributor} for {project_name} (HEAD: {head_sha[:8]})...")

        # 1. Fetch contributor's branch in an isolated temp clone
        isolated_branch = f"github-pr/{pr_number}-{contributor}"
        with tempfile.TemporaryDirectory(prefix=f"pr-{project_name}-{pr_number}-") as temp_dir:
            work_dir = Path(temp_dir)
            self._run_git(["git", "init", "-b", base_branch], cwd=work_dir)

            # Add GitHub and GitLab remotes
            gh_clone_url = f"https://github.com/{github_repo}.git"
            gl_remote_url = f"git@gitlab-server:{self.config.gitlab_username}/{project_name}.git"

            # Fetch the exact PR revision from GitHub refs/pull/<id>/head
            logger.info(f"Fetching refs/pull/{pr_number}/head from GitHub into {isolated_branch}...")
            self._run_git(
                ["git", "fetch", gh_clone_url, f"refs/pull/{pr_number}/head:{isolated_branch}"],
                cwd=work_dir,
            )

            # Push the isolated branch to GitLab
            logger.info(f"Pushing {isolated_branch} to GitLab ({gl_remote_url})...")
            self._run_git(
                ["git", "push", gl_remote_url, f"refs/heads/{isolated_branch}:refs/heads/{isolated_branch}"],
                cwd=work_dir,
            )

        # 2. Create or Update GitLab Merge Request
        mr_iid = None
        gitlab_proj_id = None

        if self.gl.token:
            try:
                gl_proj = self.gl.get_project(f"{self.config.gitlab_username}/{project_name}")
                if gl_proj:
                    gitlab_proj_id = gl_proj["id"]
                    if not existing or not existing["gitlab_mr_iid"]:
                        mr_desc = (
                            f"> **Mirrored from GitHub PR**: [#{pr_number}]({pr_html_url})\n"
                            f"> **Contributor**: `@{contributor}`\n"
                            f"> **Head SHA**: `{head_sha}`\n"
                            f"> **Base Branch**: `{base_branch}`\n\n"
                            f"---\n\n"
                            f"{pr_body}\n\n"
                            f"---\n"
                            f"*Review and test changes here in GitLab. When approved, run "
                            f"`si4k-sync merge --project {project_name} --pr {pr_number}` to execute the safe hybrid merge.*"
                        )
                        mr = self.gl.create_merge_request(
                            project_id=gitlab_proj_id,
                            source_branch=isolated_branch,
                            target_branch=base_branch,
                            title=f"[GitHub PR #{pr_number}] {pr_title}",
                            description=mr_desc,
                            labels=["github-pr", "external-contribution"],
                        )
                        mr_iid = mr.get("iid")
                        logger.info(f"Created GitLab MR !{mr_iid} for PR #{pr_number}.")
                    else:
                        mr_iid = existing["gitlab_mr_iid"]
                        self.gl.comment_on_merge_request(
                            project_id=gitlab_proj_id,
                            mr_iid=mr_iid,
                            note=f"🔄 Contributor @{contributor} updated the PR head to `{head_sha}`.",
                        )
            except Exception as e:
                logger.warning(f"Could not interact with GitLab API: {e}")

        # 3. Record state in pr_mappings
        with get_connection(self.config.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO pr_mappings (
                    project_name, github_repo, github_pr_number,
                    gitlab_project_id, gitlab_mr_iid, base_branch,
                    base_sha, head_sha, contributor_username,
                    status, idempotency_key, last_synced_at,
                    created_at, updated_at
                ) VALUES (
                    ?, ?, ?,
                    ?, ?, ?,
                    ?, ?, ?,
                    'mr_created', ?, datetime('now'),
                    datetime('now'), datetime('now')
                )
                ON CONFLICT(project_name, github_pr_number) DO UPDATE SET
                    head_sha = excluded.head_sha,
                    base_sha = excluded.base_sha,
                    gitlab_project_id = COALESCE(excluded.gitlab_project_id, pr_mappings.gitlab_project_id),
                    gitlab_mr_iid = COALESCE(excluded.gitlab_mr_iid, pr_mappings.gitlab_mr_iid),
                    status = 'mr_created',
                    idempotency_key = excluded.idempotency_key,
                    last_synced_at = datetime('now'),
                    updated_at = datetime('now');
                """,
                (
                    project_name,
                    github_repo,
                    pr_number,
                    gitlab_proj_id,
                    mr_iid,
                    base_branch,
                    base_sha,
                    head_sha,
                    contributor,
                    idempotency_key,
                ),
            )
            conn.commit()

        # 4. Post helpful link back on GitHub PR thread (if token is available)
        if self.config.github_token and mr_iid:
            try:
                comment = (
                    f"👋 Thanks for your contribution @{contributor}!\n\n"
                    f"Your pull request has been mirrored to our internal GitLab pipeline for review & CI validation: **!{mr_iid}**."
                )
                self.gh.post_pr_comment(self.config.github_username, project_name, pr_number, comment)
            except Exception as e:
                logger.debug(f"Could not comment on GitHub PR: {e}")

        logger.info(f"PR #{pr_number} ingestion complete! Status: mr_created (GitLab MR !{mr_iid})")
        return {
            "project": project_name,
            "pr_number": pr_number,
            "contributor": contributor,
            "head_sha": head_sha,
            "mr_iid": mr_iid,
        }

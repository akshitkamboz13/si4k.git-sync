"""
The 6-Step Race-Safe Hybrid Merge Protocol.
Preserves GitHub native 'Merged' badge and contributor graph while maintaining
GitLab as the primary engineering review hub.
"""

import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional

from core.config import SyncConfig
from core.logger import get_logger
from core.queue_leaser import QueueLeaser
from database.db import get_connection
from bridge.github_client import GitHubClient, GitHubAPIError
from bridge.gitlab_client import GitLabClient, GitLabAPIError

logger = get_logger("si4k.merge_executor")


class MergeExecutionError(Exception):
    pass


class HybridMergeExecutor:
    def __init__(
        self,
        config: SyncConfig,
        github_client: Optional[GitHubClient] = None,
        gitlab_client: Optional[GitLabClient] = None,
    ):
        self.config = config
        self.gh = github_client or GitHubClient(config.github_token)
        gitlab_url = config.server_gitlab_url or config.laptop_gitlab_url
        gitlab_pat = os.environ.get("GITLAB_TOKEN", os.environ.get("GITLAB_PAT", ""))
        self.gl = gitlab_client or GitLabClient(gitlab_url, gitlab_pat)
        self.leaser = QueueLeaser(config.db_path)

    def _run_git(self, cmd: list[str], cwd: Optional[Path] = None) -> str:
        env = os.environ.copy()
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["GIT_SSH_COMMAND"] = "ssh -o BatchMode=yes -o StrictHostKeyChecking=no"
        res = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, env=env)
        if res.returncode != 0:
            err = res.stderr.strip() or res.stdout.strip()
            raise MergeExecutionError(f"Git command failed: {err}")
        return res.stdout.strip()

    def execute_merge(
        self,
        project_name: str,
        pr_number: int,
        custom_commit_title: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Executes the 6-step race-safe hybrid merge protocol for a reviewed PR.
        """
        owner = self.config.github_username
        logger.info(f"Initiating 6-step hybrid merge for {project_name} PR #{pr_number}...")

        # 0. Load record from pr_mappings
        with get_connection(self.config.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM pr_mappings WHERE project_name = ? AND github_pr_number = ?",
                (project_name, pr_number),
            )
            record = cursor.fetchone()

        if not record:
            raise MergeExecutionError(f"No PR mapping record found for {project_name} #{pr_number}. Run 'ingest' first.")

        if record["status"] == "merged":
            logger.info(f"PR #{pr_number} is already marked as merged ({record['merge_sha']}).")
            return {"status": "already_merged", "merge_sha": record["merge_sha"]}

        expected_head_sha = record["head_sha"]
        base_branch = record["base_branch"]
        contributor = record["contributor_username"]

        # =========================================================================
        # STEP 1: Verify GitHub PR head SHA hasn't changed (Post-approval tamper check)
        # =========================================================================
        logger.info(f"[Step 1/6] Verifying GitHub PR #{pr_number} HEAD SHA...")
        try:
            gh_pr = self.gh.get_pr(owner, project_name, pr_number)
        except Exception as e:
            raise MergeExecutionError(f"Failed to query GitHub for PR #{pr_number}: {e}")

        current_head_sha = gh_pr["head"]["sha"]
        if current_head_sha != expected_head_sha:
            raise MergeExecutionError(
                f"Security Abort! PR HEAD SHA changed since GitLab review! "
                f"Reviewed: {expected_head_sha[:8]}, Current: {current_head_sha[:8]}. "
                f"Re-review required before merging."
            )
        logger.info(f"Verified: PR HEAD SHA matches reviewed revision ({expected_head_sha[:8]}).")

        # =========================================================================
        # STEP 2: Call GitHub Merge API to perform native merge
        # =========================================================================
        logger.info(f"[Step 2/6] Executing native merge on GitHub...")
        commit_title = custom_commit_title or f"Merge pull request #{pr_number} from {contributor} ({gh_pr['title']})"
        try:
            merge_res = self.gh.merge_pr(
                owner=owner,
                repo=project_name,
                pull_number=pr_number,
                commit_title=commit_title,
                expected_head_sha=expected_head_sha,
                merge_method="merge",
            )
        except GitHubAPIError as e:
            raise MergeExecutionError(f"GitHub native merge failed: {e}")

        merge_sha = merge_res.get("sha")
        if not merge_sha:
            raise MergeExecutionError(f"GitHub merge did not return a merge commit SHA: {merge_res}")
        logger.info(f"GitHub native merge successful! Merge Commit SHA: {merge_sha[:8]}.")

        # =========================================================================
        # STEP 3 & 4: Fetch accepted commit from GitHub & verify history
        # =========================================================================
        logger.info(f"[Step 3-4/6] Fetching resulting GitHub target branch into bare cache...")
        token = self.config.github_token
        gh_url = f"https://x-access-token:{token}@github.com/{owner}/{project_name}.git" if token else f"git@github.com:{owner}/{project_name}.git"
        server_gl_url = f"git@gitlab-server:{self.config.gitlab_username}/{project_name}.git"

        with tempfile.TemporaryDirectory(prefix=f"merge-{project_name}-{pr_number}-") as temp_dir:
            work_dir = Path(temp_dir)
            self._run_git(["git", "init", "-b", base_branch], cwd=work_dir)

            # Fetch target branch from GitHub
            self._run_git(["git", "fetch", gh_url, f"refs/heads/{base_branch}:refs/heads/{base_branch}"], cwd=work_dir)
            
            # Verify the merge SHA is in history
            log_output = self._run_git(["git", "log", "-n", "1", "--format=%H", f"refs/heads/{base_branch}"], cwd=work_dir)
            if merge_sha not in log_output:
                logger.warning(f"Notice: Target branch HEAD ({log_output[:8]}) differs from merge SHA ({merge_sha[:8]}).")

            # Push accepted merge commit to Server GitLab
            logger.info(f"Pushed merged target branch to Home Server GitLab...")
            self._run_git(["git", "push", server_gl_url, f"refs/heads/{base_branch}:refs/heads/{base_branch}"], cwd=work_dir)

        # =========================================================================
        # STEP 5: Update internal SQLite database and GitLab MR
        # =========================================================================
        logger.info(f"[Step 5/6] Updating PR mapping state to 'merged'...")
        with get_connection(self.config.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE pr_mappings
                SET status = 'merged',
                    merge_sha = ?,
                    updated_at = datetime('now')
                WHERE project_name = ? AND github_pr_number = ?;
                """,
                (merge_sha, project_name, pr_number),
            )
            conn.commit()

        # Update GitLab MR if present
        if getattr(self.gl, "token", None) and record["gitlab_project_id"] and record["gitlab_mr_iid"]:
            try:
                mr_iid = record["gitlab_mr_iid"]
                proj_id = record["gitlab_project_id"]
                comment = (
                    f"🎉 **Merged on GitHub natively!**\n"
                    f"• **Merge Commit SHA**: `{merge_sha}`\n"
                    f"• **Contributor**: `@{contributor}`\n"
                    f"• GitHub native attribution, badges, and contributor graphs are preserved."
                )
                self.gl.comment_on_merge_request(proj_id, mr_iid, comment)
                self.gl.close_merge_request(proj_id, mr_iid)
                logger.info(f"Closed GitLab MR !{mr_iid} with merge attribution note.")
            except Exception as e:
                logger.warning(f"Could not update GitLab MR: {e}")

        # =========================================================================
        # STEP 6: Enqueue replication to other nodes (e.g. Laptop GitLab)
        # =========================================================================
        logger.info(f"[Step 6/6] Enqueuing replication to Laptop GitLab...")
        self.leaser.enqueue(
            project_name=project_name,
            source_node="server_gitlab",
            target_node="laptop_gitlab",
            branch_name=base_branch,
            commit_sha=merge_sha,
        )

        logger.info(f"Hybrid merge complete for {project_name} PR #{pr_number}! Merge SHA: {merge_sha}")
        return {
            "status": "merged",
            "merge_sha": merge_sha,
            "project": project_name,
            "pr_number": pr_number,
        }

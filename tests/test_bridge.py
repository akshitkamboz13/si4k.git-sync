"""
Unit tests for GitHub Contribution Bridge, PR ingestion, and the 6-step Hybrid Merge Protocol.
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from core.config import SyncConfig
from database.db import init_db, get_connection
from bridge.github_client import GitHubClient, GitHubAPIError
from bridge.gitlab_client import GitLabClient
from bridge.merge_executor import HybridMergeExecutor, MergeExecutionError


class TestGitHubContributionBridge(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test-bridge.sqlite"
        init_db(self.db_path)

        self.config = SyncConfig(
            db_path=self.db_path,
            github_token="mock_gh_token",
            github_username="akshitkamboz13",
            gitlab_username="akshitkamboz13",
            laptop_gitlab_url="http://localhost:8929",
            server_gitlab_url="http://127.0.0.1:8929",
            worker_id="test-worker",
            lease_duration_sec=60,
            poll_interval_sec=5,
            health_interval_sec=60,
            data_dir=Path(self.temp_dir.name),
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_merge_executor_aborts_on_head_tamper(self):
        """Step 1 Security Test: Ensure merge aborts if HEAD SHA changed post-approval."""
        # 1. Insert a mock PR mapping in SQLite
        with get_connection(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO pr_mappings (
                    project_name, github_repo, github_pr_number,
                    base_branch, head_sha, contributor_username,
                    status, idempotency_key
                ) VALUES (?, ?, ?, ?, ?, ?, 'mr_created', ?);
                """,
                (
                    "PC-Papa",
                    "akshitkamboz13/PC-Papa",
                    42,
                    "main",
                    "safe_reviewed_commit_sha_12345678",
                    "untrusted_contributor",
                    "unique_key_1",
                ),
            )
            conn.commit()

        # 2. Mock GitHub API returning a DIFFERENT head_sha (tampered / pushed after approval)
        mock_gh = MagicMock(spec=GitHubClient)
        mock_gh.get_pr.return_value = {
            "number": 42,
            "title": "Malicious / Modified Change",
            "head": {"sha": "new_tampered_commit_sha_87654321"},
            "base": {"ref": "main"},
        }

        mock_gl = MagicMock(spec=GitLabClient)
        executor = HybridMergeExecutor(self.config, github_client=mock_gh, gitlab_client=mock_gl)

        # 3. Assert that execute_merge detects the mismatch and aborts immediately
        with self.assertRaises(MergeExecutionError) as ctx:
            executor.execute_merge("PC-Papa", 42)

        self.assertIn("Security Abort", str(ctx.exception))
        self.assertIn("PR HEAD SHA changed since GitLab review", str(ctx.exception))

    @patch("bridge.merge_executor.HybridMergeExecutor._run_git")
    def test_merge_executor_success(self, mock_run_git):
        """End-to-end simulation of successful 6-step hybrid merge."""
        reviewed_sha = "safe_commit_sha_11223344"
        merge_commit_sha = "accepted_merge_commit_sha_99887766"

        with get_connection(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO pr_mappings (
                    project_name, github_repo, github_pr_number,
                    gitlab_project_id, gitlab_mr_iid,
                    base_branch, head_sha, contributor_username,
                    status, idempotency_key
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'mr_created', ?);
                """,
                (
                    "PC-Papa",
                    "akshitkamboz13/PC-Papa",
                    10,
                    1,
                    5,
                    "main",
                    reviewed_sha,
                    "awesome_contributor",
                    "unique_key_2",
                ),
            )
            conn.commit()

        mock_gh = MagicMock(spec=GitHubClient)
        mock_gh.get_pr.return_value = {
            "number": 10,
            "title": "Awesome Feature",
            "head": {"sha": reviewed_sha},
            "base": {"ref": "main"},
        }
        mock_gh.merge_pr.return_value = {
            "sha": merge_commit_sha,
            "merged": True,
            "message": "Pull Request successfully merged",
        }

        mock_gl = MagicMock(spec=GitLabClient)
        mock_gl.token = "mock_gl_token"
        mock_run_git.return_value = merge_commit_sha

        executor = HybridMergeExecutor(self.config, github_client=mock_gh, gitlab_client=mock_gl)
        result = executor.execute_merge("PC-Papa", 10)

        self.assertEqual(result["status"], "merged")
        self.assertEqual(result["merge_sha"], merge_commit_sha)

        # Verify DB updated to 'merged'
        with get_connection(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM pr_mappings WHERE project_name = 'PC-Papa' AND github_pr_number = 10;")
            row = cursor.fetchone()
            self.assertEqual(row["status"], "merged")
            self.assertEqual(row["merge_sha"], merge_commit_sha)

            # Verify replication to laptop was enqueued in sync_queue (Step 6)
            cursor.execute("SELECT * FROM sync_queue WHERE project_name = 'PC-Papa' AND target_node = 'laptop_gitlab';")
            q_row = cursor.fetchone()
            self.assertIsNotNone(q_row)
            self.assertEqual(q_row["commit_sha"], merge_commit_sha)


if __name__ == "__main__":
    unittest.main()

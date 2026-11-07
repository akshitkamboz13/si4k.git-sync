"""
Unit tests for Two-Way Issue and Comment Synchronization.
"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from core.config import SyncConfig
from database.db import init_db, get_connection
from bridge.github_client import GitHubClient
from bridge.gitlab_client import GitLabClient
from bridge.issue_bridge import TwoWayIssueBridge


class TestTwoWayIssueBridge(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test-issues.sqlite"
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

    def test_inbound_github_issue_creates_gitlab_issue(self):
        """Verify that open GitHub issues are automatically mirrored to GitLab."""
        mock_gh = MagicMock(spec=GitHubClient)
        mock_gh.list_issues.return_value = [
            {
                "number": 101,
                "title": "Bug: Audio fails to stream",
                "body": "Detailed repro steps...",
                "state": "open",
                "user": {"login": "reporter_bob"},
                "html_url": "https://github.com/akshitkamboz13/PC-Papa/issues/101",
                "updated_at": "2026-11-07T12:00:00Z",
            }
        ]
        mock_gh.list_issue_comments.return_value = []

        mock_gl = MagicMock(spec=GitLabClient)
        mock_gl.token = "gl_pat_123"
        mock_gl.get_project.return_value = {"id": 45}
        mock_gl.create_issue.return_value = {"id": 999, "iid": 7}
        mock_gl.list_issues.return_value = []

        bridge = TwoWayIssueBridge(self.config, github_client=mock_gh, gitlab_client=mock_gl)
        stats = bridge.sync_project_issues("PC-Papa")

        self.assertEqual(stats["github_to_gitlab_created"], 1)

        # Check SQLite mapping
        with get_connection(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM issue_mappings WHERE github_issue_number = 101;")
            row = cursor.fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row["gitlab_issue_iid"], 7)
            self.assertEqual(row["state"], "open")
            self.assertEqual(row["author_username"], "reporter_bob")

    def test_outbound_gitlab_closure_closes_github_issue(self):
        """Verify that closing an issue in GitLab propagates to GitHub."""
        # Insert open issue mapping in DB
        with get_connection(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO issue_mappings (
                    project_name, github_repo, github_issue_number,
                    gitlab_project_id, gitlab_issue_iid, title,
                    state, author_username
                ) VALUES (?, ?, ?, ?, ?, ?, 'open', ?);
                """,
                ("PC-Papa", "akshitkamboz13/PC-Papa", 202, 45, 12, "Fix network routing", "reporter_alice"),
            )
            conn.commit()

        mock_gh = MagicMock(spec=GitHubClient)
        mock_gh.list_issues.return_value = []
        mock_gh.list_issue_comments.return_value = []

        mock_gl = MagicMock(spec=GitLabClient)
        mock_gl.token = "gl_pat_123"
        mock_gl.get_project.return_value = {"id": 45}
        # GitLab issue is marked closed
        mock_gl.get_issue.return_value = {"id": 500, "iid": 12, "state": "closed"}
        mock_gl.list_issue_notes.return_value = []

        bridge = TwoWayIssueBridge(self.config, github_client=mock_gh, gitlab_client=mock_gl)
        stats = bridge.sync_project_issues("PC-Papa")

        self.assertEqual(stats["gitlab_to_github_closed"], 1)
        mock_gh.update_issue.assert_called_once_with("akshitkamboz13", "PC-Papa", 202, state="closed")

        # Verify DB updated to 'closed'
        with get_connection(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT state FROM issue_mappings WHERE github_issue_number = 202;")
            row = cursor.fetchone()
            self.assertEqual(row["state"], "closed")


if __name__ == "__main__":
    unittest.main()

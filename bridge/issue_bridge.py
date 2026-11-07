"""
Two-Way Issue Synchronization Engine.
Mirrors external GitHub issues into internal GitLab tracking boards,
propagates resolutions/closures bi-directionally, and syncs comment threads.
"""

import json
import os
from typing import Any, Dict, List, Optional

from core.config import SyncConfig
from core.logger import get_logger
from database.db import get_connection
from bridge.github_client import GitHubClient, GitHubAPIError
from bridge.gitlab_client import GitLabClient, GitLabAPIError

logger = get_logger("si4k.issue_bridge")


class TwoWayIssueBridge:
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

    def sync_project_issues(self, project_name: str) -> Dict[str, Any]:
        """
        Executes a full two-way issue and comment synchronization for a project.
        """
        owner = self.config.github_username
        logger.info(f"Starting Two-Way Issue Sync for {project_name} ({owner}/{project_name})...")

        stats = {
            "github_to_gitlab_created": 0,
            "github_to_gitlab_closed": 0,
            "gitlab_to_github_closed": 0,
            "comments_synced": 0,
        }

        # Resolve GitLab project ID
        gitlab_proj_id = None
        if self.gl.token:
            try:
                proj = self.gl.get_project(f"{self.config.gitlab_username}/{project_name}")
                if proj:
                    gitlab_proj_id = proj["id"]
            except Exception as e:
                logger.warning(f"Could not resolve GitLab project ID for {project_name}: {e}")

        # =========================================================================
        # 1. Inbound Sync: GitHub -> GitLab
        # =========================================================================
        try:
            gh_issues = self.gh.list_issues(owner, project_name, state="all")
        except Exception as e:
            logger.error(f"Failed to fetch GitHub issues for {project_name}: {e}")
            gh_issues = []

        logger.info(f"Found {len(gh_issues)} issue(s) on GitHub {project_name}.")

        for gh_issue in gh_issues:
            gh_num = gh_issue["number"]
            gh_title = gh_issue["title"]
            gh_body = gh_issue.get("body") or ""
            gh_state = gh_issue["state"]  # "open" or "closed"
            author = gh_issue["user"]["login"]
            html_url = gh_issue["html_url"]
            updated_at = gh_issue["updated_at"]

            with get_connection(self.config.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT * FROM issue_mappings WHERE project_name = ? AND github_issue_number = ?",
                    (project_name, gh_num),
                )
                mapping = cursor.fetchone()

            # Case A: Brand new GitHub Issue
            if not mapping:
                gl_iid = None
                if self.gl.token and gitlab_proj_id:
                    desc = (
                        f"> **Synced from GitHub Issue**: [#{gh_num}]({html_url})\n"
                        f"> **Author**: `@{author}`\n"
                        f"> **Initial State**: `{gh_state}`\n\n"
                        f"---\n\n"
                        f"{gh_body}\n\n"
                        f"---\n"
                        f"*Synced via si4k.git-sync Two-Way Issue Bridge.*"
                    )
                    try:
                        gl_iss = self.gl.create_issue(
                            project_id=gitlab_proj_id,
                            title=f"[GitHub #{gh_num}] {gh_title}",
                            description=desc,
                            labels=["github-issue", "external"],
                        )
                        gl_iid = gl_iss.get("iid")
                        stats["github_to_gitlab_created"] += 1
                        logger.info(f"Created GitLab Issue #{gl_iid} from GitHub #{gh_num}.")

                        # Acknowledge on GitHub
                        ack = f"👋 Thanks for reporting! This issue has been synced to our internal GitLab tracking board: Issue #{gl_iid}."
                        self.gh.post_issue_comment(owner, project_name, gh_num, ack)
                    except Exception as e:
                        logger.warning(f"Could not create GitLab issue for GH #{gh_num}: {e}")

                with get_connection(self.config.db_path) as conn:
                    cursor = conn.cursor()
                    cursor.execute(
                        """
                        INSERT INTO issue_mappings (
                            project_name, github_repo, github_issue_number,
                            gitlab_project_id, gitlab_issue_iid, title,
                            state, author_username, last_synced_github_updated_at,
                            created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'));
                        """,
                        (
                            project_name,
                            f"{owner}/{project_name}",
                            gh_num,
                            gitlab_proj_id,
                            gl_iid,
                            gh_title,
                            gh_state,
                            author,
                            updated_at,
                        ),
                    )
                    conn.commit()

            # Case B: Existing Issue with state change from GitHub
            elif mapping:
                prev_state = mapping["state"]
                gl_iid = mapping["gitlab_issue_iid"]

                if gh_state == "closed" and prev_state == "open":
                    logger.info(f"GitHub #{gh_num} was closed by contributor. Closing internal GitLab #{gl_iid}...")
                    if self.gl.token and gitlab_proj_id and gl_iid:
                        try:
                            self.gl.comment_on_issue(gitlab_proj_id, gl_iid, "🔒 Closed on GitHub by reporter.")
                            self.gl.update_issue(gitlab_proj_id, gl_iid, state_event="close")
                            stats["github_to_gitlab_closed"] += 1
                        except Exception as e:
                            logger.warning(f"Could not close GitLab issue #{gl_iid}: {e}")

                    with get_connection(self.config.db_path) as conn:
                        cursor = conn.cursor()
                        cursor.execute(
                            "UPDATE issue_mappings SET state = 'closed', updated_at = datetime('now') WHERE id = ?",
                            (mapping["id"],),
                        )
                        conn.commit()

            # Sync comments from GitHub -> GitLab
            self._sync_github_comments_to_gitlab(project_name, gh_num, gitlab_proj_id, stats)

        # =========================================================================
        # 2. Outbound Sync: GitLab -> GitHub (Resolutions & Engineering comments)
        # =========================================================================
        if self.gl.token and gitlab_proj_id:
            with get_connection(self.config.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT * FROM issue_mappings WHERE project_name = ? AND gitlab_issue_iid IS NOT NULL",
                    (project_name,),
                )
                tracked_issues = cursor.fetchall()

            for mapping in tracked_issues:
                gl_iid = mapping["gitlab_issue_iid"]
                gh_num = mapping["github_issue_number"]
                db_state = mapping["state"]

                try:
                    gl_iss = self.gl.get_issue(gitlab_proj_id, gl_iid)
                    gl_state = gl_iss.get("state")  # "opened" or "closed"

                    # If closed on GitLab but open in DB/GitHub: close on GitHub!
                    if gl_state == "closed" and db_state == "open":
                        logger.info(f"GitLab Issue #{gl_iid} was closed internally. Resolving GitHub #{gh_num}...")
                        try:
                            self.gh.post_issue_comment(
                                owner,
                                project_name,
                                gh_num,
                                "✅ **Resolved and closed in internal engineering pipeline.**\nThank you for contributing!",
                            )
                            self.gh.update_issue(owner, project_name, gh_num, state="closed")
                            stats["gitlab_to_github_closed"] += 1
                        except Exception as e:
                            logger.warning(f"Could not close GitHub issue #{gh_num}: {e}")

                        with get_connection(self.config.db_path) as conn:
                            cursor = conn.cursor()
                            cursor.execute(
                                "UPDATE issue_mappings SET state = 'closed', updated_at = datetime('now') WHERE id = ?",
                                (mapping["id"],),
                            )
                            conn.commit()

                    # Sync internal notes from GitLab -> GitHub
                    self._sync_gitlab_notes_to_github(project_name, mapping, gitlab_proj_id, stats)

                except Exception as e:
                    logger.debug(f"Error checking GitLab issue #{gl_iid}: {e}")

        logger.info(f"Two-Way Issue Sync complete for {project_name}: {stats}")
        return stats

    def _sync_github_comments_to_gitlab(self, project_name: str, gh_num: int, gl_proj_id: Optional[int], stats: dict):
        if not gl_proj_id or not self.gl.token:
            return

        owner = self.config.github_username
        with get_connection(self.config.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT id, gitlab_issue_iid FROM issue_mappings WHERE project_name = ? AND github_issue_number = ?", (project_name, gh_num))
            mapping = cursor.fetchone()

        if not mapping or not mapping["gitlab_issue_iid"]:
            return

        mapping_id = mapping["id"]
        gl_iid = mapping["gitlab_issue_iid"]

        try:
            comments = self.gh.list_issue_comments(owner, project_name, gh_num)
        except Exception:
            return

        for c in comments:
            cid = f"gh-{c['id']}"
            author = c["user"]["login"]
            body = c["body"]

            # Skip bot comments or our own synced comments
            if "Synced from GitHub" in body or "internal GitLab" in body or "Engineering Team" in body:
                continue

            with get_connection(self.config.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT id FROM issue_comments_synced WHERE source_comment_id = ?", (cid,))
                if cursor.fetchone():
                    continue

                # Mirror comment to GitLab
                note = f"💬 **@{author}** (on GitHub):\n\n{body}"
                try:
                    self.gl.comment_on_issue(gl_proj_id, gl_iid, note)
                    cursor.execute(
                        "INSERT INTO issue_comments_synced (issue_mapping_id, source_platform, source_comment_id) VALUES (?, 'github', ?)",
                        (mapping_id, cid),
                    )
                    conn.commit()
                    stats["comments_synced"] += 1
                except Exception as e:
                    logger.warning(f"Could not mirror GitHub comment #{c['id']} to GitLab: {e}")

    def _sync_gitlab_notes_to_github(self, project_name: str, mapping: Any, gl_proj_id: int, stats: dict):
        owner = self.config.github_username
        mapping_id = mapping["id"]
        gl_iid = mapping["gitlab_issue_iid"]
        gh_num = mapping["github_issue_number"]

        try:
            notes = self.gl.list_issue_notes(gl_proj_id, gl_iid)
        except Exception:
            return

        for n in notes:
            nid = f"gl-{n['id']}"
            body = n["body"]
            is_system = n.get("system", False)

            # Skip system notes or synced notes
            if is_system or "Synced via" in body or "on GitHub" in body or "Resolved and closed" in body:
                continue

            with get_connection(self.config.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT id FROM issue_comments_synced WHERE source_comment_id = ?", (nid,))
                if cursor.fetchone():
                    continue

                # Mirror comment to GitHub
                author_name = n.get("author", {}).get("username", "developer")
                gh_comment = f"💬 **@{author_name}** (Engineering Hub):\n\n{body}"
                try:
                    self.gh.post_issue_comment(owner, project_name, gh_num, gh_comment)
                    cursor.execute(
                        "INSERT INTO issue_comments_synced (issue_mapping_id, source_platform, source_comment_id) VALUES (?, 'gitlab', ?)",
                        (mapping_id, nid),
                    )
                    conn.commit()
                    stats["comments_synced"] += 1
                except Exception as e:
                    logger.warning(f"Could not mirror GitLab note #{n['id']} to GitHub: {e}")

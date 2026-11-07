"""
GitHub REST API v3 Client for Pull Request Ingestion and Native Merges.
Pure standard library implementation (urllib.request).
"""

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

from core.logger import get_logger

logger = get_logger("si4k.github_client")


class GitHubAPIError(Exception):
    def __init__(self, message: str, status_code: Optional[int] = None, response_body: Optional[str] = None):
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body


class GitHubClient:
    BASE_URL = "https://api.github.com"

    def __init__(self, token: str, user_agent: str = "si4k-git-sync/1.0"):
        self.token = token
        self.user_agent = user_agent

    def _request(self, endpoint: str, method: str = "GET", data: Optional[Dict[str, Any]] = None) -> Any:
        url = f"{self.BASE_URL}{endpoint}"
        headers = {
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": self.user_agent,
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        encoded_data = None
        if data is not None:
            encoded_data = json.dumps(data).encode("utf-8")
            headers["Content-Type"] = "application/json"

        req = urllib.request.Request(url, data=encoded_data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=15.0) as resp:
                resp_text = resp.read().decode("utf-8")
                return json.loads(resp_text) if resp_text else {}
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            logger.error(f"GitHub API Error {e.code} on {method} {endpoint}: {body}")
            raise GitHubAPIError(f"GitHub API HTTP {e.code}: {e.reason}", status_code=e.code, response_body=body)
        except urllib.error.URLError as e:
            logger.error(f"GitHub URL error on {method} {endpoint}: {e.reason}")
            raise GitHubAPIError(f"Network error connecting to GitHub: {e.reason}")

    def list_open_prs(self, owner: str, repo: str) -> List[Dict[str, Any]]:
        """List all open pull requests for a given repository."""
        endpoint = f"/repos/{owner}/{repo}/pulls?state=open&sort=created&direction=asc"
        return self._request(endpoint, method="GET")

    def get_pr(self, owner: str, repo: str, pull_number: int) -> Dict[str, Any]:
        """Fetch details for a specific pull request."""
        endpoint = f"/repos/{owner}/{repo}/pulls/{pull_number}"
        return self._request(endpoint, method="GET")

    def merge_pr(
        self,
        owner: str,
        repo: str,
        pull_number: int,
        commit_title: str,
        expected_head_sha: str,
        merge_method: str = "merge",
    ) -> Dict[str, Any]:
        """
        Executes a native GitHub merge on an open PR.
        Passing expected_head_sha guarantees no race-conditions occurred.
        """
        endpoint = f"/repos/{owner}/{repo}/pulls/{pull_number}/merge"
        payload = {
            "commit_title": commit_title,
            "sha": expected_head_sha,
            "merge_method": merge_method,
        }
        return self._request(endpoint, method="PUT", data=payload)

    def post_pr_comment(self, owner: str, repo: str, pull_number: int, comment: str) -> Dict[str, Any]:
        """Post a comment on a pull request thread."""
        endpoint = f"/repos/{owner}/{repo}/issues/{pull_number}/comments"
        return self._request(endpoint, method="POST", data={"body": comment})

    def list_issues(self, owner: str, repo: str, state: str = "open") -> List[Dict[str, Any]]:
        """List GitHub issues (filters out pull requests)."""
        endpoint = f"/repos/{owner}/{repo}/issues?state={state}&sort=updated&direction=desc"
        items = self._request(endpoint, method="GET")
        # In GitHub API, PRs are returned by /issues unless filtered
        return [item for item in items if "pull_request" not in item]

    def get_issue(self, owner: str, repo: str, issue_number: int) -> Dict[str, Any]:
        """Fetch details for a specific issue."""
        endpoint = f"/repos/{owner}/{repo}/issues/{issue_number}"
        return self._request(endpoint, method="GET")

    def update_issue(self, owner: str, repo: str, issue_number: int, state: Optional[str] = None) -> Dict[str, Any]:
        """Update an issue state (e.g. 'open' or 'closed')."""
        endpoint = f"/repos/{owner}/{repo}/issues/{issue_number}"
        payload = {}
        if state:
            payload["state"] = state
        return self._request(endpoint, method="PATCH", data=payload)

    def list_issue_comments(self, owner: str, repo: str, issue_number: int) -> List[Dict[str, Any]]:
        """List comments for an issue."""
        endpoint = f"/repos/{owner}/{repo}/issues/{issue_number}/comments"
        return self._request(endpoint, method="GET")

    def post_issue_comment(self, owner: str, repo: str, issue_number: int, comment: str) -> Dict[str, Any]:
        """Post a comment to an issue thread."""
        endpoint = f"/repos/{owner}/{repo}/issues/{issue_number}/comments"
        return self._request(endpoint, method="POST", data={"body": comment})


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

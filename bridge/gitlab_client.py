"""
GitLab REST API v4 Client for Merge Request Management.
Pure standard library implementation (urllib.request).
"""

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

from core.logger import get_logger

logger = get_logger("si4k.gitlab_client")


class GitLabAPIError(Exception):
    def __init__(self, message: str, status_code: Optional[int] = None, response_body: Optional[str] = None):
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body


class GitLabClient:
    def __init__(self, base_url: str, private_token: str, user_agent: str = "si4k-git-sync/1.0"):
        # Strip trailing slashes and ensure /api/v4 is present
        clean_url = base_url.rstrip("/")
        if not clean_url.endswith("/api/v4"):
            clean_url = f"{clean_url}/api/v4"
        self.base_url = clean_url
        self.token = private_token
        self.user_agent = user_agent

    def _request(self, endpoint: str, method: str = "GET", data: Optional[Dict[str, Any]] = None) -> Any:
        url = f"{self.base_url}{endpoint}"
        headers = {
            "Accept": "application/json",
            "User-Agent": self.user_agent,
        }
        if self.token:
            headers["PRIVATE-TOKEN"] = self.token

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
            logger.error(f"GitLab API Error {e.code} on {method} {endpoint}: {body}")
            raise GitLabAPIError(f"GitLab API HTTP {e.code}: {e.reason}", status_code=e.code, response_body=body)
        except urllib.error.URLError as e:
            logger.error(f"GitLab URL error on {method} {endpoint}: {e.reason}")
            raise GitLabAPIError(f"Network error connecting to GitLab: {e.reason}")

    def get_project(self, project_path_or_id: str | int) -> Optional[Dict[str, Any]]:
        """Fetch project metadata by ID or namespaced path (e.g. 'akshitkamboz13/PC-Papa')."""
        encoded_id = urllib.parse.quote(str(project_path_or_id), safe="")
        endpoint = f"/projects/{encoded_id}"
        try:
            return self._request(endpoint, method="GET")
        except GitLabAPIError as e:
            if e.status_code == 404:
                return None
            raise

    def create_merge_request(
        self,
        project_id: int | str,
        source_branch: str,
        target_branch: str,
        title: str,
        description: str,
        labels: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Create a new merge request in GitLab."""
        encoded_id = urllib.parse.quote(str(project_id), safe="")
        endpoint = f"/projects/{encoded_id}/merge_requests"
        payload = {
            "source_branch": source_branch,
            "target_branch": target_branch,
            "title": title,
            "description": description,
            "remove_source_branch": False,
        }
        if labels:
            payload["labels"] = ",".join(labels)
        return self._request(endpoint, method="POST", data=payload)

    def get_merge_request(self, project_id: int | str, mr_iid: int) -> Dict[str, Any]:
        """Fetch merge request details by internal IID."""
        encoded_id = urllib.parse.quote(str(project_id), safe="")
        endpoint = f"/projects/{encoded_id}/merge_requests/{mr_iid}"
        return self._request(endpoint, method="GET")

    def comment_on_merge_request(self, project_id: int | str, mr_iid: int, note: str) -> Dict[str, Any]:
        """Add a comment/note to a merge request."""
        encoded_id = urllib.parse.quote(str(project_id), safe="")
        endpoint = f"/projects/{encoded_id}/merge_requests/{mr_iid}/notes"
        return self._request(endpoint, method="POST", data={"body": note})

    def close_merge_request(self, project_id: int | str, mr_iid: int) -> Dict[str, Any]:
        """Close an open merge request."""
        encoded_id = urllib.parse.quote(str(project_id), safe="")
        endpoint = f"/projects/{encoded_id}/merge_requests/{mr_iid}"
        return self._request(endpoint, method="PUT", data={"state_event": "close"})

    def list_open_merge_requests(self, project_id: int | str) -> List[Dict[str, Any]]:
        """List all open merge requests for a project."""
        encoded_id = urllib.parse.quote(str(project_id), safe="")
        endpoint = f"/projects/{encoded_id}/merge_requests?state=opened"
        return self._request(endpoint, method="GET")

    def list_issues(self, project_id: int | str, state: str = "opened") -> List[Dict[str, Any]]:
        """List issues for a GitLab project."""
        encoded_id = urllib.parse.quote(str(project_id), safe="")
        endpoint = f"/projects/{encoded_id}/issues?state={state}"
        return self._request(endpoint, method="GET")

    def get_issue(self, project_id: int | str, issue_iid: int) -> Dict[str, Any]:
        """Fetch details for a specific GitLab issue."""
        encoded_id = urllib.parse.quote(str(project_id), safe="")
        endpoint = f"/projects/{encoded_id}/issues/{issue_iid}"
        return self._request(endpoint, method="GET")

    def create_issue(
        self,
        project_id: int | str,
        title: str,
        description: str,
        labels: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Create a new issue in GitLab."""
        encoded_id = urllib.parse.quote(str(project_id), safe="")
        endpoint = f"/projects/{encoded_id}/issues"
        payload = {
            "title": title,
            "description": description,
        }
        if labels:
            payload["labels"] = ",".join(labels)
        return self._request(endpoint, method="POST", data=payload)

    def update_issue(self, project_id: int | str, issue_iid: int, state_event: Optional[str] = None) -> Dict[str, Any]:
        """Update issue state (state_event: 'close' or 'reopen')."""
        encoded_id = urllib.parse.quote(str(project_id), safe="")
        endpoint = f"/projects/{encoded_id}/issues/{issue_iid}"
        payload = {}
        if state_event:
            payload["state_event"] = state_event
        return self._request(endpoint, method="PUT", data=payload)

    def list_issue_notes(self, project_id: int | str, issue_iid: int) -> List[Dict[str, Any]]:
        """List comments/notes on a GitLab issue."""
        encoded_id = urllib.parse.quote(str(project_id), safe="")
        endpoint = f"/projects/{encoded_id}/issues/{issue_iid}/notes"
        return self._request(endpoint, method="GET")

    def comment_on_issue(self, project_id: int | str, issue_iid: int, note: str) -> Dict[str, Any]:
        """Add a comment/note to a GitLab issue."""
        encoded_id = urllib.parse.quote(str(project_id), safe="")
        endpoint = f"/projects/{encoded_id}/issues/{issue_iid}/notes"
        return self._request(endpoint, method="POST", data={"body": note})


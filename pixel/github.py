"""GitHub helpers the GitDb document API does not cover.

GitDb stores JSON documents. Photos and videos are binary, so their bytes are
committed with the Git Data API (blobs -> tree -> commit -> ref) using the same
optimistic, non-forced ref update GitDb uses, and read back from
``raw.githubusercontent.com`` so HTTP ``Range`` requests (needed for video
playback, notably on iOS) are passed straight through.

Every call here runs with the *visitor's* credentials (or none at all), so
GitHub itself decides what a visitor may see, download or change.
"""

from __future__ import annotations

import base64
import time
from dataclasses import dataclass
from typing import Any, Dict, Iterator, List, Mapping, Optional, Sequence
from urllib.parse import quote

import requests
from gitdb import ConflictError, GitDb, GitDbError, GitHubClient, NotFoundError
from gitdb.documents import BLOB_MODE

#: Chunk size used when streaming media to the browser.
STREAM_CHUNK = 256 * 1024

#: Response headers copied from GitHub's raw host to the browser.
_PASSTHROUGH = ("Content-Length", "Content-Range", "Accept-Ranges", "ETag", "Last-Modified")


@dataclass(frozen=True)
class Access:
    """What a visitor may do, as reported by GitHub for the data repository."""

    can_view: bool
    can_write: bool
    is_admin: bool
    private: bool

    @classmethod
    def none(cls) -> Access:
        return cls(can_view=False, can_write=False, is_admin=False, private=True)

    def as_dict(self) -> Dict[str, bool]:
        return {
            "can_view": self.can_view,
            "can_write": self.can_write,
            "is_admin": self.is_admin,
            "private": self.private,
        }


def repository_access(client: GitHubClient, repo: str) -> Access:
    """Ask GitHub what ``client``'s credentials may do on ``repo``.

    A 404 means the repository is private (or missing) for these credentials.
    Anonymous callers never get a ``permissions`` block, so for them a visible
    repository is public and read-only.
    """
    response = client.request("GET", f"/repos/{repo}", allow_404=True)
    if response.status_code == 404:
        return Access.none()
    payload = response.json()
    private = payload.get("private", True) is not False
    permissions = payload.get("permissions")
    if not isinstance(permissions, Mapping):
        return Access(can_view=True, can_write=False, is_admin=False, private=private)
    admin = bool(permissions.get("admin"))
    push = admin or bool(permissions.get("maintain")) or bool(permissions.get("push"))
    can_view = not private or bool(permissions.get("pull"))
    return Access(can_view=can_view, can_write=can_view and push, is_admin=admin, private=private)


def current_user(client: GitHubClient) -> Dict[str, Any]:
    """Return ``login``/``avatar_url`` for the token owner."""
    payload = client.get_json("/user")
    return {"login": str(payload.get("login", "")), "avatar_url": payload.get("avatar_url")}


def commit_files(
    db: GitDb,
    puts: Mapping[str, bytes],
    deletes: Sequence[str] = (),
    *,
    message: str,
    retries: int = 3,
) -> str:
    """Commit binary files (and removals) to ``db``'s branch in one commit.

    Blobs are uploaded once; when another writer moved the branch the tree is
    rebuilt on the new head and the non-forced ref update retried.
    """
    client = db.client
    repo = db.repo
    blobs: Dict[str, str] = {}
    for path, data in puts.items():
        response = client.request(
            "POST",
            f"/repos/{repo}/git/blobs",
            json={"content": base64.b64encode(data).decode("ascii"), "encoding": "base64"},
        )
        blobs[path] = str(response.json()["sha"])
    entries: List[Dict[str, Any]] = [
        {"path": path, "mode": BLOB_MODE, "type": "blob", "sha": sha} for path, sha in blobs.items()
    ]
    entries.extend({"path": path, "mode": BLOB_MODE, "type": "blob", "sha": None} for path in deletes)

    last_error: Optional[GitDbError] = None
    for attempt in range(max(0, retries) + 1):
        head = db.resolve_ref(refresh=True)
        base_tree = client.get_json(f"/repos/{repo}/git/commits/{head}")["tree"]["sha"]
        tree = client.request(
            "POST", f"/repos/{repo}/git/trees", json={"base_tree": base_tree, "tree": entries}
        ).json()
        body: Dict[str, Any] = {"message": message, "tree": tree["sha"], "parents": [head]}
        commit = client.request("POST", f"/repos/{repo}/git/commits", json=body).json()
        try:
            client.request(
                "PATCH",
                f"/repos/{repo}/git/refs/heads/{db.branch}",
                json={"sha": commit["sha"], "force": False},
            )
        except ConflictError as exc:
            last_error = exc
            time.sleep(min(2.0, 0.2 * (2**attempt)))
            continue
        # Let GitDb's own writes build on top of this commit.
        db.resolve_ref(refresh=True)
        return str(commit["sha"])
    raise last_error or ConflictError("could not update the branch ref")


@dataclass
class RawFile:
    """An open streaming response for one media file."""

    status: int
    headers: Dict[str, str]
    response: requests.Response

    def iter_bytes(self) -> Iterator[bytes]:
        try:
            yield from self.response.iter_content(STREAM_CHUNK)
        finally:
            self.response.close()


def open_raw(
    raw_url: str,
    repo: str,
    branch: str,
    path: str,
    token: Optional[str],
    *,
    range_header: Optional[str] = None,
    timeout: float = 60.0,
) -> RawFile:
    """Stream ``path`` from GitHub's raw host with the visitor's credentials.

    Raises :class:`NotFoundError` when GitHub does not serve the file to these
    credentials (private repository, revoked access or a missing file).
    """
    url = f"{raw_url.rstrip('/')}/{repo}/{quote(branch, safe='')}/{quote(path)}"
    headers = {"User-Agent": "pixel"}
    if token:
        headers["Authorization"] = f"token {token}"
    if range_header:
        headers["Range"] = range_header
    response = requests.get(url, headers=headers, stream=True, timeout=timeout)
    if response.status_code in (401, 403, 404):
        response.close()
        raise NotFoundError(f"{path} is not available", status=404)
    if response.status_code == 416:
        response.close()
        raise GitDbError("requested range not satisfiable", status=416)
    if response.status_code >= 400:
        response.close()
        raise GitDbError(f"GitHub returned {response.status_code}", status=502)
    passthrough = {name: response.headers[name] for name in _PASSTHROUGH if name in response.headers}
    return RawFile(response.status_code, passthrough, response)

"""Runtime configuration, read from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Mapping, Optional

from gitdb import DEFAULT_API_URL, DEFAULT_RAW_URL


def _flag(value: Optional[str], default: bool) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    """Everything the server needs to know about the data repository."""

    #: ``owner/name`` of the GitDb repository holding the photos and videos.
    repo: str
    branch: str = "main"
    #: Directory of the GitDb collections (albums, media) inside ``repo``.
    data_root: str = "data"
    #: Directory of the binary photo/video files inside ``repo``.
    media_root: str = "media"
    api_url: str = DEFAULT_API_URL
    raw_url: str = DEFAULT_RAW_URL
    #: Largest accepted upload in bytes. GitHub rejects files above 100 MB.
    max_upload_bytes: int = 50 * 1024 * 1024
    #: Repository receiving the visitor audit log (defaults to ``repo``).
    audit_repo: Optional[str] = None
    audit_branch: Optional[str] = None
    #: Server-held token with Contents write access to ``audit_repo``. Only
    #: ever used to append audit events, never to read photos or videos.
    audit_token: Optional[str] = field(default=None, repr=False)
    audit_flush_seconds: float = 10.0
    #: Trust ``X-Forwarded-For`` for the visitor address (behind a proxy).
    trust_proxy: bool = False
    #: Force the ``Secure`` cookie flag (otherwise derived from the request scheme).
    cookie_secure: Optional[bool] = None
    #: Seconds a session may stay idle / live at most.
    session_idle_seconds: int = 2 * 60 * 60
    session_max_seconds: int = 12 * 60 * 60
    #: How long a visitor's repository permission is trusted before re-checking.
    permission_ttl_seconds: int = 300

    @property
    def audit_enabled(self) -> bool:
        return bool(self.audit_token)

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> Settings:
        env = os.environ if env is None else env
        repo = env.get("PIXEL_REPO", "").strip()
        if repo.count("/") != 1 or not all(repo.split("/")):
            raise RuntimeError("set PIXEL_REPO to the owner/name of your GitDb repository")
        secure = env.get("PIXEL_COOKIE_SECURE")
        return cls(
            repo=repo,
            branch=env.get("PIXEL_BRANCH", "main").strip() or "main",
            data_root=env.get("PIXEL_DATA_ROOT", "data").strip("/ ") or "data",
            media_root=env.get("PIXEL_MEDIA_ROOT", "media").strip("/ ") or "media",
            api_url=env.get("PIXEL_API_URL", DEFAULT_API_URL).rstrip("/"),
            raw_url=env.get("PIXEL_RAW_URL", DEFAULT_RAW_URL).rstrip("/"),
            max_upload_bytes=int(env.get("PIXEL_MAX_UPLOAD_MB", "50")) * 1024 * 1024,
            audit_repo=env.get("PIXEL_AUDIT_REPO", "").strip() or None,
            audit_branch=env.get("PIXEL_AUDIT_BRANCH", "").strip() or None,
            audit_token=env.get("PIXEL_AUDIT_TOKEN", "").strip() or None,
            audit_flush_seconds=float(env.get("PIXEL_AUDIT_FLUSH_SECONDS", "10")),
            trust_proxy=_flag(env.get("PIXEL_TRUST_PROXY"), False),
            cookie_secure=None if secure in (None, "") else _flag(secure, True),
        )

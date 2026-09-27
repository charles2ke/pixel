"""In-memory sessions: a visitor's GitHub token never leaves server memory."""

from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, Optional

from gitdb import GitDb

from .github import Access

COOKIE = "pixel_session"


@dataclass
class Session:
    sid: str
    token: str = field(repr=False)
    login: str
    avatar_url: Optional[str]
    db: GitDb = field(repr=False)
    access: Access
    created: float = field(default_factory=time.monotonic)
    last_seen: float = field(default_factory=time.monotonic)
    access_checked: float = field(default_factory=time.monotonic)


class SessionStore:
    def __init__(self, idle_seconds: int, max_seconds: int) -> None:
        self.idle_seconds = idle_seconds
        self.max_seconds = max_seconds
        self._sessions: Dict[str, Session] = {}
        self._lock = threading.Lock()

    def create(self, token: str, login: str, avatar_url: Optional[str], db: GitDb, access: Access) -> Session:
        session = Session(secrets.token_urlsafe(32), token, login, avatar_url, db, access)
        with self._lock:
            self._expire()
            self._sessions[session.sid] = session
        return session

    def get(self, sid: Optional[str]) -> Optional[Session]:
        if not sid:
            return None
        now = time.monotonic()
        with self._lock:
            session = self._sessions.get(sid)
            if session is None:
                return None
            if now - session.last_seen > self.idle_seconds or now - session.created > self.max_seconds:
                self._drop(sid)
                return None
            session.last_seen = now
            return session

    def drop(self, sid: Optional[str]) -> None:
        if not sid:
            return
        with self._lock:
            self._drop(sid)

    def _drop(self, sid: str) -> None:
        session = self._sessions.pop(sid, None)
        if session is not None:
            session.db.close()

    def _expire(self) -> None:
        now = time.monotonic()
        for sid, session in list(self._sessions.items()):
            if now - session.last_seen > self.idle_seconds or now - session.created > self.max_seconds:
                self._drop(sid)

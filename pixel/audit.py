"""Visitor audit log, appended to a GitDb repository.

Visitors usually cannot write to the data repository, so audit events are
written with a separate server-held token (``PIXEL_AUDIT_TOKEN``). That token is
only used here. Events are buffered and flushed as one GitDb batch commit every
few seconds, so a busy album does not produce one commit per request. Every
event is also written to the server log, so nothing is lost if a flush fails.
"""

from __future__ import annotations

import json
import logging
import threading
from typing import Any, Dict, List, Optional

from gitdb import GitDb, GitDbError, GitHubClient, new_id

from .config import Settings
from .github import repository_access

AUDIT = "audit"
MAX_PENDING = 5000
MAX_FIELD = 300

log = logging.getLogger("pixel.audit")


class AuditLog:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.repo = settings.audit_repo or settings.repo
        self.enabled = settings.audit_enabled
        if self.enabled and self.repo.lower() == settings.repo.lower():
            # Everyone who can read the album could read visitors' IP addresses,
            # and uploaders could rewrite the log.
            raise RuntimeError("PIXEL_AUDIT_REPO must be a separate, private repository")
        self._pending: List[Dict[str, Any]] = []
        self._lock = threading.Lock()
        self._flush_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.db: Optional[GitDb] = None
        if self.enabled:
            self.db = GitDb(
                self.repo,
                token=settings.audit_token,
                branch=settings.audit_branch or settings.branch,
                root=settings.data_root,
                api_url=settings.api_url,
                raw_url=settings.raw_url,
                # Ids start with a timestamp: 5-character shards bucket ~9 hours.
                shard_depth=1,
                shard_width=5,
                concurrency=8,
            )

    # ------------------------------------------------------------ lifecycle
    def start(self) -> None:
        if not self.enabled or self._thread is not None:
            return
        self._require_private()
        self._thread = threading.Thread(target=self._run, name="pixel-audit", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
        self.flush()

    def _run(self) -> None:
        while not self._stop.wait(self.settings.audit_flush_seconds):
            self.flush()

    def _require_private(self) -> None:
        assert self.db is not None
        try:
            access = repository_access(GitHubClient(None, api_url=self.settings.api_url), self.repo)
        except GitDbError as exc:
            raise RuntimeError(f"could not verify audit repository {self.repo} is private") from exc
        if not access.private:
            raise RuntimeError(f"PIXEL_AUDIT_REPO {self.repo} must be a private repository")

    # --------------------------------------------------------------- events
    def record(self, event: str, **fields: Any) -> None:
        entry: Dict[str, Any] = {"event": event}
        for key, value in fields.items():
            if value is None:
                continue
            if isinstance(value, str):
                value = value[:MAX_FIELD]
            entry[key] = value
        log.info("audit %s", json.dumps(entry, sort_keys=True))
        if not self.enabled:
            return
        with self._lock:
            self._pending.append(entry)
            if len(self._pending) > MAX_PENDING:
                del self._pending[: len(self._pending) - MAX_PENDING]

    def flush(self) -> int:
        """Commit buffered events; returns how many were written."""
        if not self.enabled or self.db is None:
            return 0
        with self._flush_lock:
            with self._lock:
                batch, self._pending = self._pending, []
            if not batch:
                return 0
            try:
                with self.db.batch(message=f"pixel audit: {len(batch)} event(s)") as writer:
                    for entry in batch:
                        writer.put(AUDIT, new_id(), entry)
            except GitDbError as exc:
                log.error("could not write %d audit event(s): %s", len(batch), exc)
                with self._lock:
                    self._pending[:0] = batch
                return 0
            return len(batch)

    def recent(self, limit: int = 200) -> List[Dict[str, Any]]:
        """Return the newest events first (admin only)."""
        if not self.enabled or self.db is None:
            return []
        self.flush()
        collection = self.db.collection(AUDIT)
        ids = collection.list()
        after = ids[-limit - 1] if len(ids) > limit else None
        events = list(collection.all(after=after))
        events.reverse()
        return [
            {key: value for key, value in event.items() if key not in {"_rev", "_updated_at"}}
            for event in events
        ]

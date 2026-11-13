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
from bisect import bisect_left
from typing import Any, Dict, List, Optional, Tuple

from gitdb import GitDb, GitDbError, GitHubClient, new_id

from .config import Settings
from .github import repository_access

AUDIT = "audit"
MAX_PENDING = 5000
MAX_FIELD = 300
#: Most events one filtered visitor-log request reads. Each event not read
#: before costs one GitHub API request (later reads come from the cache).
SCAN_LIMIT = 1000

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
        try:
            access = repository_access(GitHubClient(None, api_url=self.settings.api_url), self.repo)
        except GitDbError as exc:
            # Audit events contain visitor metadata, so startup requires GitHub
            # availability to confirm the audit repository is private.
            raise RuntimeError(
                f"could not verify audit repository {self.repo} is private; "
                "check PIXEL_API_URL and network access"
            ) from exc
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
                # Ids minted in the same millisecond sort randomly; sorting them
                # keeps the log in the order the events happened.
                ids = sorted(new_id() for _ in batch)
                with self.db.batch(message=f"pixel audit: {len(batch)} event(s)") as writer:
                    for key, entry in zip(ids, batch):
                        writer.put(AUDIT, key, entry)
            except GitDbError as exc:
                log.error("could not write %d audit event(s): %s", len(batch), exc)
                with self._lock:
                    self._pending[:0] = batch
                return 0
            return len(batch)

    def recent(
        self,
        limit: int = 200,
        *,
        before: Optional[str] = None,
        event: Optional[str] = None,
        who: Optional[str] = None,
    ) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        """Return events newest first, plus the cursor (``before``) of older ones.

        ``event`` keeps one kind of event; ``who`` keeps events whose login or
        IP address contains it. A filtered request examines at most
        :data:`SCAN_LIMIT` events, so it can return fewer than ``limit`` events
        and still a cursor to look further back.
        """
        if not self.enabled or self.db is None:
            return [], None
        if before is None:
            self.flush()
        collection = self.db.collection(AUDIT)
        ids = collection.list()
        end = bisect_left(ids, before) if before else len(ids)
        needle = (who or "").strip().lstrip("@").lower()
        filtered = bool(event or needle)
        budget = SCAN_LIMIT if filtered else limit
        window = max(limit, 250) if filtered else limit
        found: List[Dict[str, Any]] = []
        while end > 0 and budget > 0:
            start = max(0, end - min(window, budget))
            budget -= end - start
            chunk = collection.all(limit=end - start, after=ids[start - 1] if start else None)
            documents = {document.get("_id"): document for document in chunk}
            for position in range(end - 1, start - 1, -1):
                document = documents.get(ids[position])
                if document is None or not _matches(document, event, needle):
                    continue
                found.append(document)
                if len(found) == limit:
                    return _public(found), ids[position] if position > 0 else None
            end = start
        return _public(found), ids[end] if end > 0 else None


def _matches(document: Dict[str, Any], event: Optional[str], needle: str) -> bool:
    if event and document.get("event") != event:
        return False
    if not needle:
        return True
    values = (document.get("login"), document.get("attempted_login"), document.get("ip"))
    return any(isinstance(value, str) and needle in value.lower() for value in values)


def _public(events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [
        {key: value for key, value in event.items() if key not in {"_rev", "_updated_at"}} for event in events
    ]

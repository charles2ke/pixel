"""Pixel: a personal photo and video album stored in a GitDb repository.

Run with ``PIXEL_REPO=owner/name uvicorn pixel.app:app``. See README.md.
"""

from __future__ import annotations

import logging
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, Dict, Optional
from urllib.parse import quote

from fastapi import Body, Depends, FastAPI, File, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from gitdb import (
    AuthError,
    ConflictError,
    GitDb,
    GitDbError,
    GitHubClient,
    NotFoundError,
    RateLimitError,
)
from gitdb import ValidationError as GitDbValidationError

from . import store
from .audit import AuditLog
from .config import Settings
from .github import Access, current_user, open_raw, repository_access
from .sessions import COOKIE, Session, SessionStore

STATIC = Path(__file__).with_name("static")
# Unauthenticated GitHub API calls are limited to 60 per hour per IP address.
ANONYMOUS_ACCESS_TTL = 600.0
CSRF_HEADER = "x-pixel"

CSP = "; ".join(
    [
        "default-src 'self'",
        "img-src 'self' blob: data:",
        "media-src 'self' blob:",
        "script-src 'self'",
        "style-src 'self'",
        "connect-src 'self'",
        "worker-src 'self'",
        "manifest-src 'self'",
        "object-src 'none'",
        "base-uri 'none'",
        "form-action 'self'",
        "frame-ancestors 'none'",
    ]
)

log = logging.getLogger("pixel")


@dataclass
class Viewer:
    session: Optional[Session]
    access: Access
    db: GitDb

    @property
    def login(self) -> Optional[str]:
        return self.session.login if self.session else None

    @property
    def token(self) -> Optional[str]:
        return self.session.token if self.session else None


def create_app(settings: Optional[Settings] = None) -> FastAPI:
    settings = settings or Settings.from_env()
    sessions = SessionStore(settings.session_idle_seconds, settings.session_max_seconds)
    audit_log = AuditLog(settings)
    anonymous_db = store.open_db(settings, None)
    anonymous_client = GitHubClient(None, api_url=settings.api_url)
    anonymous_state: Dict[str, Any] = {"access": None, "checked": 0.0}
    anonymous_lock = threading.Lock()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        audit_log.start()
        try:
            yield
        finally:
            audit_log.stop()

    app = FastAPI(title="Pixel", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    app.state.audit = audit_log
    app.state.sessions = sessions

    # ------------------------------------------------------------- helpers
    def client_ip(request: Request) -> Optional[str]:
        if settings.trust_proxy:
            forwarded = request.headers.get("x-forwarded-for")
            if forwarded:
                return forwarded.split(",")[0].strip()
        return request.client.host if request.client else None

    def record(request: Request, viewer: Optional[Viewer], event: str, **fields: Any) -> None:
        audit_log.record(
            event,
            at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            login=viewer.login if viewer else None,
            ip=client_ip(request),
            user_agent=request.headers.get("user-agent"),
            path=request.url.path,
            **fields,
        )

    def anonymous_access() -> Access:
        with anonymous_lock:
            cached = anonymous_state["access"]
            if cached is not None and time.monotonic() - anonymous_state["checked"] < ANONYMOUS_ACCESS_TTL:
                return cached
        try:
            access = repository_access(anonymous_client, settings.repo)
        except RateLimitError:
            if cached is None:
                raise
            access = cached  # keep the last known answer until the limit resets
        except GitDbError:
            access = Access.none()
        with anonymous_lock:
            anonymous_state["access"] = access
            anonymous_state["checked"] = time.monotonic()
        return access

    def viewer(request: Request) -> Viewer:
        sid = request.cookies.get(COOKIE)
        session = sessions.get(sid)
        if session is not None:
            if time.monotonic() - session.access_checked > settings.permission_ttl_seconds:
                try:
                    session.access = repository_access(session.db.client, settings.repo)
                    session.access_checked = time.monotonic()
                except AuthError:
                    # The token was revoked or expired on GitHub.
                    sessions.drop(sid)
                    session = None
                except GitDbError:
                    return Viewer(session, Access.none(), session.db)
        if session is not None:
            return Viewer(session, session.access, session.db)
        return Viewer(None, anonymous_access(), anonymous_db)

    def require_view(request: Request, who: Viewer) -> Viewer:
        if who.access.can_view:
            return who
        record(request, who, "access_denied", need="view")
        if who.session is None:
            raise HTTPException(401, "This album is private. Sign in with GitHub to continue.")
        raise HTTPException(403, f"@{who.login} has no access to {settings.repo}.")

    def require_write(request: Request, who: Viewer) -> Viewer:
        if who.access.can_write and who.session is not None:
            return who
        record(request, who, "access_denied", need="write")
        if who.session is None:
            raise HTTPException(401, "Sign in with GitHub to make changes.")
        raise HTTPException(403, f"@{who.login} cannot write to {settings.repo}.")

    def csrf(request: Request) -> None:
        if request.headers.get(CSRF_HEADER) != "1":
            raise HTTPException(403, "missing request header")

    def read_limited(upload: UploadFile, limit: int) -> Optional[bytes]:
        data = upload.file.read(limit + 1)
        return None if len(data) > limit else data

    def disposition(kind: str, filename: str) -> str:
        fallback = "".join(ch if 32 <= ord(ch) < 127 and ch not in '"\\;' else "_" for ch in filename)
        return f"{kind}; filename=\"{fallback or 'download'}\"; filename*=UTF-8''{quote(filename)}"

    def stream(request: Request, who: Viewer, item: Dict[str, Any], mode: str) -> Response:
        field = "thumb" if mode == "thumb" else "path"
        path = store.safe_media_path(settings, item.get(field))
        if path is None:
            raise HTTPException(404, "not available")
        range_header = None if mode == "thumb" else request.headers.get("range")
        raw = open_raw(
            settings.raw_url,
            settings.repo,
            settings.branch,
            path,
            who.token,
            range_header=range_header,
        )
        if mode == "thumb":
            mime = item.get("thumb_mime") if item.get("thumb_mime") in store.THUMB_TYPES else "image/jpeg"
        else:
            mime = item.get("mime") if item.get("mime") in store.MEDIA_TYPES else "application/octet-stream"
        headers = dict(raw.headers)
        headers["Cache-Control"] = "private, max-age=300"
        filename = str(item.get("filename") or "download")
        headers["Content-Disposition"] = disposition(
            "attachment" if mode == "download" else "inline", filename
        )
        return StreamingResponse(raw.iter_bytes(), status_code=raw.status, media_type=mime, headers=headers)

    # --------------------------------------------------------- middleware
    @app.middleware("http")
    async def security_headers(request: Request, call_next: Any) -> Response:
        response: Response = await call_next(request)
        response.headers.setdefault("Content-Security-Policy", CSP)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        if request.url.path.startswith("/api/") and "Cache-Control" not in response.headers:
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(store.StoreError)
    async def store_error(_: Request, exc: store.StoreError) -> JSONResponse:
        return JSONResponse({"detail": exc.message}, status_code=exc.status)

    @app.exception_handler(GitDbError)
    async def gitdb_error(_: Request, exc: GitDbError) -> JSONResponse:
        if isinstance(exc, RateLimitError):
            status, detail = 429, "GitHub rate limit reached, try again shortly."
        elif isinstance(exc, AuthError):
            status, detail = 403, "GitHub refused this request for your account."
        elif isinstance(exc, NotFoundError):
            status, detail = 404, "not found"
        elif isinstance(exc, ConflictError):
            status, detail = 409, "someone else changed this at the same time, try again"
        elif isinstance(exc, GitDbValidationError):
            status, detail = 422, str(exc)
        elif exc.status == 416:
            status, detail = 416, "range not satisfiable"
        else:
            log.warning("GitHub error: %s", exc)
            status, detail = 502, "GitHub is unavailable right now."
        return JSONResponse({"detail": detail}, status_code=status)

    # ------------------------------------------------------------ session
    @app.get("/api/session")
    def get_session(request: Request, visit: bool = False) -> Dict[str, Any]:
        who = viewer(request)
        if visit:
            record(request, who, "visit", referrer=request.headers.get("referer"))
        return {
            "repo": settings.repo,
            "signed_in": who.session is not None,
            "user": {"login": who.session.login, "avatar_url": who.session.avatar_url}
            if who.session
            else None,
            "access": who.access.as_dict(),
            "audit_enabled": audit_log.enabled,
            "max_upload_bytes": settings.max_upload_bytes,
        }

    @app.post("/api/login", dependencies=[Depends(csrf)])
    def login(request: Request, response: Response, payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
        token = payload.get("token")
        if (
            not isinstance(token, str)
            or not token.strip()
            or len(token) > 512
            or any(ch.isspace() for ch in token.strip())
        ):
            raise HTTPException(422, "paste a GitHub token")
        token = token.strip()
        db = store.open_db(settings, token)
        try:
            user = current_user(db.client)
        except AuthError as exc:
            db.close()
            record(request, None, "sign_in_failed")
            raise HTTPException(401, "GitHub did not accept this token.") from exc
        access = repository_access(db.client, settings.repo)
        if not access.can_view:
            db.close()
            record(request, None, "sign_in_denied", attempted_login=user["login"])
            raise HTTPException(403, f"@{user['login']} has no access to {settings.repo}.")
        session = sessions.create(token, user["login"], user.get("avatar_url"), db, access)
        response.set_cookie(
            COOKIE,
            session.sid,
            httponly=True,
            samesite="strict",
            secure=settings.cookie_secure
            if settings.cookie_secure is not None
            else request.url.scheme == "https",
            max_age=settings.session_max_seconds,
            path="/",
        )
        record(request, Viewer(session, access, db), "sign_in")
        return {
            "user": {"login": session.login, "avatar_url": session.avatar_url},
            "access": access.as_dict(),
        }

    @app.post("/api/logout", dependencies=[Depends(csrf)])
    def logout(request: Request, response: Response) -> Dict[str, bool]:
        who = viewer(request)
        if who.session is not None:
            record(request, who, "sign_out")
            sessions.drop(who.session.sid)
        response.delete_cookie(COOKIE, path="/")
        return {"ok": True}

    # ------------------------------------------------------------- albums
    @app.get("/api/albums")
    def albums(request: Request) -> Dict[str, Any]:
        who = require_view(request, viewer(request))
        return {"albums": store.list_albums(who.db)}

    @app.post("/api/albums", dependencies=[Depends(csrf)], status_code=201)
    def create_album(request: Request, payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
        who = require_write(request, viewer(request))
        album = store.create_album(who.db, payload.get("title"), payload.get("description"), who.login or "")
        record(request, who, "create_album", album=album.get("_id"))
        return store.public_album(album)

    @app.get("/api/albums/{album_id}")
    def album(request: Request, album_id: str) -> Dict[str, Any]:
        who = require_view(request, viewer(request))
        found = store.get_album(who.db, album_id)
        items = store.album_media(who.db, found["_id"])
        record(request, who, "view_album", album=found["_id"])
        return {**store.public_album(found), "items": [store.public_media(item) for item in items]}

    @app.delete("/api/albums/{album_id}", dependencies=[Depends(csrf)])
    def remove_album(request: Request, album_id: str) -> Dict[str, bool]:
        who = require_write(request, viewer(request))
        store.delete_album(who.db, album_id)
        record(request, who, "delete_album", album=album_id)
        return {"ok": True}

    # -------------------------------------------------------------- media
    @app.post("/api/albums/{album_id}/media", dependencies=[Depends(csrf)], status_code=201)
    def upload(
        request: Request,
        album_id: str,
        file: UploadFile = File(...),
        thumb: Optional[UploadFile] = File(None),
    ) -> Dict[str, Any]:
        who = require_write(request, viewer(request))
        data = read_limited(file, settings.max_upload_bytes)
        if data is None:
            raise HTTPException(413, "the file is too large")
        thumb_data = read_limited(thumb, store.MAX_THUMB_BYTES) if thumb is not None else None
        item = store.add_media(
            who.db,
            settings,
            album_id,
            filename=file.filename,
            data=data,
            thumb=thumb_data,
            actor=who.login or "",
        )
        record(request, who, "upload", album=item["album"], media=item["_id"], size=item["size"])
        return store.public_media(item)

    @app.delete("/api/media/{media_id}", dependencies=[Depends(csrf)])
    def remove_media(request: Request, media_id: str) -> Dict[str, bool]:
        who = require_write(request, viewer(request))
        item = store.delete_media(who.db, media_id)
        record(request, who, "delete_media", album=item.get("album"), media=media_id)
        return {"ok": True}

    @app.get("/api/media/{media_id}/thumb")
    def thumb(request: Request, media_id: str) -> Response:
        who = require_view(request, viewer(request))
        return stream(request, who, store.get_media(who.db, media_id), "thumb")

    @app.get("/api/media/{media_id}/original")
    def original(request: Request, media_id: str) -> Response:
        who = require_view(request, viewer(request))
        item = store.get_media(who.db, media_id)
        range_header = request.headers.get("range")
        if not range_header or range_header.replace(" ", "") == "bytes=0-":
            # Video players fetch many ranges (Safari probes bytes=0-1 first);
            # only the request for the whole file counts as a "view".
            record(request, who, "view_media", album=item.get("album"), media=item["_id"])
        return stream(request, who, item, "original")

    @app.get("/api/media/{media_id}/download")
    def download(request: Request, media_id: str) -> Response:
        who = require_view(request, viewer(request))
        item = store.get_media(who.db, media_id)
        if not request.headers.get("range"):
            record(request, who, "download", album=item.get("album"), media=item["_id"])
        return stream(request, who, item, "download")

    # -------------------------------------------------------------- audit
    @app.get("/api/audit")
    def audit(request: Request, limit: int = 200) -> Dict[str, Any]:
        who = viewer(request)
        if who.session is None or not who.access.is_admin:
            record(request, who, "access_denied", need="admin")
            raise HTTPException(403, "Only repository admins can read the visitor log.")
        return {"enabled": audit_log.enabled, "events": audit_log.recent(max(1, min(limit, 500)))}

    @app.get("/api/health")
    def health() -> Dict[str, str]:
        return {"status": "ok"}

    # ------------------------------------------------------------- static
    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

    @app.get("/manifest.webmanifest", include_in_schema=False)
    def manifest() -> FileResponse:
        return FileResponse(STATIC / "manifest.webmanifest", media_type="application/manifest+json")

    @app.get("/sw.js", include_in_schema=False)
    def service_worker() -> FileResponse:
        return FileResponse(
            STATIC / "sw.js",
            media_type="text/javascript",
            headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"},
        )

    @app.get("/apple-touch-icon.png", include_in_schema=False)
    def apple_touch_icon() -> FileResponse:
        return FileResponse(STATIC / "icons" / "apple-touch-icon.png")

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon() -> FileResponse:
        return FileResponse(STATIC / "icons" / "icon-192.png", media_type="image/png")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app


def __getattr__(name: str) -> Any:
    # ``uvicorn pixel.app:app`` builds the app from the environment on first use,
    # so importing this module (e.g. in tests) needs no configuration.
    if name == "app":
        application = create_app()
        globals()["app"] = application
        return application
    raise AttributeError(name)

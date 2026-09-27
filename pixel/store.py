"""Albums and media stored in a GitDb repository.

Layout inside the data repository::

    data/albums/{album_id}.json          GitDb document per album
    data/media/{media_id}.json           GitDb document per photo/video
    data/_index/media/album.json         GitDb index: album id -> media ids
    data/_manifest/{albums,media}.json   GitDb manifests (cheap listing)
    media/{album_id}/{media_id}.{ext}    the original photo or video bytes
    media/{album_id}/{media_id}.thumb.jpg  thumbnail shown in the album grid
"""

from __future__ import annotations

import logging
import re
import threading
from typing import Any, Dict, List, Optional, Tuple

from gitdb import ConflictError, GitDb, NotFoundError, ValidationError, new_id, validate_id

from .config import Settings
from .github import commit_files

ALBUMS = "albums"
MEDIA = "media"
log = logging.getLogger("pixel.store")

#: Sniffed media type -> (kind, file extension). Anything else is rejected, so
#: nothing that a browser could execute (HTML, SVG, ...) is ever stored.
MEDIA_TYPES: Dict[str, Tuple[str, str]] = {
    "image/jpeg": ("photo", "jpg"),
    "image/png": ("photo", "png"),
    "image/gif": ("photo", "gif"),
    "image/webp": ("photo", "webp"),
    "image/heic": ("photo", "heic"),
    "image/heif": ("photo", "heif"),
    "image/avif": ("photo", "avif"),
    "video/mp4": ("video", "mp4"),
    "video/quicktime": ("video", "mov"),
    "video/x-m4v": ("video", "m4v"),
    "video/3gpp": ("video", "3gp"),
    "video/webm": ("video", "webm"),
}

THUMB_TYPES = {"image/jpeg", "image/png", "image/webp"}
MAX_THUMB_BYTES = 2 * 1024 * 1024
MAX_TITLE = 120
MAX_DESCRIPTION = 1000

_HEIC_BRANDS = {b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis", b"hevm", b"hevs"}
_HEIF_BRANDS = {b"mif1", b"msf1"}
_AVIF_BRANDS = {b"avif", b"avis"}

#: GitDb maintains its album index with read-modify-write commits, so writes
#: from this process are serialized to keep index updates from racing.
WRITE_LOCK = threading.Lock()


class StoreError(Exception):
    """A request the store refuses, mapped to an HTTP status by the app."""

    def __init__(self, message: str, status: int = 422) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


def sniff(data: bytes) -> Optional[str]:
    """Return the media type of ``data`` from its magic bytes, or ``None``."""
    head = data[:32]
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if head.startswith(b"\x1a\x45\xdf\xa3"):
        return "video/webm"
    if head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand in _HEIC_BRANDS:
            return "image/heic"
        if brand in _HEIF_BRANDS:
            return "image/heif"
        if brand in _AVIF_BRANDS:
            return "image/avif"
        if brand == b"qt  ":
            return "video/quicktime"
        if brand == b"M4V ":
            return "video/x-m4v"
        if brand.startswith(b"3g"):
            return "video/3gpp"
        return "video/mp4"
    return None


def clean_text(value: Any, *, limit: int, field: str, required: bool = False) -> str:
    text = value.strip() if isinstance(value, str) else ""
    text = re.sub(r"[\x00-\x1f\x7f]", "", text)
    if required and not text:
        raise StoreError(f"{field} is required")
    if len(text) > limit:
        raise StoreError(f"{field} must be at most {limit} characters")
    return text


def clean_filename(name: Optional[str]) -> str:
    base = (name or "").replace("\\", "/").rsplit("/", 1)[-1]
    base = re.sub(r"[\x00-\x1f\x7f\"]", "", base).strip()
    return base[:200] or "upload"


def check_id(value: str, what: str) -> str:
    try:
        return validate_id(value)
    except ValidationError as exc:
        raise StoreError(f"invalid {what} id", 404) from exc


def open_db(settings: Settings, token: Optional[str]) -> GitDb:
    """Open the data repository with a visitor's token (``None`` = anonymous)."""
    return GitDb(
        settings.repo,
        token=token,
        branch=settings.branch,
        root=settings.data_root,
        api_url=settings.api_url,
        raw_url=settings.raw_url,
        read_only=token is None,
        # Anonymous reads go to raw.githubusercontent.com by branch name, which
        # costs no API quota (GitHub may cache those for a few minutes).
        pin_ref=False,
        concurrency=8,
        indexes={MEDIA: ["album"]},
        manifests=[ALBUMS, MEDIA],
    )


def safe_media_path(settings: Settings, path: Any) -> Optional[str]:
    """Only serve files below the media root, whatever a document claims."""
    if not isinstance(path, str) or ".." in path.split("/") or "\\" in path:
        return None
    if not path.startswith(settings.media_root + "/"):
        return None
    return path


# ----------------------------------------------------------------- albums
def public_album(album: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": album.get("_id"),
        "title": album.get("title", ""),
        "description": album.get("description", ""),
        "cover": album.get("cover"),
        "created_at": album.get("_created_at"),
        "updated_at": album.get("_updated_at"),
    }


def public_media(item: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": item.get("_id"),
        "album": item.get("album"),
        "title": item.get("title", ""),
        "filename": item.get("filename", ""),
        "kind": item.get("kind"),
        "mime": item.get("mime"),
        "size": item.get("size"),
        "has_thumb": bool(item.get("thumb")),
        "uploaded_by": item.get("uploaded_by"),
        "created_at": item.get("_created_at"),
    }


def list_albums(db: GitDb) -> List[Dict[str, Any]]:
    albums = [public_album(album) for album in db.collection(ALBUMS).all()]
    albums.sort(key=lambda album: album.get("created_at") or "", reverse=True)
    return albums


def get_album(db: GitDb, album_id: str) -> Dict[str, Any]:
    album = db.collection(ALBUMS).get(check_id(album_id, "album"))
    if album is None:
        raise StoreError("album not found", 404)
    return album


def album_media(db: GitDb, album_id: str) -> List[Dict[str, Any]]:
    items = db.collection(MEDIA).find_by("album", album_id)
    items.sort(key=lambda item: str(item.get("_id", "")))
    return items


def create_album(db: GitDb, title: Any, description: Any, actor: str) -> Dict[str, Any]:
    document = {
        "title": clean_text(title, limit=MAX_TITLE, field="title", required=True),
        "description": clean_text(description, limit=MAX_DESCRIPTION, field="description"),
        "cover": None,
        "created_by": actor,
    }
    with WRITE_LOCK:
        album_id = db.collection(ALBUMS).insert(document, message=f"pixel: create album {document['title']}")
    created = db.collection(ALBUMS).get(album_id)
    return created or {**document, "_id": album_id}


def delete_album(db: GitDb, album_id: str) -> None:
    get_album(db, album_id)
    if album_media(db, album_id):
        raise StoreError("remove the photos and videos first", 409)
    with WRITE_LOCK:
        db.collection(ALBUMS).delete(album_id, message=f"pixel: delete album {album_id}")


# ------------------------------------------------------------------ media
def get_media(db: GitDb, media_id: str) -> Dict[str, Any]:
    item = db.collection(MEDIA).get(check_id(media_id, "media"))
    if item is None:
        raise StoreError("photo or video not found", 404)
    return item


def add_media(
    db: GitDb,
    settings: Settings,
    album_id: str,
    *,
    filename: Optional[str],
    data: bytes,
    thumb: Optional[bytes],
    actor: str,
) -> Dict[str, Any]:
    album = get_album(db, album_id)
    if not data:
        raise StoreError("the file is empty")
    if len(data) > settings.max_upload_bytes:
        raise StoreError("the file is too large", 413)
    mime = sniff(data)
    if mime not in MEDIA_TYPES:
        raise StoreError("only photos and videos can be uploaded", 415)
    kind, extension = MEDIA_TYPES[mime]
    if thumb is not None:
        if len(thumb) > MAX_THUMB_BYTES or sniff(thumb) not in THUMB_TYPES:
            thumb = None

    name = clean_filename(filename)
    media_id = new_id()
    folder = f"{settings.media_root}/{album['_id']}"
    path = f"{folder}/{media_id}.{extension}"
    thumb_path = f"{folder}/{media_id}.thumb.jpg" if thumb else None
    files = {path: data}
    if thumb and thumb_path:
        files[thumb_path] = thumb

    document = {
        "album": album["_id"],
        "title": name.rsplit(".", 1)[0][:MAX_TITLE] or name,
        "filename": name,
        "kind": kind,
        "mime": mime,
        "size": len(data),
        "path": path,
        "thumb": thumb_path,
        "thumb_mime": sniff(thumb) if thumb else None,
        "uploaded_by": actor,
    }
    with WRITE_LOCK:
        commit_files(db, files, message=f"pixel: upload {name} to {album.get('title', album_id)}")
        db.collection(MEDIA).insert(document, id=media_id, message=f"pixel: add {kind} {media_id}")
        if not album.get("cover"):
            try:
                db.collection(ALBUMS).update(album["_id"], {"cover": media_id})
            except (ConflictError, NotFoundError):
                pass
    return {**document, "_id": media_id}


def delete_media(settings: Settings, db: GitDb, media_id: str) -> Dict[str, Any]:
    item = get_media(db, media_id)
    files: List[str] = []
    for field in ("path", "thumb"):
        value = item.get(field)
        if value is None:
            continue
        path = safe_media_path(settings, value)
        if path is None:
            log.warning("Refusing to delete invalid media %s for %s", field, item["_id"])
            raise StoreError(f"stored media {field} is outside the media directory; delete was aborted", 500)
        files.append(path)
    with WRITE_LOCK:
        db.collection(MEDIA).delete(item["_id"], message=f"pixel: remove {item.get('filename', '')}")
        if files:
            commit_files(db, {}, files, message=f"pixel: delete files of {item['_id']}")
        album_id = item.get("album")
        if isinstance(album_id, str):
            album = db.collection(ALBUMS).get(album_id)
            if album is not None and album.get("cover") == item["_id"]:
                remaining = album_media(db, album_id)
                cover = remaining[0]["_id"] if remaining else None
                db.collection(ALBUMS).update(album_id, {"cover": cover})
    return item

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
from typing import Any, Dict, Iterable, List, Optional, Tuple

from gitdb import (
    CacheEntry,
    ConflictError,
    GitDb,
    GitDbError,
    NotFoundError,
    ValidationError,
    new_id,
    validate_id,
)

from .config import Settings
from .github import commit_files, create_blob, tree_blobs

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
#: Most photos and videos one bulk request (move, delete, ZIP) may name.
MAX_SELECTION = 500

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


def clean_text(value: Any, *, limit: int, field: str, required: bool = False, multiline: bool = False) -> str:
    text = value if isinstance(value, str) else ""
    if multiline:
        # Descriptions and captions keep their line breaks (shown with pre-wrap).
        text = re.sub(r"\r\n?", "\n", text).replace("\t", " ")
        text = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", text)
    else:
        text = re.sub(r"[\x00-\x1f\x7f]", "", text)
    text = text.strip()
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


def clean_ids(ids: Any) -> List[str]:
    """Validate a selection of media ids sent by the browser (duplicates dropped)."""
    if not isinstance(ids, list) or not ids:
        raise StoreError("select at least one photo or video")
    if len(ids) > MAX_SELECTION:
        raise StoreError(f"select at most {MAX_SELECTION} photos and videos at a time")
    try:
        return list(dict.fromkeys(validate_id(value) for value in ids))
    except ValidationError as exc:
        raise StoreError("invalid media id") from exc


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


def media_files(settings: Settings, item: Dict[str, Any], action: str = "delete") -> List[str]:
    """Return the stored file paths of ``item``, refusing any outside the media folder."""
    files: List[str] = []
    for field in ("path", "thumb"):
        value = item.get(field)
        if value is None:
            continue
        path = safe_media_path(settings, value)
        if path is None:
            log.warning("Refusing to %s invalid media %s for %s", action, field, item.get("_id"))
            raise StoreError(
                f"stored media {field} is not a valid path inside the media directory; {action} was aborted",
                500,
            )
        files.append(path)
    return files


def refresh(db: GitDb, collection: str) -> None:
    """Let a signed-in session see documents that other sessions changed.

    Every session keeps its own GitDb cache, which reuses a document body for as
    long as its cached blob sha is unchanged. One tree listing refreshes the
    shas of a whole collection: changed documents are then read again while
    unchanged ones still come from the cache. Anonymous visitors read through
    ETag-revalidated requests and need no refresh.
    """
    if db.read_only:
        return
    try:
        listed = tree_blobs(db, db.collection_path(collection))
    except GitDbError as exc:
        log.warning("could not refresh %s: %s", collection, exc)
        return
    for path, sha in listed.items():
        if path.endswith(".json"):
            db.cache.set(path, CacheEntry(sha=sha))


def album_counts(db: GitDb) -> Optional[Dict[str, int]]:
    """Return ``{album id: number of items}`` from GitDb's album index.

    GitDb has no public reader for its index files, so this uses the cached,
    revalidated read ``find_by`` performs on the same file. ``None`` means the
    counts are unknown.
    """
    try:
        index, _ = db._read(db.index_path(MEDIA, "album"))
    except (AttributeError, GitDbError) as exc:
        log.warning("could not read the album index: %s", exc)
        return None
    values = index.get("values") if isinstance(index, dict) else None
    if not isinstance(values, dict):
        return {}
    return {str(album_id): len(ids) for album_id, ids in values.items() if isinstance(ids, list)}


def _existing_files(db: GitDb, paths: Iterable[str]) -> Dict[str, str]:
    """Return ``{path: blob sha}`` for those of ``paths`` present on the branch."""
    wanted = list(paths)
    found: Dict[str, str] = {}
    for folder in sorted({path.rsplit("/", 1)[0] for path in wanted}):
        found.update(tree_blobs(db, folder))
    return {path: found[path] for path in wanted if path in found}


def _describe(items: List[Dict[str, Any]]) -> str:
    if len(items) == 1:
        return str(items[0].get("filename") or items[0].get("_id"))
    return f"{len(items)} items"


def _pick(items: List[Dict[str, Any]], wanted: List[str]) -> List[Dict[str, Any]]:
    """Return the ``wanted`` items of an album, refusing ids that are not in it."""
    by_id = {str(item.get("_id")): item for item in items}
    missing = sum(1 for media_id in wanted if media_id not in by_id)
    if missing:
        noun = "item is" if missing == 1 else "items are"
        raise StoreError(f"{missing} selected {noun} no longer in this album; reload and try again", 409)
    return [by_id[media_id] for media_id in wanted]


def _first_other(items: List[Dict[str, Any]], gone: Iterable[str]) -> Optional[str]:
    excluded = set(gone)
    left = [str(item["_id"]) for item in items if str(item.get("_id")) not in excluded]
    return left[0] if left else None


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
        "description": item.get("description", ""),
        "filename": item.get("filename", ""),
        "kind": item.get("kind"),
        "mime": item.get("mime"),
        "size": item.get("size"),
        "has_thumb": bool(item.get("thumb")),
        "uploaded_by": item.get("uploaded_by"),
        "created_at": item.get("_created_at"),
    }


def list_albums(db: GitDb) -> List[Dict[str, Any]]:
    refresh(db, ALBUMS)
    counts = album_counts(db)
    albums: List[Dict[str, Any]] = []
    for album in db.collection(ALBUMS).all():
        public = public_album(album)
        public["count"] = None if counts is None else counts.get(str(public["id"]), 0)
        albums.append(public)
    albums.sort(key=lambda album: album.get("created_at") or "", reverse=True)
    return albums


def get_album(db: GitDb, album_id: str) -> Dict[str, Any]:
    album = db.collection(ALBUMS).get(check_id(album_id, "album"))
    if album is None:
        raise StoreError("album not found", 404)
    return album


def album_media(db: GitDb, album_id: str) -> List[Dict[str, Any]]:
    refresh(db, MEDIA)
    items = db.collection(MEDIA).find_by("album", album_id)
    items.sort(key=lambda item: str(item.get("_id", "")))
    return items


def create_album(db: GitDb, title: Any, description: Any, actor: str) -> Dict[str, Any]:
    document = {
        "title": clean_text(title, limit=MAX_TITLE, field="title", required=True),
        "description": clean_text(description, limit=MAX_DESCRIPTION, field="description", multiline=True),
        "cover": None,
        "created_by": actor,
    }
    with WRITE_LOCK:
        album_id = db.collection(ALBUMS).insert(document, message=f"pixel: create album {document['title']}")
    created = db.collection(ALBUMS).get(album_id)
    return created or {**document, "_id": album_id}


def update_album(db: GitDb, album_id: str, changes: Dict[str, Any]) -> Dict[str, Any]:
    """Change any of an album's ``title``, ``description`` and ``cover``."""
    patch: Dict[str, Any] = {}
    if "title" in changes:
        patch["title"] = clean_text(changes["title"], limit=MAX_TITLE, field="title", required=True)
    if "description" in changes:
        patch["description"] = clean_text(
            changes["description"], limit=MAX_DESCRIPTION, field="description", multiline=True
        )
    cover = changes.get("cover")
    if cover is not None:
        try:
            cover = validate_id(cover)
        except ValidationError as exc:
            raise StoreError("the cover must be a photo or video in this album") from exc
    with WRITE_LOCK:
        album = get_album(db, album_id)
        if "cover" in changes:
            if cover is not None:
                item = db.collection(MEDIA).get(cover)
                if item is None or item.get("album") != album["_id"]:
                    raise StoreError("the cover must be a photo or video in this album")
            patch["cover"] = cover
        # A missing field and an empty one are the same to visitors.
        patch = {key: value for key, value in patch.items() if (album.get(key) or None) != (value or None)}
        if not patch:
            return album
        title = patch.get("title") or album.get("title") or album["_id"]
        return db.collection(ALBUMS).update(album["_id"], patch, message=f"pixel: edit album {title}")


def delete_album(settings: Settings, db: GitDb, album_id: str, *, with_media: bool = False) -> int:
    """Delete an album; ``with_media`` also deletes everything in it.

    Returns how many photos and videos were deleted along with the album.
    """
    with WRITE_LOCK:
        album = get_album(db, album_id)
        items = album_media(db, album["_id"])
        if items and not with_media:
            raise StoreError("remove the photos and videos first", 409)
        files = [path for item in items for path in media_files(settings, item)]
        title = album.get("title") or album["_id"]
        if items:
            batch = db.batch(f"pixel: delete album {title} with {_describe(items)}")
            for item in items:
                batch.delete(MEDIA, item["_id"])
            batch.delete(ALBUMS, album["_id"])
            batch.commit()
        else:
            db.collection(ALBUMS).delete(album["_id"], message=f"pixel: delete album {album_id}")
        # The whole folder goes, including files an interrupted earlier change left behind.
        folder = f"{settings.media_root}/{album['_id']}"
        stale = set(tree_blobs(db, folder))
        elsewhere = [path for path in files if not path.startswith(folder + "/")]
        stale.update(_existing_files(db, elsewhere))
        if stale:
            commit_files(db, {}, sorted(stale), message=f"pixel: delete files of album {title}")
    return len(items)


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
    # Uploading the bytes needs no lock, so parallel uploads overlap here and
    # only the short commit step below is serialized.
    blobs = {target: create_blob(db, content) for target, content in files.items()}
    with WRITE_LOCK:
        album = get_album(db, album_id)  # it may have been deleted during the upload
        commit_files(db, {}, message=f"pixel: upload {name} to {album.get('title', album_id)}", blobs=blobs)
        db.collection(MEDIA).insert(document, id=media_id, message=f"pixel: add {kind} {media_id}")
        if not album.get("cover"):
            try:
                db.collection(ALBUMS).update(album["_id"], {"cover": media_id})
            except (ConflictError, NotFoundError):
                pass
    return {**document, "_id": media_id}


def update_media(db: GitDb, media_id: str, changes: Dict[str, Any]) -> Dict[str, Any]:
    """Change a photo or video's ``title`` and/or ``description`` (its caption)."""
    patch: Dict[str, Any] = {}
    if "title" in changes:
        patch["title"] = clean_text(changes["title"], limit=MAX_TITLE, field="title", required=True)
    if "description" in changes:
        patch["description"] = clean_text(
            changes["description"], limit=MAX_DESCRIPTION, field="description", multiline=True
        )
    with WRITE_LOCK:
        item = get_media(db, media_id)
        patch = {key: value for key, value in patch.items() if (item.get(key) or "") != value}
        if not patch:
            return item
        return db.collection(MEDIA).update(item["_id"], patch, message=f"pixel: edit {_describe([item])}")


def _remove(
    settings: Settings,
    db: GitDb,
    album: Optional[Dict[str, Any]],
    items: List[Dict[str, Any]],
    siblings: Optional[List[Dict[str, Any]]] = None,
) -> None:
    """Delete ``items``: documents (and a stale cover) in one commit, then their files."""
    files = [path for item in items for path in media_files(settings, item)]
    gone = [str(item["_id"]) for item in items]
    label = _describe(items)
    batch = db.batch(f"pixel: remove {label}")
    for item in items:
        batch.delete(MEDIA, item["_id"])
    if album is not None and album.get("cover") in gone:
        others = siblings if siblings is not None else album_media(db, album["_id"])
        batch.put(ALBUMS, album["_id"], {**album, "cover": _first_other(others, gone)})
    batch.commit()
    existing = _existing_files(db, files)
    if existing:
        commit_files(db, {}, sorted(existing), message=f"pixel: delete files of {label}")


def delete_media(settings: Settings, db: GitDb, media_id: str) -> Dict[str, Any]:
    with WRITE_LOCK:
        item = get_media(db, media_id)
        media_files(settings, item)  # refuse an invalid document before anything is written
        album: Optional[Dict[str, Any]] = None
        album_id = item.get("album")
        if isinstance(album_id, str):
            try:
                album = db.collection(ALBUMS).get(validate_id(album_id))
            except ValidationError:
                album = None
        _remove(settings, db, album, [item])
    return item


def delete_media_many(settings: Settings, db: GitDb, album_id: str, ids: Any) -> List[Dict[str, Any]]:
    """Delete a selection of an album's photos and videos in one go."""
    wanted = clean_ids(ids)
    with WRITE_LOCK:
        album = get_album(db, album_id)
        siblings = album_media(db, album["_id"])
        items = _pick(siblings, wanted)
        _remove(settings, db, album, items, siblings)
    return items


def move_media(
    settings: Settings, db: GitDb, album_id: str, ids: Any, target_id: Any
) -> List[Dict[str, Any]]:
    """Move a selection of photos and videos, files included, to another album.

    This takes three commits, ordered so that a failure at any step leaves at
    most unreferenced copies behind: the files are copied to the target folder
    (existing blobs are reused, so no bytes are uploaded again), then every
    document, the album index and both covers change in a single commit, and
    finally the originals are removed.
    """
    wanted = clean_ids(ids)
    if not isinstance(target_id, str) or not target_id:
        raise StoreError("choose the album to move to")
    with WRITE_LOCK:
        source = get_album(db, album_id)
        target = get_album(db, target_id)
        if target["_id"] == source["_id"]:
            raise StoreError("choose a different album")
        siblings = album_media(db, source["_id"])
        items = _pick(siblings, wanted)
        folder = f"{settings.media_root}/{target['_id']}"
        moves: Dict[str, str] = {}
        for item in items:
            for path in media_files(settings, item, "move"):
                moves[path] = f"{folder}/{path.rsplit('/', 1)[1]}"
        existing = _existing_files(db, moves)
        missing = [path for path in moves if path not in existing]
        if missing:
            log.warning("Refusing to move %d item(s): files missing: %s", len(items), missing)
            raise StoreError(f"{len(missing)} file(s) of the selection are missing; nothing was moved", 409)

        label = _describe(items)
        destination = target.get("title") or target["_id"]
        copies = {moves[path]: sha for path, sha in existing.items()}
        commit_files(db, {}, message=f"pixel: copy {label} to {destination}", blobs=copies)

        moved = [str(item["_id"]) for item in items]
        batch = db.batch(f"pixel: move {label} to {destination}")
        for item in items:
            document = dict(item)
            document["album"] = target["_id"]
            for field in ("path", "thumb"):
                if item.get(field) is not None:
                    document[field] = moves[item[field]]
            batch.put(MEDIA, item["_id"], document)
        if source.get("cover") in moved:
            batch.put(ALBUMS, source["_id"], {**source, "cover": _first_other(siblings, moved)})
        if not target.get("cover"):
            batch.put(ALBUMS, target["_id"], {**target, "cover": min(moved)})
        try:
            batch.commit()
        except GitDbError:
            try:
                commit_files(db, {}, sorted(copies), message=f"pixel: undo copy of {label}")
            except GitDbError as exc:
                log.warning("could not remove the copies of %s after a failed move: %s", label, exc)
            raise
        try:
            commit_files(db, {}, sorted(moves), message=f"pixel: remove moved originals of {label}")
        except GitDbError as exc:
            # The move itself is complete; only unreferenced originals stay behind.
            log.warning("moved %s but could not remove the originals: %s", label, exc)
    return items

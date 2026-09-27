from __future__ import annotations

import json
from dataclasses import replace

from fastapi.testclient import TestClient

from pixel import store
from pixel.app import create_app
from pixel.config import Settings
from tests.conftest import (
    AUDIT_REPO,
    DATA_REPO,
    FRIEND,
    OWNER,
    STRANGER,
    jpeg,
    make_client,
    mp4,
    sign_in,
)
from tests.fake_github import FakeGitHub


def create_album(client: TestClient, title: str = "Holidays") -> str:
    response = client.post("/api/albums", json={"title": title, "description": "Summer"})
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


def upload(client: TestClient, album: str, name: str, data: bytes, thumb: bytes | None = None):
    files = {"file": (name, data, "application/octet-stream")}
    if thumb is not None:
        files["thumb"] = ("thumb.jpg", thumb, "image/jpeg")
    return client.post(f"/api/albums/{album}/media", files=files)


# ---------------------------------------------------------------- privacy
def test_private_repository_hides_everything_from_anonymous_visitors(client: TestClient) -> None:
    session = client.get("/api/session").json()
    assert session["signed_in"] is False
    assert session["access"]["can_view"] is False
    assert client.get("/api/albums").status_code == 401


def test_public_repository_is_readable_but_not_writable_anonymously(
    github: tuple[FakeGitHub, str], settings: Settings
) -> None:
    state, _ = github
    with make_client(settings) as owner:
        sign_in(owner, OWNER)
        album = create_album(owner)
        media = upload(owner, album, "beach.jpg", jpeg(), jpeg("blue", (8, 8))).json()["id"]
    state.repos[DATA_REPO].private = False
    with make_client(settings) as guest:
        assert guest.get("/api/session").json()["access"] == {
            "can_view": True,
            "can_write": False,
            "is_admin": False,
            "private": False,
        }
        albums = guest.get("/api/albums").json()["albums"]
        assert [a["id"] for a in albums] == [album]
        assert guest.get(f"/api/media/{media}/original").content == jpeg()
        assert guest.post("/api/albums", json={"title": "x"}).status_code == 401
        assert upload(guest, album, "x.jpg", jpeg()).status_code == 401


def test_sign_in_follows_repository_permissions(client: TestClient) -> None:
    assert client.post("/api/login", json={"token": "nope"}).status_code == 401
    denied = client.post("/api/login", json={"token": STRANGER})
    assert denied.status_code == 403
    assert "stranger" in denied.json()["detail"]
    sign_in(client, FRIEND)
    session = client.get("/api/session").json()
    assert session["user"]["login"] == "friend"
    assert session["access"]["can_view"] is True
    assert session["access"]["can_write"] is False
    assert client.get("/api/albums").json() == {"albums": []}
    assert client.post("/api/albums", json={"title": "Mine"}).status_code == 403


def test_session_cookie_is_http_only_and_token_is_never_returned(client: TestClient) -> None:
    response = client.post("/api/login", json={"token": OWNER})
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=strict" in cookie
    assert OWNER not in response.text
    assert OWNER not in client.get("/api/session").text


def test_revoked_access_takes_effect(github: tuple[FakeGitHub, str], fast_permissions: Settings) -> None:
    state, _ = github
    with make_client(fast_permissions) as owner:
        sign_in(owner, OWNER)
        album = create_album(owner)
        media = upload(owner, album, "a.jpg", jpeg()).json()["id"]
    with make_client(fast_permissions) as friend:
        sign_in(friend, FRIEND)
        assert friend.get(f"/api/media/{media}/download").status_code == 200
        del state.repos[DATA_REPO].roles["friend"]
        assert friend.get(f"/api/albums/{album}").status_code == 403
        assert friend.get(f"/api/media/{media}/download").status_code == 403


def test_mutations_require_the_csrf_header(settings: Settings) -> None:
    with TestClient(create_app(settings)) as bare:
        assert bare.post("/api/login", json={"token": OWNER}).status_code == 403
        bare.post("/api/login", json={"token": OWNER}, headers={"X-Pixel": "1"})
        assert bare.post("/api/albums", json={"title": "x"}).status_code == 403


# ---------------------------------------------------------- albums/media
def test_upload_commits_photo_thumbnail_and_gitdb_documents(
    github: tuple[FakeGitHub, str], client: TestClient
) -> None:
    state, _ = github
    sign_in(client, OWNER)
    album = create_album(client)
    photo = jpeg()
    thumb = jpeg("green", (16, 12))
    response = upload(client, album, "Beach day.jpg", photo, thumb)
    assert response.status_code == 201, response.text
    item = response.json()
    assert item["kind"] == "photo" and item["mime"] == "image/jpeg" and item["has_thumb"]
    assert item["uploaded_by"] == "charles2ke"

    files = state.files(DATA_REPO)
    assert files[f"media/{album}/{item['id']}.jpg"] == photo
    assert files[f"media/{album}/{item['id']}.thumb.jpg"] == thumb
    document = json.loads(files[f"data/media/{item['id']}.json"])
    assert document["album"] == album and document["filename"] == "Beach day.jpg"
    assert json.loads(files[f"data/albums/{album}.json"])["cover"] == item["id"]
    assert "data/_index/media/album.json" in files

    detail = client.get(f"/api/albums/{album}").json()
    assert detail["title"] == "Holidays"
    assert [i["id"] for i in detail["items"]] == [item["id"]]
    albums = client.get("/api/albums").json()["albums"]
    assert albums[0]["cover"] == item["id"]

    thumb_response = client.get(f"/api/media/{item['id']}/thumb")
    assert thumb_response.content == thumb
    assert thumb_response.headers["content-type"] == "image/jpeg"
    original = client.get(f"/api/media/{item['id']}/original")
    assert original.content == photo
    assert original.headers["content-disposition"].startswith("inline")
    download = client.get(f"/api/media/{item['id']}/download")
    assert download.content == photo
    assert download.headers["content-disposition"].startswith("attachment")
    assert "Beach%20day.jpg" in download.headers["content-disposition"]


def test_video_supports_range_requests_for_playback(client: TestClient) -> None:
    sign_in(client, OWNER)
    album = create_album(client)
    video = mp4()
    item = upload(client, album, "clip.mp4", video).json()
    assert item["kind"] == "video" and item["mime"] == "video/mp4" and not item["has_thumb"]
    partial = client.get(f"/api/media/{item['id']}/original", headers={"Range": "bytes=100-199"})
    assert partial.status_code == 206
    assert partial.content == video[100:200]
    assert partial.headers["content-range"] == f"bytes 100-199/{len(video)}"
    assert partial.headers["content-type"] == "video/mp4"
    assert partial.headers["accept-ranges"] == "bytes"
    assert client.get(f"/api/media/{item['id']}/thumb").status_code == 404


def test_rejects_files_that_are_not_photos_or_videos(client: TestClient) -> None:
    sign_in(client, OWNER)
    album = create_album(client)
    assert upload(client, album, "evil.html", b"<script>alert(1)</script>").status_code == 415
    svg = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
    assert upload(client, album, "evil.svg", svg).status_code == 415
    assert upload(client, album, "empty.jpg", b"").status_code == 422


def test_rejects_uploads_above_the_limit(settings: Settings) -> None:
    with make_client(replace(settings, max_upload_bytes=1024)) as client:
        sign_in(client, OWNER)
        album = create_album(client)
        assert upload(client, album, "big.mp4", mp4(8192)).status_code == 413


def test_bad_thumbnail_is_dropped_not_stored(client: TestClient) -> None:
    sign_in(client, OWNER)
    album = create_album(client)
    item = upload(client, album, "a.jpg", jpeg(), b"<html>").json()
    assert item["has_thumb"] is False


def test_delete_media_and_album(github: tuple[FakeGitHub, str], client: TestClient) -> None:
    state, _ = github
    sign_in(client, OWNER)
    album = create_album(client)
    first = upload(client, album, "a.jpg", jpeg(), jpeg("blue", (8, 8))).json()["id"]
    second = upload(client, album, "b.jpg", jpeg("yellow")).json()["id"]
    assert client.delete(f"/api/albums/{album}").status_code == 409

    assert client.delete(f"/api/media/{first}").status_code == 200
    files = state.files(DATA_REPO)
    assert not any(path.startswith(f"media/{album}/{first}") for path in files)
    assert json.loads(files[f"data/albums/{album}.json"])["cover"] == second

    assert client.delete(f"/api/media/{second}").status_code == 200
    assert client.delete(f"/api/albums/{album}").status_code == 200
    assert client.get(f"/api/albums/{album}").status_code == 404


def test_invalid_ids_are_rejected(client: TestClient) -> None:
    sign_in(client, OWNER)
    assert client.get("/api/albums/..%2F..%2Fetc").status_code == 404
    assert client.get("/api/media/bad..id/original").status_code == 404


def test_media_paths_outside_the_media_root_are_never_served(settings: Settings) -> None:
    assert store.safe_media_path(settings, "media/a/b.jpg") == "media/a/b.jpg"
    assert store.safe_media_path(settings, "data/albums/x.json") is None
    assert store.safe_media_path(settings, "media/../data/x.json") is None
    assert store.safe_media_path(settings, None) is None


def test_sniffing() -> None:
    assert store.sniff(jpeg()) == "image/jpeg"
    assert store.sniff(b"\x89PNG\r\n\x1a\n....") == "image/png"
    assert store.sniff(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == "image/webp"
    assert store.sniff(b"\x00\x00\x00\x18ftypheic....") == "image/heic"
    assert store.sniff(b"\x00\x00\x00\x14ftypqt  ....") == "video/quicktime"
    assert store.sniff(b"\x1a\x45\xdf\xa3....") == "video/webm"
    assert store.sniff(b"<svg") is None


# ------------------------------------------------------------------ audit
def test_visitors_are_audited_to_the_audit_repository(
    github: tuple[FakeGitHub, str], settings: Settings
) -> None:
    state, _ = github
    with make_client(settings) as guest:
        guest.get("/api/session?visit=1", headers={"User-Agent": "Guest-Browser"})
        guest.get("/api/albums")
    with make_client(settings) as owner:
        sign_in(owner, OWNER)
        album = create_album(owner)
        media = upload(owner, album, "a.jpg", jpeg()).json()["id"]
        owner.get(f"/api/media/{media}/download")
        events = owner.get("/api/audit").json()["events"]

    kinds = [event["event"] for event in events]
    for expected in ("visit", "access_denied", "sign_in", "create_album", "upload", "download"):
        assert expected in kinds
    visit = next(event for event in events if event["event"] == "visit")
    assert visit["user_agent"] == "Guest-Browser" and "login" not in visit and visit["ip"]
    download = next(event for event in events if event["event"] == "download")
    assert download["login"] == "charles2ke" and download["media"] == media

    audit_files = [path for path in state.files(AUDIT_REPO) if path.startswith("data/audit/")]
    assert len(audit_files) == len(events)
    assert not any(path.startswith("data/audit") for path in state.files(DATA_REPO))


def test_only_admins_can_read_the_audit_log(client: TestClient) -> None:
    assert client.get("/api/audit").status_code == 403
    sign_in(client, FRIEND)
    assert client.get("/api/audit").status_code == 403


def test_audit_is_optional(settings: Settings) -> None:
    with make_client(replace(settings, audit_token=None)) as client:
        assert client.get("/api/session").json()["audit_enabled"] is False
        sign_in(client, OWNER)
        assert client.get("/api/audit").json() == {"enabled": False, "events": []}


# ----------------------------------------------------------------- install
def test_pwa_assets_are_served(client: TestClient) -> None:
    index = client.get("/")
    assert index.status_code == 200
    html = index.text
    for needle in (
        'rel="manifest"',
        'name="apple-mobile-web-app-capable"',
        'rel="apple-touch-icon"',
        "viewport-fit=cover",
    ):
        assert needle in html
    assert "script-src 'self'" in index.headers["content-security-policy"]

    manifest = client.get("/manifest.webmanifest")
    assert manifest.headers["content-type"].startswith("application/manifest+json")
    data = manifest.json()
    assert data["display"] == "standalone" and data["start_url"] == "/"
    sizes = {icon["sizes"] for icon in data["icons"]}
    assert {"192x192", "512x512"} <= sizes
    for icon in data["icons"]:
        assert client.get(icon["src"]).status_code == 200

    worker = client.get("/sw.js")
    assert worker.headers["service-worker-allowed"] == "/"
    assert "/api/" in worker.text
    assert client.get("/apple-touch-icon.png").headers["content-type"] == "image/png"

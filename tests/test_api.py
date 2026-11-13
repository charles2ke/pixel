from __future__ import annotations

import io
import json
import zipfile
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from pixel import archive as archive_names
from pixel import store
from pixel.app import GitDbError, create_app
from pixel.audit import AuditLog
from pixel.config import Settings
from pixel.github import commit_files, repository_access
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


def test_trusted_forwarded_https_sets_a_secure_session_cookie(settings: Settings) -> None:
    with make_client(replace(settings, trust_proxy=True)) as client:
        response = client.post("/api/login", json={"token": OWNER}, headers={"X-Forwarded-Proto": "https"})
    assert "Secure" in response.headers["set-cookie"]


def test_trusted_proxy_falls_back_to_direct_https_for_session_cookie(settings: Settings) -> None:
    with TestClient(
        create_app(replace(settings, trust_proxy=True)),
        base_url="https://testserver",
        headers={"X-Pixel": "1"},
    ) as client:
        response = client.post("/api/login", json={"token": OWNER})
    assert "Secure" in response.headers["set-cookie"]


def test_anonymous_permissions_use_the_configured_ttl(
    github: tuple[FakeGitHub, str], settings: Settings
) -> None:
    state, _ = github
    state.repos[DATA_REPO].private = False
    with make_client(replace(settings, permission_ttl_seconds=0)) as client:
        assert client.get("/api/session").json()["access"]["can_view"] is True
        state.repos[DATA_REPO].private = True
        assert client.get("/api/session").json()["access"]["can_view"] is False


def test_authenticated_access_requires_pull_permission() -> None:
    class Response:
        status_code = 200

        @staticmethod
        def json() -> dict:
            return {"private": True, "permissions": {"admin": False, "push": False, "pull": False}}

    class Client:
        @staticmethod
        def request(*_args: object, **_kwargs: object) -> Response:
            return Response()

    access = repository_access(Client(), DATA_REPO)
    assert access.can_view is False
    assert access.can_write is False


def test_authenticated_write_permission_implies_view_permission() -> None:
    class Response:
        status_code = 200

        @staticmethod
        def json() -> dict:
            return {"private": True, "permissions": {"admin": False, "push": True, "pull": False}}

    class Client:
        @staticmethod
        def request(*_args: object, **_kwargs: object) -> Response:
            return Response()

    access = repository_access(Client(), DATA_REPO)
    assert access.can_view is True
    assert access.can_write is True


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


def test_permission_refresh_error_fails_closed(
    fast_permissions: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    with make_client(fast_permissions) as client:
        sign_in(client, FRIEND)

        def unavailable(*_args: object) -> None:
            raise GitDbError("GitHub temporarily unavailable")

        with monkeypatch.context() as patch:
            patch.setattr("pixel.app.repository_access", unavailable)
            assert client.get("/api/albums").status_code == 403

        assert client.get("/api/albums").status_code == 200


def test_mutations_require_the_csrf_header(settings: Settings) -> None:
    with TestClient(create_app(settings)) as bare:
        assert bare.post("/api/login", json={"token": OWNER}).status_code == 403
        bare.post("/api/login", json={"token": OWNER}, headers={"X-Pixel": "1"})
        assert bare.post("/api/albums", json={"title": "x"}).status_code == 403
        assert bare.patch("/api/albums/a", json={"title": "x"}).status_code == 403
        assert bare.post("/api/albums/a/media/delete", json={"ids": ["b"]}).status_code == 403


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
    assert thumb_response.headers["cache-control"] == "no-store"
    original = client.get(f"/api/media/{item['id']}/original")
    assert original.content == photo
    assert original.headers["content-disposition"].startswith("inline")
    assert original.headers["cache-control"] == "no-store"
    download = client.get(f"/api/media/{item['id']}/download")
    assert download.content == photo
    assert download.headers["content-disposition"].startswith("attachment")
    assert download.headers["cache-control"] == "no-store"
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


# ------------------------------------------------------ editing and bulk
def test_album_title_description_and_cover_can_be_edited(client: TestClient) -> None:
    sign_in(client, OWNER)
    album = create_album(client)
    upload(client, album, "a.jpg", jpeg())
    second = upload(client, album, "b.jpg", jpeg("blue")).json()["id"]
    foreign = upload(client, create_album(client, "Other"), "c.jpg", jpeg("green")).json()["id"]

    edited = client.patch(
        f"/api/albums/{album}", json={"title": " Rome ", "description": "Day 1\r\nDay 2", "cover": second}
    )
    assert edited.status_code == 200, edited.text
    assert [edited.json()[key] for key in ("title", "description", "cover")] == [
        "Rome",
        "Day 1\nDay 2",
        second,
    ]
    listed = {entry["id"]: entry for entry in client.get("/api/albums").json()["albums"]}
    assert (listed[album]["title"], listed[album]["cover"], listed[album]["count"]) == ("Rome", second, 2)

    for invalid in ({"cover": foreign}, {"cover": "../x"}, {"title": " "}, {}):
        assert client.patch(f"/api/albums/{album}", json=invalid).status_code == 422
    assert client.patch(f"/api/albums/{album}", json={"cover": None}).json()["cover"] is None


def test_photo_title_and_caption_can_be_edited(github: tuple[FakeGitHub, str], client: TestClient) -> None:
    state, _ = github
    sign_in(client, OWNER)
    album = create_album(client)
    item = upload(client, album, "IMG_1.jpg", jpeg()).json()
    assert (item["title"], item["description"]) == ("IMG_1", "")

    response = client.patch(
        f"/api/media/{item['id']}", json={"title": "Sunset", "description": "From the pier"}
    )
    assert response.status_code == 200, response.text
    shown = client.get(f"/api/albums/{album}").json()["items"][0]
    assert (shown["title"], shown["description"]) == ("Sunset", "From the pier")
    document = json.loads(state.files(DATA_REPO)[f"data/media/{item['id']}.json"])
    assert document["description"] == "From the pier" and document["path"].endswith(".jpg")
    assert client.patch(f"/api/media/{item['id']}", json={"description": "x" * 1001}).status_code == 422

    # Captions keep their line breaks; tabs become spaces and other control characters go.
    caption = client.patch(
        f"/api/media/{item['id']}", json={"description": " Day one\r\nAt the\tbeach\x07\n"}
    )
    assert caption.json()["description"] == "Day one\nAt the beach"


def test_moving_media_moves_files_documents_and_covers(
    github: tuple[FakeGitHub, str], client: TestClient
) -> None:
    state, _ = github
    sign_in(client, OWNER)
    source = create_album(client, "Source")
    target = create_album(client, "Target")
    photo, thumb = jpeg(), jpeg("green", (8, 8))
    moved = upload(client, source, "a.jpg", photo, thumb).json()["id"]
    kept = upload(client, source, "b.jpg", jpeg("blue")).json()["id"]

    response = client.post(f"/api/albums/{source}/media/move", json={"ids": [moved], "album": target})
    assert response.json() == {"moved": 1}, response.text

    files = state.files(DATA_REPO)
    assert files[f"media/{target}/{moved}.jpg"] == photo
    assert files[f"media/{target}/{moved}.thumb.jpg"] == thumb
    assert not any(path.startswith(f"media/{source}/{moved}") for path in files)
    assert json.loads(files[f"data/media/{moved}.json"])["album"] == target
    index = json.loads(files["data/_index/media/album.json"])["values"]
    assert (index[source], index[target]) == ([kept], [moved])
    albums = {entry["id"]: entry for entry in client.get("/api/albums").json()["albums"]}
    assert (albums[source]["cover"], albums[target]["cover"]) == (kept, moved)
    assert [item["id"] for item in client.get(f"/api/albums/{target}").json()["items"]] == [moved]
    assert client.get(f"/api/media/{moved}/original").content == photo

    move = f"/api/albums/{source}/media/move"
    assert client.post(move, json={"ids": [kept], "album": source}).status_code == 422
    assert client.post(move, json={"ids": [moved], "album": target}).status_code == 409
    assert client.post(move, json={"ids": [kept], "album": "missing"}).status_code == 404
    assert client.post(move, json={"ids": [], "album": target}).status_code == 422


def test_selected_media_can_be_deleted_together(github: tuple[FakeGitHub, str], client: TestClient) -> None:
    state, _ = github
    sign_in(client, OWNER)
    album = create_album(client)
    ids = [
        upload(client, album, f"{color}.jpg", jpeg(color)).json()["id"] for color in ("red", "blue", "green")
    ]

    response = client.post(f"/api/albums/{album}/media/delete", json={"ids": [ids[0], ids[2]]})
    assert response.json() == {"deleted": 2}, response.text
    files = state.files(DATA_REPO)
    assert [path for path in files if path.startswith(f"media/{album}/")] == [f"media/{album}/{ids[1]}.jpg"]
    assert [path for path in files if path.startswith("data/media/")] == [f"data/media/{ids[1]}.json"]
    detail = client.get(f"/api/albums/{album}").json()
    assert detail["cover"] == ids[1] and [item["id"] for item in detail["items"]] == [ids[1]]
    assert client.post(f"/api/albums/{album}/media/delete", json={"ids": [ids[0]]}).status_code == 409


def test_an_album_can_be_deleted_with_everything_in_it(
    github: tuple[FakeGitHub, str], client: TestClient
) -> None:
    state, _ = github
    sign_in(client, OWNER)
    album = create_album(client)
    keep = create_album(client, "Keep")
    upload(client, album, "a.jpg", jpeg(), jpeg("blue", (8, 8)))
    upload(client, album, "b.mp4", mp4())
    kept = upload(client, keep, "c.jpg", jpeg()).json()["id"]
    assert client.delete(f"/api/albums/{album}").status_code == 409

    response = client.delete(f"/api/albums/{album}?with_media=true")
    assert response.json() == {"ok": True, "deleted": 2}, response.text
    files = state.files(DATA_REPO)
    assert not any(path.startswith(f"media/{album}/") for path in files)
    assert [path for path in files if path.startswith("data/media/")] == [f"data/media/{kept}.json"]
    assert client.get(f"/api/albums/{album}").status_code == 404
    assert [(entry["id"], entry["count"]) for entry in client.get("/api/albums").json()["albums"]] == [
        (keep, 1)
    ]


def test_albums_and_selections_download_as_zip(
    github: tuple[FakeGitHub, str], settings: Settings, client: TestClient
) -> None:
    sign_in(client, OWNER)
    album = create_album(client, "Summer: best/of")
    first = upload(client, album, "IMG.jpg", jpeg()).json()["id"]
    second = upload(client, album, "img.JPG", jpeg("blue")).json()["id"]
    video = upload(client, album, "clip.mp4", mp4()).json()["id"]

    response = client.get(f"/api/albums/{album}/download")
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/zip"
    assert 'attachment; filename="Summer_ best_of.zip"' in response.headers["content-disposition"]
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert archive.testzip() is None
        assert archive.namelist() == ["IMG.jpg", "img (2).JPG", "clip.mp4"]
        assert archive.read("img (2).JPG") == jpeg("blue") and archive.read("clip.mp4") == mp4()

    url = client.post(f"/api/albums/{album}/download", json={"ids": [video, first]}).json()["url"]
    with zipfile.ZipFile(io.BytesIO(client.get(url).content)) as archive:
        assert archive.namelist() == ["clip.mp4", "IMG.jpg"]
    assert client.get(f"/api/albums/{album}/download?selection=unknown").status_code == 404
    with make_client(settings) as other:
        sign_in(other, OWNER)
        assert other.get(url).status_code == 404

    commit_files(store.open_db(settings, OWNER), {}, [f"media/{album}/{second}.jpg"], message="lose a file")
    with zipfile.ZipFile(io.BytesIO(client.get(f"/api/albums/{album}/download").content)) as archive:
        assert archive.namelist() == ["IMG.jpg", "clip.mp4", "MISSING.txt"]
        assert "img (2).JPG" in archive.read("MISSING.txt").decode()


def test_viewers_can_download_but_not_change_albums(settings: Settings) -> None:
    with make_client(settings) as owner:
        sign_in(owner, OWNER)
        album = create_album(owner)
        other = create_album(owner, "Other")
        item = upload(owner, album, "a.jpg", jpeg()).json()["id"]
    with make_client(settings) as friend:
        sign_in(friend, FRIEND)
        attempts = [
            friend.patch(f"/api/albums/{album}", json={"title": "x"}),
            friend.patch(f"/api/media/{item}", json={"title": "x"}),
            friend.post(f"/api/albums/{album}/media/move", json={"ids": [item], "album": other}),
            friend.post(f"/api/albums/{album}/media/delete", json={"ids": [item]}),
            friend.delete(f"/api/albums/{album}?with_media=true"),
        ]
        assert [attempt.status_code for attempt in attempts] == [403] * len(attempts)
        assert friend.get(f"/api/albums/{album}/download").status_code == 200
    with make_client(settings) as guest:
        assert guest.get(f"/api/albums/{album}/download").status_code == 401


def test_changes_made_in_another_session_are_seen(settings: Settings) -> None:
    with make_client(settings) as first, make_client(settings) as second:
        sign_in(first, OWNER)
        sign_in(second, OWNER)
        album = create_album(first)
        item = upload(first, album, "a.jpg", jpeg()).json()["id"]
        assert first.get("/api/albums").json()["albums"][0]["count"] == 1
        assert first.get(f"/api/albums/{album}").json()["items"][0]["title"] == "a"

        second.patch(f"/api/albums/{album}", json={"title": "Renamed"})
        second.patch(f"/api/media/{item}", json={"title": "Sunset"})
        upload(second, album, "b.jpg", jpeg("blue"))

        listed = first.get("/api/albums").json()["albums"][0]
        assert (listed["title"], listed["count"]) == ("Renamed", 2)
        assert first.get(f"/api/albums/{album}").json()["items"][0]["title"] == "Sunset"


def test_archive_member_names_are_portable_and_unique() -> None:
    assert archive_names.safe_name('a<b>:c"d/e\\f|g?h*.jpg') == "a_b__c_d_e_f_g_h_.jpg"
    assert archive_names.safe_name(" .. ", "fallback") == "fallback"
    assert archive_names.safe_name("con.txt") == "_con.txt"
    long_name = archive_names.safe_name("x" * 300 + ".jpeg")
    assert len(long_name) == 150 and long_name.endswith("x.jpeg")
    names = archive_names.unique_names(["a.jpg", "A.JPG", "a.jpg", "b"])
    assert names == ["a.jpg", "A (2).JPG", "a (3).jpg", "b"]


def test_delete_media_validates_document_paths(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    deleted: list[str] = []

    class Collection:
        @staticmethod
        def delete(*_args: object, **_kwargs: object) -> None:
            pass

    class Db:
        @staticmethod
        def collection(*_args: object) -> Collection:
            return Collection()

    monkeypatch.setattr(
        store,
        "get_media",
        lambda *_args: {"_id": "media", "path": "data/albums/secret.json", "thumb": "media/a/thumb.jpg"},
    )
    monkeypatch.setattr(store, "commit_files", lambda _db, _puts, paths, **_kwargs: deleted.extend(paths))
    with pytest.raises(store.StoreError, match="stored media path is not a valid path"):
        store.delete_media(settings, Db(), "media")
    assert deleted == []


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
        assert client.get("/api/audit").json() == {"enabled": False, "events": [], "next": None}


def test_visitor_log_can_be_filtered_and_paged(settings: Settings) -> None:
    with make_client(settings) as guest:
        guest.get("/api/session?visit=1")
    with make_client(settings) as owner:
        sign_in(owner, OWNER)
        upload(owner, create_album(owner), "a.jpg", jpeg())
        everything = owner.get("/api/audit").json()
        assert len(everything["events"]) >= 4 and everything["next"] is None

        first = owner.get("/api/audit?limit=2").json()
        assert first["events"] == everything["events"][:2]
        second = owner.get(f"/api/audit?limit=2&before={first['next']}").json()
        assert second["events"] == everything["events"][2:4]

        uploads = owner.get("/api/audit?event=upload").json()["events"]
        assert [event["event"] for event in uploads] == ["upload"]
        mine = owner.get("/api/audit?who=@Charles2KE").json()["events"]
        assert mine and all(event.get("login") == "charles2ke" for event in mine)
        assert len(mine) < len(everything["events"])


def test_visitor_log_keeps_the_order_of_events_flushed_together(settings: Settings) -> None:
    audit = AuditLog(settings)
    for number in range(60):
        audit.record("view_album", n=number)
    assert audit.flush() == 60
    events, _ = audit.recent(limit=60)
    assert [event["n"] for event in events] == list(range(59, -1, -1))


def test_public_audit_repository_prevents_startup(github: tuple[FakeGitHub, str], settings: Settings) -> None:
    state, _ = github
    state.repos[AUDIT_REPO].private = False
    with pytest.raises(RuntimeError, match="must be a private repository"):
        with make_client(settings):
            pass


def test_unverified_audit_repository_prevents_startup(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unavailable(*_args: object) -> None:
        raise GitDbError("GitHub temporarily unavailable")

    monkeypatch.setattr("pixel.audit.repository_access", unavailable)
    with pytest.raises(RuntimeError, match="could not verify audit repository"):
        AuditLog(settings).start()


def test_a_video_view_is_audited_once_despite_range_probes(client: TestClient) -> None:
    sign_in(client, OWNER)
    item = upload(client, create_album(client), "clip.mp4", mp4()).json()["id"]
    for byte_range in ("bytes=0-1", "bytes=0-", "bytes=100-199"):
        client.get(f"/api/media/{item}/original", headers={"Range": byte_range})
    views = [e for e in client.get("/api/audit").json()["events"] if e["event"] == "view_media"]
    assert len(views) == 1


def test_audit_log_must_not_share_the_album_repository(settings: Settings) -> None:
    with pytest.raises(RuntimeError, match="PIXEL_AUDIT_REPO"):
        create_app(replace(settings, audit_repo=None))
    with pytest.raises(RuntimeError, match="PIXEL_AUDIT_REPO"):
        create_app(replace(settings, audit_repo=settings.repo))


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

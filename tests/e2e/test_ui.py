"""End-to-end browser tests: Pixel + a fake GitHub, driven by Playwright.

Screenshots of every step are written to ``$PIXEL_SCREENSHOTS`` (default
``test-results/screenshots``).
"""

from __future__ import annotations

import base64
import io
import os
from pathlib import Path
from typing import Iterator

import pytest
from PIL import Image, ImageDraw

pytest.importorskip("playwright")

from playwright.sync_api import Browser, BrowserContext, Page, expect  # noqa: E402

from pixel.app import create_app  # noqa: E402
from pixel.config import Settings  # noqa: E402
from tests.conftest import AUDIT, AUDIT_REPO, DATA_REPO, FRIEND, OWNER, STRANGER  # noqa: E402
from tests.fake_github import FakeGitHub, ServerThread, build_app  # noqa: E402

SHOTS = Path(os.environ.get("PIXEL_SCREENSHOTS", "test-results/screenshots"))

IPHONE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1"
)
IPAD_UA = (
    "Mozilla/5.0 (iPad; CPU OS 17_5 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1"
)
ANDROID_UA = (
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Mobile Safari/537.36"
)


def shot(page: Page, name: str, *, full_page: bool = False) -> None:
    SHOTS.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(SHOTS / f"{name}.png"), full_page=full_page)


def photo(seed: int) -> bytes:
    palettes = [
        ((255, 153, 102), (255, 94, 98), (60, 40, 90)),
        ((86, 204, 242), (47, 128, 237), (20, 40, 80)),
        ((168, 224, 99), (86, 171, 47), (30, 60, 30)),
        ((250, 208, 196), (255, 154, 158), (90, 50, 70)),
    ]
    top, bottom, hill = palettes[seed % len(palettes)]
    width, height = 1200, 800
    image = Image.new("RGB", (width, height))
    draw = ImageDraw.Draw(image)
    for y in range(height):
        t = y / height
        draw.line(
            [(0, y), (width, y)],
            fill=tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3)),
        )
    draw.ellipse([820 - seed * 90, 140, 960 - seed * 90, 280], fill=(255, 245, 220))
    draw.polygon([(0, 800), (300 + seed * 40, 420), (620, 800)], fill=hill)
    draw.polygon([(380, 800), (820 - seed * 30, 360), (1200, 800)], fill=tuple(c // 2 for c in hill))
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=88)
    return buffer.getvalue()


RECORD_WEBM = """async () => {
  const canvas = document.createElement('canvas');
  canvas.width = 320; canvas.height = 240;
  const ctx = canvas.getContext('2d');
  const stream = canvas.captureStream(30);
  const recorder = new MediaRecorder(stream, { mimeType: 'video/webm' });
  const chunks = [];
  recorder.ondataavailable = (e) => e.data.size && chunks.push(e.data);
  let frame = 0;
  const timer = setInterval(() => {
    frame += 1;
    ctx.fillStyle = `hsl(${(frame * 6) % 360}, 70%, 45%)`;
    ctx.fillRect(0, 0, 320, 240);
    ctx.fillStyle = '#fff';
    ctx.font = 'bold 48px sans-serif';
    ctx.fillText('Pixel ' + frame, 40, 140);
  }, 33);
  recorder.start(100);
  await new Promise((r) => setTimeout(r, 2500));
  recorder.stop();
  await new Promise((r) => (recorder.onstop = r));
  clearInterval(timer);
  const blob = new Blob(chunks, { type: 'video/webm' });
  const bytes = new Uint8Array(await blob.arrayBuffer());
  let binary = '';
  for (let i = 0; i < bytes.length; i += 0x8000) binary += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  return btoa(binary);
}"""


@pytest.fixture(scope="module")
def stack() -> Iterator[tuple[FakeGitHub, str]]:
    state = FakeGitHub()
    state.add_user(OWNER, "charles2ke")
    state.add_user(FRIEND, "friend")
    state.add_user(STRANGER, "stranger")
    state.add_user(AUDIT, "pixel-audit-bot")
    state.add_repo(DATA_REPO, private=True, roles={"charles2ke": "admin", "friend": "read"})
    state.add_repo(AUDIT_REPO, private=True, roles={"charles2ke": "admin", "pixel-audit-bot": "write"})
    with ServerThread(build_app(state)) as github:
        settings = Settings(
            repo=DATA_REPO,
            api_url=github.url,
            raw_url=github.url + "/raw",
            audit_repo=AUDIT_REPO,
            audit_token=AUDIT,
            audit_flush_seconds=1,
        )
        with ServerThread(create_app(settings)) as pixel:
            yield state, pixel.url


def new_context(browser: Browser, base: str, **kwargs: object) -> BrowserContext:
    return browser.new_context(base_url=base, **kwargs)  # type: ignore[arg-type]


def sign_in(page: Page, token: str) -> None:
    page.get_by_role("button", name="Sign in").first.click()
    page.get_by_label("GitHub token").fill(token)
    page.locator("#signin-submit").click()


@pytest.fixture(scope="module")
def album_url(stack: tuple[FakeGitHub, str], browser: Browser) -> Iterator[str]:
    """The owner creates an album and uploads photos and a video through the UI."""
    _, base = stack
    context = new_context(browser, base, viewport={"width": 1280, "height": 860})
    page = context.new_page()
    page.goto("/")
    expect(page.get_by_role("heading", name="This album is private")).to_be_visible()
    shot(page, "01-private-repo-locked")

    page.get_by_role("button", name="Sign in").first.click()
    page.get_by_label("GitHub token").fill(OWNER)
    shot(page, "02-sign-in-with-github")
    page.locator("#signin-submit").click()
    expect(page.locator("#user-chip")).to_contain_text("@charles2ke")

    page.get_by_role("button", name="+ New album").click()
    page.get_by_label("Title").fill("Summer 2026")
    page.get_by_label("Description").fill("Beach days and sunsets")
    page.get_by_role("button", name="Create").click()
    expect(page.get_by_role("heading", name="Summer 2026")).to_be_visible()

    video = base64.b64decode(page.evaluate(RECORD_WEBM))
    files = [{"name": f"sunset-{i}.jpg", "mimeType": "image/jpeg", "buffer": photo(i)} for i in range(4)]
    files.append({"name": "waves.webm", "mimeType": "video/webm", "buffer": video})
    page.locator("#upload-input").set_input_files(files)
    expect(page.locator(".tile")).to_have_count(5, timeout=30000)
    expect(page.locator(".tile.video .play")).to_have_count(1)
    # Every tile, including the video, got a client-side thumbnail.
    expect(page.locator(".tile img")).to_have_count(5)
    page.wait_for_function(
        "() => [...document.querySelectorAll('.tile img')].every((i) => i.complete && i.naturalWidth > 0)"
    )
    shot(page, "03-album-thumbnails")
    url = page.url
    context.close()
    yield url


def test_owner_views_photo_plays_video_and_downloads(
    stack: tuple[FakeGitHub, str], browser: Browser, album_url: str
) -> None:
    state, base = stack
    context = new_context(browser, base, viewport={"width": 1280, "height": 860})
    page = context.new_page()
    page.goto("/")
    sign_in(page, OWNER)
    expect(page.locator(".album-card")).to_have_count(1)
    page.wait_for_function(
        "() => { const i = document.querySelector('.album-cover img'); return i && i.complete && i.naturalWidth > 0; }"
    )
    shot(page, "04-albums-with-cover")

    page.locator(".album-card").click()
    page.locator(".tile.photo").first.click()
    viewer = page.locator("#lightbox")
    expect(viewer).to_be_visible()
    image = page.locator("#lightbox-stage img")
    expect(image).to_be_visible()
    page.wait_for_function(
        "() => { const i = document.querySelector('#lightbox-stage img'); return i && i.complete && i.naturalWidth === 1200; }"
    )
    shot(page, "05-photo-full-size")

    href = page.locator("#lightbox-download").get_attribute("href")
    assert href and href.endswith("/download")
    download = page.request.get(href)
    assert download.status == 200
    assert download.headers["content-disposition"].startswith("attachment")
    assert download.body()[:3] == b"\xff\xd8\xff"

    page.keyboard.press("Escape")
    page.locator(".tile.video").click()
    video = page.locator("#lightbox-stage video")
    expect(video).to_be_visible()
    page.wait_for_function(
        "() => { const v = document.querySelector('#lightbox-stage video'); return v && v.readyState >= 2 && v.currentTime > 0.3; }",
        timeout=15000,
    )
    shot(page, "06-video-playback")
    page.keyboard.press("Escape")

    # Everything landed in the GitDb repository.
    files = state.files(DATA_REPO)
    assert sum(1 for p in files if p.startswith("media/") and p.endswith(".thumb.jpg")) == 5
    assert sum(1 for p in files if p.startswith("data/media/")) == 5
    assert any(p.endswith(".webm") for p in files)

    page.goto("/#/audit")
    expect(page.get_by_role("heading", name="Visitors")).to_be_visible()
    expect(page.locator("table.audit tbody tr").first).to_be_visible()
    expect(page.locator("table.audit")).to_contain_text("download")
    expect(page.locator("table.audit")).to_contain_text("upload")
    shot(page, "07-visitor-audit-log")
    context.close()


def test_read_only_collaborator_can_view_but_not_upload(
    stack: tuple[FakeGitHub, str], browser: Browser, album_url: str
) -> None:
    _, base = stack
    context = new_context(browser, base, viewport={"width": 1280, "height": 860})
    page = context.new_page()
    page.goto(album_url)
    expect(page.get_by_role("heading", name="This album is private")).to_be_visible()
    sign_in(page, FRIEND)
    expect(page.locator("#user-chip")).to_contain_text("viewer")
    expect(page.locator(".tile")).to_have_count(5)
    expect(page.get_by_text("⬆ Upload")).to_have_count(0)
    expect(page.locator("#audit-link")).to_be_hidden()
    page.locator(".tile.photo").first.click()
    expect(page.locator("#lightbox-download")).to_be_visible()
    expect(page.locator("#lightbox-delete")).to_be_hidden()
    page.keyboard.press("Escape")
    shot(page, "08-read-only-viewer")
    context.close()


def test_account_without_repository_access_is_refused(
    stack: tuple[FakeGitHub, str], browser: Browser, album_url: str
) -> None:
    _, base = stack
    context = new_context(browser, base, viewport={"width": 1280, "height": 860})
    page = context.new_page()
    page.goto("/")
    sign_in(page, STRANGER)
    expect(page.locator("#signin-error")).to_contain_text("@stranger has no access")
    shot(page, "09-no-access-refused")
    # Direct media URLs are refused too.
    assert page.request.get("/api/albums").status == 401
    context.close()


def test_installable_on_iphone(stack: tuple[FakeGitHub, str], browser: Browser, album_url: str) -> None:
    _, base = stack
    context = new_context(
        browser,
        base,
        viewport={"width": 390, "height": 844},
        device_scale_factor=3,
        is_mobile=True,
        has_touch=True,
        user_agent=IPHONE_UA,
    )
    page = context.new_page()
    page.goto(album_url)
    expect(page.locator("#ios-hint")).to_be_visible()
    expect(page.locator("#ios-hint")).to_contain_text("Add to Home Screen")
    assert page.locator('meta[name="apple-mobile-web-app-capable"]').get_attribute("content") == "yes"
    assert page.locator('link[rel="apple-touch-icon"]').count() == 1
    sign_in(page, OWNER)
    expect(page.locator(".tile")).to_have_count(5)
    page.wait_for_function(
        "() => [...document.querySelectorAll('.tile img')].every((i) => i.complete && i.naturalWidth > 0)"
    )
    assert page.evaluate("() => document.documentElement.scrollWidth <= window.innerWidth")
    shot(page, "10-iphone-album")
    page.locator(".tile.photo").first.click()
    page.wait_for_function(
        "() => { const i = document.querySelector('#lightbox-stage img'); return i && i.complete && i.naturalWidth > 0; }"
    )
    shot(page, "11-iphone-photo")
    page.keyboard.press("Escape")
    page.locator(".tile.video").click()
    page.wait_for_function(
        "() => { const v = document.querySelector('#lightbox-stage video'); return v && v.readyState >= 2 && v.currentTime > 0.3; }",
        timeout=15000,
    )
    assert page.locator("#lightbox-stage video").get_attribute("playsinline") is not None
    shot(page, "12-iphone-video")
    context.close()


def test_installable_on_ipad(stack: tuple[FakeGitHub, str], browser: Browser, album_url: str) -> None:
    _, base = stack
    context = new_context(
        browser,
        base,
        viewport={"width": 820, "height": 1180},
        device_scale_factor=2,
        is_mobile=True,
        has_touch=True,
        user_agent=IPAD_UA,
    )
    page = context.new_page()
    page.goto("/")
    expect(page.locator("#ios-hint")).to_be_visible()
    sign_in(page, OWNER)
    expect(page.locator(".album-card")).to_have_count(1)
    page.locator(".album-card").click()
    expect(page.locator(".tile")).to_have_count(5)
    page.wait_for_function(
        "() => [...document.querySelectorAll('.tile img')].every((i) => i.complete && i.naturalWidth > 0)"
    )
    shot(page, "13-ipad-album")
    context.close()


def test_installable_on_android(stack: tuple[FakeGitHub, str], browser: Browser, album_url: str) -> None:
    _, base = stack
    context = new_context(
        browser,
        base,
        viewport={"width": 412, "height": 915},
        device_scale_factor=2.6,
        is_mobile=True,
        has_touch=True,
        user_agent=ANDROID_UA,
    )
    page = context.new_page()
    page.goto("/")
    expect(page.locator("#ios-hint")).to_be_hidden()
    scope = page.evaluate("() => navigator.serviceWorker.ready.then((r) => r.scope)")
    assert scope == base + "/"
    manifest = page.request.get("/manifest.webmanifest").json()
    assert manifest["display"] == "standalone"
    assert any(icon.get("purpose") == "maskable" for icon in manifest["icons"])
    sign_in(page, OWNER)
    expect(page.locator(".album-card")).to_have_count(1)
    page.wait_for_function(
        "() => { const i = document.querySelector('.album-cover img'); return i && i.complete && i.naturalWidth > 0; }"
    )
    assert page.evaluate("() => document.documentElement.scrollWidth <= window.innerWidth")
    shot(page, "14-android-albums")
    context.close()

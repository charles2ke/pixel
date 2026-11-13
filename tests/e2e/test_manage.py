"""End-to-end tests for organising an album: editing, captions, covers, links
to a photo, selecting, moving, ZIP downloads, slideshows, pasted and retried
uploads, and filtering the visitor log.

Screenshots continue the numbering of ``test_ui.py``.
"""

from __future__ import annotations

import io
import re
import zipfile
from pathlib import Path
from typing import Iterator, List, Tuple

import pytest
from PIL import Image

pytest.importorskip("playwright")

from playwright.sync_api import Browser, BrowserContext, Page, Route, expect  # noqa: E402

from pixel.app import create_app  # noqa: E402
from pixel.config import Settings  # noqa: E402
from tests.conftest import AUDIT, AUDIT_REPO, DATA_REPO, FRIEND, OWNER  # noqa: E402
from tests.e2e.test_ui import IPHONE_UA, new_context, photo, shot, sign_in  # noqa: E402
from tests.fake_github import FakeGitHub, ServerThread, build_app  # noqa: E402

DESKTOP = {"width": 1280, "height": 860}
CSRF = {"X-Pixel": "1"}

PASTE_SCREENSHOT = """async () => {
  const canvas = document.createElement('canvas');
  canvas.width = 640; canvas.height = 400;
  const ctx = canvas.getContext('2d');
  ctx.fillStyle = '#4b7bec'; ctx.fillRect(0, 0, 640, 400);
  ctx.fillStyle = '#fff'; ctx.font = 'bold 64px sans-serif'; ctx.fillText('Pasted', 200, 220);
  const blob = await new Promise((resolve) => canvas.toBlob(resolve, 'image/png'));
  const data = new DataTransfer();
  data.items.add(new File([blob], 'image.png', { type: 'image/png' }));
  // false when Pixel took the paste (it calls preventDefault)
  return document.dispatchEvent(new ClipboardEvent('paste', { clipboardData: data, bubbles: true, cancelable: true }));
}"""


@pytest.fixture(scope="module")
def stack() -> Iterator[Tuple[FakeGitHub, str]]:
    state = FakeGitHub()
    state.add_user(OWNER, "charles2ke")
    state.add_user(FRIEND, "friend")
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


def thumbnail(seed: int) -> bytes:
    image = Image.open(io.BytesIO(photo(seed)))
    image.thumbnail((480, 480))
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=80)
    return buffer.getvalue()


def signed_in(
    browser: Browser, base: str, token: str = OWNER, **kwargs: object
) -> Tuple[BrowserContext, Page]:
    context = new_context(browser, base, **{"viewport": DESKTOP, **kwargs})
    page = context.new_page()
    page.goto("/")
    sign_in(page, token)
    expect(page.locator("#user-chip")).to_be_visible()
    return context, page


def make_album(page: Page, title: str, photos: int = 0) -> str:
    """Create an album holding ``photos`` JPEGs through the API; returns its id."""
    response = page.request.post("/api/albums", data={"title": title}, headers=CSRF)
    assert response.status == 201, response.text()
    album = response.json()["id"]
    stem = title.lower().replace(" ", "-")
    for seed in range(photos):
        upload = page.request.post(
            f"/api/albums/{album}/media",
            multipart={
                "file": {"name": f"{stem}-{seed}.jpg", "mimeType": "image/jpeg", "buffer": photo(seed)},
                "thumb": {"name": "thumb.jpg", "mimeType": "image/jpeg", "buffer": thumbnail(seed)},
            },
            headers=CSRF,
        )
        assert upload.status == 201, upload.text()
    return album


def open_album(page: Page, album: str, count: int) -> List[str]:
    """Show an album; returns the ids of its photos and videos in order."""
    page.goto(f"/#/album/{album}")
    tiles = page.locator(".tile")
    expect(tiles).to_have_count(count)
    return [tiles.nth(i).get_attribute("data-id") or "" for i in range(count)]


def wait_for_photo(page: Page) -> None:
    image = page.locator("#lightbox-stage img")
    expect(image).to_have_js_property("complete", True)
    expect(image).not_to_have_js_property("naturalWidth", 0)


def zip_names(path: Path) -> List[str]:
    with zipfile.ZipFile(path) as archive:
        assert archive.testzip() is None
        names = archive.namelist()
        assert all(archive.read(name)[:3] == b"\xff\xd8\xff" for name in names)
        return sorted(names)


def test_owner_edits_album_and_photo_details(stack: Tuple[FakeGitHub, str], browser: Browser) -> None:
    _, base = stack
    context, page = signed_in(browser, base)
    album = make_album(page, "Road trip", photos=3)
    first, second, third = open_album(page, album, 3)
    expect(page.get_by_text("3 items", exact=True)).to_be_visible()

    page.locator("#album-menu summary").click()
    page.get_by_role("button", name="✎ Edit album").click()
    expect(page.get_by_role("heading", name="Edit album")).to_be_visible()
    expect(page.get_by_label("Title")).to_have_value("Road trip")
    page.get_by_label("Title").fill("Road trip 2026")
    page.get_by_label("Description").fill("Coast to coast")
    page.get_by_role("button", name="Save").click()
    expect(page.locator("#toast")).to_have_text("Album saved")
    expect(page.get_by_role("heading", name="Road trip 2026", level=1)).to_be_visible()
    expect(page.locator("#view")).to_contain_text("Coast to coast")

    # Opening a photo gives it its own link.
    page.locator(".tile").first.click()
    expect(page).to_have_url(re.compile(rf"#/album/{album}/{first}$"))
    page.keyboard.press("i")
    panel = page.locator("#lightbox-info")
    expect(panel).to_be_visible()
    expect(page.locator("#lightbox-info-toggle")).to_have_attribute("aria-expanded", "true")
    expect(panel).to_contain_text("road-trip-0.jpg")
    expect(panel).to_contain_text("@charles2ke")
    page.locator("#info-edit").click()
    page.locator("#info-title-input").fill("Golden hour")
    page.locator("#info-description-input").fill("First night\nby the sea")
    page.locator("#info-save").click()
    expect(page.locator("#toast")).to_have_text("Saved")
    expect(page.locator("#info-heading")).to_have_text("Golden hour")
    expect(page.locator("#lightbox-description")).to_have_js_property("innerText", "First night\nby the sea")
    expect(page.locator("#lightbox-caption")).to_contain_text("Golden hour")
    expect(page.locator(f'.tile[data-id="{first}"]')).to_have_attribute("title", "Golden hour")

    # The first upload is the cover; make the second photo the cover instead.
    expect(page.locator("#info-cover")).to_be_disabled()
    page.keyboard.press("ArrowRight")
    expect(page).to_have_url(re.compile(rf"#/album/{album}/{second}$"))
    expect(page.locator("#info-cover")).to_have_text("☆ Set as album cover")
    page.locator("#info-cover").click()
    expect(page.locator("#toast")).to_have_text("Album cover updated")
    expect(page.locator("#info-cover")).to_have_text("★ Album cover")
    expect(page.locator("#info-cover")).to_be_disabled()
    page.keyboard.press("ArrowLeft")
    wait_for_photo(page)
    shot(page, "15-photo-details-and-caption")

    # The link survives a reload; closing returns to the album.
    page.reload()
    expect(page.locator("#lightbox")).to_be_visible()
    expect(page.locator("#lightbox-caption")).to_contain_text("Golden hour")
    expect(page.locator("#lightbox-description")).to_have_js_property("innerText", "First night\nby the sea")
    page.keyboard.press("Escape")
    expect(page.locator("#lightbox")).to_be_hidden()
    expect(page).to_have_url(re.compile(rf"#/album/{album}$"))

    # Back closes the viewer and forward opens it again.
    page.locator(f'.tile[data-id="{third}"]').click()
    expect(page).to_have_url(re.compile(rf"#/album/{album}/{third}$"))
    page.go_back()
    expect(page.locator("#lightbox")).to_be_hidden()
    expect(page).to_have_url(re.compile(rf"#/album/{album}$"))
    page.go_forward()
    expect(page.locator("#lightbox")).to_be_visible()
    expect(page.locator("#lightbox-caption")).to_contain_text("3/3")
    page.keyboard.press("Escape")
    expect(page.locator("#lightbox")).to_be_hidden()

    # A link to a photo that is gone says so.
    page.goto(f"/#/album/{album}/01J00000000000000000000000")
    expect(page.locator("#toast")).to_have_text("That photo or video is no longer in this album.")
    expect(page.locator("#lightbox")).to_be_hidden()
    expect(page).to_have_url(re.compile(rf"#/album/{album}$"))

    page.get_by_role("link", name="‹ Albums").click()
    card = page.locator(".album-card", has_text="Road trip 2026")
    expect(card).to_contain_text("3 items")
    expect(card).to_contain_text("Coast to coast")
    expect(card.locator(".album-cover img")).to_have_attribute("src", f"/api/media/{second}/thumb")
    shot(page, "16-albums-with-counts")
    context.close()


def test_owner_selects_downloads_moves_and_deletes(
    stack: Tuple[FakeGitHub, str], browser: Browser, tmp_path: Path
) -> None:
    state, base = stack
    context, page = signed_in(browser, base)
    keepers = make_album(page, "Keepers")
    inbox = make_album(page, "Inbox", photos=7)
    ids = open_album(page, inbox, 7)
    tiles = page.locator(".tile")
    titles = [tiles.nth(i).get_attribute("title") for i in range(7)]

    page.locator("#select-button").click()
    expect(page.locator("#selection-bar")).to_be_visible()
    expect(page.locator("#selection-count")).to_have_text("None selected")
    expect(page.locator("#selection-move")).to_be_disabled()
    tiles.nth(0).click()
    tiles.nth(2).click(modifiers=["Shift"])
    expect(page.locator("#selection-count")).to_have_text("3 selected")
    expect(page.locator(".tile.selected")).to_have_count(3)
    expect(page.locator("#lightbox")).to_be_hidden()
    shot(page, "17-select-photos")

    with page.expect_download() as selection:
        page.locator("#selection-download").click()
    expect(page.locator("#toast")).to_have_text("Downloading 3 photos and videos as a ZIP file…")
    assert selection.value.suggested_filename == "Inbox.zip"
    selection.value.save_as(tmp_path / "selection.zip")
    assert zip_names(tmp_path / "selection.zip") == sorted(f"{title}.jpg" for title in titles[:3])

    page.locator("#selection-move").click()
    expect(page.locator("#move-dialog")).to_be_visible()
    expect(page.locator("#move-summary")).to_have_text("Move 3 photos and videos out of “Inbox”.")
    page.locator("#move-target").select_option(label="Keepers")
    shot(page, "18-move-to-another-album")
    page.locator("#move-submit").click()
    expect(page.locator("#move-dialog")).to_be_hidden()
    expect(page.locator("#toast")).to_have_text("Moved 3 photos and videos to “Keepers”")
    expect(tiles).to_have_count(4)
    expect(page.locator("#selection-bar")).to_be_hidden()
    expect(page.locator("#select-button")).to_have_text("Select")

    # One photo can be moved from the viewer's details panel too.
    tiles.first.click()
    expect(page).to_have_url(re.compile(rf"#/album/{inbox}/{ids[3]}$"))
    page.locator("#lightbox-info-toggle").click()
    page.locator("#info-move").click()
    expect(page.locator("#move-summary")).to_have_text("Move 1 photo or video out of “Inbox”.")
    page.locator("#move-target").select_option(label="Keepers")
    page.locator("#move-submit").click()
    expect(page.locator("#toast")).to_have_text("Moved 1 photo or video to “Keepers”")
    expect(page.locator("#lightbox")).to_be_hidden()
    expect(page).to_have_url(re.compile(rf"#/album/{inbox}$"))
    expect(tiles).to_have_count(3)

    # Deleting from the viewer goes back to the album, too.
    tiles.first.click()
    expect(page).to_have_url(re.compile(rf"#/album/{inbox}/{ids[4]}$"))
    page.once("dialog", lambda dialog: dialog.accept())
    page.locator("#lightbox-delete").click()
    expect(page.locator("#toast")).to_have_text("Deleted")
    expect(page.locator("#lightbox")).to_be_hidden()
    expect(page).to_have_url(re.compile(rf"#/album/{inbox}$"))
    expect(tiles).to_have_count(2)

    page.locator("#select-button").click()
    page.locator("#select-all").click()
    expect(page.locator("#selection-count")).to_have_text("2 selected")
    expect(page.locator("#select-all")).to_have_text("Select none")
    page.once("dialog", lambda dialog: dialog.accept())
    page.locator("#selection-delete").click()
    expect(page.locator("#toast")).to_have_text("Deleted 2 photos and videos")
    expect(tiles).to_have_count(0)
    expect(page.locator(".drop-zone")).to_be_visible()

    assert sorted(open_album(page, keepers, 4)) == sorted(ids[:4])
    page.locator("#album-menu summary").click()
    with page.expect_download() as everything:
        page.get_by_role("link", name="⬇ Download all (ZIP)").click()
    everything.value.save_as(tmp_path / "keepers.zip")
    assert zip_names(tmp_path / "keepers.zip") == sorted(f"{title}.jpg" for title in titles[:4])

    expect(page.locator("#album-menu")).not_to_have_attribute("open", "")
    page.locator("#album-menu summary").click()
    page.once("dialog", lambda dialog: dialog.accept())
    page.get_by_role("button", name="🗑 Delete album").click()
    expect(page.locator("#toast")).to_have_text("Album deleted with 4 photos and videos")
    expect(page.get_by_role("heading", name="Albums")).to_be_visible()
    expect(page.locator(".album-card", has_text="Inbox")).to_be_visible()
    expect(page.locator(".album-card", has_text="Keepers")).to_have_count(0)

    # Nothing of the seven photos is left in the repository's current files.
    files = state.files(DATA_REPO)
    assert not [path for path in files for media in ids if media in path]
    context.close()


def test_slideshow_plays_the_album_once(stack: Tuple[FakeGitHub, str], browser: Browser) -> None:
    _, base = stack
    context = new_context(browser, base, viewport=DESKTOP)
    page = context.new_page()
    page.clock.install()
    page.goto("/")
    sign_in(page, OWNER)
    expect(page.locator("#user-chip")).to_be_visible()
    album = make_album(page, "Slides", photos=3)
    open_album(page, album, 3)

    caption = page.locator("#lightbox-caption")
    play = page.locator("#lightbox-play")
    page.locator("#slideshow-button").click()
    expect(caption).to_contain_text("1/3")
    expect(play).to_have_attribute("aria-label", "Pause slideshow")
    wait_for_photo(page)
    page.clock.fast_forward(5000)
    expect(caption).to_contain_text("2/3")

    play.click()
    expect(play).to_have_attribute("aria-label", "Play slideshow")
    page.clock.fast_forward(15000)
    expect(caption).to_contain_text("2/3")

    play.click()
    expect(play).to_have_attribute("aria-label", "Pause slideshow")
    wait_for_photo(page)
    page.clock.fast_forward(5000)
    expect(caption).to_contain_text("3/3")
    wait_for_photo(page)
    page.clock.fast_forward(5000)
    expect(page.locator("#toast")).to_have_text("End of slideshow")
    expect(play).to_have_attribute("aria-label", "Play slideshow")
    expect(caption).to_contain_text("3/3")
    context.close()


def test_owner_pastes_and_retries_uploads(stack: Tuple[FakeGitHub, str], browser: Browser) -> None:
    _, base = stack
    context, page = signed_in(browser, base)
    album = make_album(page, "Clipboard")
    open_album(page, album, 0)
    expect(page.locator(".drop-zone")).to_contain_text("paste")

    assert page.evaluate(PASTE_SCREENSHOT) is False
    expect(page.locator(".tile")).to_have_count(1)
    expect(page.locator(".tile")).to_have_attribute("title", re.compile(r"^pasted-\d{8}-\d{6}$"))
    expect(page.locator("#toast")).to_have_text("1 file uploaded")

    def unavailable(route: Route) -> None:
        route.fulfill(
            status=503, content_type="application/json", body='{"detail": "GitHub is busy, try again"}'
        )

    page.route("**/api/albums/*/media", unavailable)
    page.locator("#upload-input").set_input_files(
        [
            {"name": "retry-me.jpg", "mimeType": "image/jpeg", "buffer": photo(1)},
            {"name": "notes.txt", "mimeType": "text/plain", "buffer": b"not a photo"},
        ]
    )
    rows = page.locator("#upload-progress li")
    retried = rows.filter(has_text="retry-me.jpg")
    refused = rows.filter(has_text="notes.txt")
    expect(retried).to_contain_text("GitHub is busy, try again")
    expect(refused).to_contain_text("Not a photo or video")
    expect(refused.get_by_role("button", name="Retry", exact=True)).to_have_count(0)
    expect(page.locator(".upload-summary")).to_contain_text("2 files not uploaded")
    shot(page, "19-upload-retry")

    page.unroute("**/api/albums/*/media")
    retried.get_by_role("button", name="Retry", exact=True).click()
    expect(page.locator(".tile")).to_have_count(2)
    expect(retried).to_have_count(0)
    expect(page.locator(".upload-summary")).to_be_hidden()
    page.get_by_role("button", name="Dismiss notes.txt").click()
    expect(rows).to_have_count(0)
    context.close()


def test_viewer_can_select_and_download_but_not_change(
    stack: Tuple[FakeGitHub, str], browser: Browser
) -> None:
    _, base = stack
    owner, page = signed_in(browser, base)
    album = make_album(page, "Shared", photos=2)
    owner.close()

    context, page = signed_in(browser, base, FRIEND)
    open_album(page, album, 2)
    page.locator("#album-menu summary").click()
    expect(page.locator("#album-menu .menu-item")).to_have_text(["⬇ Download all (ZIP)"])
    page.keyboard.press("Escape")
    expect(page.locator("#album-menu")).not_to_have_attribute("open", "")

    page.locator("#select-button").click()
    page.locator(".tile").first.click()
    expect(page.locator("#selection-download")).to_be_enabled()
    expect(page.locator("#selection-move")).to_have_count(0)
    expect(page.locator("#selection-delete")).to_have_count(0)
    page.keyboard.press("Escape")
    expect(page.locator("#selection-bar")).to_be_hidden()
    assert page.evaluate(PASTE_SCREENSHOT) is True

    page.locator(".tile").first.click()
    page.locator("#lightbox-info-toggle").click()
    expect(page.locator("#info-heading")).to_have_text("shared-0")
    expect(page.locator("#lightbox-info button")).to_have_count(0)
    context.close()


def test_selection_and_details_fit_an_iphone(stack: Tuple[FakeGitHub, str], browser: Browser) -> None:
    _, base = stack
    context, page = signed_in(
        browser,
        base,
        viewport={"width": 390, "height": 844},
        device_scale_factor=3,
        is_mobile=True,
        has_touch=True,
        user_agent=IPHONE_UA,
    )
    album = make_album(page, "Phone", photos=4)
    page.locator("#ios-hint-close").tap()
    open_album(page, album, 4)
    tiles = page.locator(".tile")
    page.wait_for_function(
        "() => [...document.querySelectorAll('.tile img')].every((i) => i.complete && i.naturalWidth > 0)"
    )

    page.locator("#select-button").tap()
    tiles.nth(1).tap()
    tiles.nth(3).tap()
    expect(page.locator("#selection-count")).to_have_text("2 selected")
    expect(page.locator("#selection-bar")).to_be_in_viewport()
    assert page.evaluate("() => document.documentElement.scrollWidth <= window.innerWidth")
    shot(page, "20-iphone-select")

    page.get_by_role("button", name="Stop selecting").tap()
    tiles.first.tap()
    page.locator("#lightbox-info-toggle").tap()
    expect(page.locator("#lightbox-info")).to_be_in_viewport()
    expect(page.locator("#info-edit")).to_be_in_viewport()
    wait_for_photo(page)
    assert page.evaluate("() => document.documentElement.scrollWidth <= window.innerWidth")
    shot(page, "21-iphone-photo-details")
    context.close()


def test_visitor_log_filters_and_loads_older_events(stack: Tuple[FakeGitHub, str], browser: Browser) -> None:
    _, base = stack
    context, page = signed_in(browser, base)
    album = make_album(page, "Popular")
    for _ in range(105):
        assert page.request.get(f"/api/albums/{album}").ok

    page.goto("/#/audit")
    rows = page.locator("table.audit tbody tr")
    expect(rows).to_have_count(100)
    expect(page.locator("#audit-summary")).to_have_text("Newest 100 events, stored in your audit repository.")
    page.locator("#audit-more").click()
    expect(rows.nth(100)).to_be_visible()

    page.get_by_label("Event").select_option("view_album")
    page.get_by_label("Who").fill("charles2ke")
    page.get_by_role("button", name="Filter").click()
    expect(page.locator("#audit-summary")).to_have_text("100 matching events, newest first.")
    expect(rows).to_have_count(100)
    assert set(rows.locator("td:nth-child(2)").all_text_contents()) == {"view album"}
    assert set(rows.locator("td:nth-child(3)").all_text_contents()) == {"@charles2ke"}
    expect(rows.first).to_contain_text("“Popular”")
    shot(page, "22-visitor-log-filters")
    page.locator("#audit-more").click()
    expect(rows.nth(104)).to_be_visible()

    page.get_by_role("button", name="Clear").click()
    expect(page.get_by_label("Event")).to_have_value("")
    expect(page.get_by_role("button", name="Clear")).to_have_count(0)
    expect(rows).to_have_count(100)
    context.close()

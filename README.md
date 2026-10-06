# pixel

A personal photo and video album whose storage **is a GitHub
repository**, managed through [GitDb](https://github.com/charles2ke/GitDb).

* Photos and videos are committed to your data repository; album and media
  metadata are GitDb documents next to them.
* **Privacy follows the repository's access settings.** Nothing is shared
  that GitHub would not show the visitor: every request is made to GitHub with
  the visitor's own token.
* Albums show a thumbnail grid. Tapping a thumbnail opens the full photo, or
  plays the video (streamed with HTTP range requests, so seeking works).
* **Downloads follow repository access too** — only visitors who can read the
  repository can download originals.
* **Visitors are audited** (visits, sign-ins, denied access, views, downloads,
  uploads, deletions) into a GitDb collection that only repository admins can
  read in the app.
* Installable as an app (PWA) on Android, iPhone and iPad.

## How access works

| Visitor's access to `PIXEL_REPO` | What they can do in pixel |
| --- | --- |
| No access (private repo, not a collaborator) | Nothing — they are told the album is private |
| Read (`pull`), or anyone if the repo is public | Browse, view, play and download |
| Write (`push`) | …plus create albums, upload and delete |
| Admin | …plus read the visitor audit log |

Visitors sign in by pasting a GitHub token (a fine-grained personal access
token scoped to the data repository with **Contents: read** — or read and
write for uploaders). The token stays on the server in an in-memory session
(HttpOnly, SameSite=Strict cookie); it is never stored in the repo and never
sent back to the browser. Permissions are re-checked with GitHub every five
minutes, so removing a collaborator or making the repo private takes effect
without restarting pixel.

To share with family: make the data repository private and invite them as
collaborators with *Read* access. To share with everyone: make it public.

## Repository layout

```
data/albums/…              GitDb documents: title, description, cover
data/media/…               GitDb documents: album, filename, type, size, paths
data/_index/media/album.json
media/<album>/<id>.<ext>   the original photo or video
media/<album>/<id>.thumb.jpg
```

The audit log is stored in `data/audit/…` of `PIXEL_AUDIT_REPO`, which must be a
separate repository (pixel refuses to start otherwise) so album viewers cannot
read visitors' IP addresses.

## Setup

1. Create the data repository on GitHub (for example `charles2ke/pixel-data`),
   initialised with a README so the `main` branch exists.
2. Create a **private** audit repository (for example `charles2ke/pixel-audit`)
   and a fine-grained token with *Contents: read and write* on it only. Audit
   entries include visitor IP addresses and user agents; pixel logs a warning
   at startup if the audit repository is public.
3. Install and run (Python 3.9+):

   ```sh
   python -m venv .venv && . .venv/bin/activate
   pip install -r requirements.txt   # or: pip install -e .
   cp .env.example .env              # edit, then export the variables
   uvicorn pixel.app:app --host 0.0.0.0 --port 8000
   ```

   Serve it over **HTTPS** (for example behind Caddy or nginx, with
   `PIXEL_TRUST_PROXY=1`) — installation on phones, service workers and
   secure cookies all require it. Run a **single** worker process: sessions are
   in memory and writes are serialised in-process.

### Deploying

GitHub Pages only serves static files, so it cannot run pixel. The
[Pages workflow](https://github.com/charles2ke/pixel/blob/main/.github/workflows/pages.yml) publishes this README as the
project page on every push to `main`. To turn it on, go to **Settings → Pages**
and set **Source** to **GitHub Actions**.

Run the app itself on any host that runs containers (Render, Fly.io, Railway,
a VPS, …). The
[container image workflow](https://github.com/charles2ke/pixel/blob/main/.github/workflows/docker-publish.yml)
publishes a ready-made image for `linux/amd64` and `linux/arm64` to GitHub
Packages:

```sh
docker run -p 8000:8000 --env-file .env ghcr.io/charles2ke/pixel:latest
```

| Tag | Built from |
| --- | --- |
| `latest`, `main` | The newest commit on `main` |
| `v1`, `v2`, … | A `v*` git tag (pushed, or created with a new GitHub release) |
| `sha-<commit>` | A single commit (short SHA) |

A new package starts out **private**, even when the repository is public. To
let anyone pull it without signing in, open it from the repository's
**Packages** list, choose **Package settings**, and under **Danger Zone** use
**Change visibility → Public** (a public package cannot be made private again).

Or build the image yourself from the included `Dockerfile`:

```sh
docker build -t pixel .
docker run -p 8000:8000 --env-file .env pixel
```

The container listens on `$PORT` (default `8000`) with a single worker. Put it
behind HTTPS and set `PIXEL_TRUST_PROXY=1` when a proxy terminates TLS.

### Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `PIXEL_REPO` | *(required)* | `owner/name` of the data repository |
| `PIXEL_BRANCH` | `main` | Branch to read and write |
| `PIXEL_DATA_ROOT` | `data` | GitDb root folder |
| `PIXEL_MEDIA_ROOT` | `media` | Folder for photo/video files |
| `PIXEL_MAX_UPLOAD_MB` | `50` | Largest accepted upload (GitHub's hard limit is 100 MB per file) |
| `PIXEL_AUDIT_REPO` | *(required with a token)* | Private repository for the visitor audit log; must differ from `PIXEL_REPO` |
| `PIXEL_AUDIT_BRANCH` | `PIXEL_BRANCH` | Branch for the audit log |
| `PIXEL_AUDIT_TOKEN` | *(none)* | Token used to write audit entries; without it events only go to the server log |
| `PIXEL_AUDIT_FLUSH_SECONDS` | `10` | How often buffered audit events are committed |
| `PIXEL_TRUST_PROXY` | `0` | Use `X-Forwarded-For` / `X-Forwarded-Proto` from a reverse proxy |
| `PIXEL_COOKIE_SECURE` | auto | Force the `Secure` cookie flag on/off (auto = on for HTTPS) |
| `PIXEL_API_URL`, `PIXEL_RAW_URL` | GitHub | Override GitHub endpoints (used by the tests) |

## Installing on phones and tablets

* **Android (Chrome):** open the site and tap **Install**, or menu ⋮ →
  *Install app* / *Add to Home screen*.
* **iPhone / iPad (Safari):** tap **Share** → **Add to Home Screen**. pixel
  shows a hint with these steps on iOS.

The installed app opens full-screen. The service worker caches only the app
shell; photos, videos and API responses are never stored by it. (The
browser may keep a photo it already showed in its private HTTP cache for up to
five minutes.) Revoking access on GitHub therefore also applies to installed
devices.

## Supported files

JPEG, PNG, GIF, WebP, HEIC/HEIF, AVIF photos and MP4, MOV, M4V, WebM, 3GP
videos. The type is detected from the file contents, not the name; anything
else (including SVG and HTML) is rejected. Thumbnails are generated in the
browser at upload time. HEIC photos and some MOV videos only display in
browsers that support them (for example Safari).

## Limitations

* Git keeps history: deleting a photo removes it from the album and the
  branch, but it remains in the repository's commit history.
* GitHub limits files to 100 MB and recommends repositories stay well under a
  few GB; this is meant for a personal album, not a backup of everything.
* Anonymous visitors of a public repository are served through
  `raw.githubusercontent.com`, which can lag a few minutes behind new uploads.
* One server process only (see Setup).

## Development

```sh
pip install -e ".[dev]"
python -m playwright install chromium
ruff check . && ruff format --check .
pytest -q                     # API tests + Playwright end-to-end tests
```

The tests run against an in-process fake GitHub (`tests/fake_github.py`) that
enforces private/public repositories and collaborator permissions. The
Playwright tests save screenshots to `test-results/screenshots`
(override with `PIXEL_SCREENSHOTS`); CI uploads them as the
`playwright-screenshots` artifact.

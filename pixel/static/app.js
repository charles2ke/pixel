"use strict";

// Pixel front end. Every photo, video and album is fetched from the Pixel
// server, which reads the GitDb repository with the visitor's own GitHub
// credentials — so what shows up here is exactly what GitHub allows.

const $ = (selector) => document.querySelector(selector);
const state = { session: null, albums: [], album: null, items: [], index: -1 };

// ------------------------------------------------------------------ helpers
function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === undefined || value === null || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

class ApiError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

async function api(path, options = {}) {
  const init = { credentials: "same-origin", ...options, headers: { "X-Pixel": "1", ...(options.headers || {}) } };
  if (init.json !== undefined) {
    init.body = JSON.stringify(init.json);
    init.headers["Content-Type"] = "application/json";
    delete init.json;
  }
  const response = await fetch(path, init);
  let payload = null;
  try {
    payload = await response.json();
  } catch (_) {
    payload = null;
  }
  if (!response.ok) throw new ApiError(response.status, (payload && payload.detail) || `Request failed (${response.status})`);
  return payload;
}

let toastTimer = null;
function toast(message, kind = "info") {
  const node = $("#toast");
  node.textContent = message;
  node.dataset.kind = kind;
  node.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (node.hidden = true), 4000);
}

function formatBytes(bytes) {
  if (!bytes && bytes !== 0) return "";
  const units = ["B", "KB", "MB", "GB"];
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value.toFixed(unit ? 1 : 0)} ${units[unit]}`;
}

function formatDate(value) {
  if (!value) return "";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

const access = () => (state.session && state.session.access) || {};

// ------------------------------------------------------------------ session
async function loadSession(visit = false) {
  state.session = await api(`/api/session${visit ? "?visit=1" : ""}`);
  renderChrome();
}

function renderChrome() {
  const session = state.session;
  const badge = $("#repo-badge");
  badge.hidden = false;
  badge.textContent = `${session.access.private ? "🔒 " : ""}${session.repo}`;
  badge.title = session.access.private ? "Private repository" : "Public repository";
  document.querySelectorAll(".repo-name").forEach((node) => (node.textContent = session.repo));
  $("#signin-button").hidden = session.signed_in;
  $("#signout-button").hidden = !session.signed_in;
  $("#audit-link").hidden = !(session.signed_in && session.access.is_admin && session.audit_enabled);
  const chip = $("#user-chip");
  chip.hidden = !session.signed_in;
  chip.replaceChildren();
  if (session.signed_in) {
    chip.append(el("span", { text: `@${session.user.login}` }));
    chip.append(el("span", { class: "role", text: session.access.is_admin ? "admin" : session.access.can_write ? "editor" : "viewer" }));
  }
}

function openSignIn() {
  $("#signin-error").hidden = true;
  $("#token-input").value = "";
  $("#signin-dialog").showModal();
  $("#token-input").focus();
}

async function signIn(event) {
  event.preventDefault();
  const button = $("#signin-submit");
  button.disabled = true;
  try {
    await api("/api/login", { method: "POST", json: { token: $("#token-input").value } });
    $("#token-input").value = "";
    $("#signin-dialog").close();
    await loadSession();
    toast(`Signed in as @${state.session.user.login}`);
    route();
  } catch (error) {
    const node = $("#signin-error");
    node.textContent = error.message;
    node.hidden = false;
  } finally {
    button.disabled = false;
  }
}

async function signOut() {
  await api("/api/logout", { method: "POST" }).catch(() => null);
  await loadSession();
  toast("Signed out");
  location.hash = "#/";
  route();
}

// ------------------------------------------------------------------- views
function setView(...children) {
  const view = $("#view");
  view.replaceChildren(...children);
}

function loading(text = "Loading…") {
  setView(el("p", { class: "muted center", text }));
}

function errorView(error) {
  if (error.status === 401) {
    setView(
      el(
        "section",
        { class: "empty" },
        el("div", { class: "empty-icon", text: "🔒" }),
        el("h1", { text: "This album is private" }),
        el("p", { class: "muted", text: `Only people with access to ${state.session.repo} on GitHub can see these photos and videos.` }),
        el("button", { class: "button", type: "button", onclick: openSignIn, text: "Sign in with GitHub" }),
      ),
    );
    return;
  }
  setView(
    el(
      "section",
      { class: "empty" },
      el("div", { class: "empty-icon", text: error.status === 403 ? "⛔" : "⚠️" }),
      el("h1", { text: error.status === 403 ? "No access" : "Something went wrong" }),
      el("p", { class: "muted", text: error.message }),
      el("a", { class: "button ghost", href: "#/", text: "Back to albums" }),
    ),
  );
}

async function showAlbums() {
  loading();
  const { albums } = await api("/api/albums");
  state.albums = albums;
  const header = el(
    "div",
    { class: "view-header" },
    el("h1", { text: "Albums" }),
    access().can_write ? el("button", { class: "button", type: "button", onclick: openNewAlbum, text: "+ New album" }) : null,
  );
  if (!albums.length) {
    setView(
      header,
      el(
        "section",
        { class: "empty" },
        el("div", { class: "empty-icon", text: "🖼️" }),
        el("p", { class: "muted", text: access().can_write ? "Create your first album to start uploading photos and videos." : "No albums yet." }),
      ),
    );
    return;
  }
  const grid = el(
    "div",
    { class: "album-grid" },
    albums.map((album) =>
      el(
        "a",
        { class: "album-card", href: `#/album/${encodeURIComponent(album.id)}` },
        el(
          "div",
          { class: "album-cover" },
          album.cover
            ? el("img", { src: `/api/media/${encodeURIComponent(album.cover)}/thumb`, alt: "", loading: "lazy", onerror: (e) => e.target.remove() })
            : null,
        ),
        el("div", { class: "album-meta" }, el("h2", { text: album.title }), album.description ? el("p", { class: "muted", text: album.description }) : null),
      ),
    ),
  );
  setView(header, grid);
}

async function showAlbum(id) {
  loading();
  const album = await api(`/api/albums/${encodeURIComponent(id)}`);
  state.album = album;
  state.items = album.items;
  const canWrite = access().can_write;
  const input = el("input", { type: "file", id: "upload-input", accept: "image/*,video/*", multiple: true, hidden: true, onchange: (e) => uploadFiles(e.target.files) });
  const header = el(
    "div",
    { class: "view-header" },
    el("div", {}, el("a", { class: "back", href: "#/", text: "‹ Albums" }), el("h1", { text: album.title }), album.description ? el("p", { class: "muted", text: album.description }) : null),
    canWrite
      ? el(
          "div",
          { class: "header-actions" },
          !album.items.length ? el("button", { class: "button ghost danger", type: "button", onclick: () => deleteAlbum(album), text: "Delete album" }) : null,
          el("label", { class: "button", for: "upload-input", text: "⬆ Upload" }),
          input,
        )
      : null,
  );
  const progress = el("ul", { id: "upload-progress", class: "upload-progress" });
  const count = el("p", { class: "muted small", text: `${album.items.length} item${album.items.length === 1 ? "" : "s"}` });
  if (!album.items.length) {
    setView(
      header,
      progress,
      el(
        "section",
        { class: "empty drop-zone" },
        el("div", { class: "empty-icon", text: "📷" }),
        el("p", { class: "muted", text: canWrite ? "Upload photos and videos to fill this album." : "This album is empty." }),
      ),
    );
  } else {
    setView(header, progress, count, el("div", { class: "media-grid" }, album.items.map((item, index) => tile(item, index))));
  }
  if (canWrite) enableDrop();
}

function tile(item, index) {
  const label = `${item.kind === "video" ? "Play video" : "View photo"} ${item.title}`;
  return el(
    "button",
    { class: `tile ${item.kind}`, type: "button", "aria-label": label, title: item.title, onclick: () => openLightbox(index), "data-id": item.id },
    item.has_thumb
      ? el("img", { src: `/api/media/${encodeURIComponent(item.id)}/thumb`, alt: item.title, loading: "lazy" })
      : el("span", { class: "placeholder", text: item.kind === "video" ? "🎬" : "🖼️" }),
    item.kind === "video" ? el("span", { class: "play", "aria-hidden": "true", text: "▶" }) : null,
  );
}

async function showAudit() {
  loading();
  const { events, enabled } = await api("/api/audit?limit=300");
  const header = el("div", { class: "view-header" }, el("div", {}, el("a", { class: "back", href: "#/", text: "‹ Albums" }), el("h1", { text: "Visitors" })));
  if (!enabled) {
    setView(header, el("p", { class: "muted", text: "Visitor auditing is off. Set PIXEL_AUDIT_TOKEN on the server to turn it on." }));
    return;
  }
  const rows = events.map((event) =>
    el(
      "tr",
      {},
      el("td", { text: formatDate(event.at || event._created_at) }),
      el("td", {}, el("span", { class: `event event-${event.event}`, text: event.event.replace(/_/g, " ") })),
      el("td", { text: event.login ? `@${event.login}` : event.attempted_login ? `(@${event.attempted_login})` : "anonymous" }),
      el("td", { text: event.ip || "" }),
      el("td", { class: "muted", text: [event.album && `album ${event.album}`, event.media && `media ${event.media}`].filter(Boolean).join(" · ") }),
      el("td", { class: "muted ua", text: event.user_agent || "" }),
    ),
  );
  setView(
    header,
    el("p", { class: "muted small", text: `Newest ${events.length} events, stored in your audit repository.` }),
    el(
      "div",
      { class: "table-wrap" },
      el(
        "table",
        { class: "audit" },
        el("thead", {}, el("tr", {}, ["When", "Event", "Who", "Address", "What", "Browser"].map((h) => el("th", { text: h })))),
        el("tbody", {}, rows),
      ),
    ),
  );
}

// ------------------------------------------------------------------ albums
function openNewAlbum() {
  $("#album-error").hidden = true;
  $("#album-form").reset();
  $("#album-dialog").showModal();
  $("#album-name").focus();
}

async function createAlbum(event) {
  event.preventDefault();
  try {
    const album = await api("/api/albums", { method: "POST", json: { title: $("#album-name").value, description: $("#album-description").value } });
    $("#album-dialog").close();
    location.hash = `#/album/${encodeURIComponent(album.id)}`;
  } catch (error) {
    const node = $("#album-error");
    node.textContent = error.message;
    node.hidden = false;
  }
}

async function deleteAlbum(album) {
  if (!confirm(`Delete the album “${album.title}”?`)) return;
  try {
    await api(`/api/albums/${encodeURIComponent(album.id)}`, { method: "DELETE" });
    toast("Album deleted");
    location.hash = "#/";
  } catch (error) {
    toast(error.message, "error");
  }
}

// ----------------------------------------------------------------- uploads
function withTimeout(promise, ms) {
  return Promise.race([promise, new Promise((resolve) => setTimeout(() => resolve(null), ms))]);
}

function canvasToJpeg(source, width, height) {
  const max = 480;
  const scale = Math.min(1, max / Math.max(width, height));
  const canvas = document.createElement("canvas");
  canvas.width = Math.max(1, Math.round(width * scale));
  canvas.height = Math.max(1, Math.round(height * scale));
  canvas.getContext("2d").drawImage(source, 0, 0, canvas.width, canvas.height);
  return new Promise((resolve) => canvas.toBlob((blob) => resolve(blob), "image/jpeg", 0.82));
}

async function imageThumb(file) {
  const url = URL.createObjectURL(file);
  try {
    const image = new Image();
    image.decoding = "async";
    image.src = url;
    await image.decode();
    return await canvasToJpeg(image, image.naturalWidth, image.naturalHeight);
  } catch (_) {
    return null; // e.g. HEIC outside Safari: the album shows a placeholder
  } finally {
    URL.revokeObjectURL(url);
  }
}

function videoThumb(file) {
  return new Promise((resolve) => {
    const url = URL.createObjectURL(file);
    const video = document.createElement("video");
    const done = (value) => {
      URL.revokeObjectURL(url);
      video.removeAttribute("src");
      video.load();
      resolve(value);
    };
    video.muted = true;
    video.playsInline = true;
    video.preload = "auto";
    video.addEventListener("loadeddata", () => {
      const target = Number.isFinite(video.duration) ? Math.min(1, video.duration / 4) : 0;
      if (target > 0) video.currentTime = target;
      else canvasToJpeg(video, video.videoWidth, video.videoHeight).then(done, () => done(null));
    });
    video.addEventListener("seeked", () => canvasToJpeg(video, video.videoWidth, video.videoHeight).then(done, () => done(null)));
    video.addEventListener("error", () => done(null));
    video.src = url;
  });
}

async function makeThumb(file) {
  if (file.type.startsWith("video/")) return withTimeout(videoThumb(file), 8000);
  return withTimeout(imageThumb(file), 8000);
}

function send(albumId, file, thumb, onProgress) {
  return new Promise((resolve, reject) => {
    const form = new FormData();
    form.append("file", file, file.name);
    if (thumb) form.append("thumb", thumb, "thumb.jpg");
    const xhr = new XMLHttpRequest();
    xhr.open("POST", `/api/albums/${encodeURIComponent(albumId)}/media`);
    xhr.setRequestHeader("X-Pixel", "1");
    xhr.upload.addEventListener("progress", (event) => event.lengthComputable && onProgress(event.loaded / event.total));
    xhr.addEventListener("load", () => {
      let payload = null;
      try {
        payload = JSON.parse(xhr.responseText);
      } catch (_) {
        payload = null;
      }
      if (xhr.status >= 200 && xhr.status < 300) resolve(payload);
      else reject(new ApiError(xhr.status, (payload && payload.detail) || `Upload failed (${xhr.status})`));
    });
    xhr.addEventListener("error", () => reject(new ApiError(0, "Network error")));
    xhr.send(form);
  });
}

async function uploadFiles(fileList) {
  const files = Array.from(fileList || []);
  if (!files.length || !state.album) return;
  const albumId = state.album.id;
  const list = $("#upload-progress");
  let uploaded = 0;
  for (const file of files) {
    const bar = el("progress", { max: "1", value: "0" });
    const status = el("span", { class: "muted small", text: "Preparing…" });
    const row = el("li", {}, el("span", { class: "name", text: file.name }), bar, status);
    list.append(row);
    if (!/^(image|video)\//.test(file.type) && !/\.(heic|heif|mov|mp4|m4v|webm|3gp|avif)$/i.test(file.name)) {
      status.textContent = "Not a photo or video";
      row.classList.add("failed");
      continue;
    }
    if (state.session.max_upload_bytes && file.size > state.session.max_upload_bytes) {
      status.textContent = `Too large (max ${formatBytes(state.session.max_upload_bytes)})`;
      row.classList.add("failed");
      continue;
    }
    try {
      const thumb = await makeThumb(file);
      status.textContent = "Uploading…";
      await send(albumId, file, thumb, (fraction) => (bar.value = fraction));
      bar.value = 1;
      status.textContent = "Saved to GitHub ✓";
      row.classList.add("done");
      uploaded += 1;
    } catch (error) {
      status.textContent = error.message;
      row.classList.add("failed");
    }
  }
  if (uploaded) {
    toast(`${uploaded} file${uploaded === 1 ? "" : "s"} uploaded`);
    if (state.album && state.album.id === albumId) {
      const failed = Array.from(list.querySelectorAll("li.failed"));
      await showAlbum(albumId);
      $("#upload-progress").append(...failed);
    }
  }
}

function enableDrop() {
  const view = $("#view");
  view.ondragover = (event) => {
    event.preventDefault();
    view.classList.add("dragging");
  };
  view.ondragleave = () => view.classList.remove("dragging");
  view.ondrop = (event) => {
    event.preventDefault();
    view.classList.remove("dragging");
    uploadFiles(event.dataTransfer.files);
  };
}

function disableDrop() {
  const view = $("#view");
  view.ondragover = view.ondragleave = view.ondrop = null;
}

// ---------------------------------------------------------------- lightbox
function openLightbox(index) {
  state.index = index;
  $("#lightbox").hidden = false;
  document.body.classList.add("no-scroll");
  renderLightbox();
  $("#lightbox-close").focus();
}

function closeLightbox() {
  const stage = $("#lightbox-stage");
  stage.querySelectorAll("video").forEach((video) => video.pause());
  stage.replaceChildren();
  $("#lightbox").hidden = true;
  document.body.classList.remove("no-scroll");
  const tileNode = state.items[state.index] && document.querySelector(`.tile[data-id="${CSS.escape(state.items[state.index].id)}"]`);
  if (tileNode) tileNode.focus();
  state.index = -1;
}

function step(delta) {
  if (!state.items.length) return;
  state.index = (state.index + delta + state.items.length) % state.items.length;
  renderLightbox();
}

function renderLightbox() {
  const item = state.items[state.index];
  if (!item) return closeLightbox();
  const id = encodeURIComponent(item.id);
  const stage = $("#lightbox-stage");
  stage.querySelectorAll("video").forEach((video) => video.pause());
  let media;
  if (item.kind === "video") {
    media = el("video", {
      src: `/api/media/${id}/original`,
      controls: true,
      autoplay: true,
      playsinline: true,
      preload: "metadata",
      poster: item.has_thumb ? `/api/media/${id}/thumb` : null,
      "aria-label": item.title,
    });
    media.addEventListener("error", () => toast("This video format cannot be played in this browser — use Download.", "error"));
  } else {
    media = el("img", { src: `/api/media/${id}/original`, alt: item.title });
    media.addEventListener("error", () => {
      if (item.has_thumb && !media.dataset.fallback) {
        media.dataset.fallback = "1";
        media.src = `/api/media/${id}/thumb`;
        toast("This photo format cannot be shown here — use Download for the original.", "error");
      }
    });
  }
  stage.replaceChildren(media);
  $("#lightbox-caption").textContent = `${item.title} · ${formatBytes(item.size)} · ${state.index + 1}/${state.items.length}`;
  const download = $("#lightbox-download");
  download.href = `/api/media/${id}/download`;
  download.setAttribute("download", item.filename || "");
  $("#lightbox-delete").hidden = !access().can_write;
  const multiple = state.items.length > 1;
  $("#lightbox-prev").hidden = !multiple;
  $("#lightbox-next").hidden = !multiple;
}

async function deleteCurrent() {
  const item = state.items[state.index];
  if (!item || !confirm(`Delete “${item.title}” from the album? It is removed from the repository's current files (git history keeps earlier commits).`)) return;
  try {
    await api(`/api/media/${encodeURIComponent(item.id)}`, { method: "DELETE" });
    closeLightbox();
    toast("Deleted");
    showAlbum(state.album.id).catch(errorView);
  } catch (error) {
    toast(error.message, "error");
  }
}

function setupSwipe() {
  const stage = $("#lightbox-stage");
  let startX = null;
  stage.addEventListener("pointerdown", (event) => (startX = event.pointerType === "mouse" ? null : event.clientX));
  stage.addEventListener("pointerup", (event) => {
    if (startX === null) return;
    const delta = event.clientX - startX;
    startX = null;
    if (Math.abs(delta) > 60) step(delta < 0 ? 1 : -1);
  });
}

// ----------------------------------------------------------------- install
let deferredPrompt = null;

function isIos() {
  const ua = navigator.userAgent;
  return /iPad|iPhone|iPod/.test(ua) || (ua.includes("Macintosh") && navigator.maxTouchPoints > 1);
}

function isStandalone() {
  return window.matchMedia("(display-mode: standalone)").matches || navigator.standalone === true;
}

function setupInstall() {
  window.addEventListener("beforeinstallprompt", (event) => {
    event.preventDefault();
    deferredPrompt = event;
    $("#install-button").hidden = false;
  });
  window.addEventListener("appinstalled", () => {
    $("#install-button").hidden = true;
    deferredPrompt = null;
  });
  $("#install-button").addEventListener("click", async () => {
    if (!deferredPrompt) return;
    deferredPrompt.prompt();
    await deferredPrompt.userChoice.catch(() => null);
    deferredPrompt = null;
    $("#install-button").hidden = true;
  });
  if (isIos() && !isStandalone() && localStorage.getItem("pixel-ios-hint") !== "dismissed") {
    $("#ios-hint").hidden = false;
  }
  $("#ios-hint-close").addEventListener("click", () => {
    $("#ios-hint").hidden = true;
    localStorage.setItem("pixel-ios-hint", "dismissed");
  });
  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.register("/sw.js", { scope: "/" }).catch(() => null);
  }
}

// ------------------------------------------------------------------ router
async function route() {
  if (!$("#lightbox").hidden) closeLightbox();
  disableDrop();
  const hash = location.hash.replace(/^#/, "") || "/";
  try {
    if (!state.session.access.can_view) {
      errorView(new ApiError(state.session.signed_in ? 403 : 401, `@${state.session.user ? state.session.user.login : ""} has no access to ${state.session.repo}.`));
      return;
    }
    const albumMatch = hash.match(/^\/album\/([^/]+)$/);
    if (albumMatch) await showAlbum(decodeURIComponent(albumMatch[1]));
    else if (hash === "/audit") await showAudit();
    else await showAlbums();
  } catch (error) {
    if (error.status === 401 && state.session.signed_in) await loadSession();
    errorView(error);
  }
  $("#view").focus({ preventScroll: true });
}

function bind() {
  $("#signin-button").addEventListener("click", openSignIn);
  $("#signout-button").addEventListener("click", signOut);
  $("#signin-form").addEventListener("submit", signIn);
  $("#album-form").addEventListener("submit", createAlbum);
  document.querySelectorAll("dialog [data-close]").forEach((button) => button.addEventListener("click", () => button.closest("dialog").close()));
  $("#lightbox-close").addEventListener("click", closeLightbox);
  $("#lightbox-prev").addEventListener("click", () => step(-1));
  $("#lightbox-next").addEventListener("click", () => step(1));
  $("#lightbox-delete").addEventListener("click", deleteCurrent);
  document.addEventListener("keydown", (event) => {
    if ($("#lightbox").hidden) return;
    if (event.key === "Escape") closeLightbox();
    else if (event.key === "ArrowLeft") step(-1);
    else if (event.key === "ArrowRight") step(1);
  });
  window.addEventListener("hashchange", route);
  setupSwipe();
  setupInstall();
}

async function boot() {
  bind();
  try {
    await loadSession(true);
  } catch (error) {
    setView(el("section", { class: "empty" }, el("h1", { text: "Pixel is unavailable" }), el("p", { class: "muted", text: error.message })));
    return;
  }
  route();
}

document.addEventListener("DOMContentLoaded", boot);

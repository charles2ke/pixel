"use strict";

// Pixel front end. Every photo, video and album is fetched from the Pixel
// server, which reads the GitDb repository with the visitor's own GitHub
// credentials — so what shows up here is exactly what GitHub allows.

const $ = (selector) => document.querySelector(selector);
const state = {
  session: null,
  albums: [],
  album: null,
  items: [],
  index: -1,
  view: null, // "albums", "album" or "audit" once that view has rendered
  selecting: false,
  selected: new Set(),
  anchor: -1,
  busy: false,
  info: false,
  playing: false,
  pushedViewer: false,
};

const BULK_CHUNK = 100; // ids per move or delete request
const UPLOAD_CONCURRENCY = 3;
const SLIDE_MS = 4000;
const AUDIT_PAGE = 100;
const AUDIT_EVENTS = [
  "visit",
  "sign_in",
  "sign_in_failed",
  "sign_in_denied",
  "sign_out",
  "view_album",
  "view_media",
  "download",
  "download_album",
  "upload",
  "create_album",
  "update_album",
  "delete_album",
  "update_media",
  "move_media",
  "delete_media",
  "access_denied",
];

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

function plural(count, word) {
  return `${count} ${word}${count === 1 ? "" : "s"}`;
}

function things(count) {
  return count === 1 ? "1 photo or video" : `${count} photos and videos`;
}

function chunked(list, size) {
  const chunks = [];
  for (let start = 0; start < list.length; start += size) chunks.push(list.slice(start, start + size));
  return chunks;
}

function isTyping(target) {
  return !!target && (target.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(target.tagName));
}

function decodePart(value) {
  try {
    return decodeURIComponent(value);
  } catch (_) {
    return value;
  }
}

const access = () => (state.session && state.session.access) || {};

// ------------------------------------------------------------------ history
// Bumped on every navigation, so a slow response cannot replace a newer view.
let routeTicket = 0;
// The hash on screen. hashchange and popstate usually both fire for one navigation.
let routedHash = null;
// Set while closing the viewer with history.back(): a photo opened before the
// browser has gone back is opened once it has.
let viewerBack = null;

const albumHash = (id) => `#/album/${encodeURIComponent(id)}`;
const mediaHash = (albumId, mediaId) => `${albumHash(albumId)}/${encodeURIComponent(mediaId)}`;
const isMediaHash = () => /^#\/album\/[^/]+\/[^/]+$/.test(location.hash);

function setHash(hash, replace = false) {
  if (location.hash !== hash) history[replace ? "replaceState" : "pushState"](null, "", hash);
  routedHash = location.hash;
}

function onNavigate() {
  if (location.hash !== routedHash) route();
}

function rerender() {
  state.view = null;
  return route();
}

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
    rerender();
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
  setHash("#/");
  rerender();
}

// ------------------------------------------------------------------- views
function setView(...children) {
  $("#view").replaceChildren(...children.filter(Boolean));
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
  const ticket = routeTicket;
  loading();
  const { albums } = await api("/api/albums");
  if (ticket !== routeTicket) return;
  state.albums = albums;
  state.view = "albums";
  const header = el(
    "div",
    { class: "view-header" },
    el("h1", { text: "Albums" }),
    access().can_write ? el("button", { class: "button", type: "button", onclick: () => openAlbumDialog(), text: "+ New album" }) : null,
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
        { class: "album-card", href: albumHash(album.id) },
        el(
          "div",
          { class: "album-cover" },
          album.cover
            ? el("img", { src: `/api/media/${encodeURIComponent(album.cover)}/thumb`, alt: "", loading: "lazy", onerror: (e) => e.target.remove() })
            : null,
        ),
        el(
          "div",
          { class: "album-meta" },
          el("h2", { text: album.title }),
          album.description ? el("p", { class: "muted", text: album.description }) : null,
          typeof album.count === "number" ? el("p", { class: "muted count", text: plural(album.count, "item") }) : null,
        ),
      ),
    ),
  );
  setView(header, grid);
}

// Loads an album. A quiet refresh keeps the page (and any open viewer) in place.
async function showAlbum(id, quiet = false) {
  const ticket = routeTicket;
  if (!quiet) loading();
  const album = await api(`/api/albums/${encodeURIComponent(id)}`);
  if (ticket !== routeTicket) return;
  const viewing = $("#lightbox").hidden ? null : state.items[state.index];
  state.album = album;
  state.items = album.items;
  state.anchor = -1;
  const present = new Set(album.items.map((item) => item.id));
  for (const selected of [...state.selected]) if (!present.has(selected)) state.selected.delete(selected);
  state.view = "album";
  renderAlbum();
  if (viewing) {
    const index = state.items.findIndex((item) => item.id === viewing.id);
    if (index < 0) closeLightbox();
    else {
      state.index = index;
      renderViewerText();
    }
  }
}

function renderAlbum() {
  const album = state.album;
  const items = state.items;
  const canWrite = access().can_write;
  if (!items.length) resetSelection();
  const active = document.activeElement;
  const focusedId = active && active.classList && active.classList.contains("tile") ? active.dataset.id : null;
  const header = el(
    "div",
    { class: "view-header" },
    el(
      "div",
      { class: "view-title" },
      el("a", { class: "back", href: "#/", text: "‹ Albums" }),
      el("h1", { text: album.title }),
      album.description ? el("p", { class: "muted", text: album.description }) : null,
    ),
    el(
      "div",
      { class: "header-actions" },
      items.length > 1 ? el("button", { id: "slideshow-button", class: "button ghost", type: "button", onclick: startSlideshow, text: "▶ Slideshow" }) : null,
      items.length ? el("button", { id: "select-button", class: "button ghost", type: "button", onclick: () => setSelecting(!state.selecting), text: "Select" }) : null,
      albumMenu(album, canWrite),
      canWrite ? el("label", { class: "button", for: "upload-input", text: "⬆ Upload" }) : null,
      canWrite
        ? el("input", { type: "file", id: "upload-input", accept: "image/*,video/*", multiple: true, hidden: true, onchange: onUploadInput })
        : null,
    ),
  );
  const content = items.length
    ? [
        el("p", { class: "muted small", text: plural(items.length, "item") }),
        el("div", { id: "media-grid", class: "media-grid" }, items.map((item, index) => tile(item, index))),
        selectionBar(canWrite),
      ]
    : [
        el(
          "section",
          { class: "empty drop-zone" },
          el("div", { class: "empty-icon", text: "📷" }),
          el("p", { class: "muted", text: canWrite ? "Upload photos and videos to fill this album — or drop or paste them here." : "This album is empty." }),
        ),
      ];
  setView(header, canWrite ? uploadPanel(album.id).section : null, ...content);
  updateSelection();
  if (canWrite) enableDrop();
  if (focusedId) {
    const node = document.querySelector(`.tile[data-id="${CSS.escape(focusedId)}"]`);
    if (node) node.focus({ preventScroll: true });
  }
}

function albumMenu(album, canWrite) {
  const entries = [
    state.items.length
      ? el("a", { class: "menu-item", href: `/api/albums/${encodeURIComponent(album.id)}/download`, download: "", text: "⬇ Download all (ZIP)" })
      : null,
    canWrite ? el("button", { class: "menu-item", type: "button", onclick: () => openAlbumDialog(album), text: "✎ Edit album" }) : null,
    canWrite ? el("button", { class: "menu-item danger", type: "button", onclick: () => deleteAlbum(album), text: "🗑 Delete album" }) : null,
  ].filter(Boolean);
  if (!entries.length) return null;
  return el(
    "details",
    { id: "album-menu", class: "menu" },
    el("summary", { class: "button ghost", "aria-label": "More album actions", title: "More album actions", text: "⋯" }),
    el("div", { class: "menu-list" }, entries),
  );
}

function closeMenus(event) {
  document.querySelectorAll("details.menu[open]").forEach((menu) => {
    if (!menu.contains(event.target)) menu.open = false;
    else if (event.target.closest(".menu-item")) setTimeout(() => (menu.open = false));
  });
}

function tileLabel(item) {
  if (state.selecting) return `Select ${item.title}`;
  return `${item.kind === "video" ? "Play video" : "View photo"} ${item.title}`;
}

function tile(item, index) {
  return el(
    "button",
    {
      class: `tile ${item.kind}`,
      type: "button",
      "aria-label": tileLabel(item),
      title: item.title,
      onclick: (event) => onTileClick(event, index),
      "data-id": item.id,
      "data-index": String(index),
    },
    item.has_thumb
      ? el("img", { src: `/api/media/${encodeURIComponent(item.id)}/thumb`, alt: item.title, loading: "lazy" })
      : el("span", { class: "placeholder", text: item.kind === "video" ? "🎬" : "🖼️" }),
    item.kind === "video" ? el("span", { class: "play", "aria-hidden": "true", text: "▶" }) : null,
    el("span", { class: "check", "aria-hidden": "true" }),
  );
}

function refreshTile(item) {
  const node = document.querySelector(`.tile[data-id="${CSS.escape(item.id)}"]`);
  if (!node) return;
  node.title = item.title;
  node.setAttribute("aria-label", tileLabel(item));
  const image = node.querySelector("img");
  if (image) image.alt = item.title;
}

// --------------------------------------------------------------- selection
function selectionBar(canWrite) {
  return el(
    "div",
    { id: "selection-bar", class: "selection-bar", role: "toolbar", "aria-label": "Selected photos and videos", hidden: true },
    el("span", { id: "selection-count", class: "selection-count", "aria-live": "polite" }),
    el("button", { id: "select-all", class: "button ghost", type: "button", onclick: toggleSelectAll, text: "Select all" }),
    el("span", { class: "spacer" }),
    el("button", { id: "selection-download", class: "button ghost", type: "button", "data-needs-selection": true, onclick: downloadSelection, text: "⬇ Download" }),
    canWrite
      ? el("button", { id: "selection-move", class: "button ghost", type: "button", "data-needs-selection": true, onclick: () => openMove(selectedIds()), text: "Move to…" })
      : null,
    canWrite
      ? el("button", { id: "selection-delete", class: "button danger", type: "button", "data-needs-selection": true, onclick: deleteSelection, text: "🗑 Delete" })
      : null,
    el("button", { class: "icon-button", type: "button", "aria-label": "Stop selecting", title: "Stop selecting", onclick: () => setSelecting(false), text: "✕" }),
  );
}

function selectedIds() {
  return state.items.filter((item) => state.selected.has(item.id)).map((item) => item.id);
}

function resetSelection() {
  state.selecting = false;
  state.selected.clear();
  state.anchor = -1;
}

function setSelecting(on) {
  if (on) state.selecting = true;
  else resetSelection();
  updateSelection();
}

function setBusy(on) {
  state.busy = on;
  updateSelection();
}

function updateSelection() {
  const grid = $("#media-grid");
  if (grid) {
    grid.classList.toggle("selecting", state.selecting);
    grid.querySelectorAll(".tile").forEach((node) => {
      const item = state.items[Number(node.dataset.index)];
      if (!item) return;
      const picked = state.selecting && state.selected.has(item.id);
      node.classList.toggle("selected", picked);
      if (state.selecting) node.setAttribute("aria-pressed", String(picked));
      else node.removeAttribute("aria-pressed");
      node.setAttribute("aria-label", tileLabel(item));
    });
  }
  const bar = $("#selection-bar");
  if (bar) {
    const count = state.selected.size;
    bar.hidden = !state.selecting;
    $("#selection-count").textContent = count ? `${count} selected` : "None selected";
    const all = $("#select-all");
    all.textContent = count && count === state.items.length ? "Select none" : "Select all";
    all.disabled = state.busy;
    bar.querySelectorAll("[data-needs-selection]").forEach((button) => (button.disabled = state.busy || !count));
  }
  const toggle = $("#select-button");
  if (toggle) toggle.textContent = state.selecting ? "Cancel" : "Select";
}

function onTileClick(event, index) {
  if (!state.selecting) {
    openLightbox(index);
    return;
  }
  const item = state.items[index];
  if (!item) return;
  if (event.shiftKey && state.anchor >= 0 && state.anchor < state.items.length) {
    const [from, to] = state.anchor < index ? [state.anchor, index] : [index, state.anchor];
    state.items.slice(from, to + 1).forEach((entry) => state.selected.add(entry.id));
  } else if (state.selected.has(item.id)) state.selected.delete(item.id);
  else state.selected.add(item.id);
  state.anchor = index;
  updateSelection();
}

function toggleSelectAll() {
  if (state.selected.size === state.items.length) state.selected.clear();
  else state.items.forEach((item) => state.selected.add(item.id));
  updateSelection();
}

function startDownload(url) {
  const link = el("a", { href: url, download: "", hidden: true });
  document.body.append(link);
  link.click();
  link.remove();
}

async function downloadSelection() {
  const ids = selectedIds();
  if (!ids.length || !state.album) return;
  let url = `/api/albums/${encodeURIComponent(state.album.id)}/download`;
  if (ids.length < state.items.length) {
    try {
      ({ url } = await api(url, { method: "POST", json: { ids } }));
    } catch (error) {
      toast(error.message, "error");
      return;
    }
  }
  startDownload(url);
  toast(`Downloading ${things(ids.length)} as a ZIP file…`);
}

// After a bulk change: leave select mode if it all worked and reload the album.
function refreshAfterBulk(albumId, finished) {
  if (state.view !== "album" || !state.album || state.album.id !== albumId) return Promise.resolve();
  if (finished) {
    if (!$("#lightbox").hidden) closeLightbox();
    setSelecting(false);
  }
  return showAlbum(albumId, true).catch(errorView);
}

async function deleteSelection() {
  const ids = selectedIds();
  if (!ids.length || state.busy || !state.album) return;
  const prompt = `Delete ${things(ids.length)} from “${state.album.title}”? They are removed from the repository's current files (git history keeps earlier commits).`;
  if (!confirm(prompt)) return;
  const albumId = state.album.id;
  let done = 0;
  let deleted = 0;
  let failure = null;
  setBusy(true);
  try {
    for (const chunk of chunked(ids, BULK_CHUNK)) {
      if (ids.length > BULK_CHUNK) toast(`Deleting… ${done} of ${ids.length} done`);
      const result = await api(`/api/albums/${encodeURIComponent(albumId)}/media/delete`, { method: "POST", json: { ids: chunk } });
      done += chunk.length;
      deleted += result.deleted;
    }
  } catch (error) {
    failure = error;
  } finally {
    setBusy(false);
  }
  if (failure) toast(done ? `Deleted ${done} of ${ids.length}, then: ${failure.message}` : failure.message, "error");
  else toast(`Deleted ${things(deleted)}`);
  await refreshAfterBulk(albumId, !failure);
}

// --------------------------------------------------------------------- move
let moveIds = [];
let moveSource = null;
let moving = false;

async function openMove(ids) {
  if (!ids.length || !state.album || moving) return;
  const source = state.album;
  let albums;
  try {
    ({ albums } = await api("/api/albums"));
  } catch (error) {
    toast(error.message, "error");
    return;
  }
  const others = albums.filter((album) => album.id !== source.id);
  if (!others.length) {
    toast("Create another album first, then move photos and videos into it.", "error");
    return;
  }
  moveIds = ids;
  moveSource = source;
  $("#move-target").replaceChildren(...others.map((album) => el("option", { value: album.id, text: album.title })));
  $("#move-summary").textContent = `Move ${things(ids.length)} out of “${source.title}”.`;
  $("#move-error").hidden = true;
  setMoveBusy(false);
  $("#move-dialog").showModal();
  $("#move-target").focus();
}

function setMoveBusy(on) {
  moving = on;
  const submit = $("#move-submit");
  submit.disabled = on;
  submit.textContent = on ? "Moving…" : "Move";
  $("#move-target").disabled = on;
  $("#move-dialog [data-close]").disabled = on;
  setBusy(on);
}

async function submitMove(event) {
  event.preventDefault();
  if (moving || !moveSource || !moveIds.length) return;
  const select = $("#move-target");
  const target = select.value;
  const targetTitle = select.selectedOptions[0] ? select.selectedOptions[0].textContent : "";
  const source = moveSource;
  const ids = moveIds;
  const summary = $("#move-summary");
  let done = 0;
  let moved = 0;
  $("#move-error").hidden = true;
  setMoveBusy(true);
  try {
    for (const chunk of chunked(ids, BULK_CHUNK)) {
      if (ids.length > BULK_CHUNK) summary.textContent = `Moving… ${done} of ${ids.length} done`;
      const result = await api(`/api/albums/${encodeURIComponent(source.id)}/media/move`, { method: "POST", json: { ids: chunk, album: target } });
      done += chunk.length;
      moved += result.moved;
    }
  } catch (error) {
    // Whatever was moved stays moved; a retry only sends the rest.
    moveIds = ids.slice(done);
    setMoveBusy(false);
    summary.textContent = `Move ${things(moveIds.length)} out of “${source.title}”.`;
    const node = $("#move-error");
    node.textContent = done ? `Moved ${done} of ${ids.length}, then: ${error.message}` : error.message;
    node.hidden = false;
    if (done) refreshAfterBulk(source.id, false);
    return;
  }
  setMoveBusy(false);
  $("#move-dialog").close();
  toast(`Moved ${things(moved)} to “${targetTitle}”`);
  refreshAfterBulk(source.id, true);
}

// ------------------------------------------------------------------ albums
let editingAlbum = null;

function openAlbumDialog(album = null) {
  editingAlbum = album;
  $("#album-error").hidden = true;
  $("#album-form").reset();
  $("#album-title").textContent = album ? "Edit album" : "New album";
  $("#album-submit").textContent = album ? "Save" : "Create";
  if (album) {
    $("#album-name").value = album.title;
    $("#album-description").value = album.description || "";
  }
  $("#album-dialog").showModal();
  $("#album-name").focus();
}

async function saveAlbum(event) {
  event.preventDefault();
  const button = $("#album-submit");
  const fields = { title: $("#album-name").value, description: $("#album-description").value };
  button.disabled = true;
  try {
    if (editingAlbum) {
      const updated = await api(`/api/albums/${encodeURIComponent(editingAlbum.id)}`, { method: "PATCH", json: fields });
      $("#album-dialog").close();
      toast("Album saved");
      if (state.view === "album" && state.album && state.album.id === updated.id) {
        Object.assign(state.album, updated);
        renderAlbum();
      }
    } else {
      const album = await api("/api/albums", { method: "POST", json: fields });
      $("#album-dialog").close();
      location.hash = albumHash(album.id);
    }
  } catch (error) {
    const node = $("#album-error");
    node.textContent = error.message;
    node.hidden = false;
  } finally {
    button.disabled = false;
  }
}

async function deleteAlbum(album) {
  const count = state.album && state.album.id === album.id ? state.items.length : 0;
  const prompt = count
    ? `Delete the album “${album.title}” and its ${things(count)}? They are removed from the repository's current files (git history keeps earlier commits).`
    : `Delete the album “${album.title}”?`;
  if (!confirm(prompt)) return;
  try {
    const query = count ? "?with_media=true" : "";
    const result = await api(`/api/albums/${encodeURIComponent(album.id)}${query}`, { method: "DELETE" });
    toast(result.deleted ? `Album deleted with ${things(result.deleted)}` : "Album deleted");
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

// Uploads run a few at a time; each album keeps its own list of progress rows
// so leaving and coming back to an album still shows what is happening.
const FINAL_UPLOAD_ERRORS = [404, 413, 415, 422];
const uploadQueue = [];
const uploadPanels = new Map();
const uploadTasks = new WeakMap();
const uploadedSince = new Map();
let uploadsActive = 0;

function uploadPanel(albumId) {
  let panel = uploadPanels.get(albumId);
  if (!panel) {
    const list = el("ul", { id: "upload-progress", class: "upload-progress" });
    const summary = el("div", { class: "upload-summary", hidden: true });
    panel = { section: el("section", { id: "uploads", class: "uploads", "aria-label": "Uploads" }, list, summary), list, summary };
    uploadPanels.set(albumId, panel);
  }
  return panel;
}

function uploadProblem(file) {
  if (!/^(image|video)\//.test(file.type) && !/\.(heic|heif|mov|mp4|m4v|webm|3gp|avif)$/i.test(file.name)) return "Not a photo or video";
  const max = state.session.max_upload_bytes;
  if (max && file.size > max) return `Too large (max ${formatBytes(max)})`;
  return null;
}

function onUploadInput(event) {
  uploadFiles(event.target.files);
  event.target.value = ""; // so choosing the same file again uploads it again
}

function uploadFiles(fileList) {
  const files = Array.from(fileList || []);
  if (!files.length || !state.album) return;
  const albumId = state.album.id;
  const panel = uploadPanel(albumId);
  for (const file of files) {
    const bar = el("progress", { max: "1", value: "0" });
    const status = el("span", { class: "muted small status", text: "Waiting…" });
    const actions = el("span", { class: "row-actions" });
    const row = el("li", {}, el("span", { class: "name", title: file.name, text: file.name }), bar, status, actions);
    panel.list.append(row);
    const task = { file, albumId, row, bar, status, actions, retryable: true };
    uploadTasks.set(row, task);
    const problem = uploadProblem(file);
    if (problem) failUpload(task, problem, false);
    else uploadQueue.push(task);
  }
  pumpUploads();
}

function pumpUploads() {
  while (uploadsActive < UPLOAD_CONCURRENCY && uploadQueue.length) {
    const task = uploadQueue.shift();
    uploadsActive += 1;
    runUpload(task).finally(() => {
      uploadsActive -= 1;
      pumpUploads();
      if (!uploadsActive && !uploadQueue.length) uploadsIdle();
    });
  }
}

async function runUpload(task) {
  const { file, albumId, row, bar, status } = task;
  try {
    status.textContent = "Preparing…";
    const thumb = await makeThumb(file);
    status.textContent = "Uploading…";
    await send(albumId, file, thumb, (fraction) => {
      bar.value = fraction;
      if (fraction >= 1) status.textContent = "Saving to GitHub…";
    });
    bar.value = 1;
    status.textContent = "Saved to GitHub ✓";
    row.classList.add("done");
    uploadedSince.set(albumId, (uploadedSince.get(albumId) || 0) + 1);
  } catch (error) {
    failUpload(task, error.message, !FINAL_UPLOAD_ERRORS.includes(error.status));
  }
}

function failUpload(task, message, retryable) {
  task.status.textContent = message;
  task.retryable = retryable;
  task.row.classList.add("failed");
  task.actions.replaceChildren(
    ...[
      retryable ? el("button", { class: "link-button", type: "button", onclick: () => retryUploads([task]), text: "Retry" }) : null,
      el("button", { class: "icon-button small", type: "button", "aria-label": `Dismiss ${task.file.name}`, onclick: () => dismissUpload(task), text: "✕" }),
    ].filter(Boolean),
  );
  updateUploadSummary(task.albumId);
}

function failedUploads(albumId) {
  const panel = uploadPanels.get(albumId);
  if (!panel) return [];
  return Array.from(panel.list.querySelectorAll("li.failed"), (row) => uploadTasks.get(row)).filter(Boolean);
}

function retryUploads(tasks) {
  for (const task of tasks) {
    if (!task.retryable || !task.row.classList.contains("failed")) continue;
    task.row.classList.remove("failed");
    task.actions.replaceChildren();
    task.status.textContent = "Waiting…";
    task.bar.value = 0;
    uploadQueue.push(task);
    updateUploadSummary(task.albumId);
  }
  pumpUploads();
}

function dismissUpload(task) {
  task.row.remove();
  updateUploadSummary(task.albumId);
}

function updateUploadSummary(albumId) {
  const panel = uploadPanels.get(albumId);
  if (!panel) return;
  const failed = failedUploads(albumId);
  panel.summary.hidden = failed.length < 2;
  if (panel.summary.hidden) return;
  panel.summary.replaceChildren(
    ...[
      el("span", { class: "muted small", text: `${plural(failed.length, "file")} not uploaded` }),
      failed.some((task) => task.retryable)
        ? el("button", { class: "link-button", type: "button", onclick: () => retryUploads(failedUploads(albumId)), text: "Retry failed" })
        : null,
      el("button", { class: "link-button", type: "button", onclick: () => failedUploads(albumId).forEach(dismissUpload), text: "Clear" }),
    ].filter(Boolean),
  );
}

async function uploadsIdle() {
  const counts = new Map(uploadedSince);
  uploadedSince.clear();
  const total = [...counts.values()].reduce((sum, count) => sum + count, 0);
  if (!total) return;
  toast(`${plural(total, "file")} uploaded`);
  const doneRows = [...counts.keys()].flatMap((albumId) => Array.from(uploadPanel(albumId).list.querySelectorAll("li.done")));
  const current = state.view === "album" && state.album && counts.has(state.album.id) ? state.album.id : null;
  if (current) {
    try {
      await showAlbum(current, true);
    } catch (error) {
      toast(`Uploaded, but the album could not be reloaded: ${error.message}`, "error");
      return;
    }
  }
  doneRows.forEach((row) => row.remove());
}

function warnUploads(event) {
  if (!uploadsActive && !uploadQueue.length) return;
  event.preventDefault();
  event.returnValue = "";
}

// Screenshots pasted from the clipboard usually arrive as "image.png".
function renamePasted(file, index, total) {
  if (file.name && !/^image\.\w+$/i.test(file.name)) return file;
  const now = new Date();
  const pad = (value) => String(value).padStart(2, "0");
  const stamp = `${now.getFullYear()}${pad(now.getMonth() + 1)}${pad(now.getDate())}-${pad(now.getHours())}${pad(now.getMinutes())}${pad(now.getSeconds())}`;
  const extension = (file.name.match(/\.(\w+)$/) || [])[1] || (file.type.split("/")[1] || "png").replace(/\W.*$/, "").replace(/^jpeg$/, "jpg");
  const suffix = total > 1 ? `-${index + 1}` : "";
  return new File([file], `pasted-${stamp}${suffix}.${extension}`, { type: file.type, lastModified: file.lastModified });
}

function onPaste(event) {
  if (state.view !== "album" || !state.album || !access().can_write) return;
  if (!$("#lightbox").hidden || document.querySelector("dialog[open]") || isTyping(event.target)) return;
  const files = Array.from((event.clipboardData && event.clipboardData.files) || []);
  if (!files.length) return;
  event.preventDefault();
  uploadFiles(files.map((file, index) => renamePasted(file, index, files.length)));
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
let infoEditing = null; // id of the item whose title and caption are being edited

function openLightbox(index, push = true) {
  const item = state.items[index];
  if (!item) return;
  if (viewerBack) {
    viewerBack.reopen = index;
    return;
  }
  const opening = $("#lightbox").hidden;
  state.index = index;
  $("#lightbox").hidden = false;
  document.body.classList.add("no-scroll");
  if (push && opening && state.album) {
    setHash(mediaHash(state.album.id, item.id));
    state.pushedViewer = true;
  }
  showInfoPanel();
  renderLightbox();
  if (opening) $("#lightbox-close").focus();
}

function closeLightbox(fromRoute = false) {
  stopSlideshow();
  const stage = $("#lightbox-stage");
  stage.querySelectorAll("video").forEach((video) => video.pause());
  stage.replaceChildren();
  stage.classList.remove("with-caption");
  $("#lightbox").hidden = true;
  document.body.classList.remove("no-scroll");
  infoEditing = null;
  const item = state.items[state.index];
  const tileNode = item && document.querySelector(`.tile[data-id="${CSS.escape(item.id)}"]`);
  if (tileNode) tileNode.focus();
  state.index = -1;
  if (!fromRoute && state.album && isMediaHash()) {
    if (state.pushedViewer) {
      viewerBack = { reopen: null };
      history.back();
    } else setHash(albumHash(state.album.id), true);
  }
  state.pushedViewer = false;
}

// Makes the viewer match a #/album/<id>/<media> link of the album on screen.
function syncViewer(mediaId) {
  const open = !$("#lightbox").hidden;
  if (!mediaId) {
    if (open) closeLightbox(true);
    return;
  }
  const index = state.items.findIndex((item) => item.id === mediaId);
  if (index < 0) {
    if (open) closeLightbox(true);
    toast("That photo or video is no longer in this album.", "error");
    setHash(albumHash(state.album.id), true);
    return;
  }
  if (!open) openLightbox(index, false);
  else if (index !== state.index) {
    state.index = index;
    renderLightbox();
  }
}

function step(delta) {
  if (!state.items.length || $("#lightbox").hidden) return;
  state.index = (state.index + delta + state.items.length) % state.items.length;
  if (state.album) setHash(mediaHash(state.album.id, state.items[state.index].id), true);
  renderLightbox();
}

function renderLightbox() {
  const item = state.items[state.index];
  if (!item) return closeLightbox();
  clearTimeout(slideTimer);
  infoEditing = null;
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
    media.addEventListener("error", () => {
      toast("This video format cannot be played in this browser — use Download.", "error");
      if (state.playing && media.isConnected) slideAfter(SLIDE_MS);
    });
    media.addEventListener("ended", () => state.playing && media.isConnected && slideAfter(600));
  } else {
    media = el("img", { src: `/api/media/${id}/original`, alt: item.title });
    media.addEventListener("load", () => state.playing && media.isConnected && slideAfter(SLIDE_MS));
    media.addEventListener("error", () => {
      if (item.has_thumb && !media.dataset.fallback) {
        media.dataset.fallback = "1";
        media.src = `/api/media/${id}/thumb`;
        toast("This photo format cannot be shown here — use Download for the original.", "error");
      } else if (state.playing && media.isConnected) slideAfter(SLIDE_MS);
    });
  }
  stage.replaceChildren(media);
  renderViewerText();
  scheduleSlide();
}

// Updates everything around the photo or video without reloading it.
function renderViewerText() {
  const item = state.items[state.index];
  if (!item) return;
  const id = encodeURIComponent(item.id);
  $("#lightbox-caption").textContent = `${item.title} · ${formatBytes(item.size)} · ${state.index + 1}/${state.items.length}`;
  const stage = $("#lightbox-stage");
  const media = stage.querySelector("img, video");
  if (media && media.tagName === "IMG") media.alt = item.title;
  else if (media) media.setAttribute("aria-label", item.title);
  let caption = stage.querySelector(".media-caption");
  if (item.description) {
    if (!caption) {
      caption = el("p", { id: "lightbox-description", class: "media-caption" });
      stage.append(caption);
    }
    caption.textContent = item.description;
  } else if (caption) caption.remove();
  stage.classList.toggle("with-caption", !!item.description);
  const download = $("#lightbox-download");
  download.href = `/api/media/${id}/download`;
  download.setAttribute("download", item.filename || "");
  $("#lightbox-delete").hidden = !access().can_write;
  const multiple = state.items.length > 1;
  $("#lightbox-prev").hidden = !multiple;
  $("#lightbox-next").hidden = !multiple;
  $("#lightbox-play").hidden = !multiple;
  updatePlayButton();
  if (state.info && infoEditing !== item.id) renderInfo();
}

async function deleteCurrent() {
  const item = state.items[state.index];
  if (!item || !confirm(`Delete “${item.title}” from the album? It is removed from the repository's current files (git history keeps earlier commits).`)) return;
  try {
    await api(`/api/media/${encodeURIComponent(item.id)}`, { method: "DELETE" });
    const albumId = state.album.id;
    closeLightbox();
    toast("Deleted");
    showAlbum(albumId, true).catch(errorView);
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

// ------------------------------------------------------------- info panel
function showInfoPanel() {
  $("#lightbox-info").hidden = !state.info;
  const toggle = $("#lightbox-info-toggle");
  toggle.setAttribute("aria-expanded", String(state.info));
  toggle.classList.toggle("active", state.info);
}

function toggleInfo() {
  state.info = !state.info;
  infoEditing = null;
  showInfoPanel();
  if (state.info) renderInfo();
}

function renderInfo() {
  const item = state.items[state.index];
  const panel = $("#lightbox-info");
  infoEditing = null;
  if (!item) {
    panel.replaceChildren();
    return;
  }
  const canWrite = access().can_write;
  const isCover = !!state.album && state.album.cover === item.id;
  const facts = [
    ["File", item.filename],
    ["Type", item.mime || item.kind],
    ["Size", formatBytes(item.size)],
    ["Uploaded by", item.uploaded_by ? `@${item.uploaded_by}` : ""],
    ["Uploaded", formatDate(item.created_at)],
  ].filter(([, value]) => value);
  panel.replaceChildren(
    ...[
      el("h2", { id: "info-heading", text: item.title }),
      item.description
        ? el("p", { class: "info-description", text: item.description })
        : canWrite
          ? el("p", { class: "info-description muted", text: "No caption yet." })
          : null,
      el("dl", { class: "info-list" }, facts.flatMap(([term, value]) => [el("dt", { text: term }), el("dd", { text: value })])),
      canWrite
        ? el(
            "div",
            { class: "info-actions" },
            el("button", { id: "info-edit", class: "button ghost", type: "button", onclick: editInfo, text: "✎ Edit title & caption" }),
            el("button", { id: "info-cover", class: "button ghost", type: "button", disabled: isCover, onclick: setCover, text: isCover ? "★ Album cover" : "☆ Set as album cover" }),
            el("button", { id: "info-move", class: "button ghost", type: "button", onclick: () => openMove([item.id]), text: "Move to…" }),
          )
        : null,
    ].filter(Boolean),
  );
}

function editInfo() {
  const item = state.items[state.index];
  if (!item) return;
  infoEditing = item.id;
  const title = el("input", { id: "info-title-input", name: "title", maxlength: "120", required: true, value: item.title });
  const description = el("textarea", { id: "info-description-input", name: "description", maxlength: "1000", rows: "4" });
  description.value = item.description || "";
  const error = el("p", { class: "error", role: "alert", hidden: true });
  const save = el("button", { id: "info-save", class: "button", type: "submit", text: "Save" });
  const form = el(
    "form",
    { class: "info-form", onsubmit: (event) => saveInfo(event, item.id, { title, description, error, save }) },
    el("label", { for: "info-title-input", text: "Title" }),
    title,
    el("label", { for: "info-description-input", text: "Caption" }),
    description,
    error,
    el("div", { class: "actions" }, el("button", { class: "button ghost", type: "button", onclick: renderInfo, text: "Cancel" }), save),
  );
  $("#lightbox-info").replaceChildren(form);
  title.focus();
}

async function saveInfo(event, mediaId, form) {
  event.preventDefault();
  form.save.disabled = true;
  form.error.hidden = true;
  try {
    const updated = await api(`/api/media/${encodeURIComponent(mediaId)}`, {
      method: "PATCH",
      json: { title: form.title.value, description: form.description.value },
    });
    const index = state.items.findIndex((item) => item.id === mediaId);
    if (index >= 0) {
      state.items[index] = { ...state.items[index], ...updated };
      refreshTile(state.items[index]);
    }
    toast("Saved");
    if (infoEditing === mediaId) infoEditing = null;
    if (!$("#lightbox").hidden && index === state.index) renderViewerText();
  } catch (error) {
    form.error.textContent = error.message;
    form.error.hidden = false;
    form.save.disabled = false;
  }
}

async function setCover() {
  const item = state.items[state.index];
  if (!item || !state.album) return;
  const button = $("#info-cover");
  if (button) button.disabled = true;
  try {
    const updated = await api(`/api/albums/${encodeURIComponent(state.album.id)}`, { method: "PATCH", json: { cover: item.id } });
    Object.assign(state.album, updated);
    toast("Album cover updated");
  } catch (error) {
    toast(error.message, "error");
  }
  if (state.info && infoEditing === null && !$("#lightbox").hidden) renderInfo();
}

// --------------------------------------------------------------- slideshow
// Plays the album once from the start: photos stay up for SLIDE_MS, videos
// play to the end. It stops at the last item rather than looping, because
// every slide is a GitHub request (and a "view" in the visitor log).
let slideTimer = null;
let wakeLock = null;

function startSlideshow() {
  if (!state.items.length) return;
  setSelecting(false);
  state.playing = true;
  requestWakeLock();
  openLightbox(0);
}

function toggleSlideshow() {
  if (state.playing) {
    stopSlideshow();
    return;
  }
  state.playing = true;
  requestWakeLock();
  updatePlayButton();
  if (state.index >= state.items.length - 1) step(1);
  else scheduleSlide();
}

function stopSlideshow() {
  state.playing = false;
  clearTimeout(slideTimer);
  releaseWakeLock();
  updatePlayButton();
}

function updatePlayButton() {
  const button = $("#lightbox-play");
  button.textContent = state.playing ? "⏸" : "▶";
  button.setAttribute("aria-label", state.playing ? "Pause slideshow" : "Play slideshow");
  button.classList.toggle("active", state.playing);
}

function slideAfter(ms) {
  clearTimeout(slideTimer);
  slideTimer = setTimeout(advanceSlide, ms);
}

function scheduleSlide() {
  clearTimeout(slideTimer);
  if (!state.playing) return;
  const media = $("#lightbox-stage").querySelector("img, video");
  if (!media) return;
  if (media.tagName === "VIDEO") {
    const started = media.play();
    if (started)
      started.catch(() => {
        if (!state.playing || !media.isConnected) return;
        media.muted = true; // browsers may refuse to autoplay sound
        media.play().catch(() => state.playing && media.isConnected && slideAfter(SLIDE_MS));
      });
  } else if (media.complete) slideAfter(SLIDE_MS); // otherwise the load event schedules it
}

function advanceSlide() {
  if (!state.playing || $("#lightbox").hidden) return;
  if (state.index >= state.items.length - 1) {
    stopSlideshow();
    toast("End of slideshow");
    return;
  }
  step(1);
}

async function requestWakeLock() {
  if (!("wakeLock" in navigator) || wakeLock) return;
  try {
    const lock = await navigator.wakeLock.request("screen");
    if (!state.playing) {
      lock.release().catch(() => null);
      return;
    }
    wakeLock = lock;
    lock.addEventListener("release", () => {
      if (wakeLock === lock) wakeLock = null;
    });
  } catch (_) {
    wakeLock = null;
  }
}

function releaseWakeLock() {
  if (wakeLock) wakeLock.release().catch(() => null);
  wakeLock = null;
}

// ------------------------------------------------------------- visitor log
const auditFilters = { event: "", who: "" };

function auditQuery(before) {
  const params = new URLSearchParams({ limit: String(AUDIT_PAGE) });
  if (before) params.set("before", before);
  if (auditFilters.event) params.set("event", auditFilters.event);
  if (auditFilters.who) params.set("who", auditFilters.who);
  return api(`/api/audit?${params}`);
}

function auditWhat(event, titles) {
  const album = (id) => `“${titles.get(id) || id}”`;
  return [
    event.album && `album ${album(event.album)}${event.target ? ` → ${album(event.target)}` : ""}`,
    event.media && `media ${event.media}`,
    typeof event.count === "number" && plural(event.count, "item"),
    event.fields && `changed ${event.fields.replace(/,/g, ", ")}`,
    event.size && formatBytes(event.size),
    event.need && `needs ${event.need}`,
  ]
    .filter(Boolean)
    .join(" · ");
}

function auditRow(event, titles) {
  return el(
    "tr",
    {},
    el("td", { text: formatDate(event.at || event._created_at) }),
    el("td", {}, el("span", { class: `event event-${event.event}`, text: event.event.replace(/_/g, " ") })),
    el("td", { text: event.login ? `@${event.login}` : event.attempted_login ? `(@${event.attempted_login})` : "anonymous" }),
    el("td", { text: event.ip || "" }),
    el("td", { class: "muted", text: auditWhat(event, titles) }),
    el("td", { class: "muted ua", title: event.user_agent || null, text: event.user_agent || "" }),
  );
}

function auditFilterForm(filtered) {
  const event = el(
    "select",
    { id: "audit-event", name: "event" },
    el("option", { value: "", text: "All events" }),
    AUDIT_EVENTS.map((name) => el("option", { value: name, selected: name === auditFilters.event, text: name.replace(/_/g, " ") })),
  );
  const who = el("input", { id: "audit-who", name: "who", type: "search", value: auditFilters.who, placeholder: "Login or IP address", autocomplete: "off", spellcheck: "false" });
  const apply = (next) => {
    Object.assign(auditFilters, next);
    rerender();
  };
  return el(
    "form",
    {
      class: "audit-filters",
      role: "search",
      onsubmit: (submit) => {
        submit.preventDefault();
        apply({ event: event.value, who: who.value.trim() });
      },
    },
    el("div", { class: "field" }, el("label", { for: "audit-event", text: "Event" }), event),
    el("div", { class: "field grow" }, el("label", { for: "audit-who", text: "Who" }), who),
    el(
      "div",
      { class: "field-buttons" },
      el("button", { id: "audit-filter", class: "button", type: "submit", text: "Filter" }),
      filtered ? el("button", { id: "audit-clear", class: "button ghost", type: "button", onclick: () => apply({ event: "", who: "" }), text: "Clear" }) : null,
    ),
  );
}

async function showAudit() {
  const ticket = routeTicket;
  loading();
  const [page, albums] = await Promise.all([auditQuery(), api("/api/albums").then((data) => data.albums, () => [])]);
  if (ticket !== routeTicket) return;
  state.view = "audit";
  const header = el("div", { class: "view-header" }, el("div", {}, el("a", { class: "back", href: "#/", text: "‹ Albums" }), el("h1", { text: "Visitors" })));
  if (!page.enabled) {
    setView(header, el("p", { class: "muted", text: "Visitor auditing is off. Set PIXEL_AUDIT_TOKEN on the server to turn it on." }));
    return;
  }
  const titles = new Map(albums.map((album) => [album.id, album.title]));
  const filtered = !!(auditFilters.event || auditFilters.who);
  let next = page.next;
  const rows = el("tbody", {}, page.events.map((event) => auditRow(event, titles)));
  const summary = el("p", { id: "audit-summary", class: "muted small" });
  const more = el("button", { id: "audit-more", class: "button ghost load-more", type: "button", text: "Load older" });
  const update = () => {
    const count = rows.children.length;
    if (count) summary.textContent = filtered ? `${plural(count, "matching event")}, newest first.` : `Newest ${plural(count, "event")}, stored in your audit repository.`;
    else if (next) summary.textContent = "No matching events among the newest ones — load older events to keep looking.";
    else summary.textContent = filtered ? "No matching events." : "No events yet.";
    more.hidden = !next;
  };
  more.addEventListener("click", async () => {
    more.disabled = true;
    try {
      const older = await auditQuery(next);
      if (ticket !== routeTicket) return;
      rows.append(...older.events.map((event) => auditRow(event, titles)));
      next = older.next;
      update();
    } catch (error) {
      toast(error.message, "error");
    } finally {
      more.disabled = false;
    }
  });
  update();
  setView(
    header,
    auditFilterForm(filtered),
    summary,
    el(
      "div",
      { class: "table-wrap" },
      el("table", { class: "audit" }, el("thead", {}, el("tr", {}, ["When", "Event", "Who", "Address", "What", "Browser"].map((h) => el("th", { text: h })))), rows),
    ),
    more,
  );
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
  routedHash = location.hash;
  const pendingBack = viewerBack;
  viewerBack = null;
  const hash = location.hash.replace(/^#/, "") || "/";
  const albumMatch = hash.match(/^\/album\/([^/]+)(?:\/([^/]+))?$/);
  const albumId = albumMatch ? decodePart(albumMatch[1]) : null;
  const mediaId = albumMatch && albumMatch[2] ? decodePart(albumMatch[2]) : null;
  if (albumId && state.view === "album" && state.album && state.album.id === albumId) {
    // Same album: only the photo viewer follows the link (back, forward, reload of a deep link).
    state.pushedViewer = false;
    syncViewer(mediaId);
    if (pendingBack && pendingBack.reopen !== null && $("#lightbox").hidden) openLightbox(pendingBack.reopen);
    return;
  }
  const ticket = ++routeTicket;
  if (!$("#lightbox").hidden) closeLightbox(true);
  disableDrop();
  resetSelection();
  state.view = null;
  try {
    if (!state.session.access.can_view) {
      errorView(new ApiError(state.session.signed_in ? 403 : 401, `@${state.session.user ? state.session.user.login : ""} has no access to ${state.session.repo}.`));
      return;
    }
    if (albumId) await showAlbum(albumId);
    else if (hash === "/audit") await showAudit();
    else await showAlbums();
  } catch (error) {
    if (ticket !== routeTicket) return;
    if (error.status === 401 && state.session.signed_in) await loadSession();
    errorView(error);
  }
  if (ticket !== routeTicket) return;
  $("#view").focus({ preventScroll: true });
  if (mediaId && state.view === "album") syncViewer(mediaId);
}

function onKeydown(event) {
  if (document.querySelector("dialog[open]")) return;
  const menu = document.querySelector("details.menu[open]");
  if (event.key === "Escape" && menu) {
    menu.open = false;
    menu.querySelector("summary").focus();
    return;
  }
  if (!$("#lightbox").hidden) {
    if (isTyping(event.target)) {
      if (event.key === "Escape" && infoEditing) {
        event.preventDefault();
        renderInfo();
        $("#lightbox-info-toggle").focus();
      }
      return;
    }
    if (event.altKey || event.ctrlKey || event.metaKey) return;
    if (event.key === "Escape") closeLightbox();
    else if (event.key === "ArrowLeft") step(-1);
    else if (event.key === "ArrowRight") step(1);
    else if (event.key === "i" || event.key === "I") toggleInfo();
    return;
  }
  if (event.key === "Escape" && state.selecting && !isTyping(event.target)) setSelecting(false);
}

function bind() {
  $("#signin-button").addEventListener("click", openSignIn);
  $("#signout-button").addEventListener("click", signOut);
  $("#signin-form").addEventListener("submit", signIn);
  $("#album-form").addEventListener("submit", saveAlbum);
  $("#move-form").addEventListener("submit", submitMove);
  $("#move-dialog").addEventListener("cancel", (event) => moving && event.preventDefault());
  document.querySelectorAll("dialog [data-close]").forEach((button) => button.addEventListener("click", () => button.closest("dialog").close()));
  $("#lightbox-close").addEventListener("click", () => closeLightbox());
  $("#lightbox-prev").addEventListener("click", () => step(-1));
  $("#lightbox-next").addEventListener("click", () => step(1));
  $("#lightbox-delete").addEventListener("click", deleteCurrent);
  $("#lightbox-play").addEventListener("click", toggleSlideshow);
  $("#lightbox-info-toggle").addEventListener("click", toggleInfo);
  document.addEventListener("keydown", onKeydown);
  document.addEventListener("paste", onPaste);
  document.addEventListener("click", closeMenus);
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible" && state.playing) requestWakeLock();
  });
  window.addEventListener("hashchange", onNavigate);
  window.addEventListener("popstate", onNavigate);
  window.addEventListener("beforeunload", warnUploads);
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

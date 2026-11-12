"use strict";

const $ = (s, el = document) => el.querySelector(s);
const state = { me: null, people: [], current: null, items: [], person: null, editing: null, buying: null,
  photo: { blob: null, remove: false, previewUrl: null }, loadedAt: 0, refreshing: false };

const ICON = {
  link: '<svg viewBox="0 0 24 24"><path d="M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>',
  store: '<svg viewBox="0 0 24 24"><path d="M4 9l1.5-5h13L20 9M4 9v11h16V9M4 9h16M9 20v-6h6v6" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"/></svg>',
  check: '<svg viewBox="0 0 24 24"><path d="M5 12.5l4.5 4.5L19 7.5" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"/></svg>',
};

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
const money = n => n == null ? "" : "$" + Number(n).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const host = u => { try { return new URL(u).hostname.replace(/^www\./, ""); } catch { return ""; } };
const day = t => new Date(t * 1000).toLocaleDateString(undefined, { month: "short", day: "numeric" });
const store = {
  get(k) { try { return localStorage.getItem(k); } catch { return null; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch { /* storage unavailable */ } },
};

const session = {
  get(k) { try { return sessionStorage.getItem(k); } catch { return null; } },
  set(k, v) { try { sessionStorage.setItem(k, v); return true; } catch { return false; } },
  del(k) { try { sessionStorage.removeItem(k); } catch { /* storage unavailable */ } },
};

// The server never redirects API calls, so a redirect is Cloudflare Access sending an expired
// session to its login page. 401s and the server's own "sign-in required" 403 mean the same.
// A plain network error is not one of these: offline, a reload would only lose the list.
const isAuthFailure = err => err.status === 0 || err.status === 401 ||
  (err.status === 403 && /sign-in required/i.test(err.message));

// Reload once so Access can show its login. The flag stops a loop when signing in doesn't
// help; the next request that succeeds clears it.
const RELOAD_FLAG = "wl.authReload";
function reloadToSignIn() {
  if (session.get(RELOAD_FLAG) || !session.set(RELOAD_FLAG, "1")) return false;
  location.reload();
  return true;
}

async function send(path, init, what = "Request") {
  // Following Access's redirect fails as an opaque "Failed to fetch"; stopping at it lets us tell.
  const r = await fetch(path, { ...init, credentials: "same-origin", redirect: "manual" });
  let data = null;
  try { data = await r.json(); } catch { /* non-JSON */ }
  if (r.ok) { session.del(RELOAD_FLAG); return data; }
  const err = new Error(r.type === "opaqueredirect" ? "Your sign-in has expired. Reload the page to sign in again."
    : (data && data.error) || `${what} failed (${r.status})`);
  err.status = r.status;
  if (isAuthFailure(err) && reloadToSignIn()) return new Promise(() => {}); // the page is going away
  throw err;
}

function api(path, opts = {}) {
  const init = { method: opts.method || "GET", headers: { "X-Wishlist": "1" } };
  if (opts.body !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(opts.body);
  }
  return send(path, init);
}

function toast(msg) {
  const t = $("#toast");
  t.textContent = msg; t.hidden = false;
  clearTimeout(toast._t); toast._t = setTimeout(() => (t.hidden = true), 2600);
}

const isEditor = () => state.me && state.me.role === "editor";

// ------------------------------------------------------------ per-person colors
const DEFAULT_COLOR = "#1f4d3a";
const colorOf = p => (p && p.color) || DEFAULT_COLOR;
function inkFor(hex) {
  const [r, g, b] = [1, 3, 5].map(i => parseInt(hex.slice(i, i + 2), 16) / 255)
    .map(v => (v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4));
  const L = 0.2126 * r + 0.7152 * g + 0.0722 * b;
  // pick whichever text color has more contrast
  return (L + 0.05) / 0.05 > 1.05 / (L + 0.05) ? "#1d2420" : "#ffffff";
}
function applyAccent(person) {
  const root = document.documentElement.style;
  if (person && person.color) {
    root.setProperty("--accent", person.color);
    root.setProperty("--accent-ink", inkFor(person.color));
  } else {
    root.removeProperty("--accent");
    root.removeProperty("--accent-ink");
  }
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.content = colorOf(person);
}

// ------------------------------------------------------------ loading
async function boot() {
  try {
    state.me = await api("/api/me");
  } catch (e) {
    document.body.innerHTML = `<p class="empty">${esc(e.message)}</p>`;
    return;
  }
  $("#me").textContent = `${state.me.name} · ${isEditor() ? "can edit" : "can check off"}`;
  $("#settingsBtn").hidden = !isEditor();
  $("#addBtn").hidden = !isEditor();
  $("#logout").hidden = state.me.source !== "cloudflare";
  $("#sort").value = store.get("wl.sort") || "manual";
  $("#showBought").checked = store.get("wl.showBought") !== "0";
  await loadPeople();
  const saved = Number(store.get("wl.tab"));
  const first = state.people.find(p => p.id === saved) || state.people[0];
  if (first) await selectPerson(first.id); else renderItems();
}

async function loadPeople() {
  state.people = await api("/api/people");
  state.loadedAt = Date.now();
  renderTabs();
}

async function selectPerson(id) {
  state.current = id;
  store.set("wl.tab", String(id));
  renderTabs();
  const data = await api(`/api/people/${id}/items`);
  if (state.current !== id) return; // another tab was picked while this one loaded
  state.person = data.person;
  state.items = data.items;
  renderItems();
}

async function refresh() {
  await loadPeople();
  if (state.current && state.people.some(p => p.id === state.current)) await selectPerson(state.current);
  else if (state.people[0]) await selectPerson(state.people[0].id);
  else { state.person = null; state.items = []; renderItems(); }
}

// Installed on a phone, the app sits open for days. Catch up when it comes back to the front.
const REFRESH_AFTER_MS = 30_000;
const shouldRefresh = ({ visible, dialogOpen, busy, loadedAt, now }) =>
  visible && !dialogOpen && !busy && loadedAt > 0 && now - loadedAt > REFRESH_AFTER_MS;

async function backgroundRefresh() {
  state.refreshing = true;
  try { await refresh(); } catch { /* quietly keep what is on screen */ }
  finally { state.refreshing = false; }
}

// ------------------------------------------------------------ rendering
function renderTabs() {
  const nav = $("#tabs");
  nav.innerHTML = state.people.map(p => `
    <button class="tab" role="tab" data-id="${p.id}" aria-selected="${p.id === state.current}" style="--dot:${colorOf(p)}">
      <span class="dot"></span>${esc(p.name)}<span class="count">${p.count}</span>
    </button>`).join("");
  applyAccent(state.people.find(p => p.id === state.current));
  const sel = nav.querySelector('[aria-selected="true"]');
  if (sel) sel.scrollIntoView({ block: "nearest", inline: "nearest" });
}

const SORTS = {
  manual: (a, b) => a.position - b.position || a.id - b.id,
  priority: (a, b) => a.priority - b.priority || a.position - b.position,
  "price-asc": (a, b) => (a.price ?? Infinity) - (b.price ?? Infinity),
  "price-desc": (a, b) => (b.price ?? -Infinity) - (a.price ?? -Infinity),
  name: (a, b) => a.name.localeCompare(b.name, undefined, { sensitivity: "base" }),
  newest: (a, b) => b.created_at - a.created_at,
  where: (a, b) => (a.kind === b.kind ? 0 : a.kind === "online" ? -1 : 1) ||
                   (a.store || host(a.url)).localeCompare(b.store || host(b.url)),
};

function renderItems() {
  const ul = $("#items"), empty = $("#empty");
  const p = state.person;
  $("#ownerNote").hidden = !(p && p.is_me);

  if (!p) {
    ul.innerHTML = "";
    $("#summary").textContent = "";
    empty.hidden = false;
    empty.textContent = isEditor() ? "No lists yet. Open Settings to add a person." : "No lists yet.";
    return;
  }

  const sortKey = $("#sort").value;
  const showBought = $("#showBought").checked;
  let items = [...state.items].sort(SORTS[sortKey] || SORTS.manual);
  items.sort((a, b) => a.purchased - b.purchased); // stable: purchased sink to bottom
  if (!showBought) items = items.filter(i => !i.purchased);

  const total = state.items.length;
  const bought = state.items.filter(i => i.purchased).length;
  const remaining = state.items.filter(i => !i.purchased).reduce((s, i) => s + (i.price || 0), 0);
  $("#summary").innerHTML = p.is_me
    ? `<b>${total}</b> item${total === 1 ? "" : "s"} on your list`
    : `<b>${total - bought}</b> still wanted · <b>${bought}</b> purchased${remaining ? ` · ${money(remaining)} to go` : ""}`;

  const canMove = isEditor() && sortKey === "manual";
  ul.innerHTML = items.map(i => itemHTML(i, canMove)).join("");

  empty.hidden = items.length > 0;
  if (!items.length) {
    empty.textContent = total ? "Everything on this list has been purchased."
      : isEditor() ? `Nothing on ${p.name}'s list yet. Tap “Add item”.` : `Nothing on ${p.name}'s list yet.`;
  }
}

function itemHTML(i, canMove) {
  const p = state.person;
  const name = i.kind === "online" && i.url
    ? `<a href="${esc(i.url)}" target="_blank" rel="noopener noreferrer">${esc(i.name)}</a>`
    : esc(i.name);
  const where = i.kind === "online"
    ? `<span class="where">${ICON.link}${esc(host(i.url) || "Online")}</span>`
    : `<span class="where">${ICON.store}${esc(i.store || "In store")}</span>`;
  const stars = `<span class="stars" title="Priority">${"★".repeat(4 - i.priority)}</span>`;

  const acts = [];
  let got = "";
  if (!p.is_me) {
    if (i.purchased) {
      got = `<div class="got">${ICON.check}Got by ${esc(i.purchased_by)} · ${day(i.purchased_at)}</div>`;
      if (i.mine || isEditor()) acts.push(`<button class="btn small" data-act="unbuy" data-id="${i.id}">Undo purchase</button>`);
    } else {
      acts.push(`<button class="btn buy" data-act="buy" data-id="${i.id}">Mark purchased</button>`);
    }
  }
  acts.push('<span class="spacer"></span>');
  if (isEditor()) {
    if (canMove && !i.purchased) {
      acts.push(`<button class="btn small" data-act="up" data-id="${i.id}" aria-label="Move up">↑</button>`);
      acts.push(`<button class="btn small" data-act="down" data-id="${i.id}" aria-label="Move down">↓</button>`);
    }
    acts.push(`<button class="btn small" data-act="edit" data-id="${i.id}">Edit</button>`);
    acts.push(`<button class="btn small danger" data-act="del" data-id="${i.id}">Delete</button>`);
  }

  const thumb = i.thumb_url
    ? `<button class="thumb" data-act="photo" data-id="${i.id}" aria-label="View photo of ${esc(i.name)}"><img src="${esc(i.thumb_url)}" alt="" loading="lazy"></button>`
    : "";

  return `<li class="item${i.purchased ? " bought" : ""}${thumb ? " has-photo" : ""}">
    ${thumb}
    <div class="name">${name}</div>
    <div class="price${i.price == null ? " none" : ""}">${i.price == null ? "no price" : money(i.price)}</div>
    <div class="meta">${where}${stars}${i.removed_by_owner ? '<span class="removed">Removed from list after purchase</span>' : ""}</div>
    ${i.notes ? `<p class="notes">${esc(i.notes)}</p>` : ""}
    ${got}
    <div class="item-actions">${acts.join("")}</div>
  </li>`;
}

// ------------------------------------------------------------ item dialog
function setKind(form, kind) {
  form.classList.toggle("kind-online", kind === "online");
  form.classList.toggle("kind-store", kind === "store");
}

function openItemDialog(item) {
  const f = $("#itemForm");
  f.reset();
  $("#itemErr").textContent = "";
  state.editing = item || null;
  $("#itemDlgTitle").textContent = item ? "Edit item" : `Add to ${state.person.name}'s list`;
  const kind = item ? item.kind : (store.get("wl.kind") || "online");
  f.kind.value = kind;
  setKind(f, kind);
  if (item) {
    f.name.value = item.name;
    f.url.value = item.url || "";
    f.store.value = item.store || "";
    f.price.value = item.price ?? "";
    f.priority.value = String(item.priority);
    f.notes.value = item.notes || "";
  }
  resetPhoto(item && item.thumb_url);
  // Suggest stores already used on any visible list
  const stores = [...new Set(state.items.map(i => i.store).filter(Boolean))];
  $("#storeList").innerHTML = stores.map(s => `<option value="${esc(s)}">`).join("");
  $("#itemDlg").showModal();
  f.name.focus();
}

async function saveItem(e) {
  e.preventDefault();
  const f = e.target;
  const kind = f.kind.value;
  const body = {
    name: f.name.value, kind,
    url: kind === "online" ? f.url.value : "",
    store: kind === "store" ? f.store.value : "",
    price: f.price.value, priority: f.priority.value, notes: f.notes.value,
  };
  const submit = f.querySelector('button[type="submit"]');
  submit.disabled = true;
  try {
    const saved = state.editing
      ? await api(`/api/items/${state.editing.id}`, { method: "PATCH", body })
      : await api(`/api/people/${state.person.id}/items`, { method: "POST", body });
    try {
      if (state.photo.blob) {
        $("#photoHint").textContent = "Uploading photo…";
        await uploadPhoto(saved.id, state.photo.blob);
      } else if (state.photo.remove) {
        await api(`/api/items/${saved.id}/photo`, { method: "DELETE", body: {} });
      }
    } catch (err) {
      // The item itself saved; keep the dialog open on the edit view so the photo can be retried.
      state.editing = saved;
      $("#itemDlgTitle").textContent = "Edit item";
      $("#photoHint").textContent = "";
      $("#itemErr").textContent = `Item saved, but the photo didn't upload: ${err.message}`;
      await refresh();
      return;
    }
    store.set("wl.kind", kind);
    $("#itemDlg").close();
    toast(state.editing ? "Saved" : "Added");
    await refresh();
  } catch (err) { $("#itemErr").textContent = err.message; }
  finally { submit.disabled = false; }
}

// ------------------------------------------------------------ item photos
const PHOTO_HINT = "Take a picture or choose one. On a computer you can also paste or drop a screenshot here.";

function setPreview(url) {
  const img = $("#photoPreview");
  if (state.photo.previewUrl && state.photo.previewUrl.startsWith("blob:")) URL.revokeObjectURL(state.photo.previewUrl);
  state.photo.previewUrl = url;
  img.hidden = !url;
  if (url) img.src = url; else img.removeAttribute("src");
  $("#photoPick").textContent = url ? "Change photo" : "Add photo";
  $("#photoRemove").hidden = !url;
}

function resetPhoto(existingUrl) {
  state.photo.blob = null;
  state.photo.remove = false;
  $("#photoInput").value = "";
  $("#photoHint").textContent = PHOTO_HINT;
  setPreview(existingUrl || null);
}

// Shrink big phone photos before upload (faster over mobile data); the server re-encodes anyway.
async function shrinkImage(file, maxSide = 1600) {
  try {
    const bmp = await createImageBitmap(file, { imageOrientation: "from-image" });
    const scale = Math.min(1, maxSide / Math.max(bmp.width, bmp.height));
    const canvas = document.createElement("canvas");
    canvas.width = Math.round(bmp.width * scale);
    canvas.height = Math.round(bmp.height * scale);
    const ctx = canvas.getContext("2d");
    ctx.fillStyle = "#fff";                       // screenshots with transparency get a white background
    ctx.fillRect(0, 0, canvas.width, canvas.height);
    ctx.drawImage(bmp, 0, 0, canvas.width, canvas.height);
    bmp.close && bmp.close();
    const blob = await new Promise(res => canvas.toBlob(res, "image/jpeg", 0.88));
    return blob || file;
  } catch {
    return file; // browser couldn't decode it; let the server try
  }
}

async function takePhoto(file) {
  if (!file) return;
  if (!file.type.startsWith("image/")) { $("#itemErr").textContent = "That isn't an image file."; return; }
  $("#itemErr").textContent = "";
  $("#photoHint").textContent = "Preparing photo…";
  const blob = await shrinkImage(file);
  state.photo.blob = blob;
  state.photo.remove = false;
  setPreview(URL.createObjectURL(blob));
  $("#photoHint").textContent = "Photo will be saved with the item.";
}

async function uploadPhoto(itemId, blob) {
  const fd = new FormData();
  fd.append("photo", blob, "photo.jpg");
  return send(`/api/items/${itemId}/photo`, { method: "POST", headers: { "X-Wishlist": "1" }, body: fd }, "Upload");
}

function imageFrom(dataTransfer) {
  if (!dataTransfer) return null;
  for (const f of dataTransfer.files || []) if (f.type.startsWith("image/")) return f;
  for (const it of dataTransfer.items || []) {
    if (it.kind === "file" && it.type.startsWith("image/")) return it.getAsFile();
  }
  return null;
}

function openPhoto(item) {
  $("#photoBig").src = item.photo_url;
  $("#photoBig").alt = item.name;
  $("#photoCap").textContent = item.name;
  $("#photoDlg").showModal();
}

// ------------------------------------------------------------ purchase dialog
function openBuyDialog(item) {
  state.buying = item;
  const f = $("#buyForm");
  f.reset();
  $("#buyErr").textContent = "";
  $("#buyWhat").textContent = `${item.name} for ${state.person.name}`;
  f.by.value = store.get("wl.buyer") || (state.me.source === "lan" ? "" : state.me.name);
  $("#buyDlg").showModal();
  f.by.focus(); f.by.select();
}

async function saveBuy(e) {
  e.preventDefault();
  const by = e.target.by.value.trim();
  try {
    await api(`/api/items/${state.buying.id}/purchase`, { method: "POST", body: { purchased: true, by } });
    if (state.me.source === "lan") store.set("wl.buyer", by);
    $("#buyDlg").close();
    toast("Marked as purchased");
    await refresh();
  } catch (err) { $("#buyErr").textContent = err.message; }
}

// ------------------------------------------------------------ list actions
async function onListClick(e) {
  const b = e.target.closest("button[data-act]");
  if (!b) return;
  const item = state.items.find(i => i.id === Number(b.dataset.id));
  if (!item) return;
  try {
    switch (b.dataset.act) {
      case "buy": return openBuyDialog(item);
      case "photo": return openPhoto(item);
      case "edit": return openItemDialog(item);
      case "unbuy":
        if (!confirm(`Undo "${item.name}" being purchased by ${item.purchased_by}?`)) return;
        await api(`/api/items/${item.id}/purchase`, { method: "POST", body: { purchased: false } });
        break;
      case "del":
        if (!confirm(`Delete "${item.name}"?`)) return;
        await api(`/api/items/${item.id}`, { method: "DELETE", body: {} });
        toast("Deleted");
        break;
      case "up": case "down":
        await api(`/api/items/${item.id}/move`, { method: "POST", body: { dir: b.dataset.act } });
        break;
    }
    await refresh();
  } catch (err) { toast(err.message); }
}

// ------------------------------------------------------------ settings
async function openSettings() {
  $("#setErr").textContent = "";
  await renderSettings();
  $("#setDlg").showModal();
}

async function renderSettings() {
  const people = await api("/api/people");
  $("#peopleList").innerHTML = people.map((p, idx) => `
    <li>
      <input type="color" class="swatch" data-color-id="${p.id}" value="${colorOf(p)}" aria-label="Color for ${esc(p.name)}" title="Tab color">
      <span class="grow">${esc(p.name)}<span class="sub">${p.owner_email ? esc(p.owner_email) : "No email set"}</span></span>
      <button class="btn small" data-pact="up" data-id="${p.id}" ${idx === 0 ? "disabled" : ""} aria-label="Move left">↑</button>
      <button class="btn small" data-pact="down" data-id="${p.id}" ${idx === people.length - 1 ? "disabled" : ""} aria-label="Move right">↓</button>
      <button class="btn small" data-pact="edit" data-id="${p.id}">Edit</button>
      <button class="btn small danger" data-pact="del" data-id="${p.id}">Delete</button>
    </li>`).join("") || '<li class="muted">No lists yet.</li>';
  state._setPeople = people;

  const m = await api("/api/members");
  const rows = m.admins.map(a => `<li><span class="grow">${esc(a)}<span class="sub">Admin (set on the server) · can edit</span></span></li>`);
  for (const x of m.members) {
    if (m.admins.includes(x.email)) continue;
    rows.push(`<li><span class="grow">${esc(x.display_name)}<span class="sub">${esc(x.email)} · ${x.role === "editor" ? "can edit" : "can check off"}</span></span>
      <button class="btn small" data-mact="edit" data-email="${esc(x.email)}">Edit</button>
      <button class="btn small danger" data-mact="del" data-email="${esc(x.email)}">Remove</button></li>`);
  }
  $("#memberList").innerHTML = rows.join("") || '<li class="muted">No one added yet.</li>';
  state._members = m.members;
}

async function onSettingsClick(e) {
  const pb = e.target.closest("button[data-pact]");
  const mb = e.target.closest("button[data-mact]");
  $("#setErr").textContent = "";
  try {
    if (pb) {
      const id = Number(pb.dataset.id);
      const p = state._setPeople.find(x => x.id === id);
      if (pb.dataset.pact === "del") {
        if (!confirm(`Delete ${p.name}'s list and all of its items?`)) return;
        await api(`/api/people/${id}`, { method: "DELETE", body: {} });
      } else if (pb.dataset.pact === "edit") {
        const name = prompt("Name", p.name); if (name === null) return;
        const owner = prompt("Their email (leave blank for none). Their own tab will hide purchases.", p.owner_email || "");
        if (owner === null) return;
        await api(`/api/people/${id}`, { method: "PATCH", body: { name, owner_email: owner } });
      } else {
        await api(`/api/people/${id}`, { method: "PATCH", body: { move: pb.dataset.pact } });
      }
    } else if (mb) {
      const email = mb.dataset.email;
      if (mb.dataset.mact === "del") {
        if (!confirm(`Remove ${email}? They can still sign in if Cloudflare Access allows them, as a check-off-only user.`)) return;
        await api(`/api/members/${encodeURIComponent(email)}`, { method: "DELETE", body: {} });
      } else {
        const x = state._members.find(m => m.email === email);
        const f = $("#addMemberForm");
        f.email.value = x.email; f.display_name.value = x.display_name; f.role.value = x.role;
        f.display_name.focus();
        return;
      }
    } else return;
    await renderSettings();
  } catch (err) { $("#setErr").textContent = err.message; }
}

// ------------------------------------------------------------ wiring
document.addEventListener("DOMContentLoaded", () => {
  $("#tabs").addEventListener("click", e => {
    const t = e.target.closest(".tab");
    if (t) selectPerson(Number(t.dataset.id)).catch(err => toast(err.message));
  });
  $("#sort").addEventListener("change", e => { store.set("wl.sort", e.target.value); renderItems(); });
  $("#showBought").addEventListener("change", e => { store.set("wl.showBought", e.target.checked ? "1" : "0"); renderItems(); });
  $("#items").addEventListener("click", onListClick);
  $("#addBtn").addEventListener("click", () => state.person ? openItemDialog(null) : openSettings());
  $("#itemForm").addEventListener("submit", saveItem);
  $("#itemForm").addEventListener("change", e => { if (e.target.name === "kind") setKind(e.currentTarget, e.target.value); });
  $("#buyForm").addEventListener("submit", saveBuy);
  // photos: picker, paste, drag-and-drop, viewer
  $("#photoPick").addEventListener("click", () => $("#photoInput").click());
  $("#photoInput").addEventListener("change", e => takePhoto(e.target.files[0]));
  $("#photoRemove").addEventListener("click", () => {
    state.photo.blob = null;
    state.photo.remove = true;
    $("#photoInput").value = "";
    setPreview(null);
    $("#photoHint").textContent = "Photo will be removed when you save.";
  });
  $("#itemDlg").addEventListener("paste", e => {
    const file = imageFrom(e.clipboardData);
    if (file) { e.preventDefault(); takePhoto(file); }
  });
  const form = $("#itemForm");
  form.addEventListener("dragover", e => { e.preventDefault(); form.classList.add("dragging"); });
  form.addEventListener("dragleave", e => { if (!form.contains(e.relatedTarget)) form.classList.remove("dragging"); });
  form.addEventListener("drop", e => {
    e.preventDefault();
    form.classList.remove("dragging");
    takePhoto(imageFrom(e.dataTransfer));
  });
  $("#itemDlg").addEventListener("close", () => setPreview(null));
  $("#photoDlg").addEventListener("click", () => $("#photoDlg").close());
  $("#settingsBtn").addEventListener("click", () => openSettings().catch(err => toast(err.message)));
  $("#setDlg").addEventListener("click", onSettingsClick);
  $("#peopleList").addEventListener("change", async e => {
    const input = e.target.closest("input[data-color-id]");
    if (!input) return;
    try {
      await api(`/api/people/${input.dataset.colorId}`, { method: "PATCH", body: { color: input.value } });
      const p = state.people.find(x => x.id === Number(input.dataset.colorId));
      if (p) { p.color = input.value; renderTabs(); }
    } catch (err) { $("#setErr").textContent = err.message; }
  });
  $("#setDlg").addEventListener("close", () => refresh().catch(() => {}));
  $("#addPersonForm").addEventListener("submit", async e => {
    e.preventDefault();
    const f = e.target;
    try {
      await api("/api/people", { method: "POST", body: { name: f.name.value, owner_email: f.owner_email.value, color: f.color.value } });
      f.reset(); await renderSettings();
    } catch (err) { $("#setErr").textContent = err.message; }
  });
  $("#addMemberForm").addEventListener("submit", async e => {
    e.preventDefault();
    const f = e.target;
    try {
      await api("/api/members", { method: "PUT", body: { email: f.email.value, display_name: f.display_name.value, role: f.role.value } });
      f.reset(); await renderSettings();
    } catch (err) { $("#setErr").textContent = err.message; }
  });
  document.querySelectorAll("dialog [data-close]").forEach(b => b.addEventListener("click", () => b.closest("dialog").close()));
  document.addEventListener("visibilitychange", () => {
    if (shouldRefresh({ visible: document.visibilityState === "visible", dialogOpen: !!$("dialog[open]"),
                        busy: state.refreshing, loadedAt: state.loadedAt, now: Date.now() })) backgroundRefresh();
  });
  boot();
});

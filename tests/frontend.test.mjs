// Frontend unit tests: run with `node --test tests/` (pytest runs them too, see test_frontend.py).
// static/app.js is a plain browser script, so load it into a sandbox with a stub DOM
// and pull out the pure helpers.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import vm from "node:vm";

const src = readFileSync(new URL("../static/app.js", import.meta.url), "utf8");
const el = () => ({ hidden: false, textContent: "", innerHTML: "", value: "", addEventListener() {}, querySelector: () => null });
const session = new Map();
const nav = { reloads: 0 };
const ctx = vm.createContext({
  URL, atob, Blob,
  sessionStorage: {
    getItem: k => session.get(k) ?? null,
    setItem: (k, v) => session.set(k, String(v)),
    removeItem: k => session.delete(k),
  },
  location: { reload() { nav.reloads++; } },
  document: {
    addEventListener() {},
    querySelector: el,
    querySelectorAll: () => [],
    documentElement: { style: { setProperty() {}, removeProperty() {} } },
  },
});
vm.runInContext(src, ctx, { filename: "app.js" });
const { esc, money, host, inkFor, colorOf, SORTS, itemHTML, state, api, selectPerson, isAuthFailure, shouldRefresh,
        visibleItems, occasionsIn, canAddIdea, linkFill, linkOfferLabel, dataUrlBlob } =
  vm.runInContext(`({ esc, money, host, inkFor, colorOf, SORTS, itemHTML, state, api, selectPerson, isAuthFailure,
                      shouldRefresh, visibleItems, occasionsIn, canAddIdea, linkFill, linkOfferLabel,
                      dataUrlBlob })`, ctx);

const item = (over = {}) => ({
  id: 1, person_id: 1, name: "Thing", kind: "online", url: null, store: null, price: null, priority: 2,
  notes: null, position: 0, created_at: 1700000000, purchased: false, purchased_by: null,
  purchased_at: null, mine: false, removed_by_owner: false, photo_url: null, thumb_url: null, occasion: null,
  secret: false, my_idea: false, created_by: "Dad", reserved_by: null, reserved_at: null, reserved_mine: false,
  archived_at: null, ...over,
});
const as = (role, isMe = false) => {
  state.me = { role, name: "Tester", source: "cloudflare", email: "tester@example.com" };
  state.person = { id: 1, name: "Alex", is_me: isMe };
};
const names = list => list.map(i => i.name).join("");

test("esc escapes every HTML-significant character", () => {
  assert.equal(esc(`<a href="x" title='y'>&</a>`), "&lt;a href=&quot;x&quot; title=&#39;y&#39;&gt;&amp;&lt;/a&gt;");
  assert.equal(esc(null), "");
  assert.equal(esc(undefined), "");
  assert.equal(esc(0), "0");
});

test("money formats to two decimals and blanks out missing prices", () => {
  assert.equal(money(null), "");
  assert.equal(money(undefined), "");
  assert.match(money(5), /^\$5[.,]00$/);
  assert.match(money(0), /^\$0[.,]00$/);
});

test("host shows the bare domain and survives junk", () => {
  assert.equal(host("https://www.amazon.com/dp/B08?x=1"), "amazon.com");
  assert.equal(host("https://shop.example.co.uk:8443/a"), "shop.example.co.uk");
  assert.equal(host("not a url"), "");
  assert.equal(host(null), "");
});

test("inkFor picks readable text for light and dark tab colors", () => {
  assert.equal(inkFor("#000000"), "#ffffff");
  assert.equal(inkFor("#1f4d3a"), "#ffffff");
  assert.equal(inkFor("#ffffff"), "#1d2420");
  assert.equal(inkFor("#ffe066"), "#1d2420");
  assert.equal(colorOf(null), "#1f4d3a");
  assert.equal(colorOf({ color: "#abcdef" }), "#abcdef");
});

test("sorts", () => {
  const a = item({ id: 1, name: "banana", price: 10, priority: 3, position: 2, created_at: 5 });
  const b = item({ id: 2, name: "Apple", price: null, priority: 1, position: 0, created_at: 9 });
  const c = item({ id: 3, name: "cherry", price: 2.5, priority: 3, position: 1, created_at: 1, kind: "store", store: "Target" });
  const by = k => names([a, b, c].sort(SORTS[k]));
  assert.equal(by("manual"), "Applecherrybanana");
  assert.equal(by("priority"), "Applecherrybanana"); // ties fall back to manual order
  assert.equal(by("price-asc"), "cherrybananaApple"); // unpriced last
  assert.equal(by("price-desc"), "bananacherryApple"); // unpriced last here too
  assert.equal(by("name"), "Applebananacherry"); // case-insensitive
  assert.equal(by("newest"), "Applebananacherry");
  assert.equal(names([c, a].sort(SORTS.where)), "bananacherry"); // online before in-store
});

test("item markup escapes everything users can type", () => {
  as("editor");
  const evil = `"><script>alert(1)</script><img src=x onerror=alert(1)>`;
  const html = itemHTML(item({
    name: evil, notes: evil, url: `https://example.com/?q=${evil}`, purchased: true, purchased_by: evil,
    purchased_at: 1700000000, thumb_url: `/photos/1/thumb?v=1${evil}`,
  }), true) + itemHTML(item({ kind: "store", store: evil, name: evil }), true);
  assert.ok(!/<script|<img src=x|onerror=alert\(1\)>/.test(html), html);
  assert.ok(html.includes("&lt;script&gt;"));
});

test("links open safely in a new tab; in-store items are not links", () => {
  as("shopper");
  const html = itemHTML(item({ url: "https://example.com/p" }), false);
  assert.ok(html.includes('href="https://example.com/p" target="_blank" rel="noopener noreferrer"'));
  assert.ok(!itemHTML(item({ kind: "store", store: "Target", url: "https://example.com" }), false).includes("<a "));
});

test("shoppers can mark purchased but get no editing controls", () => {
  as("shopper");
  const html = itemHTML(item(), true);
  assert.ok(html.includes('data-act="buy"'));
  for (const act of ["edit", "del", "up", "down"]) assert.ok(!html.includes(`data-act="${act}"`), act);
});

test("undo is offered only to the buyer or an editor", () => {
  const bought = { purchased: true, purchased_by: "Grandma", purchased_at: 1700000000 };
  as("shopper");
  assert.ok(!itemHTML(item(bought), false).includes('data-act="unbuy"'));
  assert.ok(itemHTML(item({ ...bought, mine: true }), false).includes('data-act="unbuy"'));
  assert.ok(itemHTML(item(bought), false).includes("Got by Grandma"));
  as("editor");
  assert.ok(itemHTML(item(bought), false).includes('data-act="unbuy"'));
});

test("the owner's own tab shows no purchase controls or purchase state", () => {
  as("editor", true);
  // the server strips these fields for owners; the markup must not add anything back even if one slipped through
  const html = itemHTML(item({ purchased: true, purchased_by: "Grandma", purchased_at: 1700000000, mine: true }), true);
  for (const leak of ['data-act="buy"', 'data-act="unbuy"', "Got by", "Grandma"]) assert.ok(!html.includes(leak), leak);
  assert.ok(html.includes('data-act="edit"'));
});

test("move buttons only in manual order, and not on purchased items", () => {
  as("editor");
  assert.ok(itemHTML(item(), true).includes('data-act="up"'));
  assert.ok(!itemHTML(item(), false).includes('data-act="up"'));
  assert.ok(!itemHTML(item({ purchased: true, purchased_by: "G", purchased_at: 1 }), true).includes('data-act="up"'));
});

const acts = html => [...html.matchAll(/data-act="(\w+)"/g)].map(m => m[1]);

test("open items offer Reserve beside Mark purchased", () => {
  as("shopper");
  assert.deepEqual(acts(itemHTML(item(), false)), ["buy", "reserve"]);
});

test("someone else's reservation blocks buying; only an editor can release it", () => {
  const held = { reserved_by: "Grandma", reserved_at: 1700000000 };
  as("shopper");
  const html = itemHTML(item(held), false);
  assert.ok(html.includes("Reserved by Grandma"));
  assert.deepEqual(acts(html), []);
  as("editor");
  assert.deepEqual(acts(itemHTML(item(held), false)), ["release", "edit", "del"]);
});

test("the reserver can mark purchased or release", () => {
  as("shopper");
  assert.deepEqual(acts(itemHTML(item({ reserved_by: "Me", reserved_at: 1, reserved_mine: true }), false)), ["buy", "release"]);
});

test("the owner's own tab shows no reservation controls or state", () => {
  as("editor", true);
  const html = itemHTML(item({ reserved_by: "Grandma", reserved_at: 1, reserved_mine: true }), false);
  for (const leak of ['data-act="reserve"', 'data-act="release"', "Reserved by", "Grandma"]) assert.ok(!html.includes(leak), leak);
});

test("secret ideas are labelled, and their author can edit them but not reorder", () => {
  as("shopper");
  const evil = "<b>x</b>";
  const html = itemHTML(item({ secret: true, my_idea: true, created_by: evil }), true);
  assert.ok(html.includes("Secret idea from &lt;b&gt;x&lt;/b&gt;") && html.includes('class="item secret'));
  assert.deepEqual(acts(html), ["buy", "reserve", "edit", "del"]);
  assert.deepEqual(acts(itemHTML(item({ secret: true, created_by: "Dad" }), true)), ["buy", "reserve"]);
});

test("only signed-in visitors to someone else's tab can add secret ideas", () => {
  as("shopper");
  assert.equal(canAddIdea(state.person), true);
  assert.equal(canAddIdea({ ...state.person, is_me: true }), false);
  assert.equal(canAddIdea(null), false);
  state.me = { role: "editor", name: "Home", source: "lan", email: null };
  assert.equal(canAddIdea(state.person), false);
});

test("editors can archive purchased items, and restore them from History", () => {
  const bought = { purchased: true, purchased_by: "Grandma", purchased_at: 1700000000 };
  as("editor");
  assert.ok(acts(itemHTML(item(bought), false)).includes("archive"));
  assert.deepEqual(acts(itemHTML(item({ ...bought, archived_at: 1700000500 }), false)), ["restore", "del"]);
  as("shopper");
  assert.ok(!acts(itemHTML(item(bought), false)).includes("archive"));
});

test("History shows the owner who gave each gift, without restore", () => {
  as("editor", true);
  const html = itemHTML(item({ purchased: true, purchased_by: "Grandma", purchased_at: 1700000000, archived_at: 1700000500 }), false);
  assert.ok(html.includes("Given by Grandma"));
  assert.ok(!html.includes('data-act="restore"'));
});

test("occasion tags are escaped and drive the filter", () => {
  as("shopper");
  assert.ok(itemHTML(item({ occasion: "<i>Xmas</i>" }), false).includes('<span class="tag">&lt;i&gt;Xmas&lt;/i&gt;</span>'));
  const list = [item({ id: 1, name: "a", occasion: "birthday" }), item({ id: 2, name: "b", occasion: "Christmas" }),
                item({ id: 3, name: "c", occasion: "Christmas", purchased: true }), item({ id: 4, name: "d" })];
  assert.equal(occasionsIn(list).join(), "birthday,Christmas");
  assert.equal(names(visibleItems(list, { sortKey: "manual", occasion: "Christmas" })), "bc");
  assert.equal(names(visibleItems(list, { sortKey: "manual", occasion: "Christmas", showBought: false })), "b");
  assert.equal(names(visibleItems(list, { sortKey: "name" })), "abdc"); // purchased still sink
  assert.equal(names(visibleItems([list[3], list[0]], {})), "da"); // no sort key keeps the server's order
});

test("priority stars: 1 is three stars, 3 is one", () => {
  as("shopper");
  const stars = p => itemHTML(item({ priority: p }), false).match(/★+/)[0].length;
  assert.deepEqual([stars(1), stars(2), stars(3)], [3, 2, 1]);
});

// ------------------------------------------------------------ refresh and expired sessions
const reply = (status, body, type = "basic") => ({
  status, type, ok: status >= 200 && status < 300,
  json: async () => { if (body === undefined) throw new SyntaxError("not JSON"); return body; },
});
const ACCESS_REDIRECT = reply(0, undefined, "opaqueredirect"); // what fetch sees when Access bounces to its login
const serve = (...replies) => {
  const seen = [];
  ctx.fetch = async (path, init) => { seen.push(init); return replies.shift(); };
  return seen;
};
const settled = p => Promise.race([p.then(() => "resolved", () => "rejected"), new Promise(r => setTimeout(r, 20, "pending"))]);
const fresh = () => { session.clear(); nav.reloads = 0; };

test("auth failures are a redirect from Access, a 401, or the server's sign-in 403", () => {
  const fail = (status, message = "x") => Object.assign(new Error(message), { status });
  assert.ok(isAuthFailure(fail(0)));
  assert.ok(isAuthFailure(fail(401)));
  assert.ok(isAuthFailure(fail(403, "Cloudflare Access sign-in required.")));
  assert.ok(!isAuthFailure(fail(403, "Only editors can do that.")));
  assert.ok(!isAuthFailure(fail(403, "That's your own list — no peeking!")));
  assert.ok(!isAuthFailure(fail(400)));
  // offline: a reload would only swap the list for the browser's error page
  assert.ok(!isAuthFailure(new TypeError("Failed to fetch")));
});

test("refresh on return only when the list is stale, the page visible and nothing is open", () => {
  const ok = { visible: true, dialogOpen: false, busy: false, loadedAt: 1_000_000, now: 1_031_000 };
  assert.equal(shouldRefresh(ok), true);
  assert.equal(shouldRefresh({ ...ok, now: ok.loadedAt + 5_000 }), false);
  assert.equal(shouldRefresh({ ...ok, visible: false }), false);
  assert.equal(shouldRefresh({ ...ok, dialogOpen: true }), false);
  assert.equal(shouldRefresh({ ...ok, busy: true }), false);
  assert.equal(shouldRefresh({ ...ok, loadedAt: 0 }), false); // still booting, or boot failed
});

test("requests don't follow redirects, so an Access login bounce is visible", async () => {
  fresh();
  const seen = serve(reply(200, { ok: true }));
  await api("/api/me");
  assert.equal(seen[0].redirect, "manual");
});

test("an expired session reloads the page once, then reports the error instead of looping", async () => {
  fresh();
  serve(ACCESS_REDIRECT, ACCESS_REDIRECT);
  assert.equal(await settled(api("/api/people")), "pending"); // the page is going away: no toast, nothing cleared
  assert.equal(nav.reloads, 1);
  const again = api("/api/people");
  assert.equal(await settled(again), "rejected");
  await assert.rejects(again, e => e.status === 0 && /sign/i.test(e.message));
  assert.equal(nav.reloads, 1);
});

test("a successful request re-arms the reload", async () => {
  fresh();
  serve(reply(403, { error: "Cloudflare Access sign-in required." }), reply(200, []),
        reply(403, { error: "Cloudflare Access sign-in required." }));
  api("/api/people");
  await api("/api/people");
  assert.equal(await settled(api("/api/people")), "pending");
  assert.equal(nav.reloads, 2);
});

test("other errors are thrown with their message and never reload", async () => {
  fresh();
  serve(reply(403, { error: "Only editors can do that." }), reply(500, undefined));
  await assert.rejects(api("/api/items/1", { method: "DELETE", body: {} }), { message: "Only editors can do that." });
  await assert.rejects(api("/api/people"), { message: "Request failed (500)" });
  assert.equal(nav.reloads, 0);
});

test("no reload when the guard can't be stored, so it can't loop", async () => {
  fresh();
  const { setItem } = ctx.sessionStorage;
  ctx.sessionStorage.setItem = () => { throw new Error("blocked"); };
  try {
    serve(ACCESS_REDIRECT);
    assert.equal(await settled(api("/api/people")), "rejected");
    assert.equal(nav.reloads, 0);
  } finally { ctx.sessionStorage.setItem = setItem; }
});

test("a slow response for a tab you've left doesn't replace the tab you're on", async () => {
  fresh();
  const pending = [];
  ctx.fetch = () => new Promise(res => pending.push(res));
  state.me = { role: "shopper", name: "Tester", source: "cloudflare" };
  state.people = [{ id: 1, name: "Alex" }, { id: 2, name: "Sam" }];
  const first = selectPerson(1), second = selectPerson(2);
  await new Promise(r => setTimeout(r, 0));
  pending[1](reply(200, { person: { id: 2, name: "Sam" }, items: [item({ name: "for Sam" })] }));
  await second;
  pending[0](reply(200, { person: { id: 1, name: "Alex" }, items: [item({ name: "for Alex" })] }));
  await first;
  assert.equal(state.person.name, "Sam");
  assert.equal(names(state.items), "for Sam");
});

test("link preview fills only fields that are still empty", () => {
  const found = { title: "Orchid", price: 49.5, image: "data:image/jpeg;base64,AAAA" };
  const empty = { name: "", price: "", hasPhoto: false };
  assert.deepEqual({ ...linkFill(found, empty) }, { name: "Orchid", price: "49.50", image: found.image });
  assert.deepEqual({ ...linkFill(found, { name: "My words", price: " 12 ", hasPhoto: true }) }, {});
  assert.deepEqual({ ...linkFill(found, { ...empty, name: "  " }) }, { name: "Orchid", price: "49.50", image: found.image });
  assert.deepEqual({ ...linkFill({ title: null, price: null, image: null }, empty) }, {});
  assert.deepEqual({ ...linkFill(null, empty) }, {});
  assert.deepEqual({ ...linkFill({ title: "", price: 0, image: null }, empty) }, { price: "0.00" });
});

test("link offer names what it would fill", () => {
  assert.equal(linkOfferLabel({}), "");
  assert.equal(linkOfferLabel({ price: "1.00" }), "Fill in price from the page");
  assert.equal(linkOfferLabel({ name: "x", image: "data:" }), "Fill in name and photo from the page");
  assert.equal(linkOfferLabel({ name: "x", price: "1.00", image: "data:" }), "Fill in name, price and photo from the page");
});

test("preview photos decode from data: URLs and nothing else", async () => {
  const blob = dataUrlBlob("data:image/jpeg;base64," + Buffer.from([0xff, 0xd8, 0xff, 0x00]).toString("base64"));
  assert.equal(blob.type, "image/jpeg");
  assert.deepEqual([...new Uint8Array(await blob.arrayBuffer())], [0xff, 0xd8, 0xff, 0x00]);
  assert.equal(dataUrlBlob("https://shop.example.com/a.jpg"), null);
  assert.equal(dataUrlBlob("data:image/jpeg;base64,<script>"), null);
  assert.equal(dataUrlBlob(null), null);
});

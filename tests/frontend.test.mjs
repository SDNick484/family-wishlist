// Frontend unit tests: run with `node --test tests/` (pytest runs them too, see test_frontend.py).
// static/app.js is a plain browser script, so load it into a sandbox with a stub DOM
// and pull out the pure helpers.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import vm from "node:vm";

const src = readFileSync(new URL("../static/app.js", import.meta.url), "utf8");
const el = () => ({ hidden: false, textContent: "", innerHTML: "", value: "", addEventListener() {} });
const ctx = vm.createContext({
  URL,
  document: {
    addEventListener() {},
    querySelector: el,
    querySelectorAll: () => [],
    documentElement: { style: { setProperty() {}, removeProperty() {} } },
  },
});
vm.runInContext(src, ctx, { filename: "app.js" });
const { esc, money, host, inkFor, colorOf, SORTS, itemHTML, state } =
  vm.runInContext("({ esc, money, host, inkFor, colorOf, SORTS, itemHTML, state })", ctx);

const item = (over = {}) => ({
  id: 1, person_id: 1, name: "Thing", kind: "online", url: null, store: null, price: null, priority: 2,
  notes: null, position: 0, created_at: 1700000000, purchased: false, purchased_by: null,
  purchased_at: null, mine: false, removed_by_owner: false, photo_url: null, thumb_url: null, ...over,
});
const as = (role, isMe = false) => {
  state.me = { role, name: "Tester", source: "cloudflare" };
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

test("priority stars: 1 is three stars, 3 is one", () => {
  as("shopper");
  const stars = p => itemHTML(item({ priority: p }), false).match(/★+/)[0].length;
  assert.deepEqual([stars(1), stars(2), stars(3)], [3, 2, 1]);
});

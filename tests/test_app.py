import io
import os

import pytest
from PIL import Image

import app as wishlist
from conftest import ADMIN, KEY, OTHER_KEY, LAN_IP  # noqa: F401


# ------------------------------------------------------------------ identity / access
class TestAccess:
    def test_cloudflare_request_without_token_is_rejected(self, client):
        assert client.get("/api/me").status_code == 403

    @pytest.mark.parametrize("tok", [{"key": OTHER_KEY}, {"aud": "wrong"}, {"exp": -10},
                                     {"iss": "https://evil.cloudflareaccess.com"}])
    def test_bad_tokens_are_rejected(self, client, tok):
        assert client.get("/api/me", ADMIN, **tok).status_code == 403

    def test_admin_email_is_editor(self, client):
        assert client.get("/api/me", ADMIN).json["role"] == "editor"

    def test_unknown_email_is_shopper(self, client):
        me = client.get("/api/me", "grandma@example.com").json
        assert me["role"] == "shopper" and me["name"] == "Grandma"

    def test_token_from_cookie(self, client):
        client.c.set_cookie("CF_Authorization", __import__("conftest").token(ADMIN))
        r = client.c.get("/api/me", headers={"Cf-Ray": "x"}, environ_base={"REMOTE_ADDR": "203.0.113.5"})
        assert r.status_code == 200 and r.json["email"] == ADMIN

    def test_spoofed_email_header_on_lan_is_rejected(self, client):
        r = client.lan("get", "/api/me", headers={"Cf-Access-Authenticated-User-Email": ADMIN})
        assert r.status_code == 403

    def test_plain_lan_request_gets_lan_role(self, client):
        me = client.lan("get", "/api/me").json
        assert me["role"] == "editor" and me["source"] == "lan"

    def test_public_ip_without_cloudflare_is_rejected(self, client):
        assert client.c.get("/api/me", environ_base={"REMOTE_ADDR": "8.8.8.8"}).status_code == 403

    def test_health_check_is_open(self, client):
        assert client.c.get("/healthz", environ_base={"REMOTE_ADDR": "8.8.8.8"}).data == b"ok"

    def test_security_headers(self, client):
        r = client.lan("get", "/")
        assert r.status_code == 200
        assert "frame-ancestors 'none'" in r.headers["Content-Security-Policy"]
        assert r.headers["X-Frame-Options"] == "DENY"


class TestCsrf:
    def test_mutation_without_custom_header_blocked(self, client, people):
        r = client.post(f"/api/people/{people['Alex']}/items", ADMIN,
                        json={"name": "x", "kind": "online"}, csrf=False)
        assert r.status_code == 400

    def test_cross_origin_mutation_blocked(self, client, people):
        h = client.headers(ADMIN)
        h["Origin"] = "https://evil.example"
        r = client.req("post", f"/api/people/{people['Alex']}/items", json={"name": "x", "kind": "online"},
                       headers=h)
        assert r.status_code == 403


# ------------------------------------------------------------------ items
def add(client, pid, email=ADMIN, **fields):
    body = {"name": "Thing", "kind": "online", **fields}
    return client.post(f"/api/people/{pid}/items", email, json=body)


class TestItems:
    def test_add_online_item_normalizes_url_and_price(self, client, people):
        r = add(client, people["Alex"], url="amazon.com/dp/B08", price="$1,049.99", priority=1)
        assert r.status_code == 201
        assert r.json["url"] == "https://amazon.com/dp/B08" and r.json["price"] == 1049.99

    def test_add_in_store_item(self, client, people):
        r = add(client, people["Alex"], kind="store", store="Michaels", price=25)
        assert r.json["store"] == "Michaels" and r.json["kind"] == "store"

    @pytest.mark.parametrize("url", ["javascript:alert(1)", "data:text/html,hi", "file:///etc/passwd"])
    def test_non_web_links_rejected(self, client, people, url):
        assert add(client, people["Alex"], url=url).status_code == 400

    def test_validation(self, client, people):
        assert add(client, people["Alex"], name=" ").status_code == 400
        assert add(client, people["Alex"], kind="mail").status_code == 400
        assert add(client, people["Alex"], price="abc").status_code == 400
        assert add(client, people["Alex"], price=-1).status_code == 400

    def test_shopper_cannot_add_or_edit(self, client, people):
        assert add(client, people["Alex"], "grandma@example.com").status_code == 403
        iid = add(client, people["Alex"]).json["id"]
        assert client.patch(f"/api/items/{iid}", "grandma@example.com", json={"price": 1}).status_code == 403

    def test_edit_and_manual_reorder(self, client, people):
        a = add(client, people["Alex"], name="A").json["id"]
        b = add(client, people["Alex"], name="B").json["id"]
        assert client.patch(f"/api/items/{b}", ADMIN, json={"notes": "blue"}).json["notes"] == "blue"
        client.post(f"/api/items/{b}/move", ADMIN, json={"dir": "up"})
        items = client.get(f"/api/people/{people['Alex']}/items", ADMIN).json["items"]
        assert [i["id"] for i in sorted(items, key=lambda i: i["position"])] == [b, a]


# ------------------------------------------------------------------ purchases + spoilers
class TestPurchases:
    def setup_member(self, client):
        client.put("/api/members", ADMIN, json={"email": "grandma@example.com",
                                                "display_name": "Grandma", "role": "shopper"})

    def test_mark_purchased_uses_member_name(self, client, people):
        self.setup_member(client)
        iid = add(client, people["Alex"]).json["id"]
        r = client.post(f"/api/items/{iid}/purchase", "grandma@example.com", json={"purchased": True})
        assert r.json["purchased"] and r.json["purchased_by"] == "Grandma" and r.json["mine"]

    def test_custom_who_got_it(self, client, people):
        iid = add(client, people["Alex"]).json["id"]
        r = client.post(f"/api/items/{iid}/purchase", "uncle@example.com", json={"by": "Uncle & family"})
        assert r.json["purchased_by"] == "Uncle & family"

    def test_double_purchase_blocked(self, client, people):
        iid = add(client, people["Alex"]).json["id"]
        client.post(f"/api/items/{iid}/purchase", "a@example.com")
        assert client.post(f"/api/items/{iid}/purchase", "b@example.com").status_code == 400

    def test_only_buyer_or_editor_can_undo(self, client, people):
        iid = add(client, people["Alex"]).json["id"]
        client.post(f"/api/items/{iid}/purchase", "a@example.com")
        assert client.post(f"/api/items/{iid}/purchase", "b@example.com",
                           json={"purchased": False}).status_code == 403
        assert client.post(f"/api/items/{iid}/purchase", ADMIN,
                           json={"purchased": False}).json["purchased"] is False

    def test_tab_stats(self, client, people):
        a = add(client, people["Alex"], price=10).json["id"]
        add(client, people["Alex"], price=22.5)
        client.post(f"/api/items/{a}/purchase", "a@example.com")
        tab = {p["name"]: p for p in client.get("/api/people", "a@example.com").json}["Alex"]
        assert tab["bought"] == 1 and tab["remaining"] == 22.5
        assert "owner_email" not in tab  # shoppers never see emails


class TestSpoilerProtection:
    @pytest.fixture
    def setup(self, client, people):
        pid = people["Alex"]
        client.patch(f"/api/people/{pid}", ADMIN, json={"owner_email": "alex@example.com"})
        client.put("/api/members", ADMIN, json={"email": "alex@example.com", "display_name": "Alex",
                                                "role": "editor"})
        bought = add(client, pid, name="Bought").json["id"]
        open_ = add(client, pid, name="Open").json["id"]
        client.post(f"/api/items/{bought}/purchase", "grandma@example.com")
        return pid, bought, open_

    def test_owner_sees_no_purchase_state(self, client, setup):
        pid, *_ = setup
        own = client.get(f"/api/people/{pid}/items", "alex@example.com").json
        assert own["person"]["is_me"]
        assert not any(i["purchased"] or i["purchased_by"] for i in own["items"])
        tab = {p["id"]: p for p in client.get("/api/people", "alex@example.com").json}[pid]
        assert "bought" not in tab

    def test_owner_cannot_mark_own_items(self, client, setup):
        _, _, open_ = setup
        assert client.post(f"/api/items/{open_}/purchase", "alex@example.com").status_code == 403

    def test_owner_deleting_bought_item_hides_it_but_keeps_buyer_record(self, client, setup):
        pid, bought, _ = setup
        client.delete(f"/api/items/{bought}", "alex@example.com")
        own = client.get(f"/api/people/{pid}/items", "alex@example.com").json["items"]
        assert bought not in [i["id"] for i in own]
        theirs = {i["id"]: i for i in client.get(f"/api/people/{pid}/items", "grandma@example.com").json["items"]}
        assert theirs[bought]["purchased"] and theirs[bought]["removed_by_owner"]
        # undoing the purchase of a removed item finally deletes it
        r = client.post(f"/api/items/{bought}/purchase", "grandma@example.com", json={"purchased": False})
        assert r.json == {"deleted": True}

    def test_owner_deleting_unbought_item_really_deletes(self, client, setup):
        pid, _, open_ = setup
        client.delete(f"/api/items/{open_}", "alex@example.com")
        ids = [i["id"] for i in client.get(f"/api/people/{pid}/items", ADMIN).json["items"]]
        assert open_ not in ids


# ------------------------------------------------------------------ settings
class TestSettings:
    def test_seeded_people_and_default_colors(self, client):
        tabs = client.get("/api/people", ADMIN).json
        assert [p["name"] for p in tabs] == ["Alex", "Sam", "Jordan"]
        assert [p["color"] for p in tabs] == ["#1f4d3a", "#2563eb", None]

    def test_color_set_in_settings_survives_restart(self, client, people):
        client.patch(f"/api/people/{people['Sam']}", ADMIN, json={"color": "#ABCDEF"})
        wishlist.init_db()  # what a service restart does
        tabs = {p["name"]: p for p in client.get("/api/people", ADMIN).json}
        assert tabs["Sam"]["color"] == "#abcdef"

    def test_color_validation_and_permissions(self, client, people):
        assert client.patch(f"/api/people/{people['Sam']}", ADMIN, json={"color": "red; x"}).status_code == 400
        assert client.patch(f"/api/people/{people['Sam']}", "s@example.com",
                            json={"color": "#000000"}).status_code == 403

    def test_add_reorder_delete_person(self, client, people):
        new = client.post("/api/people", ADMIN, json={"name": "Grandpa"}).json["id"]
        client.patch(f"/api/people/{new}", ADMIN, json={"move": "up"})
        assert [p["name"] for p in client.get("/api/people", ADMIN).json] == ["Alex", "Sam", "Grandpa", "Jordan"]
        client.delete(f"/api/people/{people['Sam']}", ADMIN)
        assert "Sam" not in [p["name"] for p in client.get("/api/people", ADMIN).json]

    def test_members_are_editor_only(self, client):
        assert client.get("/api/members", "s@example.com").status_code == 403
        assert client.put("/api/members", ADMIN, json={"email": "bad", "display_name": "x"}).status_code == 400


# ------------------------------------------------------------------ photos
def png(w=900, h=600):
    im = Image.new("RGBA", (w, h), (200, 30, 60, 0))  # fully transparent background
    im.paste((10, 200, 10, 255), (w // 4, h // 4, w // 2, h // 2))
    buf = io.BytesIO()
    im.save(buf, "PNG")
    buf.seek(0)
    return buf


def no_files(iid):
    return not any(os.path.exists(p) for p in wishlist.photo_paths(iid))


class TestPhotos:
    def test_upload_view_and_flatten_transparency(self, client, people):
        iid = add(client, people["Alex"]).json["id"]
        r = client.upload(iid, png())
        assert r.status_code == 200 and r.json["thumb_url"].endswith("?v=1")
        full = client.get(r.json["photo_url"], "shopper@example.com")
        assert full.status_code == 200 and full.mimetype == "image/jpeg"
        assert "immutable" in full.headers["Cache-Control"]
        im = Image.open(io.BytesIO(full.data))
        assert im.size == (900, 600) and im.getpixel((5, 5)) > (240, 240, 240)
        thumb = Image.open(io.BytesIO(client.get(r.json["thumb_url"], "shopper@example.com").data))
        assert max(thumb.size) == 400

    def test_phone_photo_rotated_resized_and_stripped(self, client, people):
        iid = add(client, people["Alex"]).json["id"]
        big = Image.new("RGB", (4000, 3000), (90, 90, 90))
        exif = big.getexif()
        exif[0x0112] = 6  # "rotate 90° CW"
        buf = io.BytesIO()
        big.save(buf, "JPEG", exif=exif.tobytes())
        buf.seek(0)
        r = client.upload(iid, buf, filename="IMG_0001.jpg")
        im = Image.open(io.BytesIO(client.get(r.json["photo_url"], ADMIN).data))
        assert im.size == (1200, 1600) and not im.getexif()

    def test_replacing_bumps_version(self, client, people):
        iid = add(client, people["Alex"]).json["id"]
        client.upload(iid, png())
        assert client.upload(iid, png()).json["photo_url"].endswith("?v=2")

    def test_rejections(self, client, people):
        iid = add(client, people["Alex"]).json["id"]
        assert client.upload(iid, png(), email="shopper@example.com").status_code == 403
        assert client.upload(iid, png(), csrf=False).status_code == 400
        assert client.upload(iid, io.BytesIO(b"not an image"), filename="x.jpg").status_code == 400
        bomb = io.BytesIO()
        Image.new("L", (9000, 9000)).save(bomb, "PNG")
        bomb.seek(0)
        assert client.upload(iid, bomb, filename="bomb.png").status_code == 400
        r = client.upload(iid, io.BytesIO(b"0" * (21 * 1024 * 1024)), filename="huge.jpg")
        assert r.status_code == 413 and "too large" in r.json["error"]

    def test_photo_requires_sign_in(self, client, people):
        iid = add(client, people["Alex"]).json["id"]
        client.upload(iid, png())
        r = client.c.get(f"/photos/{iid}/full", environ_base={"REMOTE_ADDR": "8.8.8.8"})
        assert r.status_code == 403

    def test_remove_photo(self, client, people):
        iid = add(client, people["Alex"]).json["id"]
        client.upload(iid, png())
        r = client.delete(f"/api/items/{iid}/photo", ADMIN)
        assert r.json["photo_url"] is None and no_files(iid)
        assert client.get(f"/photos/{iid}/full", ADMIN).status_code == 404

    def test_deleting_item_or_person_removes_photo_files(self, client, people):
        a = add(client, people["Alex"]).json["id"]
        client.upload(a, png())
        client.delete(f"/api/items/{a}", ADMIN)
        assert no_files(a)
        b = add(client, people["Sam"]).json["id"]
        client.upload(b, png())
        client.delete(f"/api/people/{people['Sam']}", ADMIN)
        assert no_files(b)


# ------------------------------------------------------------------ upgrades
def test_upgrade_from_first_release_schema(tmp_path, monkeypatch):
    """Databases created before tab colors and photos existed gain the new columns, keeping data."""
    import sqlite3
    db = tmp_path / "wishlist.db"
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE people (id INTEGER PRIMARY KEY, name TEXT NOT NULL, owner_email TEXT,
                             position INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE items (id INTEGER PRIMARY KEY, person_id INTEGER NOT NULL, name TEXT NOT NULL,
          kind TEXT NOT NULL, url TEXT, store TEXT, price REAL, priority INTEGER NOT NULL DEFAULT 2,
          notes TEXT, position INTEGER NOT NULL DEFAULT 0, created_at INTEGER NOT NULL, created_by TEXT,
          purchased INTEGER NOT NULL DEFAULT 0, purchased_by TEXT, purchased_by_email TEXT,
          purchased_at INTEGER, removed_by_owner INTEGER NOT NULL DEFAULT 0);
        INSERT INTO people (name, position) VALUES ('Sammy', 0);
        INSERT INTO items (person_id, name, kind, created_at) VALUES (1, 'Kept', 'online', 0);
    """)
    conn.commit()
    conn.close()
    monkeypatch.setattr(wishlist, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(wishlist, "DB_PATH", str(db))
    wishlist.init_db()
    conn = sqlite3.connect(db)
    assert "color" in [r[1] for r in conn.execute("PRAGMA table_info(people)")]
    assert "photo_ver" in [r[1] for r in conn.execute("PRAGMA table_info(items)")]
    assert conn.execute("SELECT name FROM items").fetchall() == [("Kept",)]
    # "Sam|Sammy" in PERSON_COLORS matches the alias
    assert conn.execute("SELECT color FROM people").fetchone() == ("#2563eb",)

"""Items, purchases, spoiler protection, tabs and photos: edge cases not covered in test_app.py."""
import io
import sqlite3

import pytest
from PIL import Image

import app as wishlist
from conftest import ADMIN, LAN_IP, PUBLIC_IP, token
from test_app import add, no_files, png

GRANDMA = "grandma@example.com"
ALEX = "alex@example.com"


def reserved_at(iid):
    con = sqlite3.connect(wishlist.DB_PATH)
    try:
        return con.execute("SELECT reserved_at FROM items WHERE id = ?", (iid,)).fetchone()[0]
    finally:
        con.close()


def items_of(client, pid, email=ADMIN):
    return client.get(f"/api/people/{pid}/items", email).json["items"]


def order(client, pid, email=ADMIN):
    return [i["name"] for i in sorted(items_of(client, pid, email), key=lambda i: (i["position"], i["id"]))]


def tab(client, pid, email):
    return {p["id"]: p for p in client.get("/api/people", email).json}[pid]


@pytest.fixture
def owned(client, people):
    """Alex's tab belongs to alex@example.com, who is also an editor."""
    pid = people["Alex"]
    client.patch(f"/api/people/{pid}", ADMIN, json={"owner_email": "Alex@Example.com"})
    client.put("/api/members", ADMIN, json={"email": ALEX, "display_name": "Alex", "role": "editor"})
    return pid


FEED_TOKEN = "feed-test-token-0123456789"


@pytest.fixture
def feed_on(monkeypatch):
    """The companion feed is configured and exposes Alex's tab only."""
    monkeypatch.setattr(wishlist, "FEED_TOKEN", FEED_TOKEN)
    monkeypatch.setattr(wishlist, "FEED_PEOPLE", {"alex"})


def feed(client, method, path, json=None, bearer=FEED_TOKEN, ip=LAN_IP, headers=None):
    """A request as the companion service: a server on the LAN with a bearer token, no X-Wishlist."""
    h = {"Authorization": f"Bearer {bearer}"} if bearer is not None else {}
    h.update(headers or {})
    kw = {"json": json} if json is not None else {}
    return getattr(client.c, method)(path, headers=h, environ_base={"REMOTE_ADDR": ip}, **kw)


def unknown(path):
    """The same path with the feed's name swapped for a route that doesn't exist."""
    return path.replace("/api/feed", "/api/nofeed")


# ------------------------------------------------------------------ item fields
class TestItemFields:
    @pytest.mark.parametrize("given,stored", [
        ("https://example.com/a?b=1#c", "https://example.com/a?b=1#c"),
        ("HTTP://Example.com/x", "HTTP://Example.com/x"),
        ("  example.com/x  ", "https://example.com/x"),
        ("localhost:8080/x", "https://localhost:8080/x"),
        ("www.example.com", "https://www.example.com"),
        ("", None),
        (None, None),
    ])
    def test_url_normalization(self, client, people, given, stored):
        r = add(client, people["Alex"], url=given)
        assert r.status_code == 201 and r.json["url"] == stored

    @pytest.mark.parametrize("url", ["JavaScript:alert(1)", "  javascript:alert(1)", "vbscript:x",
                                     "mailto:a@example.com", "ftp://example.com/f", "blob:abc"])
    def test_more_non_web_links_rejected(self, client, people, url):
        assert add(client, people["Alex"], url=url).status_code == 400
        iid = add(client, people["Alex"]).json["id"]
        assert client.patch(f"/api/items/{iid}", ADMIN, json={"url": url}).status_code == 400

    @pytest.mark.parametrize("given,stored", [("", None), (None, None), (0, 0), ("0", 0), ("12.346", 12.35),
                                              (" $ 5 ", 5), ("1,000", 1000), (19.999, 20)])
    def test_price_parsing(self, client, people, given, stored):
        r = add(client, people["Alex"], price=given)
        assert r.status_code == 201 and r.json["price"] == stored

    @pytest.mark.parametrize("price", ["-0.01", "free", "1.2.3", "$", True])
    def test_bad_prices_rejected(self, client, people, price):
        assert add(client, people["Alex"], price=price).status_code == 400

    @pytest.mark.parametrize("price", ["inf", "-inf", "Infinity", "1e999", "nan", 1e999])
    def test_non_finite_price_rejected(self, client, people, price):
        # bare Infinity/NaN isn't valid JSON: one such price would stop every browser loading the tabs
        assert add(client, people["Alex"], price=price).status_code == 400
        iid = add(client, people["Alex"], price=5).json["id"]
        assert client.patch(f"/api/items/{iid}", ADMIN, json={"price": price}).status_code == 400
        raw = client.get("/api/people", GRANDMA).get_data(as_text=True)
        assert "Infinity" not in raw and "NaN" not in raw

    @pytest.mark.parametrize("given,stored", [(1, 1), ("3", 3), (0, 1), (-5, 1), (9, 3)])
    def test_priority_is_clamped(self, client, people, given, stored):
        assert add(client, people["Alex"], priority=given).json["priority"] == stored

    def test_priority_defaults_to_middle_and_rejects_junk(self, client, people):
        assert add(client, people["Alex"]).json["priority"] == 2
        assert add(client, people["Alex"], priority="high").status_code == 400
        assert add(client, people["Alex"], priority=None).status_code == 400

    def test_long_text_is_truncated(self, client, people):
        r = add(client, people["Alex"], name="n" * 500, kind="store", store="s" * 500, notes="x" * 5000)
        assert (len(r.json["name"]), len(r.json["store"]), len(r.json["notes"])) == (200, 120, 1000)

    def test_blank_optional_fields_become_null(self, client, people):
        r = add(client, people["Alex"], kind="store", store="  ", notes=" \n ")
        assert r.json["store"] is None and r.json["notes"] is None

    def test_created_by_and_timestamps(self, client, people):
        client.put("/api/members", ADMIN, json={"email": ADMIN, "display_name": "Dad", "role": "editor"})
        r = add(client, people["Alex"]).json
        assert r["created_by"] == "Dad" and r["created_at"] > 1_700_000_000
        assert r["purchased"] is False and r["photo_url"] is None

    def test_html_is_stored_verbatim_not_mangled(self, client, people):
        # escaping is the frontend's job (see frontend.test.mjs); the API must round-trip text
        name = '<img src=x onerror=alert(1)> & "quotes"'
        assert add(client, people["Alex"], name=name, notes=name).json["notes"] == name

    @pytest.mark.parametrize("body", [[1], "text", 7, {"name": 5, "kind": "online"},
                                      {"name": "x", "kind": "online", "priority": 1e999},
                                      {"name": "x", "kind": "online", "url": ["a"]},
                                      {"name": "x", "kind": "online", "notes": {"a": 1}}])
    def test_malformed_bodies_are_a_400_not_a_500(self, client, people, body):
        r = client.post(f"/api/people/{people['Alex']}/items", ADMIN, json=body)
        assert r.status_code == 400 and r.json["error"]

    @pytest.mark.parametrize("method,path,body", [
        ("post", "/api/people", {"name": 5}),
        ("post", "/api/people", {"name": "X", "color": 255}),
        ("post", "/api/people", [1]),
        ("patch", "/api/people/1", {"owner_email": ["a@example.com"]}),
        ("put", "/api/members", {"email": 5, "display_name": "G"}),
        ("put", "/api/members", [1]),
        ("post", "/api/items/1/move", [1]),
        ("post", "/api/items/1/purchase", {"by": 5}),
        ("post", "/api/items/1/purchase", [1]),
        ("post", "/api/items/1/reserve", {"by": 5}),
        ("post", "/api/items/1/reserve", [1]),
        ("post", "/api/items/1/archive", [1]),
        ("patch", "/api/items/1", {"occasion": 5}),
        ("post", "/api/people/1/ideas", [1]),
        ("post", "/api/people/1/ideas", {"name": "x", "kind": "online", "occasion": ["a"]}),
    ])
    def test_malformed_bodies_elsewhere_are_a_400(self, client, people, method, path, body):
        add(client, people["Alex"])
        assert client.req(method, path, ADMIN, json=body).status_code == 400


class TestItemEditing:
    def test_patch_is_partial(self, client, people):
        iid = add(client, people["Alex"], name="Bike", url="example.com", price=10, notes="red").json["id"]
        r = client.patch(f"/api/items/{iid}", ADMIN, json={"price": "12"}).json
        assert (r["name"], r["url"], r["price"], r["notes"]) == ("Bike", "https://example.com", 12, "red")

    def test_patch_can_clear_fields(self, client, people):
        iid = add(client, people["Alex"], url="example.com", price=10, notes="red").json["id"]
        r = client.patch(f"/api/items/{iid}", ADMIN, json={"url": "", "price": "", "notes": ""}).json
        assert (r["url"], r["price"], r["notes"]) == (None, None, None)

    def test_patch_validates_what_it_is_given(self, client, people):
        iid = add(client, people["Alex"]).json["id"]
        assert client.patch(f"/api/items/{iid}", ADMIN, json={"name": ""}).status_code == 400
        assert client.patch(f"/api/items/{iid}", ADMIN, json={"kind": "mail"}).status_code == 400
        assert client.patch(f"/api/items/{iid}", ADMIN, json={}).status_code == 200

    def test_patch_ignores_fields_it_does_not_own(self, client, people):
        iid = add(client, people["Alex"]).json["id"]
        r = client.patch(f"/api/items/{iid}", ADMIN, json={
            "purchased": 1, "purchased_by": "Mallory", "person_id": people["Sam"], "id": 999,
            "removed_by_owner": 1, "position": 50, "photo_ver": 3}).json
        assert r["id"] == iid and r["person_id"] == people["Alex"]
        assert not r["purchased"] and r["position"] == 0 and r["photo_url"] is None

    def test_unknown_ids_are_404(self, client):
        assert client.post("/api/people/999/items", ADMIN, json={"name": "x", "kind": "online"}).status_code == 404
        assert client.patch("/api/items/999", ADMIN, json={"name": "x"}).status_code == 404
        assert client.delete("/api/items/999", ADMIN).status_code == 404
        assert client.post("/api/items/999/move", ADMIN, json={"dir": "up"}).status_code == 404
        assert client.post("/api/items/999/purchase", ADMIN).status_code == 404

    def test_shopper_cannot_delete_or_move(self, client, people):
        iid = add(client, people["Alex"]).json["id"]
        assert client.delete(f"/api/items/{iid}", GRANDMA).status_code == 403
        assert client.post(f"/api/items/{iid}/move", GRANDMA, json={"dir": "up"}).status_code == 403
        assert len(items_of(client, people["Alex"])) == 1


class TestOrdering:
    @pytest.fixture
    def abc(self, client, people):
        return {n: add(client, people["Alex"], name=n).json["id"] for n in "ABC"}

    def test_new_items_go_to_the_end(self, client, people, abc):
        assert order(client, people["Alex"]) == ["A", "B", "C"]

    def test_move_down_and_up(self, client, people, abc):
        client.post(f"/api/items/{abc['A']}/move", ADMIN, json={"dir": "down"})
        assert order(client, people["Alex"]) == ["B", "A", "C"]
        client.post(f"/api/items/{abc['C']}/move", ADMIN, json={"dir": "up"})
        assert order(client, people["Alex"]) == ["B", "C", "A"]

    def test_moving_past_either_end_is_a_no_op(self, client, people, abc):
        assert client.post(f"/api/items/{abc['A']}/move", ADMIN, json={"dir": "up"}).status_code == 200
        assert client.post(f"/api/items/{abc['C']}/move", ADMIN, json={"dir": "down"}).status_code == 200
        assert order(client, people["Alex"]) == ["A", "B", "C"]

    def test_lists_are_ordered_independently(self, client, people, abc):
        add(client, people["Sam"], name="S1")
        add(client, people["Sam"], name="S2")
        client.post(f"/api/items/{abc['B']}/move", ADMIN, json={"dir": "up"})
        assert order(client, people["Sam"]) == ["S1", "S2"]

    def test_order_survives_deleting_from_the_middle(self, client, people, abc):
        client.delete(f"/api/items/{abc['B']}", ADMIN)
        add(client, people["Alex"], name="D")
        client.post(f"/api/items/{abc['C']}/move", ADMIN, json={"dir": "up"})
        assert order(client, people["Alex"]) == ["C", "A", "D"]

    def test_owner_reorder_skips_items_hidden_from_them(self, client, owned):
        ids = {n: add(client, owned, name=n).json["id"] for n in "ABC"}
        client.post(f"/api/items/{ids['B']}/purchase", GRANDMA)
        client.delete(f"/api/items/{ids['B']}", ALEX)           # soft-removed: hidden from Alex only
        assert order(client, owned, ALEX) == ["A", "C"]
        client.post(f"/api/items/{ids['C']}/move", ALEX, json={"dir": "up"})
        assert order(client, owned, ALEX) == ["C", "A"]
        client.post(f"/api/items/{ids['C']}/move", ALEX, json={"dir": "down"})
        assert order(client, owned, ALEX) == ["A", "C"]
        # everyone else still sees all three, and moves one visible step at a time
        assert sorted(order(client, owned)) == ["A", "B", "C"]
        before = order(client, owned)
        client.post(f"/api/items/{ids['A']}/move", ADMIN, json={"dir": "down"})
        after = order(client, owned)
        assert after.index("A") == before.index("A") + 1


# ------------------------------------------------------------------ purchases
class TestPurchasesMore:
    def test_shopper_can_undo_their_own_purchase(self, client, people):
        iid = add(client, people["Alex"]).json["id"]
        client.post(f"/api/items/{iid}/purchase", GRANDMA)
        r = client.post(f"/api/items/{iid}/purchase", GRANDMA, json={"purchased": False})
        assert r.status_code == 200 and r.json["purchased"] is False and r.json["purchased_by"] is None
        # ...and then someone else can claim it
        assert client.post(f"/api/items/{iid}/purchase", "uncle@example.com").json["mine"] is True

    def test_buyer_view_versus_other_shopper_view(self, client, people):
        iid = add(client, people["Alex"]).json["id"]
        client.post(f"/api/items/{iid}/purchase", GRANDMA, json={"by": "Grandma"})
        mine = {i["id"]: i for i in items_of(client, people["Alex"], GRANDMA)}[iid]
        theirs = {i["id"]: i for i in items_of(client, people["Alex"], "uncle@example.com")}[iid]
        assert mine["mine"] is True and theirs["mine"] is False
        assert theirs["purchased_by"] == "Grandma" and theirs["purchased_at"]
        assert "purchased_by_email" not in theirs  # buyer emails never leave the server

    def test_who_is_trimmed_and_capped(self, client, people):
        iid = add(client, people["Alex"]).json["id"]
        r = client.post(f"/api/items/{iid}/purchase", GRANDMA, json={"by": "  " + "g" * 200})
        assert r.json["purchased_by"] == "g" * 80

    def test_blank_who_falls_back_to_signed_in_name(self, client, people):
        iid = add(client, people["Alex"]).json["id"]
        r = client.post(f"/api/items/{iid}/purchase", "great.aunt@example.com", json={"by": "   "})
        assert r.json["purchased_by"] == "Great Aunt"

    def test_undoing_something_not_purchased_is_harmless(self, client, people):
        iid = add(client, people["Alex"]).json["id"]
        r = client.post(f"/api/items/{iid}/purchase", ADMIN, json={"purchased": False})
        assert r.status_code == 200 and r.json["purchased"] is False

    def test_lan_purchase_records_the_typed_name(self, client, people):
        iid = add(client, people["Alex"]).json["id"]
        r = client.lan("post", f"/api/items/{iid}/purchase", json={"by": "Mom"}, headers={"X-Wishlist": "1"})
        assert r.json["purchased_by"] == "Mom" and r.json["mine"] is False

    def test_double_purchase_error_names_the_first_buyer(self, client, people):
        iid = add(client, people["Alex"]).json["id"]
        client.post(f"/api/items/{iid}/purchase", GRANDMA, json={"by": "Grandma"})
        r = client.post(f"/api/items/{iid}/purchase", "uncle@example.com")
        assert r.status_code == 400 and "Grandma" in r.json["error"]
        assert {i["id"]: i for i in items_of(client, people["Alex"])}[iid]["purchased_by"] == "Grandma"

    def test_simultaneous_purchases_only_one_wins(self, client, people, monkeypatch):
        """Two shoppers tap at once: the second request read the item before the first one committed."""
        iid = add(client, people["Alex"]).json["id"]
        real_get_item = wishlist.get_item

        def stale_read(item_id):
            row = real_get_item(item_id)
            monkeypatch.setattr(wishlist, "get_item", real_get_item)  # only the first read is stale
            other = sqlite3.connect(wishlist.DB_PATH)
            other.execute("UPDATE items SET purchased = 1, purchased_by = 'Grandma', "
                          "purchased_by_email = ?, purchased_at = 1 WHERE id = ?", (GRANDMA, item_id))
            other.commit()
            other.close()
            return row

        monkeypatch.setattr(wishlist, "get_item", stale_read)
        r = client.post(f"/api/items/{iid}/purchase", "uncle@example.com", json={"by": "Uncle"})
        assert r.status_code == 400 and "Grandma" in r.json["error"]
        item = items_of(client, people["Alex"], GRANDMA)[0]
        assert item["purchased_by"] == "Grandma" and item["mine"] is True

    def test_tab_stats_ignore_unpriced_and_owner_removed_items(self, client, owned):
        a = add(client, owned, price=10).json["id"]
        add(client, owned, price=None)
        add(client, owned, price=2.5)
        client.post(f"/api/items/{a}/purchase", GRANDMA)
        assert tab(client, owned, GRANDMA) | {"color": None} == {
            "id": owned, "name": "Alex", "color": None, "is_me": False, "count": 3, "bought": 1, "reserved": 0,
            "remaining": 2.5, "archived": 0}
        client.delete(f"/api/items/{a}", ALEX)  # owner removes the bought one
        t = tab(client, owned, GRANDMA)
        assert (t["count"], t["bought"], t["remaining"]) == (2, 0, 2.5)

    def test_editing_a_purchased_item_keeps_the_purchase(self, client, people):
        iid = add(client, people["Alex"]).json["id"]
        client.post(f"/api/items/{iid}/purchase", GRANDMA)
        r = client.patch(f"/api/items/{iid}", ADMIN, json={"name": "Renamed", "price": 3}).json
        assert r["purchased"] is True and r["name"] == "Renamed"


# ------------------------------------------------------------------ secret ideas
def idea(client, pid, email=GRANDMA, **fields):
    return client.post(f"/api/people/{pid}/ideas", email, json={"name": "Idea", "kind": "online", **fields})


class TestSecretIdeas:
    def test_shopper_adds_an_idea_everyone_else_sees_as_secret(self, client, people):
        r = idea(client, people["Alex"], name="Drone", price=40)
        assert r.status_code == 201
        assert (r.json["secret"], r.json["my_idea"], r.json["created_by"]) == (True, True, "Grandma")
        assert "created_by_email" not in r.json
        theirs = items_of(client, people["Alex"], "uncle@example.com")[0]
        assert (theirs["name"], theirs["secret"], theirs["my_idea"]) == ("Drone", True, False)
        assert tab(client, people["Alex"], "uncle@example.com")["count"] == 1

    def test_ideas_are_validated_like_items(self, client, people):
        assert idea(client, people["Alex"], name=" ").status_code == 400
        assert idea(client, people["Alex"], url="javascript:alert(1)").status_code == 400
        assert client.post("/api/people/999/ideas", GRANDMA, json={"name": "x", "kind": "online"}).status_code == 404

    def test_regular_items_still_need_an_editor(self, client, people):
        r = client.post(f"/api/people/{people['Alex']}/items", GRANDMA, json={"name": "x", "kind": "online"})
        assert r.status_code == 403

    def test_author_or_editor_can_change_an_idea_and_nobody_else(self, client, people):
        iid = idea(client, people["Alex"]).json["id"]
        uncle = "uncle@example.com"
        assert client.patch(f"/api/items/{iid}", uncle, json={"name": "Nope"}).status_code == 403
        assert client.delete(f"/api/items/{iid}", uncle).status_code == 403
        assert client.upload(iid, png(), email=uncle).status_code == 403
        assert client.patch(f"/api/items/{iid}", GRANDMA, json={"name": "Better"}).json["name"] == "Better"
        assert client.upload(iid, png(), email=GRANDMA).status_code == 200
        assert client.delete(f"/api/items/{iid}/photo", GRANDMA).status_code == 200
        assert client.patch(f"/api/items/{iid}", ADMIN, json={"notes": "ok"}).status_code == 200
        assert client.delete(f"/api/items/{iid}", GRANDMA).json == {"ok": True}
        assert items_of(client, people["Alex"]) == []

    def test_authors_cannot_edit_regular_items_or_reorder(self, client, people):
        iid = idea(client, people["Alex"]).json["id"]
        regular = add(client, people["Alex"]).json["id"]
        assert client.patch(f"/api/items/{regular}", GRANDMA, json={"name": "x"}).status_code == 403
        assert client.post(f"/api/items/{iid}/move", GRANDMA, json={"dir": "up"}).status_code == 403

    def test_ideas_can_be_purchased_like_any_item(self, client, people):
        iid = idea(client, people["Alex"]).json["id"]
        r = client.post(f"/api/items/{iid}/purchase", "uncle@example.com", json={"by": "Uncle"})
        assert r.json["purchased_by"] == "Uncle" and r.json["secret"] is True


# ------------------------------------------------------------------ reservations
def reserve(client, iid, email=GRANDMA, **body):
    return client.post(f"/api/items/{iid}/reserve", email, json=body)


class TestReservations:
    def test_reserve_shows_who_to_everyone_else(self, client, people):
        iid = add(client, people["Alex"]).json["id"]
        r = reserve(client, iid)
        assert r.status_code == 200 and r.json["reserved_by"] == "Grandma" and r.json["reserved_mine"] is True
        theirs = items_of(client, people["Alex"], "uncle@example.com")[0]
        assert (theirs["reserved_by"], theirs["reserved_mine"], theirs["purchased"]) == ("Grandma", False, False)
        assert theirs["reserved_at"] and "reserved_by_email" not in theirs
        t = tab(client, people["Alex"], "uncle@example.com")
        assert (t["count"], t["reserved"], t["bought"]) == (1, 1, 0)

    def test_second_reservation_is_refused_naming_the_first(self, client, people):
        iid = add(client, people["Alex"]).json["id"]
        reserve(client, iid, by="Grandma G")
        r = reserve(client, iid, "uncle@example.com")
        assert r.status_code == 400 and "Grandma G" in r.json["error"]
        assert reserve(client, iid).status_code == 400  # not even twice by the same person

    def test_purchased_items_cannot_be_reserved(self, client, people):
        iid = add(client, people["Alex"]).json["id"]
        client.post(f"/api/items/{iid}/purchase", GRANDMA, json={"by": "Grandma"})
        r = reserve(client, iid, "uncle@example.com")
        assert r.status_code == 400 and "purchased by Grandma" in r.json["error"]

    def test_only_the_reserver_can_mark_it_purchased(self, client, people, monkeypatch):
        iid = add(client, people["Alex"]).json["id"]
        reserve(client, iid, by="Grandma")
        for email in ("uncle@example.com", ADMIN):
            r = client.post(f"/api/items/{iid}/purchase", email)
            assert r.status_code == 400 and "Grandma" in r.json["error"]
        r = client.post(f"/api/items/{iid}/purchase", GRANDMA, json={"by": "Grandma"})
        assert r.json["purchased"] is True and r.json["reserved_by"] is None
        # undoing the purchase leaves it open, not reserved
        r = client.post(f"/api/items/{iid}/purchase", GRANDMA, json={"purchased": False})
        assert r.json["purchased"] is False and r.json["reserved_by"] is None

    def test_reservation_made_after_the_item_was_read_still_blocks_the_purchase(self, client, people, monkeypatch):
        iid = add(client, people["Alex"]).json["id"]
        real_get_item = wishlist.get_item

        def stale_read(item_id):
            row = real_get_item(item_id)
            monkeypatch.setattr(wishlist, "get_item", real_get_item)
            other = sqlite3.connect(wishlist.DB_PATH)
            other.execute("UPDATE items SET reserved_by = 'Grandma', reserved_by_email = ?, reserved_at = 1 "
                          "WHERE id = ?", (GRANDMA, item_id))
            other.commit()
            other.close()
            return row

        monkeypatch.setattr(wishlist, "get_item", stale_read)
        r = client.post(f"/api/items/{iid}/purchase", "uncle@example.com")
        assert r.status_code == 400 and "Grandma" in r.json["error"]
        assert items_of(client, people["Alex"])[0]["purchased"] is False

    def test_release_by_the_reserver_or_an_editor(self, client, people):
        iid = add(client, people["Alex"]).json["id"]
        reserve(client, iid)
        r = reserve(client, iid, "uncle@example.com", reserved=False)
        assert r.status_code == 403
        r = reserve(client, iid, reserved=False)
        assert r.status_code == 200 and r.json["reserved_by"] is None
        reserve(client, iid, "uncle@example.com")
        assert reserve(client, iid, ADMIN, reserved=False).json["reserved_by"] is None
        assert reserve(client, iid).json["reserved_mine"] is True  # open again for anyone

    def test_lan_reservation_uses_the_typed_name(self, client, people):
        iid = add(client, people["Alex"]).json["id"]
        h = {"X-Wishlist": "1"}
        r = client.lan("post", f"/api/items/{iid}/reserve", json={"by": "Mom"}, headers=h)
        assert r.json["reserved_by"] == "Mom" and r.json["reserved_mine"] is False
        # a LAN purchase can't prove it is the reserver; the LAN editor releases first
        assert client.lan("post", f"/api/items/{iid}/purchase", json={}, headers=h).status_code == 400
        assert client.lan("post", f"/api/items/{iid}/reserve", json={"reserved": False}, headers=h).status_code == 200

    def test_editing_keeps_the_reservation(self, client, people):
        iid = add(client, people["Alex"]).json["id"]
        reserve(client, iid)
        assert client.patch(f"/api/items/{iid}", ADMIN, json={"name": "Renamed"}).json["reserved_by"] == "Grandma"


# ------------------------------------------------------------------ occasions and archive
def history(client, pid, email=ADMIN):
    return client.get(f"/api/people/{pid}/history", email).json["items"]


class TestOccasions:
    def test_occasion_is_trimmed_capped_and_clearable(self, client, people):
        iid = add(client, people["Alex"], occasion="  Christmas  ").json["id"]
        assert items_of(client, people["Alex"])[0]["occasion"] == "Christmas"
        assert client.patch(f"/api/items/{iid}", ADMIN, json={"occasion": "x" * 100}).json["occasion"] == "x" * 60
        assert client.patch(f"/api/items/{iid}", ADMIN, json={"occasion": ""}).json["occasion"] is None
        assert add(client, people["Alex"]).json["occasion"] is None

    def test_suggestions_come_from_every_tab_most_used_first(self, client, people):
        add(client, people["Alex"], occasion="Birthday")
        add(client, people["Sam"], occasion="Christmas")
        add(client, people["Jordan"], occasion="Christmas")
        add(client, people["Jordan"], occasion="anniversary")
        assert client.get("/api/occasions", GRANDMA).json == ["Christmas", "anniversary", "Birthday"]

    def test_secret_idea_occasions_are_not_suggested_to_lan(self, client, people):
        idea(client, people["Alex"], occasion="Surprise party")
        assert client.get("/api/occasions", GRANDMA).json == ["Surprise party"]
        assert client.lan("get", "/api/occasions").json == []


class TestArchive:
    @pytest.fixture
    def given(self, client, people):
        """Alex's tab: two purchased items and one still wanted."""
        ids = {n: add(client, people["Alex"], name=n, price=10).json["id"] for n in ("Kite", "Yo-yo", "Bike")}
        client.post(f"/api/items/{ids['Kite']}/purchase", GRANDMA, json={"by": "Grandma"})
        client.post(f"/api/items/{ids['Yo-yo']}/purchase", "uncle@example.com", json={"by": "Uncle"})
        return ids

    def test_only_purchased_items_can_be_archived(self, client, people, given):
        r = client.post(f"/api/items/{given['Bike']}/archive", ADMIN)
        assert r.status_code == 400 and history(client, people["Alex"]) == []

    def test_archived_items_leave_the_list_and_counts(self, client, people, given):
        r = client.post(f"/api/items/{given['Kite']}/archive", ADMIN)
        assert r.status_code == 200 and r.json["archived_at"]
        assert [i["name"] for i in items_of(client, people["Alex"])] == ["Yo-yo", "Bike"]
        t = tab(client, people["Alex"], GRANDMA)
        assert (t["count"], t["bought"], t["archived"], t["remaining"]) == (2, 1, 1, 10)
        assert [(i["name"], i["purchased_by"]) for i in history(client, people["Alex"], GRANDMA)] == [("Kite", "Grandma")]

    def test_restore_puts_it_back_on_the_list(self, client, people, given):
        client.post(f"/api/items/{given['Kite']}/archive", ADMIN)
        r = client.post(f"/api/items/{given['Kite']}/archive", ADMIN, json={"archived": False})
        assert r.json["archived_at"] is None and r.json["purchased"] is True
        assert order(client, people["Alex"]) == ["Kite", "Yo-yo", "Bike"] and history(client, people["Alex"]) == []

    def test_archive_everything_purchased_on_a_tab(self, client, people, given):
        add(client, people["Sam"], name="Elsewhere")
        sam_bought = add(client, people["Sam"]).json["id"]
        client.post(f"/api/items/{sam_bought}/purchase", GRANDMA)
        assert client.post(f"/api/people/{people['Alex']}/archive", ADMIN).json == {"archived": 2}
        assert client.post(f"/api/people/{people['Alex']}/archive", ADMIN).json == {"archived": 0}
        assert [i["name"] for i in items_of(client, people["Alex"])] == ["Bike"]
        assert len(items_of(client, people["Sam"])) == 2  # other tabs untouched
        assert client.post("/api/people/999/archive", ADMIN).status_code == 404

    def test_history_is_newest_first(self, client, people, given, monkeypatch):
        client.post(f"/api/items/{given['Kite']}/archive", ADMIN)
        monkeypatch.setattr(wishlist.time, "time", lambda: 4_000_000_000)
        client.post(f"/api/items/{given['Yo-yo']}/archive", ADMIN)
        assert [i["name"] for i in history(client, people["Alex"])] == ["Yo-yo", "Kite"]

    def test_shoppers_cannot_archive(self, client, people, given):
        assert client.post(f"/api/items/{given['Kite']}/archive", GRANDMA).status_code == 403
        assert client.post(f"/api/people/{people['Alex']}/archive", GRANDMA).status_code == 403

    def test_archived_items_cannot_be_bought_reserved_undone_or_moved(self, client, people, given):
        kite = given["Kite"]
        client.post(f"/api/items/{kite}/archive", ADMIN)
        for path, body in ((f"/api/items/{kite}/purchase", {"purchased": False}), (f"/api/items/{kite}/purchase", {}),
                           (f"/api/items/{kite}/reserve", {}), (f"/api/items/{kite}/move", {"dir": "up"})):
            assert client.post(path, ADMIN, json=body).status_code == 400, path
        assert history(client, people["Alex"])[0]["purchased_by"] == "Grandma"

    def test_archived_items_can_still_be_deleted(self, client, people, given):
        client.post(f"/api/items/{given['Kite']}/archive", ADMIN)
        assert client.delete(f"/api/items/{given['Kite']}", ADMIN).status_code == 200
        assert history(client, people["Alex"]) == []


# ------------------------------------------------------------------ spoilers
class TestSpoilersMore:
    """The owner of a tab must not be able to learn what has been bought, from any endpoint."""

    HIDDEN = {"purchased": False, "purchased_by": None, "purchased_at": None, "mine": False,
              "removed_by_owner": False, "secret": False, "my_idea": False, "reserved_by": None,
              "reserved_at": None, "reserved_mine": False}

    @pytest.fixture
    def bought(self, client, owned):
        iid = add(client, owned, name="Bought", price=40).json["id"]
        add(client, owned, name="Open", price=5)
        client.post(f"/api/items/{iid}/purchase", GRANDMA, json={"by": "Grandma"})
        return iid

    def hidden(self, item):
        return {k: item[k] for k in self.HIDDEN} == self.HIDDEN

    def test_owner_match_is_case_insensitive(self, client, owned):
        # the fixture stored "Alex@Example.com"; the token says alex@example.com
        assert tab(client, owned, ALEX)["is_me"] is True
        assert client.get(f"/api/people/{owned}/items", ALEX).json["person"]["is_me"] is True

    def test_tab_summary_gives_owner_only_a_count(self, client, owned, bought):
        t = tab(client, owned, ALEX)
        assert "bought" not in t and "remaining" not in t and t["count"] == 2

    def test_no_response_body_mentions_the_buyer(self, client, owned, bought):
        for path in ("/api/people", f"/api/people/{owned}/items", "/api/me"):
            assert "Grandma" not in client.get(path, ALEX).get_data(as_text=True)

    def test_edit_response_hides_purchase_from_owner(self, client, owned, bought):
        r = client.patch(f"/api/items/{bought}", ALEX, json={"notes": "size M"})
        assert r.status_code == 200 and self.hidden(r.json)

    def test_photo_responses_hide_purchase_from_owner(self, client, owned, bought):
        up = client.upload(bought, png(), email=ALEX)
        assert up.status_code == 200 and self.hidden(up.json)
        assert self.hidden(client.delete(f"/api/items/{bought}/photo", ALEX).json)

    def test_owner_cannot_probe_with_purchase_or_undo(self, client, owned, bought):
        for body in ({"purchased": True}, {"purchased": False}):
            r = client.post(f"/api/items/{bought}/purchase", ALEX, json=body)
            assert r.status_code == 403 and "Grandma" not in r.get_data(as_text=True)
        # the purchase is untouched
        theirs = {i["id"]: i for i in items_of(client, owned, GRANDMA)}[bought]
        assert theirs["purchased_by"] == "Grandma"

    def test_delete_looks_the_same_to_owner_whether_or_not_it_was_bought(self, client, owned, bought):
        open_ = next(i["id"] for i in items_of(client, owned, ALEX) if i["name"] == "Open")
        a = client.delete(f"/api/items/{bought}", ALEX)
        b = client.delete(f"/api/items/{open_}", ALEX)
        assert (a.status_code, a.json) == (b.status_code, b.json) == (200, {"ok": True})
        assert items_of(client, owned, ALEX) == [] and tab(client, owned, ALEX)["count"] == 0

    def test_owner_removed_item_keeps_its_photo_for_the_buyer_only(self, client, owned, bought):
        client.upload(bought, png())
        client.delete(f"/api/items/{bought}", ALEX)
        assert client.get(f"/photos/{bought}/full", ALEX).status_code == 404
        assert client.get(f"/photos/{bought}/thumb", GRANDMA).status_code == 200
        # once the buyer undoes the purchase the item and its files are gone for good
        client.post(f"/api/items/{bought}/purchase", GRANDMA, json={"purchased": False})
        assert no_files(bought) and client.get(f"/photos/{bought}/full", GRANDMA).status_code == 404

    def test_non_owner_editor_deleting_a_bought_item_really_deletes(self, client, owned, bought):
        client.delete(f"/api/items/{bought}", ADMIN)
        assert bought not in [i["id"] for i in items_of(client, owned, GRANDMA)]

    def test_owner_of_one_tab_sees_purchases_on_other_tabs(self, client, owned, people):
        iid = add(client, people["Sam"]).json["id"]
        client.post(f"/api/items/{iid}/purchase", GRANDMA, json={"by": "Grandma"})
        assert items_of(client, people["Sam"], ALEX)[0]["purchased_by"] == "Grandma"
        assert client.post(f"/api/items/{iid}/purchase", ALEX).status_code == 400  # allowed to try, already bought

    def test_clearing_owner_email_ends_protection(self, client, owned, bought):
        client.patch(f"/api/people/{owned}", ADMIN, json={"owner_email": ""})
        assert tab(client, owned, ALEX)["is_me"] is False

    def test_lan_visitors_are_never_treated_as_an_owner(self, client, owned, bought):
        # Documents current behavior: someone at home on the LAN address sees purchase state
        # for every tab, including their own. LAN_ROLE=none closes this.
        data = client.lan("get", f"/api/people/{owned}/items").json
        assert data["person"]["is_me"] is False
        assert any(i["purchased"] for i in data["items"])

    def test_removed_item_answers_owner_like_a_missing_item(self, client, owned, bought):
        # a second delete or a photo request must not tell "bought, so kept" from "really deleted"
        client.upload(bought, png())
        client.delete(f"/api/items/{bought}", ALEX)
        for method, path, body in (("delete", "/api/items/{}", {}), ("patch", "/api/items/{}", {"notes": "x"}),
                                   ("get", "/photos/{}/full", None)):
            a = client.req(method, path.format(bought), ALEX, json=body)
            b = client.req(method, path.format(99999), ALEX, json=body)
            assert (a.status_code, a.get_data()) == (b.status_code, b.get_data()) and a.status_code == 404, path

    # secret ideas
    def test_feed_writes_leave_every_owner_view_byte_identical(self, client, owned, bought, feed_on):
        """After the feed reserves, re-labels, buys, undoes and releases, nothing the owner can ask for
        differs, down to the byte, from before; everyone else sees it all."""
        kite = add(client, owned, name="Kite", occasion="Birthday", price=7).json["id"]
        ball = add(client, owned, name="Ball").json["id"]
        surprise = idea(client, owned, name="Surprise").json["id"]
        client.upload(kite, png())
        open_ = next(i["id"] for i in items_of(client, owned, ALEX) if i["name"] == "Open")
        views = ("/api/people", f"/api/people/{owned}/items", f"/api/people/{owned}/history", "/api/occasions",
                 "/api/me", f"/photos/{kite}/full", f"/photos/{kite}/thumb", f"/photos/{surprise}/full")

        def snapshot():
            out = {path: (r.status_code, r.get_data()) for path in views for r in [client.get(path, ALEX)]}
            for path in ("/api/items/{}/purchase", "/api/items/{}/reserve"):
                for iid in (kite, ball, surprise, open_, bought):
                    r = client.req("post", path.format(iid), ALEX, json={})
                    out[(path, iid)] = (r.status_code, r.get_data())
            return out

        def fr(iid, by):
            return feed(client, "post", f"/api/feed/items/{iid}/reserve", {"by": by})

        def fp(iid, by="Uncle", purchased=True):
            return feed(client, "post", f"/api/feed/items/{iid}/purchase", {"by": by, "purchased": purchased})

        before = snapshot()
        for step in (lambda: fr(kite, "Aunt"), lambda: fr(kite, "Uncle"), lambda: fr(surprise, "Aunt"),
                     lambda: fp(ball, "Aunt"), lambda: fp(surprise, "Uncle"), lambda: fp(ball, purchased=False),
                     lambda: fr(kite, None), lambda: fr(open_, "Aunt"), lambda: fp(open_, "Aunt")):
            assert step().status_code == 200
            assert snapshot() == before
        theirs = {i["id"]: i for i in items_of(client, owned, GRANDMA)}
        assert (theirs[open_]["purchased_by"], theirs[surprise]["purchased_by"]) == ("Aunt", "Uncle")

    @pytest.fixture
    def secret(self, client, owned):
        r = idea(client, owned, name="Surprise drone", occasion="Hush birthday")
        assert r.status_code == 201
        return r.json["id"]

    def test_owner_never_sees_secret_ideas(self, client, owned, secret):
        add(client, owned, name="Wanted")
        assert [i["name"] for i in items_of(client, owned, ALEX)] == ["Wanted"]
        assert tab(client, owned, ALEX)["count"] == 1 and tab(client, owned, GRANDMA)["count"] == 2
        for path in ("/api/people", f"/api/people/{owned}/items", f"/api/people/{owned}/history", "/api/occasions"):
            body = client.get(path, ALEX).get_data(as_text=True)
            assert "Surprise" not in body and "Hush" not in body, path

    def test_secret_idea_answers_owner_like_a_missing_item(self, client, owned, secret):
        client.upload(secret, png())
        missing = 99999
        for method, path, body in (
                ("patch", "/api/items/{}", {"notes": "x"}), ("delete", "/api/items/{}", {}),
                ("post", "/api/items/{}/move", {"dir": "up"}), ("post", "/api/items/{}/purchase", {}),
                ("post", "/api/items/{}/purchase", {"purchased": False}), ("post", "/api/items/{}/reserve", {}),
                ("post", "/api/items/{}/reserve", {"reserved": False}), ("post", "/api/items/{}/archive", {}),
                ("delete", "/api/items/{}/photo", {}), ("get", "/photos/{}/full", None),
                ("get", "/photos/{}/thumb", None)):
            a = client.req(method, path.format(secret), ALEX, json=body)
            b = client.req(method, path.format(missing), ALEX, json=body)
            assert (a.status_code, a.get_data()) == (b.status_code, b.get_data()), (method, path)
        a, b = client.upload(secret, png(), email=ALEX), client.upload(missing, png(), email=ALEX)
        assert (a.status_code, a.json) == (b.status_code, b.json) == (404, {"error": "No such item."})
        kept = items_of(client, owned, GRANDMA)[0]
        assert (kept["id"], kept["notes"], kept["purchased"]) == (secret, None, False)

    def test_owner_cannot_add_ideas_to_their_own_tab(self, client, owned):
        assert idea(client, owned, ALEX).status_code == 403
        assert items_of(client, owned) == []

    def test_lan_visitors_neither_see_nor_add_secret_ideas(self, client, owned, secret):
        h = {"X-Wishlist": "1"}
        assert client.lan("get", f"/api/people/{owned}/items").json["items"] == []
        assert {p["id"]: p for p in client.lan("get", "/api/people").json}[owned]["count"] == 0
        r = client.lan("post", f"/api/people/{owned}/ideas", json={"name": "x", "kind": "online"}, headers=h)
        assert r.status_code == 403
        assert client.lan("patch", f"/api/items/{secret}", json={"name": "x"}, headers=h).status_code == 404

    def test_archived_secret_idea_is_revealed_as_a_gift(self, client, owned, secret):
        client.post(f"/api/items/{secret}/purchase", GRANDMA, json={"by": "Grandma"})
        assert client.post(f"/api/people/{owned}/archive", ADMIN).json == {"archived": 1}
        hist = history(client, owned, ALEX)
        assert [(i["name"], i["purchased_by"], i["secret"]) for i in hist] == [("Surprise drone", "Grandma", True)]
        assert items_of(client, owned, ALEX) == []

    # reservations
    def test_owner_sees_reserved_items_as_open(self, client, owned):
        iid = add(client, owned, name="Kite", price=7).json["id"]
        reserve(client, iid, by="Grandma")
        assert self.hidden(items_of(client, owned, ALEX)[0])
        t = tab(client, owned, ALEX)
        assert "reserved" not in t and t["count"] == 1
        for path in ("/api/people", f"/api/people/{owned}/items"):
            assert "Grandma" not in client.get(path, ALEX).get_data(as_text=True)
        assert self.hidden(client.patch(f"/api/items/{iid}", ALEX, json={"notes": "blue"}).json)

    def test_owner_cannot_probe_with_reserve_or_release(self, client, owned):
        reserved, open_ = add(client, owned).json["id"], add(client, owned).json["id"]
        reserve(client, reserved, by="Grandma")
        for body in ({}, {"reserved": False}):
            a, b = reserve(client, reserved, ALEX, **body), reserve(client, open_, ALEX, **body)
            assert (a.status_code, a.json) == (b.status_code, b.json) and a.status_code == 403
        assert {i["id"]: i for i in items_of(client, owned, GRANDMA)}[reserved]["reserved_by"] == "Grandma"

    def test_owner_deleting_a_reserved_item_keeps_it_for_the_reserver(self, client, owned):
        kite, yoyo = add(client, owned, name="Kite").json["id"], add(client, owned, name="Yo-yo").json["id"]
        reserve(client, kite)
        a, b = client.delete(f"/api/items/{kite}", ALEX), client.delete(f"/api/items/{yoyo}", ALEX)
        assert (a.status_code, a.json) == (b.status_code, b.json) == (200, {"ok": True})
        assert items_of(client, owned, ALEX) == []
        assert [(i["name"], i["removed_by_owner"]) for i in items_of(client, owned, GRANDMA)] == [("Kite", True)]
        # once released there is nothing left to keep
        assert reserve(client, kite, reserved=False).json == {"deleted": True}
        assert items_of(client, owned, GRANDMA) == []

    # archive
    def test_owner_sees_nothing_until_archived_then_the_giver(self, client, owned, bought):
        assert history(client, owned, ALEX) == [] and tab(client, owned, ALEX)["archived"] == 0
        assert client.post(f"/api/people/{owned}/archive", ADMIN).json == {"archived": 1}
        assert [(i["name"], i["purchased"], i["purchased_by"]) for i in history(client, owned, ALEX)] == [
            ("Bought", True, "Grandma")]
        assert [i["name"] for i in items_of(client, owned, ALEX)] == ["Open"]
        t = tab(client, owned, ALEX)
        assert (t["count"], t["archived"]) == (1, 1) and "bought" not in t

    def test_owner_cannot_probe_with_archive(self, client, owned, bought):
        open_ = next(i["id"] for i in items_of(client, owned, ALEX) if i["name"] == "Open")
        for body in ({}, {"archived": False}):
            a = client.post(f"/api/items/{bought}/archive", ALEX, json=body)
            b = client.post(f"/api/items/{open_}/archive", ALEX, json=body)
            assert (a.status_code, a.json) == (b.status_code, b.json) and a.status_code == 403
        assert client.post(f"/api/people/{owned}/archive", ALEX).status_code == 403
        assert history(client, owned, GRANDMA) == []

    def test_owner_removed_items_stay_hidden_after_archiving(self, client, owned, bought):
        client.delete(f"/api/items/{bought}", ALEX)
        client.post(f"/api/people/{owned}/archive", ADMIN)
        assert history(client, owned, ALEX) == [] and tab(client, owned, ALEX)["archived"] == 0
        assert [i["name"] for i in history(client, owned, GRANDMA)] == ["Bought"]

    # companion feed: to anyone but the feed caller it is a route that doesn't exist
    FEED_PROBES = (("get", "/api/feed", None), ("get", "/api/feed?archived=1", None),
                   ("post", "/api/feed/items/{}/reserve", {"by": "Uncle"}),
                   ("post", "/api/feed/items/{}/reserve", {"by": None}),
                   ("post", "/api/feed/items/{}/purchase", {"by": "Uncle"}),
                   ("post", "/api/feed/items/{}/purchase", {"purchased": False}),
                   ("get", "/api/feed/items/{}/reserve", None), ("post", "/api/feed", {}),
                   ("options", "/api/feed", None), ("options", "/api/feed/items/{}/purchase", None),
                   ("put", "/api/feed/items/{}/purchase", {}))

    def same_as_unknown(self, send, bought):
        for method, path, body in self.FEED_PROBES:
            path = path.format(bought)
            a, b = send(method, path, body), send(method, unknown(path), body)
            assert (a.status_code, a.get_data()) == (b.status_code, b.get_data()), (method, path)
            assert a.status_code in (400, 403, 404), (method, path, a.status_code)
            assert "Grandma" not in a.get_data(as_text=True)

    def assert_untouched(self, client, owned, bought):
        item = {i["id"]: i for i in items_of(client, owned, GRANDMA)}[bought]
        assert (item["purchased_by"], item["reserved_by"]) == ("Grandma", None)

    @pytest.mark.parametrize("email", [ALEX, GRANDMA, ADMIN])
    def test_feed_is_an_unknown_route_to_signed_in_users(self, client, owned, bought, feed_on, email):
        # the tab owner, a shopper and an editor, with and without the CSRF header
        for csrf in (True, False):
            self.same_as_unknown(lambda m, p, b: client.req(m, p, email, json=b, csrf=csrf), bought)
        assert client.get("/api/feed", email).status_code == 404
        self.assert_untouched(client, owned, bought)

    def test_feed_is_an_unknown_route_to_lan_editors(self, client, owned, bought, feed_on):
        assert wishlist.LAN_ROLE == "editor"
        for h in ({}, {"X-Wishlist": "1"}):
            self.same_as_unknown(lambda m, p, b: feed(client, m, p, b, bearer=None, headers=h), bought)
        assert feed(client, "get", "/api/feed", bearer=None).status_code == 404
        self.assert_untouched(client, owned, bought)

    @pytest.mark.parametrize("bearer", [None, "", "wrong", FEED_TOKEN + "x", FEED_TOKEN[:-1],
                                        FEED_TOKEN.upper(), "Bearer " + FEED_TOKEN])
    def test_feed_with_no_or_wrong_token_is_an_unknown_route(self, client, owned, bought, feed_on, bearer):
        for h in ({}, {"X-Wishlist": "1"}):
            self.same_as_unknown(lambda m, p, b: feed(client, m, p, b, bearer=bearer, headers=h), bought)
        self.assert_untouched(client, owned, bought)

    def test_feed_token_in_another_scheme_is_refused(self, client, owned, bought, feed_on):
        for auth in (f"Basic {FEED_TOKEN}", FEED_TOKEN, f"Token {FEED_TOKEN}"):
            self.same_as_unknown(lambda m, p, b: feed(client, m, p, b, bearer=None,
                                                      headers={"Authorization": auth}), bought)

    @pytest.mark.parametrize("header", wishlist.CF_HEADERS)
    def test_feed_with_a_cloudflare_header_is_refused_even_with_the_token(self, client, owned, bought,
                                                                         feed_on, header):
        for h in ({header: "x"}, {header: "x", "X-Wishlist": "1"}):
            self.same_as_unknown(lambda m, p, b: feed(client, m, p, b, headers=h), bought)
        self.assert_untouched(client, owned, bought)

    @pytest.mark.parametrize("cookie", ["bogus", token(GRANDMA)])
    def test_feed_with_an_access_cookie_is_refused_even_with_the_token(self, client, owned, bought, feed_on,
                                                                      cookie):
        client.c.set_cookie("CF_Authorization", cookie)
        self.same_as_unknown(lambda m, p, b: feed(client, m, p, b), bought)
        self.assert_untouched(client, owned, bought)

    def test_feed_through_the_tunnel_with_a_real_sign_in_is_a_404(self, client, owned, bought, feed_on):
        # valid token *and* a valid Access JWT: still just an unknown route
        h = {**client.headers(GRANDMA), "Authorization": f"Bearer {FEED_TOKEN}"}
        self.same_as_unknown(lambda m, p, b: feed(client, m, p, b, headers=h, ip=PUBLIC_IP), bought)
        r = feed(client, "get", "/api/feed", headers=h, ip=PUBLIC_IP)
        assert (r.status_code, r.json) == (404, client.get("/api/nofeed", GRANDMA).json)

    def test_feed_from_a_public_address_is_refused(self, client, owned, bought, feed_on):
        # (the documentation ranges count as private to ipaddress, so use a real public address)
        self.same_as_unknown(lambda m, p, b: feed(client, m, p, b, ip="8.8.8.8"), bought)
        assert feed(client, "get", "/api/feed", ip="8.8.8.8").status_code == 403

    @pytest.mark.parametrize("unset", ["", None])
    def test_feed_with_no_token_configured_is_an_unknown_route(self, client, owned, bought, feed_on,
                                                              monkeypatch, unset):
        monkeypatch.setattr(wishlist, "FEED_TOKEN", "")
        for bearer in (unset, FEED_TOKEN, "anything"):
            self.same_as_unknown(lambda m, p, b: feed(client, m, p, b, bearer=bearer), bought)
        self.assert_untouched(client, owned, bought)

    def test_feed_caller_sees_what_the_owner_must_not(self, client, owned, bought, feed_on):
        # the reason all of the above matters
        body = feed(client, "get", "/api/feed").get_data(as_text=True)
        assert "Grandma" in body and GRANDMA in body

# ------------------------------------------------------------------ companion feed
class TestFeed:
    ITEM_KEYS = {"id", "name", "url", "store", "price", "occasion", "secret", "reserved_by", "reserved_by_email",
                 "purchased", "purchased_by", "purchased_by_email", "archived_at"}

    def items(self, client, archived=False):
        r = feed(client, "get", "/api/feed?archived=1" if archived else "/api/feed")
        assert r.status_code == 200
        return {i["name"]: i for p in r.json["people"] for i in p["items"]}

    def reserve(self, client, iid, by):
        return feed(client, "post", f"/api/feed/items/{iid}/reserve", {"by": by})

    def purchase(self, client, iid, by="Uncle", purchased=True):
        return feed(client, "post", f"/api/feed/items/{iid}/purchase", {"by": by, "purchased": purchased})

    def state(self, client, pid, iid):
        i = {i["id"]: i for i in items_of(client, pid)}[iid]
        return i["reserved_by"], i["purchased"], i["purchased_by"]

    def test_lists_configured_tabs_with_everything(self, client, owned, people, feed_on):
        bought = add(client, owned, name="Bought", url="example.com/b", store="Shop", price=12,
                     occasion="Birthday").json["id"]
        client.post(f"/api/items/{bought}/purchase", GRANDMA, json={"by": "Grandma"})
        held = add(client, owned, name="Held").json["id"]
        reserve(client, held, by="Grandma")
        idea(client, owned, name="Idea")
        removed = add(client, owned, name="Removed").json["id"]
        reserve(client, removed)
        client.delete(f"/api/items/{removed}", ALEX)
        old = add(client, owned, name="Old").json["id"]
        client.post(f"/api/items/{old}/purchase", GRANDMA)
        client.post(f"/api/items/{old}/archive", ADMIN)
        add(client, people["Sam"], name="Not exposed")

        r = feed(client, "get", "/api/feed")
        assert r.status_code == 200 and r.headers["Cache-Control"] == "no-store"
        assert [(p["id"], p["name"]) for p in r.json["people"]] == [(owned, "Alex")]
        items = self.items(client)
        assert set(items) == {"Bought", "Held", "Idea", "Removed"}
        assert all(set(i) == self.ITEM_KEYS for i in items.values())
        assert {k: items["Bought"][k] for k in self.ITEM_KEYS - {"id"}} == {
            "name": "Bought", "url": "https://example.com/b", "store": "Shop", "price": 12.0,
            "occasion": "Birthday", "secret": False, "reserved_by": None, "reserved_by_email": None,
            "purchased": True, "purchased_by": "Grandma", "purchased_by_email": GRANDMA, "archived_at": None}
        assert (items["Held"]["reserved_by"], items["Held"]["reserved_by_email"]) == ("Grandma", GRANDMA)
        assert items["Idea"]["secret"] is True and items["Removed"]["reserved_by"] == "Grandma"
        archived = self.items(client, archived=True)
        assert set(archived) == set(items) | {"Old"} and archived["Old"]["archived_at"]

    def test_tab_names_match_case_insensitively_and_empty_means_none(self, client, people, monkeypatch):
        monkeypatch.setattr(wishlist, "FEED_TOKEN", FEED_TOKEN)
        monkeypatch.setattr(wishlist, "FEED_PEOPLE", {"sam", "jordan", "nobody"})
        assert [p["name"] for p in feed(client, "get", "/api/feed").json["people"]] == ["Sam", "Jordan"]
        monkeypatch.setattr(wishlist, "FEED_PEOPLE", set())
        assert feed(client, "get", "/api/feed").json == {"people": []}

    def test_feed_caller_needs_no_lan_role(self, client, owned, feed_on, monkeypatch):
        monkeypatch.setattr(wishlist, "LAN_ROLE", "none")
        assert client.lan("get", "/api/people").status_code == 403
        iid = add(client, owned).json["id"]
        assert feed(client, "get", "/api/feed").status_code == 200
        assert self.reserve(client, iid, "Uncle").status_code == 200

    def test_reserve_relabel_and_release(self, client, owned, feed_on):
        iid = add(client, owned).json["id"]
        r = self.reserve(client, iid, "  Uncle  ")
        assert r.status_code == 200 and (r.json["reserved_by"], r.json["reserved_by_email"]) == ("Uncle", None)
        assert set(r.json) == self.ITEM_KEYS
        assert self.state(client, owned, iid) == ("Uncle", False, None)
        assert self.reserve(client, iid, "Aunt").json["reserved_by"] == "Aunt"
        r = self.reserve(client, iid, None)
        assert r.status_code == 200 and r.json["reserved_by"] is None
        assert self.reserve(client, iid, None).status_code == 200  # releasing an open item is harmless
        assert self.state(client, owned, iid) == (None, False, None)

    def test_a_feed_reservation_blocks_shoppers(self, client, owned, feed_on):
        iid = add(client, owned).json["id"]
        self.reserve(client, iid, "Uncle")
        r = reserve(client, iid)
        assert r.status_code == 400 and "Uncle" in r.json["error"]
        r = client.post(f"/api/items/{iid}/purchase", GRANDMA)
        assert r.status_code == 400 and "Uncle" in r.json["error"]
        assert self.state(client, owned, iid) == ("Uncle", False, None)

    def test_shopper_reservation_is_409_for_reserve_relabel_and_release(self, client, owned, feed_on):
        iid = add(client, owned).json["id"]
        reserve(client, iid, by="Grandma")
        for by in ("Uncle", None):
            r = self.reserve(client, iid, by)
            assert (r.status_code, r.json) == (409, {"error": "Reserved by Grandma."})
        r = self.purchase(client, iid)
        assert (r.status_code, r.json) == (409, {"error": "Reserved by Grandma."})
        assert self.state(client, owned, iid) == ("Grandma", False, None)

    def test_reserving_a_purchased_item_is_409(self, client, owned, feed_on):
        theirs, ours = add(client, owned).json["id"], add(client, owned).json["id"]
        client.post(f"/api/items/{theirs}/purchase", GRANDMA, json={"by": "Grandma"})
        self.purchase(client, ours, "Uncle")
        for iid, who in ((theirs, "Grandma"), (ours, "Uncle")):
            r = self.reserve(client, iid, "Aunt")
            assert (r.status_code, r.json) == (409, {"error": f"Already purchased by {who}."})
            assert self.state(client, owned, iid) == (None, True, who)

    def test_purchase_ends_a_feed_reservation_and_undo_leaves_it_open(self, client, owned, feed_on):
        iid = add(client, owned).json["id"]
        self.reserve(client, iid, "Uncle")
        r = self.purchase(client, iid, "Uncle")
        assert r.status_code == 200 and (r.json["purchased"], r.json["purchased_by"],
                                         r.json["purchased_by_email"], r.json["reserved_by"]) == (
            True, "Uncle", None, None)
        assert self.state(client, owned, iid) == (None, True, "Uncle")
        r = self.purchase(client, iid, "Uncle")
        assert (r.status_code, r.json) == (409, {"error": "Already purchased by Uncle."})
        r = self.purchase(client, iid, None, purchased=False)  # by isn't needed to undo
        assert r.status_code == 200 and (r.json["purchased"], r.json["purchased_by"]) == (False, None)
        assert self.state(client, owned, iid) == (None, False, None)
        assert self.purchase(client, iid, purchased=False).status_code == 200  # harmless when not bought

    def test_undoing_a_shoppers_purchase_is_409(self, client, owned, feed_on):
        iid = add(client, owned).json["id"]
        client.post(f"/api/items/{iid}/purchase", GRANDMA, json={"by": "Grandma"})
        r = self.purchase(client, iid, purchased=False)
        assert (r.status_code, r.json) == (409, {"error": "Purchased by Grandma."})
        assert self.state(client, owned, iid) == (None, True, "Grandma")

    def test_shoppers_cannot_undo_a_feed_purchase_but_editors_can(self, client, owned, feed_on):
        iid = add(client, owned).json["id"]
        self.purchase(client, iid, "Uncle")
        assert client.post(f"/api/items/{iid}/purchase", GRANDMA, json={"purchased": False}).status_code == 403
        assert client.post(f"/api/items/{iid}/purchase", ADMIN, json={"purchased": False}).status_code == 200

    def test_archived_items_answer_like_a_missing_item(self, client, owned, feed_on):
        iid = add(client, owned).json["id"]
        self.purchase(client, iid, "Uncle")
        client.post(f"/api/items/{iid}/archive", ADMIN)
        for path, body in (("reserve", {"by": "Aunt"}), ("reserve", {"by": None}),
                           ("purchase", {"by": "Aunt", "purchased": True}), ("purchase", {"by": "Uncle", "purchased": False})):
            a = feed(client, "post", f"/api/feed/items/{iid}/{path}", body)
            b = feed(client, "post", f"/api/feed/items/99999/{path}", body)
            assert (a.status_code, a.json) == (b.status_code, b.json) == (404, {"error": "No such item."})
        assert self.items(client, archived=True)["Thing"]["purchased_by"] == "Uncle"

    def test_ids_sqlite_cannot_hold_answer_like_a_missing_item(self, client, feed_on):
        for path in ("reserve", "purchase"):
            r = feed(client, "post", f"/api/feed/items/{2 ** 63}/{path}", {"by": "Uncle"})
            assert (r.status_code, r.json) == (404, {"error": "No such item."})

    def test_relabel_keeps_reserved_at_only_for_the_same_name(self, client, owned, feed_on, monkeypatch):
        iid = add(client, owned).json["id"]
        monkeypatch.setattr(wishlist.time, "time", lambda: 1000)
        self.reserve(client, iid, "Uncle")
        monkeypatch.setattr(wishlist.time, "time", lambda: 2000)
        self.reserve(client, iid, "Uncle")
        assert reserved_at(iid) == 1000
        self.reserve(client, iid, "Aunt")
        assert reserved_at(iid) == 2000

    def test_tab_outside_feed_people_answers_like_a_missing_item(self, client, people, owned, feed_on):
        iid = add(client, people["Sam"]).json["id"]
        for send in (lambda i: self.reserve(client, i, "Uncle"), lambda i: self.reserve(client, i, None),
                     lambda i: self.purchase(client, i), lambda i: self.purchase(client, i, purchased=False)):
            a, b = send(iid), send(99999)
            assert (a.status_code, a.json) == (b.status_code, b.json) == (404, {"error": "No such item."})
        assert self.state(client, people["Sam"], iid) == (None, False, None)

    def test_owner_removed_item_survives_the_trackers_relabel(self, client, owned, feed_on):
        # The tracker re-labels a purchase as "undo, then buy (or reserve) again", and takes a failed
        # change back the same way; neither step may delete the wish in between.
        held, bought = add(client, owned, name="Held").json["id"], add(client, owned, name="Bought").json["id"]
        self.reserve(client, held, "Uncle")
        self.purchase(client, bought, "Uncle")
        client.delete(f"/api/items/{held}", ALEX)
        client.delete(f"/api/items/{bought}", ALEX)
        assert set(self.items(client)) == {"Held", "Bought"}
        r = self.purchase(client, bought, "Uncle", purchased=False)
        assert r.status_code == 200 and r.json["purchased"] is False
        r = self.purchase(client, bought, "Aunt")
        assert r.status_code == 200 and r.json["purchased_by"] == "Aunt"
        r = self.reserve(client, held, None)
        assert r.status_code == 200 and r.json["reserved_by"] is None
        r = self.reserve(client, held, "Uncle")  # the tracker's rollback of that release
        assert r.status_code == 200 and r.json["reserved_by"] == "Uncle"
        assert set(self.items(client)) == {"Held", "Bought"}
        assert tab(client, owned, ALEX)["count"] == 0 and items_of(client, owned, ALEX) == []

    def test_owner_removed_item_nobody_is_getting_is_gone_everywhere(self, client, owned, feed_on):
        held = add(client, owned, name="Held").json["id"]
        self.reserve(client, held, "Uncle")
        client.delete(f"/api/items/{held}", ALEX)
        assert self.reserve(client, held, None).status_code == 200
        # Like a wish deleted on the wishlist: out of the feed, and unseen by shoppers, editors and the owner.
        assert self.items(client) == {}
        for email in (ADMIN, GRANDMA, ALEX):
            assert items_of(client, owned, email) == []
            assert reserve(client, held, email).status_code == 404
            assert client.post(f"/api/items/{held}/purchase", email).status_code == 404
        # The feed can still claim it back (the tracker's rollback), which brings it back for shoppers.
        assert self.reserve(client, held, "Uncle").status_code == 200
        assert [i["name"] for i in items_of(client, owned, GRANDMA)] == ["Held"]
        assert items_of(client, owned, ALEX) == []

    @pytest.mark.parametrize("body", [{}, {"by": ""}, {"by": "   "}, {"by": 5}, {"by": ["x"]}])
    def test_reserve_needs_a_name_or_null(self, client, owned, feed_on, body):
        iid = add(client, owned).json["id"]
        assert feed(client, "post", f"/api/feed/items/{iid}/reserve", body).status_code == 400

    @pytest.mark.parametrize("body", [{}, {"by": ""}, {"by": 5}, {"by": "x", "purchased": "yes"},
                                      {"by": "x", "purchased": 1}, {"purchased": None}])
    def test_purchase_validates_its_body(self, client, owned, feed_on, body):
        iid = add(client, owned).json["id"]
        assert feed(client, "post", f"/api/feed/items/{iid}/purchase", body).status_code == 400
        assert self.state(client, owned, iid) == (None, False, None)

    def test_by_is_capped_at_80(self, client, owned, feed_on):
        iid = add(client, owned).json["id"]
        assert self.reserve(client, iid, "x" * 200).json["reserved_by"] == "x" * 80

    @pytest.mark.parametrize("data", [None, "[1]", "not json"])
    def test_writes_need_a_json_object(self, client, owned, feed_on, data):
        iid = add(client, owned).json["id"]
        h = {"Authorization": f"Bearer {FEED_TOKEN}"}
        r = client.c.post(f"/api/feed/items/{iid}/reserve", data={"by": "Uncle"} if data is None else data,
                          headers=h, content_type=None if data is None else "application/json",
                          environ_base={"REMOTE_ADDR": LAN_IP})
        assert r.status_code == 400
        assert self.state(client, owned, iid) == (None, False, None)

    def test_feed_writes_need_no_csrf_header_or_origin_but_nothing_else_is_exempt(self, client, owned, feed_on):
        iid = add(client, owned).json["id"]
        assert self.reserve(client, iid, "Uncle").status_code == 200  # no X-Wishlist, no Origin
        h = {"Authorization": f"Bearer {FEED_TOKEN}"}
        # the token does nothing for the rest of the API: CSRF still applies there
        for method, path, body in (("post", f"/api/people/{owned}/items", {"name": "x", "kind": "online"}),
                                   ("post", f"/api/items/{iid}/reserve", {"reserved": False}),
                                   ("patch", f"/api/items/{iid}", {"name": "x"}),
                                   ("delete", f"/api/items/{iid}", {})):
            assert feed(client, method, path, body).status_code == 400, path
            r = feed(client, method, path, body, headers={"X-Wishlist": "1", "Origin": "http://evil.example.com"})
            assert r.status_code == 403, path
        # and a valid token on another route is just a LAN request, with the LAN role
        assert feed(client, "get", "/api/me").json["source"] == "lan"
        assert self.state(client, owned, iid) == ("Uncle", False, None)
        assert [i["name"] for i in items_of(client, owned)] == ["Thing"]

    def test_unknown_feed_paths_are_still_404_for_the_feed(self, client, feed_on):
        for path in ("/api/feed/items", "/api/feed/items/1", "/api/feed/people"):
            assert feed(client, "get", path).status_code == 404

    # concurrency: the claim is one statement, so a write landing after the read still wins
    def stale(self, monkeypatch, name, sql, args):
        real = getattr(wishlist, name)

        def read_then_someone_else_writes(item_id):
            row = real(item_id)
            monkeypatch.setattr(wishlist, name, real)  # only the first read is stale
            other = sqlite3.connect(wishlist.DB_PATH)
            other.execute(sql, (*args, item_id))
            other.commit()
            other.close()
            return row

        monkeypatch.setattr(wishlist, name, read_then_someone_else_writes)

    SHOPPER_RESERVES = ("UPDATE items SET reserved_by = 'Grandma', reserved_by_email = ?, reserved_at = 1 "
                        "WHERE id = ?", (GRANDMA,))
    SHOPPER_BUYS = ("UPDATE items SET purchased = 1, purchased_by = 'Grandma', purchased_by_email = ?, "
                    "purchased_at = 1 WHERE id = ?", (GRANDMA,))
    FEED_RESERVES = ("UPDATE items SET reserved_by = 'Uncle', reserved_by_email = NULL, reserved_at = 1 "
                     "WHERE id = ?", ())

    def test_shopper_reserving_first_wins_over_the_feed(self, client, owned, feed_on, monkeypatch):
        iid = add(client, owned).json["id"]
        self.stale(monkeypatch, "feed_item", *self.SHOPPER_RESERVES)
        r = self.reserve(client, iid, "Uncle")
        assert (r.status_code, r.json) == (409, {"error": "Reserved by Grandma."})
        assert self.state(client, owned, iid) == ("Grandma", False, None)

    def test_shopper_buying_first_wins_over_a_feed_purchase(self, client, owned, feed_on, monkeypatch):
        iid = add(client, owned).json["id"]
        self.stale(monkeypatch, "feed_item", *self.SHOPPER_BUYS)
        r = self.purchase(client, iid, "Uncle")
        assert (r.status_code, r.json) == (409, {"error": "Already purchased by Grandma."})
        assert self.state(client, owned, iid) == (None, True, "Grandma")

    def test_shopper_reserving_first_wins_over_a_feed_purchase(self, client, owned, feed_on, monkeypatch):
        iid = add(client, owned).json["id"]
        self.stale(monkeypatch, "feed_item", *self.SHOPPER_RESERVES)
        assert self.purchase(client, iid, "Uncle").status_code == 409
        assert self.state(client, owned, iid) == ("Grandma", False, None)

    def test_feed_reserving_first_wins_over_a_shopper(self, client, owned, feed_on, monkeypatch):
        iid = add(client, owned).json["id"]
        self.stale(monkeypatch, "get_item", *self.FEED_RESERVES)
        r = reserve(client, iid, by="Grandma")
        assert r.status_code == 400 and "Uncle" in r.json["error"]
        assert self.state(client, owned, iid) == ("Uncle", False, None)

    def test_feed_reserving_first_wins_over_a_shopper_purchase(self, client, owned, feed_on, monkeypatch):
        iid = add(client, owned).json["id"]
        self.stale(monkeypatch, "get_item", *self.FEED_RESERVES)
        r = client.post(f"/api/items/{iid}/purchase", GRANDMA, json={"by": "Grandma"})
        assert r.status_code == 400 and "Uncle" in r.json["error"]
        assert self.state(client, owned, iid) == ("Uncle", False, None)

# ------------------------------------------------------------------ tabs
class TestPeople:
    def test_add_person_normalizes_fields(self, client):
        r = client.post("/api/people", ADMIN, json={"name": "  " + "G" * 100, "owner_email": " Gp@Example.com ",
                                                    "color": "#AABBCC"})
        assert r.status_code == 201
        p = {x["id"]: x for x in client.get("/api/people", ADMIN).json}[r.json["id"]]
        assert (p["name"], p["owner_email"], p["color"], p["count"]) == ("G" * 60, "gp@example.com", "#aabbcc", 0)

    @pytest.mark.parametrize("body", [{}, {"name": "  "}, {"name": "X", "color": "blue"},
                                      {"name": "X", "color": "#12345"}, {"name": "X", "color": "#12345g"}])
    def test_add_person_validation(self, client, body):
        assert client.post("/api/people", ADMIN, json=body).status_code == 400
        assert len(client.get("/api/people", ADMIN).json) == 3

    def test_edit_person(self, client, people):
        pid = people["Jordan"]
        assert client.patch(f"/api/people/{pid}", ADMIN, json={"name": " "}).status_code == 400
        client.patch(f"/api/people/{pid}", ADMIN, json={"name": "Jo", "color": "#000000"})
        client.patch(f"/api/people/{pid}", ADMIN, json={"color": ""})  # back to the default color
        p = {x["id"]: x for x in client.get("/api/people", ADMIN).json}[pid]
        assert (p["name"], p["color"]) == ("Jo", None)

    def test_move_tabs_and_ends_are_no_ops(self, client, people):
        names = lambda: [p["name"] for p in client.get("/api/people", ADMIN).json]  # noqa: E731
        client.patch(f"/api/people/{people['Alex']}", ADMIN, json={"move": "up"})
        client.patch(f"/api/people/{people['Jordan']}", ADMIN, json={"move": "down"})
        assert names() == ["Alex", "Sam", "Jordan"]
        client.patch(f"/api/people/{people['Alex']}", ADMIN, json={"move": "down"})
        assert names() == ["Sam", "Alex", "Jordan"]
        client.patch(f"/api/people/{people['Alex']}", ADMIN, json={"move": "sideways"})
        assert names() == ["Sam", "Alex", "Jordan"]

    def test_unknown_person_is_404(self, client):
        assert client.patch("/api/people/999", ADMIN, json={"name": "x"}).status_code == 404
        assert client.delete("/api/people/999", ADMIN).status_code == 404

    def test_deleting_a_person_deletes_their_items(self, client, people):
        iid = add(client, people["Sam"]).json["id"]
        keep = add(client, people["Alex"]).json["id"]
        client.delete(f"/api/people/{people['Sam']}", ADMIN)
        assert client.patch(f"/api/items/{iid}", ADMIN, json={"name": "ghost"}).status_code == 404
        assert client.get(f"/api/people/{people['Sam']}/items", ADMIN).status_code == 404
        assert [i["id"] for i in items_of(client, people["Alex"])] == [keep]

    def test_restart_does_not_reseed_or_recolor(self, client, people):
        client.delete(f"/api/people/{people['Jordan']}", ADMIN)
        client.patch(f"/api/people/{people['Alex']}", ADMIN, json={"color": "#111111"})
        wishlist.init_db()
        tabs = client.get("/api/people", ADMIN).json
        assert [(p["name"], p["color"]) for p in tabs] == [("Alex", "#111111"), ("Sam", "#2563eb")]

    def test_malformed_person_colors_are_ignored(self, client, monkeypatch):
        monkeypatch.setenv("PERSON_COLORS", "Jordan=red,=#123456,Jordan,Jordan=#12345,,jordan = #ABCDEF ")
        wishlist.init_db()
        tabs = {p["name"]: p["color"] for p in client.get("/api/people", ADMIN).json}
        assert tabs["Jordan"] == "#abcdef"


# ------------------------------------------------------------------ photos
def encoded(img, fmt, **kw):
    buf = io.BytesIO()
    img.save(buf, fmt, **kw)
    buf.seek(0)
    return buf


class TestPhotosMore:
    def test_upload_errors(self, client, people):
        assert client.upload(999, png()).status_code == 404
        iid = add(client, people["Alex"]).json["id"]
        r = client.c.post(f"/api/items/{iid}/photo", data={"other": "x"}, headers=client.headers(ADMIN),
                          content_type="multipart/form-data", environ_base={"REMOTE_ADDR": "203.0.113.5"})
        assert r.status_code == 400 and "No photo" in r.json["error"]
        assert client.upload(iid, io.BytesIO(b""), filename="empty.jpg").status_code == 400
        assert items_of(client, people["Alex"])[0]["photo_url"] is None and no_files(iid)

    def test_get_photo_errors(self, client, people):
        iid = add(client, people["Alex"]).json["id"]
        assert client.get(f"/photos/{iid}/full", ADMIN).status_code == 404   # no photo yet
        client.upload(iid, png())
        assert client.get(f"/photos/{iid}/original", ADMIN).status_code == 404
        assert client.get("/photos/999/full", ADMIN).status_code == 404

    @pytest.mark.parametrize("make", [
        lambda: encoded(Image.new("P", (300, 200), 3), "PNG", transparency=3),
        lambda: encoded(Image.new("LA", (300, 200), (120, 0)), "PNG"),
        lambda: encoded(Image.new("CMYK", (300, 200), (0, 80, 80, 0)), "JPEG"),
        lambda: encoded(Image.new("L", (300, 200), 77), "PNG"),
        lambda: encoded(Image.new("RGB", (300, 200), (1, 2, 3)), "WEBP"),
        lambda: encoded(Image.new("RGB", (300, 200), (1, 2, 3)), "GIF"),
    ], ids=["palette-transparent", "gray-alpha", "cmyk-jpeg", "grayscale", "webp", "gif"])
    def test_any_common_format_is_stored_as_rgb_jpeg(self, client, people, make):
        iid = add(client, people["Alex"]).json["id"]
        r = client.upload(iid, make())
        assert r.status_code == 200
        im = Image.open(io.BytesIO(client.get(r.json["photo_url"], ADMIN).data))
        assert (im.format, im.mode, im.size) == ("JPEG", "RGB", (300, 200))

    def test_small_photos_are_not_upscaled(self, client, people):
        iid = add(client, people["Alex"]).json["id"]
        r = client.upload(iid, encoded(Image.new("RGB", (120, 80)), "PNG"))
        thumb = Image.open(io.BytesIO(client.get(r.json["thumb_url"], ADMIN).data))
        assert thumb.size == (120, 80)

    def test_failed_replacement_keeps_the_old_photo(self, client, people):
        iid = add(client, people["Alex"]).json["id"]
        first = client.upload(iid, png()).json["photo_url"]
        assert client.upload(iid, io.BytesIO(b"junk"), filename="x.png").status_code == 400
        assert items_of(client, people["Alex"])[0]["photo_url"] == first
        assert client.get(first, ADMIN).status_code == 200

    def test_no_temp_files_left_behind(self, client, people):
        import os
        iid = add(client, people["Alex"]).json["id"]
        client.upload(iid, png())
        assert sorted(os.listdir(wishlist.PHOTO_DIR)) == [f"{iid}.jpg", f"{iid}_t.jpg"]

    def test_removing_a_photo_twice_is_fine(self, client, people):
        iid = add(client, people["Alex"]).json["id"]
        assert client.delete(f"/api/items/{iid}/photo", ADMIN).status_code == 200
        assert client.delete(f"/api/items/{iid}/photo", "shopper@example.com").status_code == 403

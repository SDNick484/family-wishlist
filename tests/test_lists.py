"""Items, purchases, spoiler protection, tabs and photos: edge cases not covered in test_app.py."""
import base64
import io
import re
import sqlite3

import pytest
from PIL import Image

import app as wishlist
from conftest import ADMIN, FEED_TOKEN, LAN_IP, PUBLIC_IP, token
from test_app import add, no_files, png

GRANDMA = "grandma@example.com"
ALEX = "alex@example.com"


def items_of(client, pid, email=ADMIN):
    return client.get(f"/api/people/{pid}/items", email).json["items"]


def order(client, pid, email=ADMIN):
    return [i["name"] for i in sorted(items_of(client, pid, email), key=lambda i: (i["position"], i["id"]))]


def item_rows():
    """Every column of every item, straight from the database."""
    con = sqlite3.connect(wishlist.DB_PATH)
    try:
        return con.execute("SELECT * FROM items ORDER BY id").fetchall()
    finally:
        con.close()


def tab(client, pid, email):
    return {p["id"]: p for p in client.get("/api/people", email).json}[pid]


@pytest.fixture
def owned(client, people):
    """Alex's tab belongs to alex@example.com, who is also an editor."""
    pid = people["Alex"]
    client.patch(f"/api/people/{pid}", ADMIN, json={"owner_email": "Alex@Example.com"})
    client.put("/api/members", ADMIN, json={"email": ALEX, "display_name": "Alex", "role": "editor"})
    return pid


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

    # the private feed (/api/feed): it returns exactly what the rules above hide, so nobody but the
    # companion service may tell it apart from a path that does not exist
    FEED_CALLS = (("GET", "/api/feed", None), ("GET", "/api/feed?archived=1", None),
                  ("POST", "/api/feed/items/{}/reserve", {"by": "Mallory"}),
                  ("POST", "/api/feed/items/{}/reserve", {"by": None}),
                  ("POST", "/api/feed/items/{}/purchase", {"by": "Mallory", "purchased": True}),
                  ("POST", "/api/feed/items/{}/purchase", {"by": "Mallory", "purchased": False}))

    def like_missing(self, client, method, path, body=None, headers=None, ip=LAN_IP, missing=None):
        """`path` answers exactly (status, headers, body) as the same request for a path that does not
        exist, and the items table is unchanged afterwards. Returns the response."""
        missing = missing or re.sub("(?i)feed", "nope", path, count=1)
        before = item_rows()

        def send(p):
            kw = {"headers": headers or {}, "environ_base": {"REMOTE_ADDR": ip}}
            if body is not None:
                kw["json"] = body
            return client.c.open(p, method=method, **kw)

        a, b = send(path), send(missing)
        assert (a.status_code, sorted(a.headers.items()), a.get_data()) == (
            b.status_code, sorted(b.headers.items()), b.get_data()), (method, path, headers, ip)
        assert FEED_TOKEN not in a.get_data(as_text=True)
        assert item_rows() == before, (method, path)
        return a

    def caller(self, name):
        """(headers, remote address) of someone who is not the companion service."""
        bearer = {"Authorization": f"Bearer {FEED_TOKEN}"}
        owner = {"Cf-Ray": "r", "Cf-Access-Jwt-Assertion": token(ALEX)}
        shopper = {"Cf-Ray": "r", "Cf-Access-Jwt-Assertion": token(GRANDMA)}
        return {
            "owner": (owner, PUBLIC_IP),
            "owner with the token": (owner | bearer, PUBLIC_IP),
            "owner with the token via a local tunnel": (owner | bearer, "127.0.0.1"),
            "owner with a wrong token": (owner | {"Authorization": "Bearer guess"}, PUBLIC_IP),
            "shopper": (shopper, PUBLIC_IP),
            "shopper with the token": (shopper | bearer, PUBLIC_IP),
            "Cloudflare without sign-in, with the token": ({"Cf-Ray": "r"} | bearer, PUBLIC_IP),
            "LAN editor": ({}, LAN_IP),
            "LAN with a wrong token": ({"Authorization": "Bearer guess"}, LAN_IP),
            "LAN with a token prefix": ({"Authorization": f"Bearer {FEED_TOKEN[:-1]}"}, LAN_IP),
            "LAN with a token plus one": ({"Authorization": f"Bearer {FEED_TOKEN}x"}, LAN_IP),
            "LAN with an empty bearer": ({"Authorization": "Bearer "}, LAN_IP),
            "LAN with a bare scheme": ({"Authorization": "Bearer"}, LAN_IP),
            "LAN with no scheme": ({"Authorization": FEED_TOKEN}, LAN_IP),
            "LAN with Basic": ({"Authorization": "Basic " + base64.b64encode(f"feed:{FEED_TOKEN}".encode()).decode()},
                               LAN_IP),
            "LAN with another scheme": ({"Authorization": f"Token {FEED_TOKEN}"}, LAN_IP),
            "LAN with another header": ({"X-Feed-Token": FEED_TOKEN}, LAN_IP),
            "the token from a public address": (bearer, "8.8.8.8"),
        }[name]

    CALLERS = ("owner", "owner with the token", "owner with the token via a local tunnel", "owner with a wrong token",
               "shopper", "shopper with the token", "Cloudflare without sign-in, with the token", "LAN editor",
               "LAN with a wrong token", "LAN with a token prefix", "LAN with a token plus one",
               "LAN with an empty bearer", "LAN with a bare scheme", "LAN with no scheme", "LAN with Basic",
               "LAN with another scheme", "LAN with another header", "the token from a public address")

    @pytest.mark.parametrize("who", CALLERS)
    @pytest.mark.parametrize("csrf", [False, True])
    def test_feed_is_a_missing_path_to_everyone_else(self, client, owned, bought, feed, who, csrf):
        headers, ip = self.caller(who)
        if csrf:  # what the app's own pages send, so the request gets as far as routing
            headers = headers | {"X-Wishlist": "1"}
        held = add(client, owned, name="Held").json["id"]
        assert feed.reserve(held, "Aunt Riley").status_code == 200  # real items: one held by the feed, one bought
        for iid in (held, bought):
            for method, path, body in self.FEED_CALLS:
                self.like_missing(client, method, path.format(iid), body, headers, ip)

    def test_feed_is_a_missing_path_to_the_owner(self, client, owned, bought, feed):
        """The plain case, spelled out: the owner signed in through Cloudflare gets the generic 404."""
        for method, path, body in self.FEED_CALLS:
            r = self.like_missing(client, method, path.format(bought), body,
                                  {"Cf-Ray": "r", "Cf-Access-Jwt-Assertion": token(ALEX), "X-Wishlist": "1"},
                                  PUBLIC_IP)
            assert r.status_code == 404 and "Grandma" not in r.get_data(as_text=True)

    @pytest.mark.parametrize("header", wishlist.CF_HEADERS)
    @pytest.mark.parametrize("ip", [LAN_IP, "127.0.0.1"])
    def test_a_valid_token_with_any_cloudflare_header_is_refused(self, client, owned, bought, feed, header, ip):
        value = token(ALEX) if header == "Cf-Access-Jwt-Assertion" else "x"
        for name in (header, header.lower()):
            for extra in ({}, {"X-Wishlist": "1"}):
                headers = {"Authorization": f"Bearer {FEED_TOKEN}", name: value, **extra}
                for method, path, body in self.FEED_CALLS:
                    self.like_missing(client, method, path.format(bought), body, headers, ip)
        assert feed.get().status_code == 200  # the same token alone works

    def test_a_valid_token_with_a_cloudflare_cookie_is_refused(self, client, owned, bought, feed):
        client.c.set_cookie("CF_Authorization", token(ALEX))
        for method, path, body in self.FEED_CALLS:
            self.like_missing(client, method, path.format(bought), body, {"Authorization": f"Bearer {FEED_TOKEN}"})

    def test_the_token_only_works_in_the_authorization_header(self, client, owned, bought, feed):
        for method, path, body in self.FEED_CALLS:
            path = path.format(bought)
            sep = "&" if "?" in path else "?"
            for key in ("token", "access_token", "feed_token", "FEED_TOKEN", "bearer"):
                self.like_missing(client, method, f"{path}{sep}{key}={FEED_TOKEN}", body)
            for key in ("token", "feed_token", "FEED_TOKEN", "Authorization"):
                client.c.set_cookie(key, FEED_TOKEN)
                self.like_missing(client, method, path, body)
                self.like_missing(client, method, path, body, {"X-Wishlist": "1"})
                client.c.delete_cookie(key)

    @pytest.mark.parametrize("auth", [None, "Bearer guess", f"Bearer {FEED_TOKEN}"])
    def test_head_options_and_other_methods_look_missing(self, client, owned, bought, feed, auth):
        headers = {"Authorization": auth} if auth else {}
        for path in ("/api/feed", f"/api/feed/items/{bought}/reserve", f"/api/feed/items/{bought}/purchase"):
            for method in ("OPTIONS", "PUT", "PATCH", "DELETE", "TRACE", "PROPFIND"):
                self.like_missing(client, method, path, {}, headers)
                self.like_missing(client, method, path, {}, headers | {"X-Wishlist": "1"})
            if path != "/api/feed":
                self.like_missing(client, "GET", path, None, headers)
                self.like_missing(client, "HEAD", path, None, headers)
        if auth != f"Bearer {FEED_TOKEN}":
            self.like_missing(client, "HEAD", "/api/feed", None, headers)
        else:  # HEAD is GET without the body, for the feed only
            r = client.c.head("/api/feed", headers=headers, environ_base={"REMOTE_ADDR": LAN_IP})
            assert r.status_code == 200 and r.get_data() == b""

    def test_other_spellings_of_the_path_look_missing_even_with_the_token(self, client, owned, bought, feed):
        headers = {"Authorization": f"Bearer {FEED_TOKEN}"}
        for method, path, body in (
                ("GET", "/api/feed/", None), ("GET", "/API/FEED", None), ("GET", "/api/Feed", None),
                ("GET", "/api//feed", None), ("GET", "/api/feed/people", None), ("GET", "/api/feeds", None),
                ("POST", f"/api/feed/items/{bought}/reserve/", {"by": "x"}),
                ("POST", f"/api/feed/items/{bought}/Reserve", {"by": "x"}),
                ("POST", f"/api/FEED/items/{bought}/purchase", {"by": "x", "purchased": True}),
                ("POST", f"/api/feed/items/{bought}/archive", {}),
                ("POST", f"/api/feed//items/{bought}/reserve", {"by": "x"}),
                ("POST", "/api/feed/items/-1/reserve", {"by": "x"}),
                ("POST", "/api/feed/items/x/reserve", {"by": "x"}),
                ("POST", "/api/feed/items/99999999999999999999999/reserve", {"by": "x"})):
            for extra in ({}, {"X-Wishlist": "1"}):
                self.like_missing(client, method, path, body, headers | extra)

    @pytest.mark.parametrize("value", ["", "   ", " \t "])
    def test_an_unset_or_blank_token_turns_the_feed_off(self, client, owned, bought, feed, monkeypatch, value):
        monkeypatch.setattr(wishlist, "FEED_TOKEN", value)
        for auth in (None, "Bearer", "Bearer ", f"Bearer {value}", "Bearer \t", f"Bearer {FEED_TOKEN}"):
            headers = {"Authorization": auth} if auth is not None else {}
            for method, path, body in self.FEED_CALLS:
                self.like_missing(client, method, path.format(bought), body, headers)
                self.like_missing(client, method, path.format(bought), body, headers | {"X-Wishlist": "1"})

    def test_feed_writes_leave_every_owner_view_byte_identical(self, client, owned, bought, feed):
        """After the feed reserves, re-labels, buys, undoes and releases, nothing the owner can ask for
        differs, down to the byte, from before."""
        kite = add(client, owned, name="Kite", occasion="Birthday", price=7).json["id"]
        ball = add(client, owned, name="Ball").json["id"]
        surprise = idea(client, owned, name="Surprise").json["id"]
        client.upload(kite, png())
        open_ = next(i["id"] for i in items_of(client, owned, ALEX) if i["name"] == "Open")
        views = ("/api/people", f"/api/people/{owned}/items", f"/api/people/{owned}/history", "/api/occasions",
                 "/api/me", f"/photos/{kite}/full", f"/photos/{kite}/thumb", f"/photos/{surprise}/full",
                 f"/api/items/{kite}", f"/api/items/{surprise}")

        def snapshot():
            out = {}
            for path in views:
                r = client.get(path, ALEX)
                out[path] = (r.status_code, r.get_data())
            for method, path in (("post", "/api/items/{}/purchase"), ("post", "/api/items/{}/reserve")):
                for iid in (kite, ball, surprise, open_, bought):
                    r = client.req(method, path.format(iid), ALEX, json={})
                    out[(path, iid)] = (r.status_code, r.get_data())
            return out

        before = snapshot()
        steps = [lambda: feed.reserve(kite, "Aunt Riley"), lambda: feed.reserve(kite, "Uncle Casey"),
                 lambda: feed.reserve(surprise, "Aunt Riley"), lambda: feed.purchase(ball, "Aunt Riley"),
                 lambda: feed.purchase(surprise, "Uncle Casey")]
        for step in steps:
            assert step().status_code == 200
            assert snapshot() == before
        for step in (lambda: feed.purchase(ball, purchased=False), lambda: feed.reserve(kite, None),
                     lambda: feed.reserve(open_, "Aunt Riley"), lambda: feed.purchase(open_, "Aunt Riley")):
            assert step().status_code == 200
            assert snapshot() == before
        # ...while everyone else sees it all
        theirs = {i["id"]: i for i in items_of(client, owned, GRANDMA)}
        assert (theirs[open_]["purchased_by"], theirs[surprise]["purchased_by"]) == ("Aunt Riley", "Uncle Casey")

    def test_feed_reservation_answers_the_owner_like_any_other(self, client, owned, feed):
        """Owner deletes and edits look the same whether the feed holds the item or nobody does."""
        held, free = add(client, owned, name="Held").json["id"], add(client, owned, name="Free").json["id"]
        feed.reserve(held, "Aunt Riley")
        a = client.patch(f"/api/items/{held}", ALEX, json={"notes": "blue"})
        b = client.patch(f"/api/items/{free}", ALEX, json={"notes": "blue"})
        assert self.hidden(a.json) and self.hidden(b.json)
        a, b = client.delete(f"/api/items/{held}", ALEX), client.delete(f"/api/items/{free}", ALEX)
        assert (a.status_code, a.json) == (b.status_code, b.json) == (200, {"ok": True})
        assert items_of(client, owned, ALEX) == []
        # the feed still has the one it holds, flagged nowhere the owner can see
        assert held in feed.items() and free not in feed.items()


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

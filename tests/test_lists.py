"""Items, purchases, spoiler protection, tabs and photos: edge cases not covered in test_app.py."""
import io
import sqlite3

import pytest
from PIL import Image

import app as wishlist
from conftest import ADMIN
from test_app import add, no_files, png

GRANDMA = "grandma@example.com"
ALEX = "alex@example.com"


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
            "id": owned, "name": "Alex", "color": None, "is_me": False, "count": 3, "bought": 1, "remaining": 2.5}
        client.delete(f"/api/items/{a}", ALEX)  # owner removes the bought one
        t = tab(client, owned, GRANDMA)
        assert (t["count"], t["bought"], t["remaining"]) == (2, 0, 2.5)

    def test_editing_a_purchased_item_keeps_the_purchase(self, client, people):
        iid = add(client, people["Alex"]).json["id"]
        client.post(f"/api/items/{iid}/purchase", GRANDMA)
        r = client.patch(f"/api/items/{iid}", ADMIN, json={"name": "Renamed", "price": 3}).json
        assert r["purchased"] is True and r["name"] == "Renamed"


# ------------------------------------------------------------------ spoilers
class TestSpoilersMore:
    """The owner of a tab must not be able to learn what has been bought, from any endpoint."""

    HIDDEN = {"purchased": False, "purchased_by": None, "purchased_at": None, "mine": False,
              "removed_by_owner": False}

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

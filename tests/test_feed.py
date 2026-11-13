"""The private feed (/api/feed) as the companion service uses it. Who else can reach it, and what the
owner of a tab can learn from it, is in TestSpoilersMore (test_lists.py)."""
import logging
import sqlite3

import pytest

import app as wishlist
from conftest import ADMIN, FEED_TOKEN, LAN_IP
from test_app import add
from test_lists import ALEX, GRANDMA, idea, item_rows, items_of, reserve

ITEM_KEYS = {"id", "name", "url", "store", "price", "occasion", "secret", "reserved_by", "reserved_by_email",
             "purchased", "purchased_by", "purchased_by_email", "archived_at"}


@pytest.fixture
def owned(client, people):
    client.patch(f"/api/people/{people['Alex']}", ADMIN, json={"owner_email": ALEX})
    return people["Alex"]


def row(iid):
    con = sqlite3.connect(wishlist.DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        return con.execute("SELECT * FROM items WHERE id = ?", (iid,)).fetchone()
    finally:
        con.close()


def write_directly(sql, *args):
    """Another connection commits a change: what a second request does between two statements of ours."""
    con = sqlite3.connect(wishlist.DB_PATH)
    con.execute(sql, args)
    con.commit()
    con.close()


class TestRead:
    def test_shape_and_tabs(self, client, people, feed):
        kite = add(client, people["Alex"], name="Kite", url="example.com/kite", store="Toy Shop", price=12.5,
                   occasion="Birthday").json["id"]
        add(client, people["Jordan"], name="Not in the feed")
        r = feed.get()
        assert r.status_code == 200 and r.headers["Cache-Control"] == "no-store"
        assert [(p["id"], p["name"]) for p in r.json["people"]] == [(people["Alex"], "Alex"), (people["Sam"], "Sam")]
        assert r.json["people"][0]["items"] == [{
            "id": kite, "name": "Kite", "url": "https://example.com/kite", "store": "Toy Shop", "price": 12.5,
            "occasion": "Birthday", "secret": False, "reserved_by": None, "reserved_by_email": None,
            "purchased": False, "purchased_by": None, "purchased_by_email": None, "archived_at": None}]
        assert r.json["people"][1]["items"] == []

    def test_includes_secret_ideas_removed_items_and_who_holds_what(self, client, owned, feed):
        secret = idea(client, owned, GRANDMA, name="Drone").json["id"]
        bought = add(client, owned, name="Bought").json["id"]
        held = add(client, owned, name="Held").json["id"]
        client.post(f"/api/items/{bought}/purchase", GRANDMA, json={"by": "Grandma"})
        reserve(client, held, "uncle@example.com", by="Uncle")
        client.delete(f"/api/items/{bought}", ALEX)  # the owner removes it: kept, hidden from them
        items = feed.items()
        assert set(items) == {secret, bought, held} and all(set(i) == ITEM_KEYS for i in items.values())
        assert items[secret]["secret"] is True
        assert (items[bought]["purchased"], items[bought]["purchased_by"], items[bought]["purchased_by_email"]) == (
            True, "Grandma", GRANDMA)
        assert (items[held]["reserved_by"], items[held]["reserved_by_email"]) == ("Uncle", "uncle@example.com")

    def test_archived_items_only_on_request(self, client, people, feed):
        given = add(client, people["Sam"], name="Given").json["id"]
        client.post(f"/api/items/{given}/purchase", GRANDMA)
        client.post(f"/api/items/{given}/archive", ADMIN)
        assert given not in feed.items()
        assert feed.items(archived=True)[given]["archived_at"]
        assert feed.get("/api/feed?archived=yes").json == feed.get().json

    def test_tab_names_match_trimmed_and_case_insensitively(self, client, people, feed, monkeypatch):
        monkeypatch.setattr(wishlist, "FEED_PEOPLE", ("  jordan ", "SAM"))
        assert [p["name"] for p in feed.get().json["people"]] == ["Sam", "Jordan"]  # tab order

    def test_no_tabs_configured_means_none(self, client, people, feed, monkeypatch):
        iid = add(client, people["Alex"]).json["id"]
        monkeypatch.setattr(wishlist, "FEED_PEOPLE", ())
        assert feed.get().json == {"people": []}
        assert feed.reserve(iid, "Aunt Riley").status_code == 404
        assert feed.purchase(iid).status_code == 404

    def test_lan_role_none_does_not_turn_off_the_feed(self, client, people, feed, monkeypatch):
        monkeypatch.setattr(wishlist, "LAN_ROLE", "none")
        assert client.lan("get", "/api/people").status_code == 403
        assert feed.get().status_code == 200

    def test_bearer_scheme_is_case_insensitive(self, client, people, feed):
        r = client.c.get("/api/feed", headers={"Authorization": f"bearer {FEED_TOKEN}"},
                         environ_base={"REMOTE_ADDR": LAN_IP})
        assert r.status_code == 200


class TestReserve:
    def test_reserve_under_a_label_without_an_email(self, client, people, feed):
        iid = add(client, people["Alex"]).json["id"]
        r = feed.reserve(iid, "  Aunt Riley  ")
        assert r.status_code == 200 and (r.json["reserved_by"], r.json["reserved_by_email"]) == ("Aunt Riley", None)
        assert row(iid)["reserved_at"]
        theirs = items_of(client, people["Alex"], GRANDMA)[0]
        assert (theirs["reserved_by"], theirs["reserved_mine"]) == ("Aunt Riley", False)

    def test_a_feed_reservation_blocks_shoppers(self, client, people, feed):
        iid = add(client, people["Alex"]).json["id"]
        feed.reserve(iid, "Aunt Riley")
        assert reserve(client, iid).status_code == 400
        r = client.post(f"/api/items/{iid}/purchase", GRANDMA)
        assert r.status_code == 400 and "Aunt Riley" in r.json["error"]
        assert reserve(client, iid, reserved=False).status_code == 403  # not theirs to release
        assert row(iid)["reserved_by"] == "Aunt Riley"

    def test_relabel_and_release_a_feed_reservation(self, client, people, feed):
        iid = add(client, people["Alex"]).json["id"]
        feed.reserve(iid, "Aunt Riley")
        write_directly("UPDATE items SET reserved_at = 1 WHERE id = ?", iid)
        assert feed.reserve(iid, "Aunt Riley").json["reserved_by"] == "Aunt Riley"
        assert row(iid)["reserved_at"] == 1  # the same label keeps its time
        assert feed.reserve(iid, "Uncle Casey").json["reserved_by"] == "Uncle Casey"
        assert row(iid)["reserved_at"] > 1
        r = feed.reserve(iid, None)
        assert r.status_code == 200 and r.json["reserved_by"] is None and row(iid)["reserved_at"] is None

    def test_releasing_what_nobody_holds_succeeds_and_changes_nothing(self, client, people, feed):
        iid = add(client, people["Alex"]).json["id"]
        before = item_rows()
        r = feed.reserve(iid, None)
        assert r.status_code == 200 and r.json["reserved_by"] is None and item_rows() == before

    def test_a_shoppers_reservation_is_409(self, client, people, feed):
        iid = add(client, people["Alex"]).json["id"]
        reserve(client, iid, by="Grandma G")
        before = item_rows()
        for by in ("Aunt Riley", None):
            r = feed.reserve(iid, by)
            assert (r.status_code, r.json) == (409, {"error": "Reserved by Grandma G."})
        assert item_rows() == before

    def test_reserving_a_purchased_item_is_409(self, client, people, feed):
        a, b = add(client, people["Alex"]).json["id"], add(client, people["Alex"]).json["id"]
        client.post(f"/api/items/{a}/purchase", GRANDMA, json={"by": "Grandma"})
        feed.purchase(b, "Aunt Riley")
        before = item_rows()
        assert (feed.reserve(a, "Uncle Casey").status_code, feed.reserve(a, "Uncle Casey").json) == (
            409, {"error": "Purchased by Grandma."})
        assert feed.reserve(b, "Uncle Casey").status_code == 409
        assert item_rows() == before

    def test_label_is_capped_at_80(self, client, people, feed):
        iid = add(client, people["Alex"]).json["id"]
        assert feed.reserve(iid, "  " + "r" * 200).json["reserved_by"] == "r" * 80


class TestPurchase:
    def test_purchase_ends_a_feed_reservation(self, client, people, feed):
        iid = add(client, people["Alex"]).json["id"]
        feed.reserve(iid, "Aunt Riley")
        r = feed.purchase(iid, "Uncle Casey")
        assert r.status_code == 200
        assert (r.json["purchased"], r.json["purchased_by"], r.json["purchased_by_email"]) == (True, "Uncle Casey", None)
        assert (r.json["reserved_by"], r.json["reserved_by_email"]) == (None, None) and row(iid)["purchased_at"]

    def test_undo_leaves_it_open_not_reserved(self, client, people, feed):
        iid = add(client, people["Alex"]).json["id"]
        feed.reserve(iid, "Aunt Riley")
        feed.purchase(iid, "Aunt Riley")
        r = feed.purchase(iid, "Aunt Riley", purchased=False)
        assert r.status_code == 200
        assert (r.json["purchased"], r.json["purchased_by"], r.json["reserved_by"]) == (False, None, None)
        assert row(iid)["purchased_at"] is None
        assert feed.reserve(iid, "Aunt Riley").status_code == 200  # the caller reserves again

    def test_undoing_what_nobody_bought_succeeds_and_changes_nothing(self, client, people, feed):
        iid = add(client, people["Alex"]).json["id"]
        feed.reserve(iid, "Aunt Riley")
        before = item_rows()
        assert feed.purchase(iid, purchased=False).status_code == 200 and item_rows() == before

    def test_a_shoppers_reservation_blocks_the_purchase(self, client, people, feed):
        iid = add(client, people["Alex"]).json["id"]
        reserve(client, iid, by="Grandma G")
        before = item_rows()
        r = feed.purchase(iid)
        assert (r.status_code, r.json) == (409, {"error": "Reserved by Grandma G."}) and item_rows() == before

    def test_already_purchased_is_409(self, client, people, feed):
        a, b = add(client, people["Alex"]).json["id"], add(client, people["Alex"]).json["id"]
        client.post(f"/api/items/{a}/purchase", GRANDMA, json={"by": "Grandma"})
        feed.purchase(b, "Aunt Riley")
        before = item_rows()
        r = feed.purchase(a, "Uncle Casey")
        assert (r.status_code, r.json) == (409, {"error": "Purchased by Grandma."})
        assert feed.purchase(b, "Uncle Casey").status_code == 409  # re-labelling is undo, then purchase
        assert item_rows() == before

    def test_undoing_a_shoppers_purchase_is_409(self, client, people, feed):
        iid = add(client, people["Alex"]).json["id"]
        client.post(f"/api/items/{iid}/purchase", GRANDMA, json={"by": "Grandma"})
        before = item_rows()
        r = feed.purchase(iid, purchased=False)
        assert (r.status_code, r.json) == (409, {"error": "Purchased by Grandma."}) and item_rows() == before

    def test_a_feed_purchase_shows_to_shoppers_and_an_editor_can_undo_it(self, client, people, feed):
        iid = add(client, people["Alex"]).json["id"]
        feed.purchase(iid, "Aunt Riley")
        theirs = items_of(client, people["Alex"], GRANDMA)[0]
        assert (theirs["purchased_by"], theirs["mine"]) == ("Aunt Riley", False)
        assert client.post(f"/api/items/{iid}/purchase", GRANDMA, json={"purchased": False}).status_code == 403
        assert client.post(f"/api/items/{iid}/purchase", ADMIN, json={"purchased": False}).status_code == 200


class TestWriteRules:
    @pytest.mark.parametrize("action", ["reserve", "purchase"])
    def test_tab_outside_the_feed_answers_like_a_missing_item(self, client, people, feed, action):
        iid = add(client, people["Jordan"]).json["id"]
        body = {"by": "Aunt Riley", "purchased": True}
        before = item_rows()
        a = feed.post(f"/api/feed/items/{iid}/{action}", body)
        b = feed.post(f"/api/feed/items/99999/{action}", body)
        assert (a.status_code, a.get_data()) == (b.status_code, b.get_data())
        assert (a.status_code, a.json) == (404, {"error": "No such item."}) and item_rows() == before

    @pytest.mark.parametrize("action", ["reserve", "purchase"])
    def test_archived_items_are_gone_for_writes(self, client, people, feed, action):
        iid = add(client, people["Alex"]).json["id"]
        feed.purchase(iid, "Aunt Riley")
        client.post(f"/api/items/{iid}/archive", ADMIN)
        before = item_rows()
        for body in ({"by": "x", "purchased": False}, {"by": None, "purchased": False}, {"by": "x", "purchased": True}):
            r = feed.post(f"/api/feed/items/{iid}/{action}", body)
            assert (r.status_code, r.json) == (404, {"error": "No such item."})
        assert item_rows() == before

    def test_writes_need_no_csrf_header_or_origin_but_a_json_body(self, client, people, feed):
        iid = add(client, people["Alex"]).json["id"]
        before = item_rows()
        for kw in ({"data": "by=x"}, {"data": '{"by": "x"}'}, {"data": '{"by": "x"}', "content_type": "text/plain"},
                   {"data": "{not json", "content_type": "application/json"},
                   {"data": "", "content_type": "application/json"}):
            r = client.c.post(f"/api/feed/items/{iid}/reserve", headers=feed.headers(),
                              environ_base={"REMOTE_ADDR": LAN_IP}, **kw)
            assert r.status_code == 400, kw
        assert item_rows() == before

    @pytest.mark.parametrize("action,body", [
        ("reserve", {}), ("reserve", {"by": ""}), ("reserve", {"by": "   "}), ("reserve", {"by": 3}),
        ("reserve", {"by": ["x"]}), ("reserve", ["x"]), ("reserve", "x"),
        ("purchase", {"by": "x"}), ("purchase", {"by": "x", "purchased": 1}), ("purchase", {"by": "x", "purchased": "true"}),
        ("purchase", {"by": "", "purchased": True}), ("purchase", {"purchased": True}),
        ("purchase", {"by": None, "purchased": True}), ("purchase", {"by": 3, "purchased": False})])
    def test_bad_bodies_are_400(self, client, people, feed, action, body):
        iid = add(client, people["Alex"]).json["id"]
        before = item_rows()
        r = feed.post(f"/api/feed/items/{iid}/{action}", body)
        assert r.status_code == 400 and "error" in r.json and item_rows() == before

    def test_unknown_item_is_404(self, client, people, feed):
        assert feed.reserve(99999, "x").status_code == 404
        assert feed.purchase(99999).status_code == 404


class TestConcurrency:
    """A shopper and the feed act on the same item at once: the second one read it before the first committed."""

    def stale_feed_read(self, monkeypatch, sql, *args):
        real = wishlist.feed_item

        def stale(iid):
            got = real(iid)
            monkeypatch.setattr(wishlist, "feed_item", real)  # only the first read is stale
            write_directly(sql, *args, iid)
            return got

        monkeypatch.setattr(wishlist, "feed_item", stale)

    def stale_shopper_read(self, monkeypatch, sql, *args):
        real = wishlist.get_item

        def stale(iid):
            got = real(iid)
            monkeypatch.setattr(wishlist, "get_item", real)
            write_directly(sql, *args, iid)
            return got

        monkeypatch.setattr(wishlist, "get_item", stale)

    SHOPPER_RESERVES = ("UPDATE items SET reserved_by = 'Grandma', reserved_by_email = ?, reserved_at = 1 "
                        "WHERE id = ? AND purchased = 0 AND reserved_by IS NULL")
    SHOPPER_BUYS = ("UPDATE items SET purchased = 1, purchased_by = 'Grandma', purchased_by_email = ?, "
                    "purchased_at = 1 WHERE id = ? AND purchased = 0")
    FEED_RESERVES = ("UPDATE items SET reserved_by = 'Aunt Riley', reserved_by_email = NULL, reserved_at = 1 "
                     "WHERE id = ? AND purchased = 0 AND reserved_by IS NULL")
    FEED_BUYS = ("UPDATE items SET purchased = 1, purchased_by = 'Aunt Riley', purchased_by_email = NULL, "
                 "purchased_at = 1 WHERE id = ? AND purchased = 0")

    @pytest.mark.parametrize("shopper", ["reserves", "buys"])
    @pytest.mark.parametrize("action", ["reserve", "purchase"])
    def test_shopper_first_feed_loses(self, client, people, feed, monkeypatch, shopper, action):
        iid = add(client, people["Alex"]).json["id"]
        self.stale_feed_read(monkeypatch, self.SHOPPER_RESERVES if shopper == "reserves" else self.SHOPPER_BUYS,
                             GRANDMA)
        r = feed.reserve(iid, "Aunt Riley") if action == "reserve" else feed.purchase(iid, "Aunt Riley")
        word = "Reserved" if shopper == "reserves" else "Purchased"
        assert (r.status_code, r.json) == (409, {"error": f"{word} by Grandma."})
        now = row(iid)
        assert (now["reserved_by_email"] or now["purchased_by_email"]) == GRANDMA
        assert "Aunt Riley" not in (now["reserved_by"], now["purchased_by"])

    def test_shopper_first_feed_release_loses(self, client, people, feed, monkeypatch):
        iid = add(client, people["Alex"]).json["id"]
        self.stale_feed_read(monkeypatch, self.SHOPPER_RESERVES, GRANDMA)
        r = feed.reserve(iid, None)
        assert (r.status_code, r.json) == (409, {"error": "Reserved by Grandma."})
        assert row(iid)["reserved_by_email"] == GRANDMA

    @pytest.mark.parametrize("feed_does", ["reserves", "buys"])
    @pytest.mark.parametrize("action", ["reserve", "purchase"])
    def test_feed_first_shopper_loses(self, client, people, feed, monkeypatch, feed_does, action):
        iid = add(client, people["Alex"]).json["id"]
        self.stale_shopper_read(monkeypatch, self.FEED_RESERVES if feed_does == "reserves" else self.FEED_BUYS)
        r = client.post(f"/api/items/{iid}/{action}", GRANDMA, json={"by": "Grandma"})
        assert r.status_code == 400 and "Aunt Riley" in r.json["error"]
        now = row(iid)
        assert now["reserved_by_email"] is None and now["purchased_by_email"] is None
        assert "Aunt Riley" in (now["reserved_by"], now["purchased_by"])

    def test_both_at_once_through_the_real_routes(self, client, people, feed, monkeypatch):
        """The feed's read is stale, and the shopper's whole request lands in between."""
        iid = add(client, people["Alex"]).json["id"]
        real = wishlist.feed_item

        def stale(item_id):
            got = real(item_id)
            monkeypatch.setattr(wishlist, "feed_item", real)
            with wishlist.app.test_client() as other:
                r = other.post(f"/api/items/{item_id}/reserve", json={"by": "Grandma"},
                               headers=client.headers(GRANDMA), environ_base={"REMOTE_ADDR": "203.0.113.5"})
                assert r.status_code == 200
            return got

        monkeypatch.setattr(wishlist, "feed_item", stale)
        r = feed.purchase(iid, "Aunt Riley")
        assert (r.status_code, r.json) == (409, {"error": "Reserved by Grandma."})
        assert (row(iid)["reserved_by_email"], row(iid)["purchased"]) == (GRANDMA, 0)


class TestSecrecy:
    def test_the_token_is_compared_in_constant_time(self, client, people, feed, monkeypatch):
        calls = []
        real = wishlist.hmac.compare_digest

        def spy(a, b):
            calls.append((a, b))
            return real(a, b)

        monkeypatch.setattr(wishlist.hmac, "compare_digest", spy)
        assert feed.get().status_code == 200 and feed.get(token="wrong").status_code == 404
        assert calls == [(FEED_TOKEN.encode(), FEED_TOKEN.encode()), (b"wrong", FEED_TOKEN.encode())]

    def test_non_ascii_tokens_do_not_break_the_comparison(self, client, people, feed, monkeypatch):
        monkeypatch.setattr(wishlist, "FEED_TOKEN", "töken")
        assert feed.get(token="töken".encode().decode("latin-1")).status_code in (200, 404)
        assert feed.get(token="café").status_code == 404

    def test_the_token_never_appears_in_a_log_line_or_a_response(self, client, people, feed, caplog):
        caplog.set_level(logging.DEBUG)
        iid = add(client, people["Alex"]).json["id"]
        jordan = add(client, people["Jordan"]).json["id"]
        reserve(client, iid, by="Grandma")
        responses = [
            feed.get(), feed.get("/api/feed?archived=1"), feed.reserve(iid, "x"), feed.reserve(iid, None),
            feed.purchase(iid), feed.purchase(iid, purchased=False), feed.reserve(jordan, "x"),
            feed.reserve(99999, "x"), feed.post(f"/api/feed/items/{iid}/reserve", {"by": 3}),
            feed.post(f"/api/feed/items/{iid}/purchase", {"by": "x", "purchased": "yes"}),
            client.c.post(f"/api/feed/items/{iid}/reserve", data="{bad", content_type="application/json",
                          headers=feed.headers(), environ_base={"REMOTE_ADDR": LAN_IP}),
            feed.get(f"/api/feed?token={FEED_TOKEN}", token="wrong"), feed.get(token=FEED_TOKEN + "x"),
            feed.get(headers={"Cf-Ray": "r"}), feed.get(ip="8.8.8.8"),
            client.c.delete("/api/feed", headers=feed.headers(), environ_base={"REMOTE_ADDR": LAN_IP}),
        ]
        for r in responses:
            assert FEED_TOKEN not in r.get_data(as_text=True)
            assert FEED_TOKEN not in str(r.headers)
        assert FEED_TOKEN not in caplog.text

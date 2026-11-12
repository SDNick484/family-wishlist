"""Link preview (POST /api/preview): parsing saved pages, and every guard on the server's fetch.

The network is stubbed: DNS through socket.getaddrinfo, HTTP through app._open."""
import base64
import http.server
import io
import socket
import threading
from urllib.parse import urlparse

import pytest
from PIL import Image

import app as wishlist
from conftest import ADMIN

SHOP = "93.184.215.14"  # a public address; never contacted, the network is stubbed


class FakeResponse:
    def __init__(self, status=200, headers=None, body=b"", error=None):
        self.status = status
        self.headers = {k.lower(): v for k, v in (headers or {}).items()}
        self.body = io.BytesIO(body)
        self.error = error  # raised while the body is read
        self.closed = False

    def getheader(self, name, default=None):
        return self.headers.get(name.lower(), default)

    def read1(self, n):
        if self.error:
            raise self.error
        return self.body.read(n)

    def close(self):
        self.closed = True


def page(html, ctype="text/html; charset=utf-8", **kw):
    return FakeResponse(headers={"Content-Type": ctype}, body=html.encode(), **kw)


def redirect(location, status=302):
    return FakeResponse(status, {"Location": location})


def png_bytes(size=(40, 30), color="red"):
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG")
    return buf.getvalue()


class Net:
    """A stand-in internet: `dns` maps host -> addresses, `pages` maps "host/path" -> response."""

    def __init__(self):
        self.dns = {}
        self.pages = {}
        self.opened = []  # (url, ip) for every connection made
        self.timeouts = []

    def getaddrinfo(self, host, port, *a, **kw):
        if host not in self.dns:
            raise socket.gaierror(f"unknown host {host}")
        return [(socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))
                for ip in self.dns[host]]

    def open(self, url, ip, timeout):
        self.opened.append((url, ip))
        self.timeouts.append(timeout)
        u = urlparse(url)
        resp = self.pages.get(f"{u.hostname}{u.path}")
        if resp is None:
            raise ConnectionRefusedError("nothing there")
        if isinstance(resp, Exception):
            raise resp
        return resp() if callable(resp) else resp


@pytest.fixture
def net(monkeypatch):
    n = Net()
    n.dns["shop.example.com"] = [SHOP]
    monkeypatch.setattr(wishlist.socket, "getaddrinfo", n.getaddrinfo)
    monkeypatch.setattr(wishlist, "_open", n.open)
    return n


def preview(client, url, email=ADMIN):
    return client.post("/api/preview", email, json={"url": url})


PRODUCT = """<!doctype html><html><head>
<title>Ignored when og:title exists</title>
<meta property="og:title" content="LEGO Botanical &amp; Orchid">
<meta property="og:image" content="/img/orchid.png">
<meta property="product:price:amount" content="49.99">
</head><body>…</body></html>"""


# ------------------------------------------------------------------ parsing
class TestParsing:
    def test_open_graph_tags(self):
        found = wishlist.parse_preview(PRODUCT, "https://shop.example.com/p/1")
        assert found == {"title": "LEGO Botanical & Orchid", "price": 49.99,
                         "image_url": "https://shop.example.com/img/orchid.png"}

    def test_falls_back_to_title_tag_and_twitter_image(self):
        html = """<head><title>
          Plain   Page </title><meta name="twitter:image" content="https://cdn.example.com/a.jpg"></head>"""
        found = wishlist.parse_preview(html, "https://shop.example.com/")
        assert found == {"title": "Plain Page", "price": None, "image_url": "https://cdn.example.com/a.jpg"}

    def test_json_ld_offer_price(self):
        html = """<script type="application/ld+json">{"@context": "https://schema.org", "@graph": [
            {"@type": "WebPage", "name": "x"},
            {"@type": "Product", "name": "Kettle", "offers": {"@type": "Offer", "price": "1,299.50",
             "priceCurrency": "USD"}}]}</script><title>Kettle</title>"""
        assert wishlist.parse_preview(html, "https://shop.example.com/")["price"] == 1299.5

    def test_json_ld_offer_list_and_aggregate(self):
        html = """<script type="application/ld+json">[{"@type": "Product",
            "offers": [{"@type": "AggregateOffer", "lowPrice": 15, "highPrice": 30}]}]</script>"""
        assert wishlist.parse_preview(html, "https://shop.example.com/")["price"] == 15

    def test_price_outside_an_offer_is_ignored(self):
        html = """<script type="application/ld+json">{"@type": "Thing", "price": 5}</script>"""
        assert wishlist.parse_preview(html, "https://shop.example.com/")["price"] is None

    def test_meta_price_wins_over_json_ld(self):
        html = """<meta property="product:price:amount" content="10.00">
        <script type="application/ld+json">{"offers": {"price": 99}}</script>"""
        assert wishlist.parse_preview(html, "https://shop.example.com/")["price"] == 10

    def test_microdata_price(self):
        html = """<span itemprop="price" content="$24.95">$24.95</span>"""
        assert wishlist.parse_preview(html, "https://shop.example.com/")["price"] == 24.95

    @pytest.mark.parametrize("raw,price", [
        ("19.99", 19.99), ("$1,234.5", 1234.5), ("19,99", 19.99), (7, 7), ("0", None), ("0.00", None),
        ("free", None), ("-5", 5), (True, None), (None, None), ("inf", None), ("9" * 400, None), ([3], None),
        (float("nan"), None), (float("inf"), None)])
    def test_price_values(self, raw, price):
        assert wishlist._price(raw) == price

    def test_broken_json_ld_and_markup_are_survivable(self):
        html = """<meta property="og:title" content="Still here"><script type="application/ld+json">{not json
        </script><div <<< ><script type="application/ld+json">""" + "[" * 5000 + "]" * 5000 + "</script>"
        found = wishlist.parse_preview(html, "https://shop.example.com/")
        assert found["title"] == "Still here" and found["price"] is None

    def test_only_http_images(self):
        html = """<meta property="og:image" content="javascript:alert(1)">
                  <meta name="twitter:image" content="data:image/png;base64,AAAA">"""
        assert wishlist.parse_preview(html, "https://shop.example.com/")["image_url"] is None

    def test_long_title_is_trimmed(self):
        html = f"<meta property='og:title' content='{'x' * 500}'>"
        assert len(wishlist.parse_preview(html, "https://shop.example.com/")["title"]) == 200

    def test_nothing_found(self):
        assert wishlist.parse_preview("", "https://shop.example.com/") == {
            "title": None, "price": None, "image_url": None}


# ------------------------------------------------------------------ the endpoint
class TestPreviewEndpoint:
    def test_fills_title_price_and_photo(self, client, net):
        net.pages["shop.example.com/p/1"] = page(PRODUCT)
        net.pages["shop.example.com/img/orchid.png"] = FakeResponse(
            headers={"Content-Type": "image/png"}, body=png_bytes())
        r = preview(client, "shop.example.com/p/1")  # no scheme: https is assumed, as when saving
        assert r.status_code == 200
        assert r.json["title"] == "LEGO Botanical & Orchid" and r.json["price"] == 49.99
        prefix = "data:image/jpeg;base64,"
        assert r.json["image"].startswith(prefix)
        img = Image.open(io.BytesIO(base64.b64decode(r.json["image"][len(prefix):])))
        assert img.format == "JPEG" and img.size == (40, 30)  # re-encoded like an upload
        assert [u for u, _ in net.opened] == ["https://shop.example.com:443/p/1",
                                              "https://shop.example.com:443/img/orchid.png"]

    def test_connects_to_the_address_it_checked(self, client, net):
        net.pages["shop.example.com/"] = page("<title>Hi</title>")
        assert preview(client, "http://shop.example.com/").json["title"] == "Hi"
        assert net.opened == [("http://shop.example.com:80/", SHOP)]

    def test_page_without_image_or_unreadable_image(self, client, net):
        net.pages["shop.example.com/a"] = page("<title>A</title>")
        assert preview(client, "https://shop.example.com/a").json == {"title": "A", "price": None, "image": None}
        net.pages["shop.example.com/b"] = page(PRODUCT.replace("/img/orchid.png", "/img/bad.png"))
        net.pages["shop.example.com/img/bad.png"] = FakeResponse(headers={"Content-Type": "image/png"},
                                                                 body=b"not a png")
        r = preview(client, "https://shop.example.com/b")
        assert r.status_code == 200 and r.json["title"] and r.json["image"] is None

    def test_image_formats_beyond_the_common_ones_are_refused(self, client, net):
        buf = io.BytesIO()
        Image.new("RGB", (10, 10)).save(buf, "BMP")
        net.pages["shop.example.com/p/1"] = page(PRODUCT)
        net.pages["shop.example.com/img/orchid.png"] = FakeResponse(headers={"Content-Type": "image/bmp"},
                                                                    body=buf.getvalue())
        assert preview(client, "https://shop.example.com/p/1").json["image"] is None

    def test_page_charset_is_honoured(self, client, net):
        net.pages["shop.example.com/"] = FakeResponse(headers={"Content-Type": "text/html; charset=iso-8859-1"},
                                                      body="<title>Café</title>".encode("latin-1"))
        assert preview(client, "https://shop.example.com/").json["title"] == "Café"
        net.pages["shop.example.com/x"] = FakeResponse(headers={"Content-Type": "text/html; charset=bogus"},
                                                       body=b"<title>Ok</title>")
        assert preview(client, "https://shop.example.com/x").json["title"] == "Ok"

    def test_shoppers_cannot_use_it(self, client, net):
        net.pages["shop.example.com/"] = page("<title>Hi</title>")
        assert preview(client, "https://shop.example.com/", "grandma@example.com").status_code == 403
        assert net.opened == []

    def test_needs_csrf_header_and_json(self, client, net):
        r = client.post("/api/preview", ADMIN, json={"url": "https://shop.example.com/"}, csrf=False)
        assert r.status_code == 400 and net.opened == []

    @pytest.mark.parametrize("body", [{}, {"url": ""}, {"url": 5}, {"url": "ftp://shop.example.com/"},
                                      {"url": "javascript:alert(1)"}, {"url": "file:///etc/passwd"}])
    def test_bad_input(self, client, net, body):
        assert client.post("/api/preview", ADMIN, json=body).status_code == 400
        assert net.opened == []

    def test_failure_is_a_plain_error(self, client, net):
        r = preview(client, "https://nowhere.example.com/")
        assert r.status_code == 502 and r.json == {"error": "Couldn't read that page."}

    def test_lan_editor_can_use_it(self, client, net):
        net.pages["shop.example.com/"] = page("<title>Hi</title>")
        r = client.lan("post", "/api/preview", json={"url": "https://shop.example.com/"},
                       headers={"X-Wishlist": "1"})
        assert r.status_code == 200 and r.json["title"] == "Hi"


# ------------------------------------------------------------------ fetch guards
def fetch(url, accept="text/html", limit=wishlist.PREVIEW_PAGE_MAX, **kw):
    return wishlist._fetch(url, accept, limit, **kw)


class TestFetchGuards:
    @pytest.mark.parametrize("addr", [
        "127.0.0.1", "10.0.0.5", "172.16.1.1", "192.168.1.20", "169.254.169.254", "0.0.0.0",
        "100.64.0.1", "224.0.0.251", "255.255.255.255", "192.0.2.1",
        "::1", "fe80::1", "fd00::1", "::ffff:127.0.0.1", "::ffff:192.168.1.1", "2002:c0a8:101::1",
        "2001:0:4136:e378:8000:63bf:3fff:fdd2", "::", "not an ip"])
    def test_private_addresses_are_not_public(self, addr):
        assert not wishlist._public_ip(addr)

    @pytest.mark.parametrize("addr", [SHOP, "2606:4700::1111", "1.1.1.1"])
    def test_public_addresses(self, addr):
        assert wishlist._public_ip(addr)

    @pytest.mark.parametrize("addr", ["127.0.0.1", "192.168.1.20", "169.254.169.254", "::1", "fd00::1"])
    def test_host_on_a_private_address_is_refused(self, net, addr):
        net.dns["router.example.com"] = [addr]
        net.pages["router.example.com/"] = page("<title>Router admin</title>")
        with pytest.raises(wishlist.PreviewError, match="private"):
            fetch("http://router.example.com/")
        assert net.opened == []

    def test_ip_literal_on_the_home_network_is_refused(self, net):
        net.dns["192.168.1.1"] = ["192.168.1.1"]
        with pytest.raises(wishlist.PreviewError, match="private"):
            fetch("http://192.168.1.1/")
        assert net.opened == []

    def test_any_private_answer_spoils_the_host(self, net):
        net.dns["mixed.example.com"] = [SHOP, "10.0.0.1"]
        with pytest.raises(wishlist.PreviewError, match="private"):
            fetch("https://mixed.example.com/")
        assert net.opened == []

    def test_unresolvable_host(self, net):
        with pytest.raises(wishlist.PreviewError, match="resolve"):
            fetch("https://nowhere.example.com/")

    @pytest.mark.parametrize("url", ["ftp://shop.example.com/", "file:///etc/passwd", "gopher://shop.example.com/",
                                     "https:///nohost", "http://shop.example.com:99999/"])
    def test_only_http_and_https(self, net, url):
        with pytest.raises(wishlist.PreviewError):
            fetch(url)
        assert net.opened == []

    def test_redirect_to_a_private_address_is_refused(self, net):
        net.dns["evil.example.com"] = ["127.0.0.1"]
        net.pages["shop.example.com/go"] = redirect("http://evil.example.com/admin")
        with pytest.raises(wishlist.PreviewError, match="private"):
            fetch("https://shop.example.com/go")
        assert [ip for _, ip in net.opened] == [SHOP]

    def test_redirect_to_another_scheme_is_refused(self, net):
        net.pages["shop.example.com/go"] = redirect("file:///etc/passwd")
        with pytest.raises(wishlist.PreviewError, match="http"):
            fetch("https://shop.example.com/go")

    def test_redirects_are_followed_and_rechecked(self, net):
        net.dns["www.example.com"] = ["2606:4700::1111"]
        net.pages["shop.example.com/a"] = redirect("/b", 301)
        net.pages["shop.example.com/b"] = redirect("https://www.example.com/c", 308)
        net.pages["www.example.com/c"] = page("<title>Here</title>")
        url, _ctype, body = fetch("https://shop.example.com/a")
        assert url == "https://www.example.com/c" and b"Here" in body
        assert [ip for _, ip in net.opened] == [SHOP, SHOP, "2606:4700::1111"]

    def test_at_most_three_redirects(self, net):
        for n in range(5):
            net.pages[f"shop.example.com/{n}"] = redirect(f"/{n + 1}")
        net.pages["shop.example.com/3"] = page("<title>three hops</title>")
        assert b"three hops" in fetch("https://shop.example.com/0")[2]
        net.pages["shop.example.com/3"] = redirect("/4")
        net.pages["shop.example.com/4"] = page("<title>four hops</title>")
        with pytest.raises(wishlist.PreviewError, match="redirects"):
            fetch("https://shop.example.com/0")
        assert len(net.opened) == 4 + 4

    def test_redirect_without_location(self, net):
        net.pages["shop.example.com/"] = FakeResponse(302)
        with pytest.raises(wishlist.PreviewError, match="Location"):
            fetch("https://shop.example.com/")

    @pytest.mark.parametrize("status", [404, 403, 500, 204])
    def test_error_status(self, net, status):
        net.pages["shop.example.com/"] = FakeResponse(status, {"Content-Type": "text/html"}, b"<title>no</title>")
        with pytest.raises(wishlist.PreviewError, match=str(status)):
            fetch("https://shop.example.com/")

    @pytest.mark.parametrize("ctype", ["application/json", "image/png", "text/plain", "", "application/xhtml+xml"])
    def test_wrong_content_type_for_a_page(self, net, ctype):
        net.pages["shop.example.com/"] = FakeResponse(headers={"Content-Type": ctype} if ctype else {},
                                                      body=b"<title>x</title>")
        with pytest.raises(wishlist.PreviewError, match="not text/html"):
            fetch("https://shop.example.com/")

    def test_wrong_content_type_for_an_image(self, net):
        net.pages["shop.example.com/i.png"] = page("<title>not an image</title>")
        with pytest.raises(wishlist.PreviewError, match="not image/"):
            fetch("https://shop.example.com/i.png", "image/", wishlist.PREVIEW_IMAGE_MAX)

    def test_encoded_body_is_refused(self, net):
        net.pages["shop.example.com/"] = FakeResponse(headers={"Content-Type": "text/html",
                                                               "Content-Encoding": "gzip"}, body=b"\x1f\x8b")
        with pytest.raises(wishlist.PreviewError, match="encoded"):
            fetch("https://shop.example.com/")

    def test_oversize_image_by_content_length(self, net):
        resp = FakeResponse(headers={"Content-Type": "image/png", "Content-Length": str(21 * 1024 * 1024)})
        net.pages["shop.example.com/big.png"] = resp
        with pytest.raises(wishlist.PreviewError, match="bytes"):
            fetch("https://shop.example.com/big.png", "image/", wishlist.PREVIEW_IMAGE_MAX)
        assert resp.body.tell() == 0 and resp.closed  # refused before reading anything

    def test_oversize_image_without_content_length(self, net):
        resp = FakeResponse(headers={"Content-Type": "image/png"}, body=b"x" * 2001)
        net.pages["shop.example.com/big.png"] = resp
        with pytest.raises(wishlist.PreviewError, match="more than 2000"):
            fetch("https://shop.example.com/big.png", "image/", 2000)
        assert resp.body.tell() <= 2001  # stopped at the cap
        net.pages["shop.example.com/ok.png"] = FakeResponse(headers={"Content-Type": "image/png"}, body=b"x" * 2000)
        assert len(fetch("https://shop.example.com/ok.png", "image/", 2000)[2]) == 2000

    def test_oversize_page_is_cut_at_the_cap(self, net):
        big = "<title>Top</title>" + "x" * (wishlist.PREVIEW_PAGE_MAX + 5000) + "<meta property='og:title' content='Late'>"
        resp = page(big)
        resp.headers["content-length"] = str(len(big))
        net.pages["shop.example.com/"] = resp
        _url, _ctype, body = fetch("https://shop.example.com/", truncate=True)
        assert len(body) == wishlist.PREVIEW_PAGE_MAX and resp.body.tell() <= wishlist.PREVIEW_PAGE_MAX + 1

    def test_oversize_page_through_the_endpoint(self, client, net):
        big = "<title>Top</title>" + "x" * wishlist.PREVIEW_PAGE_MAX + "<meta property='og:title' content='Late'>"
        net.pages["shop.example.com/"] = page(big)
        assert preview(client, "https://shop.example.com/").json["title"] == "Top"

    def test_oversize_image_through_the_endpoint(self, client, net, monkeypatch):
        monkeypatch.setattr(wishlist, "PREVIEW_IMAGE_MAX", 10)
        net.pages["shop.example.com/p/1"] = page(PRODUCT)
        net.pages["shop.example.com/img/orchid.png"] = FakeResponse(headers={"Content-Type": "image/png"},
                                                                    body=png_bytes())
        r = preview(client, "https://shop.example.com/p/1")
        assert r.status_code == 200 and r.json["title"] and r.json["image"] is None

    def test_connect_timeout(self, net):
        net.pages["shop.example.com/"] = TimeoutError("timed out")
        with pytest.raises(wishlist.PreviewError, match="timed out"):
            fetch("https://shop.example.com/")

    def test_read_timeout(self, net):
        net.pages["shop.example.com/"] = page("<title>x</title>", error=TimeoutError("timed out"))
        with pytest.raises(wishlist.PreviewError, match="timed out"):
            fetch("https://shop.example.com/")

    def test_slow_body_hits_the_overall_deadline(self, net, monkeypatch):
        clock = iter(range(0, 1000, 2))  # every look at the clock is two seconds later
        monkeypatch.setattr(wishlist.time, "monotonic", lambda: next(clock))

        class Drip(FakeResponse):
            def read1(self, n):
                return b"x"

        net.pages["shop.example.com/"] = Drip(headers={"Content-Type": "text/html"})
        with pytest.raises(wishlist.PreviewError, match="timed out"):
            fetch("https://shop.example.com/")

    def test_redirect_chain_shares_one_deadline(self, net, monkeypatch):
        clock = iter(range(0, 1000, 2))
        monkeypatch.setattr(wishlist.time, "monotonic", lambda: next(clock))
        for n in range(3):
            net.pages[f"shop.example.com/{n}"] = redirect(f"/{n + 1}")
        net.pages["shop.example.com/3"] = page("<title>late</title>")
        with pytest.raises(wishlist.PreviewError, match="timed out"):
            fetch("https://shop.example.com/0")

    def test_timeout_given_to_the_connection_is_what_is_left(self, net):
        net.pages["shop.example.com/"] = page("<title>x</title>")
        fetch("https://shop.example.com/")
        assert 0 < net.timeouts[0] <= wishlist.PREVIEW_TIMEOUT

    def test_responses_are_closed(self, net):
        first, last = redirect("/b"), page("<title>x</title>")
        net.pages["shop.example.com/a"], net.pages["shop.example.com/b"] = first, last
        fetch("https://shop.example.com/a")
        assert first.closed and last.closed


# ------------------------------------------------------------------ the real connection
class TestPinnedConnection:
    """_open really connects to the checked address while naming the host the link gave."""

    def test_connects_to_the_given_ip_with_the_original_host(self):
        seen = {}

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                seen.update(host=self.headers["Host"], path=self.path, ua=self.headers["User-Agent"])
                body = b"<title>pinned</title>"
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            port = server.server_address[1]
            # The name doesn't resolve anywhere: only the pinned address can have answered.
            resp = wishlist._open(f"http://shop.invalid:{port}/a b/é?q=1 2", "127.0.0.1", 5)
            assert resp.status == 200 and resp.read1(100) == b"<title>pinned</title>"
            resp.close()
        finally:
            server.shutdown()
            server.server_close()
        assert seen["host"] == f"shop.invalid:{port}"
        assert seen["path"] == "/a%20b/%C3%A9?q=1%202"
        assert "FamilyWishList" in seen["ua"]

    def test_https_checks_the_certificate_for_the_name(self, monkeypatch):
        captured = {}

        class Ctx:
            def wrap_socket(self, sock, server_hostname):
                captured["name"] = server_hostname
                sock.close()
                raise OSError("stop here")

        monkeypatch.setattr(wishlist.ssl, "create_default_context", lambda: Ctx())
        monkeypatch.setattr(wishlist.socket, "create_connection",
                            lambda addr, timeout: captured.setdefault("addr", addr) and socket.socket())
        with pytest.raises(OSError, match="stop here"):
            wishlist._open("https://shop.example.com:443/x", SHOP, 5)
        assert captured == {"addr": (SHOP, 443), "name": "shop.example.com"}

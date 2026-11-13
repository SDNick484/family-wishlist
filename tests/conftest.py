"""Test setup: the app reads its config from the environment at import time,
so set that up before importing it, and give every test a fresh database."""
import os
import shutil
import sys
import tempfile
import time
import types

import pytest

DATA = tempfile.mkdtemp(prefix="wishlist-test-")
os.environ.update(
    DATA_DIR=DATA,
    ADMIN_EMAILS="admin@example.com",
    CF_TEAM_DOMAIN="family.cloudflareaccess.com",
    CF_AUD="aud-test",
    LAN_ROLE="editor",
    SEED_PEOPLE="Alex,Sam,Jordan",
    PERSON_COLORS="Alex=#1f4d3a,Sam|Sammy=#2563eb",
)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import jwt  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402

import app as wishlist  # noqa: E402

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)

# Stand in for Cloudflare's JWKS endpoint: always hand back our test public key.
wishlist._jwk_client = types.SimpleNamespace(
    get_signing_key_from_jwt=lambda _t: types.SimpleNamespace(key=KEY.public_key()))

PUBLIC_IP = "203.0.113.5"
LAN_IP = "192.0.2.50"  # RFC 5737 documentation range (still "private" to ipaddress)


def token(email, key=KEY, aud="aud-test", iss="https://family.cloudflareaccess.com", exp=3600):
    return jwt.encode({"email": email, "aud": aud, "iss": iss, "exp": int(time.time()) + exp},
                      key, algorithm="RS256")


class Client:
    """Small wrapper: requests arrive 'through Cloudflare' as a given signed-in email."""

    def __init__(self, flask_client):
        self.c = flask_client

    def headers(self, email=None, csrf=True, **tok):
        h = {"Cf-Ray": "test"}
        if csrf:
            h["X-Wishlist"] = "1"
        if email:
            h["Cf-Access-Jwt-Assertion"] = token(email, **tok)
        return h

    def req(self, method, path, email=None, json=None, headers=None, ip=PUBLIC_IP, **kw):
        h = headers if headers is not None else self.headers(email, **kw)
        return getattr(self.c, method)(path, json=json, headers=h, environ_base={"REMOTE_ADDR": ip})

    def get(self, path, email=None, **kw):
        return self.req("get", path, email, **kw)

    def post(self, path, email=None, json=None, **kw):
        return self.req("post", path, email, json=json if json is not None else {}, **kw)

    def patch(self, path, email=None, json=None, **kw):
        return self.req("patch", path, email, json=json if json is not None else {}, **kw)

    def put(self, path, email=None, json=None, **kw):
        return self.req("put", path, email, json=json if json is not None else {}, **kw)

    def delete(self, path, email=None, **kw):
        return self.req("delete", path, email, json={}, **kw)

    def lan(self, method, path, **kw):
        return getattr(self.c, method)(path, environ_base={"REMOTE_ADDR": LAN_IP}, **kw)

    def upload(self, item_id, fileobj, email="admin@example.com", filename="photo.png", csrf=True):
        return self.c.post(f"/api/items/{item_id}/photo", data={"photo": (fileobj, filename)},
                           headers=self.headers(email, csrf=csrf), content_type="multipart/form-data",
                           environ_base={"REMOTE_ADDR": PUBLIC_IP})


@pytest.fixture
def client():
    # fresh database + photo folder for every test
    for name in os.listdir(DATA):
        path = os.path.join(DATA, name)
        shutil.rmtree(path) if os.path.isdir(path) else os.remove(path)
    wishlist.init_db()
    return Client(wishlist.app.test_client())


@pytest.fixture
def people(client):
    """{name: id} for the seeded tabs."""
    return {p["name"]: p["id"] for p in client.get("/api/people", "admin@example.com").json}


ADMIN = "admin@example.com"


FEED_TOKEN = "feed-test-token-6f1d0c2a9b"


class Feed:
    """The companion service: straight from the LAN, `Authorization: Bearer <token>`, no Cloudflare
    header, no X-Wishlist and no Origin."""

    def __init__(self, flask_client):
        self.c = flask_client

    def headers(self, token=FEED_TOKEN, extra=None):
        h = {"Authorization": f"Bearer {token}"} if token is not None else {}
        return {**h, **(extra or {})}

    def get(self, path="/api/feed", token=FEED_TOKEN, ip=LAN_IP, headers=None):
        return self.c.get(path, headers=self.headers(token, headers), environ_base={"REMOTE_ADDR": ip})

    def post(self, path, json, token=FEED_TOKEN, ip=LAN_IP, headers=None):
        return self.c.post(path, json=json, headers=self.headers(token, headers), environ_base={"REMOTE_ADDR": ip})

    def reserve(self, iid, by, **kw):
        return self.post(f"/api/feed/items/{iid}/reserve", {"by": by}, **kw)

    def purchase(self, iid, by="Aunt Riley", purchased=True, **kw):
        return self.post(f"/api/feed/items/{iid}/purchase", {"by": by, "purchased": purchased}, **kw)

    def items(self, archived=False):
        r = self.get("/api/feed?archived=1" if archived else "/api/feed")
        assert r.status_code == 200, r.data
        return {i["id"]: i for p in r.json["people"] for i in p["items"]}


@pytest.fixture
def feed(client, monkeypatch):
    """The feed switched on for the Alex and Sam tabs (Jordan's is not in the feed)."""
    monkeypatch.setattr(wishlist, "FEED_TOKEN", FEED_TOKEN)
    monkeypatch.setattr(wishlist, "FEED_PEOPLE", ("Alex", "Sam"))
    return Feed(client.c)

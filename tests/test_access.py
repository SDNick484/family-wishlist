"""Identity, roles, CSRF and response-shape edge cases not covered in test_app.py."""
import time

import jwt
import pytest

import app as wishlist
from conftest import ADMIN, KEY, LAN_IP, PUBLIC_IP, token

GRANDMA = "grandma@example.com"


def raw_token(claims, key=KEY, algorithm="RS256"):
    base = {"aud": "aud-test", "iss": "https://family.cloudflareaccess.com", "exp": int(time.time()) + 3600}
    return jwt.encode({**base, **claims}, key, algorithm=algorithm)


def with_token(client, tok, path="/api/me"):
    return client.c.get(path, headers={"Cf-Ray": "x", "Cf-Access-Jwt-Assertion": tok},
                        environ_base={"REMOTE_ADDR": PUBLIC_IP})


# ------------------------------------------------------------------ tokens
class TestTokens:
    def test_token_without_email_claim_is_rejected(self, client):
        assert with_token(client, raw_token({"sub": "service-token"})).status_code == 403

    def test_email_claim_is_normalized(self, client):
        me = with_token(client, raw_token({"email": "  Admin@Example.COM "})).json
        assert me["email"] == ADMIN and me["role"] == "editor"

    def test_unsigned_token_is_rejected(self, client):
        tok = jwt.encode({"email": ADMIN, "aud": "aud-test", "iss": "https://family.cloudflareaccess.com",
                          "exp": int(time.time()) + 3600}, None, algorithm="none")
        assert with_token(client, tok).status_code == 403

    def test_hmac_signed_token_is_rejected(self, client):
        tok = raw_token({"email": ADMIN}, key="not-the-cloudflare-key-" * 3, algorithm="HS256")
        assert with_token(client, tok).status_code == 403

    def test_garbage_token_is_rejected(self, client):
        assert with_token(client, "not.a.jwt").status_code == 403

    def test_token_without_expiry_is_still_bound_to_audience(self, client):
        tok = jwt.encode({"email": ADMIN, "aud": "someone-elses-app",
                          "iss": "https://family.cloudflareaccess.com"}, KEY, algorithm="RS256")
        assert with_token(client, tok).status_code == 403

    def test_invalid_cookie_on_lan_does_not_fall_back_to_lan_role(self, client):
        # Presenting a token opts in to Cloudflare identity; a bad one must not become "Home".
        client.c.set_cookie("CF_Authorization", "bogus")
        assert client.c.get("/api/me", environ_base={"REMOTE_ADDR": LAN_IP}).status_code == 403

    @pytest.mark.parametrize("missing", ["CF_AUD", "CF_TEAM_DOMAIN"])
    def test_unconfigured_cloudflare_fails_closed(self, client, monkeypatch, missing):
        monkeypatch.setattr(wishlist, missing, "")
        r = client.get("/api/me", ADMIN)
        assert r.status_code == 403 and "not configured" in r.json["error"]


# ------------------------------------------------------------------ LAN
class TestLan:
    def test_loopback_counts_as_lan(self, client):
        r = client.c.get("/api/me", environ_base={"REMOTE_ADDR": "127.0.0.1"})
        assert r.json == {"email": None, "name": "Home", "role": "editor", "source": "lan"}

    def test_lan_role_shopper_cannot_edit(self, client, people, monkeypatch):
        monkeypatch.setattr(wishlist, "LAN_ROLE", "shopper")
        assert client.lan("get", "/api/me").json["role"] == "shopper"
        r = client.lan("post", f"/api/people/{people['Alex']}/items",
                       json={"name": "x", "kind": "online"}, headers={"X-Wishlist": "1"})
        assert r.status_code == 403

    @pytest.mark.parametrize("role", ["none", "admin", ""])
    def test_lan_role_none_or_unknown_denies_lan(self, client, monkeypatch, role):
        monkeypatch.setattr(wishlist, "LAN_ROLE", role)
        assert client.lan("get", "/api/me").status_code == 403
        assert client.lan("get", "/").status_code == 403
        # ...but Cloudflare sign-ins keep working
        assert client.get("/api/me", ADMIN).status_code == 200

    def test_lan_name_is_used_for_created_by(self, client, people, monkeypatch):
        monkeypatch.setattr(wishlist, "LAN_NAME", "Kitchen tablet")
        r = client.lan("post", f"/api/people/{people['Alex']}/items",
                       json={"name": "x", "kind": "online"}, headers={"X-Wishlist": "1"})
        assert r.status_code == 201 and r.json["created_by"] == "Kitchen tablet"

    def test_lan_mutations_still_need_csrf_header(self, client, people):
        r = client.lan("post", f"/api/people/{people['Alex']}/items", json={"name": "x", "kind": "online"})
        assert r.status_code == 400

    @pytest.mark.parametrize("addr", ["8.8.8.8", "100.64.0.1", "::ffff:8.8.8.8", "", "not-an-ip"])
    def test_non_private_addresses_are_not_lan(self, addr):
        assert wishlist._is_private(addr) is False

    @pytest.mark.parametrize("addr", ["10.0.0.5", "172.16.3.4", "192.168.1.20", "127.0.0.1", "::1"])
    def test_private_addresses_are_lan(self, addr):
        assert wishlist._is_private(addr) is True


# ------------------------------------------------------------------ roles
class TestRoles:
    def member(self, client, email, role, name="Grandma"):
        return client.put("/api/members", ADMIN, json={"email": email, "display_name": name, "role": role})

    def test_member_marked_editor_can_edit_until_demoted(self, client, people):
        url = f"/api/people/{people['Alex']}/items"
        body = {"name": "x", "kind": "online"}
        assert client.post(url, GRANDMA, json=body).status_code == 403
        self.member(client, GRANDMA, "editor")
        assert client.get("/api/me", GRANDMA).json["role"] == "editor"
        assert client.post(url, GRANDMA, json=body).status_code == 201
        self.member(client, GRANDMA, "shopper")
        assert client.post(url, GRANDMA, json=body).status_code == 403

    def test_removed_member_falls_back_to_shopper(self, client):
        self.member(client, GRANDMA, "editor", name="Gran")
        client.delete("/api/members/Grandma@Example.com", ADMIN)  # lookup is case-insensitive
        me = client.get("/api/me", GRANDMA).json
        assert me["role"] == "shopper" and me["name"] == "Grandma"

    def test_admin_cannot_be_demoted_through_members(self, client):
        self.member(client, ADMIN, "shopper", name="Boss")
        me = client.get("/api/me", ADMIN).json
        assert me["role"] == "editor" and me["name"] == "Boss"

    def test_member_upsert_updates_and_normalizes(self, client):
        self.member(client, "Gran@Example.com", "shopper", name="  Gran  ")
        self.member(client, "gran@example.com", "editor", name="Granny")
        data = client.get("/api/members", ADMIN).json
        assert data["members"] == [{"email": "gran@example.com", "display_name": "Granny", "role": "editor"}]
        assert data["admins"] == [ADMIN]

    @pytest.mark.parametrize("body", [
        {"email": "g@example.com", "display_name": "G", "role": "owner"},
        {"email": "g@example.com", "display_name": " "},
        {"display_name": "G"},
    ])
    def test_member_validation(self, client, body):
        assert client.put("/api/members", ADMIN, json=body).status_code == 400

    def test_shopper_cannot_manage_members_or_people(self, client, people):
        assert client.put("/api/members", GRANDMA, json={"email": GRANDMA, "display_name": "G",
                                                         "role": "editor"}).status_code == 403
        assert client.delete(f"/api/members/{ADMIN}", GRANDMA).status_code == 403
        assert client.post("/api/people", GRANDMA, json={"name": "Me"}).status_code == 403
        assert client.delete(f"/api/people/{people['Alex']}", GRANDMA).status_code == 403
        assert client.get("/api/me", GRANDMA).json["role"] == "shopper"

    def test_only_editors_see_owner_emails(self, client, people):
        client.patch(f"/api/people/{people['Alex']}", ADMIN, json={"owner_email": "alex@example.com"})
        as_editor = {p["name"]: p for p in client.get("/api/people", ADMIN).json}
        as_shopper = {p["name"]: p for p in client.get("/api/people", GRANDMA).json}
        assert as_editor["Alex"]["owner_email"] == "alex@example.com"
        assert all("owner_email" not in p for p in as_shopper.values())


# ------------------------------------------------------------------ CSRF
class TestCsrfMore:
    def test_form_post_with_header_is_blocked(self, client, people):
        r = client.c.post(f"/api/people/{people['Alex']}/items", data={"name": "x", "kind": "online"},
                          headers=client.headers(ADMIN), environ_base={"REMOTE_ADDR": PUBLIC_IP})
        assert r.status_code == 400

    def test_multipart_only_allowed_on_photo_upload(self, client, people):
        r = client.c.post(f"/api/people/{people['Alex']}/items", data={"name": "x", "kind": "online"},
                          headers=client.headers(ADMIN), content_type="multipart/form-data",
                          environ_base={"REMOTE_ADDR": PUBLIC_IP})
        assert r.status_code == 400

    def test_same_origin_header_is_allowed(self, client, people):
        h = client.headers(ADMIN)
        h["Origin"] = "http://localhost"  # the test client's Host
        r = client.req("post", f"/api/people/{people['Alex']}/items",
                       json={"name": "x", "kind": "online"}, headers=h)
        assert r.status_code == 201

    @pytest.mark.parametrize("method,path", [("delete", "/api/items/1"), ("patch", "/api/items/1"),
                                             ("put", "/api/members"), ("delete", "/api/people/1")])
    def test_every_mutating_verb_needs_the_header(self, client, method, path):
        assert client.req(method, path, ADMIN, json={}, csrf=False).status_code == 400

    def test_reads_do_not_need_the_header(self, client):
        assert client.get("/api/people", ADMIN, csrf=False).status_code == 200


# ------------------------------------------------------------------ responses
class TestResponses:
    def test_api_responses_are_never_cached(self, client):
        assert client.get("/api/people", ADMIN).headers["Cache-Control"] == "no-store"

    def test_api_errors_are_json_and_page_errors_are_html(self, client):
        r = client.get("/api/people/999/items", ADMIN)
        assert r.status_code == 404 and r.json == {"error": "No such list."}
        r = client.get("/nope", ADMIN)
        assert r.status_code == 404 and r.mimetype == "text/html"

    def test_static_files_need_sign_in(self, client):
        for path in ("/", "/static/app.js", "/static/manifest.json"):
            assert client.c.get(path, environ_base={"REMOTE_ADDR": "8.8.8.8"}).status_code == 403
            assert client.get(path, "anyone@example.com").status_code == 200

    @pytest.mark.parametrize("path", ["/static/../app.py", "/static/%2e%2e/app.py", "/static/..%2fapp.py",
                                      "/photos/../wishlist.db"])
    def test_no_path_traversal(self, client, path):
        assert client.get(path, ADMIN).status_code == 404

    def test_hardening_headers_on_every_response(self, client):
        for r in (client.get("/api/me", ADMIN), client.get("/api/me"), client.c.get("/healthz")):
            assert r.headers["X-Content-Type-Options"] == "nosniff"
            assert r.headers["Referrer-Policy"] == "no-referrer"
            csp = r.headers["Content-Security-Policy"]
            assert "script-src 'self'" in csp and "unsafe-eval" not in csp

    def test_token_helper_round_trips(self, client):
        # guards the test harness itself: a valid token for another email is that email
        assert client.get("/api/me", "a@example.com").json["email"] == "a@example.com"
        assert token("a@example.com") != token("b@example.com")

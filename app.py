"""Family Wish List — Flask + SQLite.

Identity comes from Cloudflare Access (verified JWT). Requests that come
straight from the LAN (no Cloudflare headers) get LAN_ROLE.

Environment:
  DATA_DIR        where wishlist.db lives (default ./data)
  ADMIN_EMAILS    comma list; always editors
  CF_TEAM_DOMAIN  e.g. myteam.cloudflareaccess.com
  CF_AUD          Application Audience (AUD) tag of the Access app
  LAN_ROLE        editor | shopper | none   (default editor)
  LAN_NAME        display name for LAN users (default "Home")

Photos are stored re-encoded as JPEG under DATA_DIR/photos (needs Pillow).
"""
import ipaddress
import math
import os
import warnings
import re
import sqlite3
import time
from collections import Counter
from functools import wraps
from urllib.parse import urlparse

from flask import Flask, abort, g, jsonify, request, send_from_directory

try:
    import jwt
    from jwt import PyJWKClient
except ImportError:  # verification simply unavailable
    jwt = None
    PyJWKClient = None

try:
    from PIL import Image, ImageOps
    Image.MAX_IMAGE_PIXELS = 40_000_000  # refuse absurd dimensions (decompression bombs)
    warnings.simplefilter("error", Image.DecompressionBombWarning)
except ImportError:  # photos simply unavailable
    Image = ImageOps = None

BASE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("DATA_DIR", os.path.join(BASE, "data"))
DB_PATH = os.path.join(DATA_DIR, "wishlist.db")
PHOTO_DIR = os.path.join(DATA_DIR, "photos")
PHOTO_MAX = 1600   # longest side of the stored photo, px
THUMB_MAX = 400    # longest side of the list thumbnail, px
ADMIN_EMAILS = {e.strip().lower() for e in os.environ.get("ADMIN_EMAILS", "").split(",") if e.strip()}
CF_TEAM_DOMAIN = os.environ.get("CF_TEAM_DOMAIN", "").strip().rstrip("/").removeprefix("https://")
CF_AUD = os.environ.get("CF_AUD", "").strip()
LAN_ROLE = os.environ.get("LAN_ROLE", "editor").strip().lower()
LAN_NAME = os.environ.get("LAN_NAME", "Home").strip() or "Home"

CF_HEADERS = ("Cf-Ray", "Cf-Connecting-Ip", "Cf-Access-Jwt-Assertion",
              "Cf-Access-Authenticated-User-Email")

app = Flask(__name__, static_folder=None)
app.config["JSON_SORT_KEYS"] = False
app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024  # largest upload accepted

_jwk_client = None


def jwk_client():
    global _jwk_client
    if _jwk_client is None and PyJWKClient and CF_TEAM_DOMAIN:
        _jwk_client = PyJWKClient(f"https://{CF_TEAM_DOMAIN}/cdn-cgi/access/certs",
                                  cache_keys=True, lifespan=3600)
    return _jwk_client


# ---------------------------------------------------------------- database
SCHEMA = """
CREATE TABLE IF NOT EXISTS people (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  owner_email TEXT,
  position INTEGER NOT NULL DEFAULT 0,
  color TEXT
);
CREATE TABLE IF NOT EXISTS members (
  email TEXT PRIMARY KEY,
  display_name TEXT NOT NULL,
  role TEXT NOT NULL DEFAULT 'shopper'
);
CREATE TABLE IF NOT EXISTS items (
  id INTEGER PRIMARY KEY,
  person_id INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('online','store')),
  url TEXT,
  store TEXT,
  price REAL,
  priority INTEGER NOT NULL DEFAULT 2,
  notes TEXT,
  position INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL,
  created_by TEXT,
  purchased INTEGER NOT NULL DEFAULT 0,
  purchased_by TEXT,
  purchased_by_email TEXT,
  purchased_at INTEGER,
  removed_by_owner INTEGER NOT NULL DEFAULT 0,
  photo_ver INTEGER,
  secret INTEGER NOT NULL DEFAULT 0,
  created_by_email TEXT,
  reserved_by TEXT,
  reserved_by_email TEXT,
  reserved_at INTEGER,
  occasion TEXT,
  archived_at INTEGER
);
CREATE INDEX IF NOT EXISTS items_person ON items(person_id);
"""


def db():
    if "db" not in g:
        os.makedirs(DATA_DIR, exist_ok=True)
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def init_db():
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    # Upgrade databases created by earlier releases.
    for table, col, decl in (("people", "color", "TEXT"), ("items", "photo_ver", "INTEGER"),
                             ("items", "secret", "INTEGER NOT NULL DEFAULT 0"),
                             ("items", "created_by_email", "TEXT"), ("items", "reserved_by", "TEXT"),
                             ("items", "reserved_by_email", "TEXT"), ("items", "reserved_at", "INTEGER"),
                             ("items", "occasion", "TEXT"), ("items", "archived_at", "INTEGER")):
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if cols and col not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
    conn.executescript(SCHEMA)
    conn.execute("PRAGMA journal_mode = WAL")
    seed = os.environ.get("SEED_PEOPLE", "")
    if seed and conn.execute("SELECT COUNT(*) FROM people").fetchone()[0] == 0:
        for i, name in enumerate(n.strip() for n in seed.split(",") if n.strip()):
            conn.execute("INSERT INTO people (name, position) VALUES (?, ?)", (name, i))
    # Default tab colors by name, e.g. "Alex=#1f4d3a,Sam|Sammy=#c62f3c".
    # Only fills tabs that have no color yet, so choices made in Settings win.
    for entry in os.environ.get("PERSON_COLORS", "").split(","):
        names, _, color = entry.partition("=")
        color = color.strip().lower()
        if not re.fullmatch(r"#[0-9a-f]{6}", color):
            continue
        for n in (x.strip().lower() for x in names.split("|") if x.strip()):
            conn.execute("UPDATE people SET color = ? WHERE color IS NULL AND lower(name) = ?", (color, n))
    conn.commit()
    conn.close()


# ---------------------------------------------------------------- identity
def _is_private(addr):
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    return ip.is_private or ip.is_loopback


def _verify_cf_token(token):
    client = jwk_client()
    if not (client and CF_AUD and jwt):
        return None
    try:
        key = client.get_signing_key_from_jwt(token).key
        claims = jwt.decode(token, key, algorithms=["RS256"], audience=CF_AUD,
                            issuer=f"https://{CF_TEAM_DOMAIN}")
    except Exception as exc:  # noqa: BLE001 — any failure means "not authenticated"
        app.logger.warning("Access JWT rejected: %s", exc)
        return None
    email = (claims.get("email") or "").strip().lower()
    return email or None


def resolve_user():
    """Return dict(email, name, role, source) or abort."""
    via_cloudflare = any(h in request.headers for h in CF_HEADERS)
    token = request.headers.get("Cf-Access-Jwt-Assertion") or request.cookies.get("CF_Authorization")

    if via_cloudflare or token:
        if not (CF_TEAM_DOMAIN and CF_AUD):
            abort(403, "Cloudflare Access is not configured on the server (CF_TEAM_DOMAIN / CF_AUD).")
        email = _verify_cf_token(token) if token else None
        if not email:
            abort(403, "Cloudflare Access sign-in required.")
        row = db().execute("SELECT display_name, role FROM members WHERE email = ?", (email,)).fetchone()
        role = "editor" if email in ADMIN_EMAILS else (row["role"] if row else "shopper")
        name = row["display_name"] if row else email.split("@")[0].replace(".", " ").title()
        return {"email": email, "name": name, "role": role, "source": "cloudflare"}

    if _is_private(request.remote_addr or "") and LAN_ROLE in ("editor", "shopper"):
        return {"email": None, "name": LAN_NAME, "role": LAN_ROLE, "source": "lan"}

    abort(403, "Access denied.")


@app.before_request
def load_user():
    if request.endpoint in ("healthz",):
        return
    g.user = resolve_user()
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        # CSRF: require our custom header (cross-site pages can't send it without a CORS
        # preflight we never allow) and a JSON body, or multipart for photo uploads.
        multipart = request.mimetype == "multipart/form-data" and request.endpoint == "upload_photo"
        if request.headers.get("X-Wishlist") != "1" or not (request.is_json or multipart):
            abort(400, "Bad request.")
        origin = request.headers.get("Origin")
        if origin and urlparse(origin).netloc != request.host:
            abort(403, "Cross-origin request blocked.")


def editor_required(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        if g.user["role"] != "editor":
            abort(403, "Only editors can do that.")
        return fn(*a, **kw)
    return wrapper


@app.after_request
def security_headers(resp):
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; "
        "script-src 'self'; connect-src 'self'; frame-ancestors 'none'")
    if request.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


@app.errorhandler(400)
@app.errorhandler(403)
@app.errorhandler(404)
@app.errorhandler(413)
@app.errorhandler(503)
def err(e):
    if e.code == 413:
        e.description = "That photo is too large (20 MB max)."
    if request.path.startswith("/api/"):
        return jsonify(error=e.description), e.code
    return (f"<!doctype html><meta name=viewport content='width=device-width'>"
            f"<p style='font-family:sans-serif;padding:2rem'>{e.description}</p>"), e.code


# ---------------------------------------------------------------- helpers
def is_owner(person_row):
    em = g.user["email"]
    return bool(em and person_row["owner_email"] and person_row["owner_email"].lower() == em)


def get_person(pid):
    row = db().execute("SELECT * FROM people WHERE id = ?", (pid,)).fetchone()
    if not row:
        abort(404, "No such list.")
    return row


def can_see(item, owner):
    """Whether the user may know this item exists; `owner` says whether its tab is theirs.

    Every route reaches items through this, so a hidden item answers exactly like a missing one.
    """
    if owner and item["removed_by_owner"]:
        return False
    if item["secret"]:
        # Secret ideas need an identity (LAN users could be the owner) and stay hidden from
        # the owner until the gift has been given and archived.
        return bool(g.user["email"]) and not (owner and not item["archived_at"])
    return True


def get_item(iid):
    row = db().execute("SELECT * FROM items WHERE id = ?", (iid,)).fetchone()
    if not row or not can_see(row, is_owner(get_person(row["person_id"]))):
        abort(404, "No such item.")
    return row


def tab_items(person, archived=False):
    """The items on a tab this user can see: the list itself, or its archived gifts."""
    q = "SELECT * FROM items WHERE person_id = ? AND archived_at IS " + ("NOT NULL" if archived else "NULL")
    owner = is_owner(person)
    return [r for r in db().execute(q + " ORDER BY position, id", (person["id"],)) if can_see(r, owner)]


def require_edit(item):
    """Editors can change any item; whoever added a secret idea can change that idea."""
    em = g.user["email"]
    if g.user["role"] != "editor" and not (em and item["secret"] and item["created_by_email"] == em):
        abort(403, "Only editors can do that.")


def item_json(row, owner_view):
    em = g.user["email"]
    d = {k: row[k] for k in ("id", "person_id", "name", "kind", "url", "store", "price", "priority",
                             "notes", "occasion", "position", "created_at", "created_by", "archived_at")}
    if owner_view and not row["archived_at"]:  # keep the surprise until the gift is archived
        d.update(purchased=False, purchased_by=None, purchased_at=None, mine=False,
                 removed_by_owner=False, secret=False, my_idea=False,
                 reserved_by=None, reserved_at=None, reserved_mine=False)
    else:
        d.update(purchased=bool(row["purchased"]), purchased_by=row["purchased_by"],
                 purchased_at=row["purchased_at"], removed_by_owner=bool(row["removed_by_owner"]),
                 mine=bool(em and row["purchased_by_email"] == em),
                 secret=bool(row["secret"]), my_idea=bool(em and row["secret"] and row["created_by_email"] == em),
                 reserved_by=row["reserved_by"], reserved_at=row["reserved_at"],
                 reserved_mine=bool(em and row["reserved_by_email"] == em))
    ver = row["photo_ver"]
    d["photo_url"] = f"/photos/{row['id']}/full?v={ver}" if ver else None
    d["thumb_url"] = f"/photos/{row['id']}/thumb?v={ver}" if ver else None
    return d


def photo_paths(iid):
    return (os.path.join(PHOTO_DIR, f"{iid}.jpg"), os.path.join(PHOTO_DIR, f"{iid}_t.jpg"))


def remove_photo_files(*item_ids):
    for iid in item_ids:
        for path in photo_paths(iid):
            try:
                os.remove(path)
            except FileNotFoundError:
                pass


def json_body():
    data = request.get_json()
    if data is None:
        return {}
    if not isinstance(data, dict):
        abort(400, "Expected a JSON object.")
    return data


def text(data, key):
    value = data.get(key)
    if value is None:
        return ""
    if not isinstance(value, str):
        abort(400, f"{key} must be text.")
    return value.strip()


def clean_color(data):
    value = text(data, "color")
    if not value:
        return None
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", value):
        abort(400, "Color must look like #1f4d3a.")
    return value.lower()


def clean_item_payload(data, partial=False):
    out = {}
    if "name" in data or not partial:
        name = text(data, "name")
        if not name:
            abort(400, "Item name is required.")
        out["name"] = name[:200]
    if "kind" in data or not partial:
        kind = data.get("kind")
        if kind not in ("online", "store"):
            abort(400, "Kind must be 'online' or 'store'.")
        out["kind"] = kind
    if "url" in data:
        url = text(data, "url")
        scheme = re.match(r"^([a-zA-Z][a-zA-Z0-9+.-]*):(?!\d)", url)
        if scheme and scheme.group(1).lower() not in ("http", "https"):
            abort(400, "Link must be an http(s) URL.")
        if url and not url.lower().startswith(("http://", "https://")):
            url = "https://" + url
        if url and urlparse(url).scheme not in ("http", "https"):
            abort(400, "Link must be an http(s) URL.")
        out["url"] = url[:2000] or None
    if "store" in data:
        out["store"] = text(data, "store")[:120] or None
    if "price" in data:
        p = data.get("price")
        if p in (None, ""):
            out["price"] = None
        else:
            try:
                out["price"] = round(float(str(p).replace("$", "").replace(",", "").strip()), 2)
            except (ValueError, OverflowError):
                abort(400, "Price must be a number.")
            if not math.isfinite(out["price"]):  # inf/nan aren't valid JSON and would break every client
                abort(400, "Price must be a number.")
            if out["price"] < 0:
                abort(400, "Price can't be negative.")
    if "priority" in data:
        try:
            pr = int(data.get("priority"))
        except (TypeError, ValueError, OverflowError):
            abort(400, "Priority must be 1, 2 or 3.")
        out["priority"] = min(3, max(1, pr))
    if "notes" in data:
        out["notes"] = text(data, "notes")[:1000] or None
    if "occasion" in data:
        out["occasion"] = text(data, "occasion")[:60] or None
    return out


# ---------------------------------------------------------------- routes
@app.get("/healthz")
def healthz():
    return "ok"


@app.get("/")
def index():
    return send_from_directory(os.path.join(BASE, "static"), "index.html")


@app.get("/static/<path:fn>")
def static_files(fn):
    return send_from_directory(os.path.join(BASE, "static"), fn)


@app.get("/api/me")
def me():
    return jsonify(g.user)


@app.get("/api/people")
def list_people():
    rows = db().execute("SELECT * FROM people ORDER BY position, id").fetchall()
    out = []
    for r in rows:
        owner = is_owner(r)
        # Counts leave out what the owner removed, for everyone: it's no longer on their list.
        listed = [i for i in tab_items(r) if not i["removed_by_owner"]]
        archived = [i for i in tab_items(r, archived=True) if not i["removed_by_owner"]]
        d = {"id": r["id"], "name": r["name"], "color": r["color"], "is_me": owner,
             "count": len(listed), "archived": len(archived)}
        if not owner:
            open_ = [i for i in listed if not i["purchased"]]
            d.update(bought=len(listed) - len(open_),
                     reserved=sum(1 for i in open_ if i["reserved_by"] is not None),
                     remaining=round(sum(i["price"] or 0 for i in open_), 2))
        if g.user["role"] == "editor":
            d["owner_email"] = r["owner_email"]
        out.append(d)
    return jsonify(out)


@app.get("/api/people/<int:pid>/items")
def list_items(pid):
    person = get_person(pid)
    owner = is_owner(person)
    return jsonify({"person": {"id": person["id"], "name": person["name"], "is_me": owner},
                    "items": [item_json(r, owner) for r in tab_items(person)]})


@app.get("/api/people/<int:pid>/history")
def list_history(pid):
    """Archived gifts, newest first. Archiving ends the surprise, so the owner sees the givers."""
    person = get_person(pid)
    owner = is_owner(person)
    rows = sorted(tab_items(person, archived=True), key=lambda r: (-r["archived_at"], r["id"]))
    return jsonify({"person": {"id": person["id"], "name": person["name"], "is_me": owner},
                    "items": [item_json(r, owner) for r in rows]})


@app.get("/api/occasions")
def list_occasions():
    """Occasion tags on items this user can see, most used first: suggestions for the item form."""
    rows = db().execute("SELECT items.*, people.owner_email FROM items JOIN people ON people.id = person_id "
                        "WHERE occasion IS NOT NULL")
    used = Counter(r["occasion"] for r in rows if can_see(r, is_owner(r)))
    return jsonify(sorted(used, key=lambda o: (-used[o], o.lower())))


def insert_item(pid, data, secret=False):
    data.setdefault("priority", 2)
    pos = db().execute("SELECT COALESCE(MAX(position), -1) + 1 FROM items WHERE person_id = ?",
                       (pid,)).fetchone()[0]
    cur = db().execute(
        "INSERT INTO items (person_id, name, kind, url, store, price, priority, notes, occasion, position, "
        "created_at, created_by, created_by_email, secret) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (pid, data["name"], data["kind"], data.get("url"), data.get("store"), data.get("price"),
         data["priority"], data.get("notes"), data.get("occasion"), pos, int(time.time()),
         g.user["name"], g.user["email"], int(secret)))
    db().commit()
    return jsonify(item_json(get_item(cur.lastrowid), is_owner(get_person(pid)))), 201


@app.post("/api/people/<int:pid>/items")
@editor_required
def add_item(pid):
    get_person(pid)
    return insert_item(pid, clean_item_payload(json_body()))


@app.post("/api/people/<int:pid>/ideas")
def add_idea(pid):
    """A secret gift idea: anyone signed in can add one to someone else's tab."""
    person = get_person(pid)
    if not g.user["email"]:
        abort(403, "Sign in to add secret ideas.")
    if is_owner(person):
        abort(403, "Secret ideas are for other people's lists.")
    return insert_item(pid, clean_item_payload(json_body()), secret=True)


@app.patch("/api/items/<int:iid>")
def edit_item(iid):
    item = get_item(iid)
    require_edit(item)
    data = clean_item_payload(json_body(), partial=True)
    if data:
        sets = ", ".join(f"{k} = ?" for k in data)
        db().execute(f"UPDATE items SET {sets} WHERE id = ?", (*data.values(), iid))
        db().commit()
    return jsonify(item_json(get_item(iid), is_owner(get_person(item["person_id"]))))


@app.delete("/api/items/<int:iid>")
def delete_item(iid):
    item = get_item(iid)
    require_edit(item)
    if is_owner(get_person(item["person_id"])) and (item["purchased"] or item["reserved_by"] is not None):
        # Already bought or reserved for them: hide from the owner instead of erasing the record.
        db().execute("UPDATE items SET removed_by_owner = 1 WHERE id = ?", (iid,))
    else:
        db().execute("DELETE FROM items WHERE id = ?", (iid,))
        remove_photo_files(iid)
    db().commit()
    return jsonify(ok=True)


@app.post("/api/items/<int:iid>/move")
@editor_required
def move_item(iid):
    item = get_item(iid)
    if item["archived_at"]:
        abort(400, "Archived items aren't on the list. Restore it first.")
    direction = json_body().get("dir")
    rows = db().execute("SELECT * FROM items WHERE person_id = ? AND archived_at IS NULL ORDER BY position, id",
                        (item["person_id"],)).fetchall()
    ids = [r["id"] for r in rows]
    # Swap with the neighbor the user can see: the owner's view leaves out removed items and secret ideas.
    owner = is_owner(get_person(item["person_id"]))
    seen = [n for n, r in enumerate(rows) if can_see(r, owner)]
    k = seen.index(ids.index(iid))
    k2 = k - 1 if direction == "up" else k + 1
    if 0 <= k2 < len(seen):
        i, j = seen[k], seen[k2]
        ids[i], ids[j] = ids[j], ids[i]
        for pos, x in enumerate(ids):
            db().execute("UPDATE items SET position = ? WHERE id = ?", (pos, x))
        db().commit()
    return jsonify(ok=True)


def shopper_item(iid):
    """An item someone may buy or reserve: not on their own tab, and not archived."""
    item = get_item(iid)
    if is_owner(get_person(item["person_id"])):
        abort(403, "That's your own list — no peeking!")
    if item["archived_at"]:
        abort(400, "That gift has been archived. Restore it to the list first.")
    return item


@app.post("/api/items/<int:iid>/purchase")
def purchase(iid):
    item = shopper_item(iid)
    data = json_body()
    em = g.user["email"]
    if data.get("purchased", True):
        who = text(data, "by")[:80] or g.user["name"]
        # Claim it in one statement so two shoppers tapping at once can't both succeed. Someone
        # else's reservation blocks it; buying ends the reservation.
        cur = db().execute("UPDATE items SET purchased = 1, purchased_by = ?, purchased_by_email = ?, "
                           "purchased_at = ?, reserved_by = NULL, reserved_by_email = NULL, reserved_at = NULL "
                           "WHERE id = ? AND purchased = 0 AND (reserved_by IS NULL OR reserved_by_email = ?)",
                           (who, em, int(time.time()), iid, em))
        if cur.rowcount == 0:
            now = get_item(iid)
            if now["purchased"]:
                abort(400, f"Already marked as purchased by {now['purchased_by']}.")
            abort(400, f"{now['reserved_by']} has reserved this. Only they can mark it purchased.")
    else:
        mine = em and item["purchased_by_email"] == em
        if not (mine or g.user["role"] == "editor"):
            abort(403, "Only the person who marked it (or an editor) can undo that.")
        if item["removed_by_owner"]:
            # Owner already removed it; once it's no longer bought there's nothing to keep.
            db().execute("DELETE FROM items WHERE id = ?", (iid,))
            db().commit()
            remove_photo_files(iid)
            return jsonify(deleted=True)
        db().execute("UPDATE items SET purchased = 0, purchased_by = NULL, purchased_by_email = NULL, "
                     "purchased_at = NULL WHERE id = ?", (iid,))
    db().commit()
    return jsonify(item_json(get_item(iid), False))


@app.post("/api/items/<int:iid>/reserve")
def reserve(iid):
    item = shopper_item(iid)
    data = json_body()
    em = g.user["email"]
    if data.get("reserved", True):
        who = text(data, "by")[:80] or g.user["name"]
        cur = db().execute("UPDATE items SET reserved_by = ?, reserved_by_email = ?, reserved_at = ? "
                           "WHERE id = ? AND purchased = 0 AND reserved_by IS NULL",
                           (who, em, int(time.time()), iid))
        if cur.rowcount == 0:
            now = get_item(iid)
            if now["purchased"]:
                abort(400, f"Already marked as purchased by {now['purchased_by']}.")
            abort(400, f"Already reserved by {now['reserved_by']}.")
    else:
        mine = em and item["reserved_by_email"] == em
        if not (mine or g.user["role"] == "editor"):
            abort(403, "Only the person who reserved it (or an editor) can release it.")
        if item["removed_by_owner"] and not item["purchased"]:
            # Owner already removed it; once nobody is getting it there's nothing to keep.
            db().execute("DELETE FROM items WHERE id = ?", (iid,))
            db().commit()
            remove_photo_files(iid)
            return jsonify(deleted=True)
        db().execute("UPDATE items SET reserved_by = NULL, reserved_by_email = NULL, reserved_at = NULL "
                     "WHERE id = ?", (iid,))
    db().commit()
    return jsonify(item_json(get_item(iid), False))


OWN_ARCHIVE = "Someone else has to archive the gifts on your own list."


@app.post("/api/items/<int:iid>/archive")
@editor_required
def archive_item(iid):
    item = get_item(iid)
    if is_owner(get_person(item["person_id"])):
        abort(403, OWN_ARCHIVE)
    if json_body().get("archived", True):
        if not item["purchased"]:
            abort(400, "Only purchased items can be archived.")
        db().execute("UPDATE items SET archived_at = ? WHERE id = ? AND archived_at IS NULL",
                     (int(time.time()), iid))
    else:
        db().execute("UPDATE items SET archived_at = NULL WHERE id = ?", (iid,))
    db().commit()
    return jsonify(item_json(get_item(iid), False))


@app.post("/api/people/<int:pid>/archive")
@editor_required
def archive_purchased(pid):
    """Archive everything purchased on a tab, e.g. once the presents have been opened."""
    if is_owner(get_person(pid)):
        abort(403, OWN_ARCHIVE)
    cur = db().execute("UPDATE items SET archived_at = ? WHERE person_id = ? AND purchased = 1 "
                       "AND archived_at IS NULL", (int(time.time()), pid))
    db().commit()
    return jsonify(archived=cur.rowcount)


# ---------------------------------------------------------------- photos
def _flatten(img):
    """Apply camera rotation, drop metadata, and composite transparency onto white."""
    img = ImageOps.exif_transpose(img)
    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        bg = Image.new("RGB", rgba.size, (255, 255, 255))
        bg.paste(rgba, mask=rgba.getchannel("A"))
        return bg
    return img.convert("RGB")


def _save_jpeg(img, path, max_side):
    out = img.copy()
    out.thumbnail((max_side, max_side), Image.LANCZOS)
    tmp = path + ".tmp"
    out.save(tmp, "JPEG", quality=84, optimize=True, progressive=True)
    os.replace(tmp, path)


@app.post("/api/items/<int:iid>/photo")
def upload_photo(iid):
    item = get_item(iid)
    require_edit(item)
    if Image is None:
        abort(503, "Photo support isn't installed on the server (py3-pillow).")
    f = request.files.get("photo")
    if not f:
        abort(400, "No photo received.")
    try:
        img = Image.open(f.stream)
        img.load()
        img = _flatten(img)
    except Exception:  # noqa: BLE001 — anything Pillow can't decode is "not an image"
        abort(400, "That file isn't a photo I can read. Try a JPEG or PNG.")
    os.makedirs(PHOTO_DIR, exist_ok=True)
    full, thumb = photo_paths(iid)
    _save_jpeg(img, full, PHOTO_MAX)
    _save_jpeg(img, thumb, THUMB_MAX)
    ver = (item["photo_ver"] or 0) + 1
    db().execute("UPDATE items SET photo_ver = ? WHERE id = ?", (ver, iid))
    db().commit()
    return jsonify(item_json(get_item(iid), is_owner(get_person(item["person_id"]))))


@app.delete("/api/items/<int:iid>/photo")
def delete_photo(iid):
    item = get_item(iid)
    require_edit(item)
    db().execute("UPDATE items SET photo_ver = NULL WHERE id = ?", (iid,))
    db().commit()
    remove_photo_files(iid)
    return jsonify(item_json(get_item(iid), is_owner(get_person(item["person_id"]))))


@app.get("/photos/<int:iid>/<size>")
def get_photo(iid, size):
    if size not in ("full", "thumb"):
        abort(404, "Not found.")
    item = get_item(iid)
    if not item["photo_ver"]:
        abort(404, "No photo.")
    full, thumb = photo_paths(iid)
    resp = send_from_directory(PHOTO_DIR, os.path.basename(full if size == "full" else thumb),
                               mimetype="image/jpeg")
    # URL carries ?v=<version>, so a changed photo gets a new URL; cache privately.
    resp.headers["Cache-Control"] = "private, max-age=31536000, immutable"
    return resp


# ---------------------------------------------------------------- settings (editors)
@app.post("/api/people")
@editor_required
def add_person():
    data = json_body()
    name = text(data, "name")[:60]
    if not name:
        abort(400, "Name is required.")
    owner = text(data, "owner_email").lower() or None
    color = clean_color(data)
    pos = db().execute("SELECT COALESCE(MAX(position), -1) + 1 FROM people").fetchone()[0]
    cur = db().execute("INSERT INTO people (name, owner_email, position, color) VALUES (?,?,?,?)",
                       (name, owner, pos, color))
    db().commit()
    return jsonify(id=cur.lastrowid), 201


@app.patch("/api/people/<int:pid>")
@editor_required
def edit_person(pid):
    get_person(pid)
    data = json_body()
    if "name" in data:
        name = text(data, "name")[:60]
        if not name:
            abort(400, "Name is required.")
        db().execute("UPDATE people SET name = ? WHERE id = ?", (name, pid))
    if "color" in data:
        db().execute("UPDATE people SET color = ? WHERE id = ?", (clean_color(data), pid))
    if "owner_email" in data:
        db().execute("UPDATE people SET owner_email = ? WHERE id = ?",
                     (text(data, "owner_email").lower() or None, pid))
    if data.get("move") in ("up", "down"):
        ids = [r["id"] for r in db().execute("SELECT id FROM people ORDER BY position, id")]
        i = ids.index(pid)
        j = i - 1 if data["move"] == "up" else i + 1
        if 0 <= j < len(ids):
            ids[i], ids[j] = ids[j], ids[i]
            for pos, x in enumerate(ids):
                db().execute("UPDATE people SET position = ? WHERE id = ?", (pos, x))
    db().commit()
    return jsonify(ok=True)


@app.delete("/api/people/<int:pid>")
@editor_required
def delete_person(pid):
    get_person(pid)
    ids = [r["id"] for r in db().execute("SELECT id FROM items WHERE person_id = ?", (pid,))]
    db().execute("DELETE FROM people WHERE id = ?", (pid,))  # items cascade
    db().commit()
    remove_photo_files(*ids)
    return jsonify(ok=True)


@app.get("/api/members")
@editor_required
def list_members():
    rows = db().execute("SELECT * FROM members ORDER BY display_name").fetchall()
    return jsonify(members=[dict(r) for r in rows], admins=sorted(ADMIN_EMAILS))


@app.put("/api/members")
@editor_required
def upsert_member():
    data = json_body()
    email = text(data, "email").lower()
    name = text(data, "display_name")[:60]
    role = data.get("role", "shopper")
    if "@" not in email or not name or role not in ("editor", "shopper"):
        abort(400, "Need an email, a display name and a role (editor or shopper).")
    db().execute("INSERT INTO members (email, display_name, role) VALUES (?,?,?) "
                 "ON CONFLICT(email) DO UPDATE SET display_name = excluded.display_name, "
                 "role = excluded.role", (email, name, role))
    db().commit()
    return jsonify(ok=True)


@app.delete("/api/members/<path:email>")
@editor_required
def delete_member(email):
    db().execute("DELETE FROM members WHERE email = ?", (email.lower(),))
    db().commit()
    return jsonify(ok=True)


init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8766)), debug=False)

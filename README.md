# Family Wish Lists

A small self-hosted wish list tracker for the family: one tab per person, online items (link + price)
and in-store items (store + price), sorting, photos, occasion tags, and "who got it" tracking that is
hidden from the person whose list it is. Shoppers can reserve an item before buying it and leave
secret gift ideas on someone else's tab; once gifts are given, an editor archives them to a
per-tab History that doubles as a thank-you list. Runs on an Alpine LXC on Proxmox and is shared through a Cloudflare
Tunnel with Cloudflare Access (email one-time PIN) in front of it.

- **Backend:** Flask + SQLite (`app.py`), served by gunicorn under OpenRC
- **Frontend:** plain HTML/CSS/JS in `static/`, no build step, installable as a phone home-screen app
- **Photos:** Pillow; re-encoded to JPEG (max 1600 px + 400 px thumbnail), EXIF stripped
- **Fill in from a link:** paste a product link and the item dialog offers the page's name, price and
  photo (Open Graph tags or JSON-LD). Many shops block it; then the form just works as usual

## Layout

```
app.py                       the whole server
static/                      index.html, app.js, style.css, icon.svg, manifest.json
deploy/setup-wishlist.sh.in  installer template (Alpine + OpenRC)
deploy/deploy.env.example    household defaults for the installer -> copy to deploy/deploy.env
tools/build_installer.py     builds dist/setup-wishlist.sh (app embedded as a base64 tarball)
tests/                       pytest suite
.github/                     CI (pytest on pip packages and on Alpine's py3-* packages) and Dependabot
```

## Run it locally

```sh
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
SEED_PEOPLE="Alex,Sam" python3 app.py          # http://localhost:8766
python3 -m pytest -q
```

Requests from localhost/LAN without Cloudflare headers get the `LAN_ROLE` (default `editor`),
so local development needs no login. Data goes to `./data/` (git-ignored).

## Deploy / upgrade on the container

```sh
cp deploy/deploy.env.example deploy/deploy.env   # first time only; fill in emails, names, colors
python3 tools/build_installer.py                 # -> dist/setup-wishlist.sh

scp dist/setup-wishlist.sh root@<proxmox-host>:/root/
ssh root@<proxmox-host> 'pct push <ctid> /root/setup-wishlist.sh /root/setup-wishlist.sh && pct exec <ctid> -- sh /root/setup-wishlist.sh'
```

The installer is idempotent. It installs packages with `apk`, replaces `/opt/wishlist`, writes
`/etc/init.d/wishlist`, and (re)starts the service. It **keeps** the database and photos in
`/var/lib/wishlist` and the settings in `/etc/conf.d/wishlist`; schema changes are migrated in
place on start-up. Values from `deploy.env` are only used when `/etc/conf.d/wishlist` doesn't exist yet.

Logs: `tail -f /var/log/wishlist.log` · Restart: `rc-service wishlist restart`

## Configuration (`/etc/conf.d/wishlist`)

| Variable | Meaning |
|---|---|
| `WISHLIST_PORT` | Port gunicorn listens on (default 8766) |
| `ADMIN_EMAILS` | Comma list of emails that can always edit |
| `CF_TEAM_DOMAIN` | Zero Trust team domain, e.g. `xxx.cloudflareaccess.com` (Settings → Team name and domain) |
| `CF_AUD` | Access application's AUD tag (Access controls → Applications → app → Configure → Additional settings) |
| `LAN_ROLE` | Role for direct LAN requests: `editor`, `shopper` or `none` |
| `LAN_NAME` | Name shown for LAN users (default `Home`) |
| `SEED_PEOPLE` | Tabs created on the very first start |
| `PERSON_COLORS` | Default tab colors, `Name=#hex,Other|Alias=#hex`; only fills tabs without a color |
| `FEED_TOKEN` | Bearer token of the private feed (see below). Unset or empty: the feed is off and every `/api/feed` path is a 404 |
| `FEED_PEOPLE` | Comma list of tab names the feed exposes (matched ignoring case). Empty: none |

## How access works

1. Cloudflare Access decides who can reach the site at all (policy: Include → Emails; login
   method: One-time PIN, so family need no Cloudflare account).
2. The app verifies the `Cf-Access-Jwt-Assertion` JWT (signature via the team's JWKS, audience,
   issuer, expiry) and takes the email from it. A forged email header alone is never trusted.
3. Roles: emails in `ADMIN_EMAILS` and members marked "Can edit" in Settings are **editors**
   (add/edit/delete items, photos, tabs, archive). Everyone else Access lets in is a **shopper** (view,
   reserve, mark purchased, add secret ideas and edit their own).
4. **Spoiler protection:** give a tab an owner email in Settings → Lists. When that person opens their
   own tab, purchase and reservation state, buyer names and counts are stripped server-side, and secret
   ideas don't exist for them. Archiving purchased items is the reveal: History shows the owner who gave
   what. Secret ideas need a Cloudflare sign-in; LAN visitors can't see or add them.
5. Mutating requests need the `X-Wishlist: 1` header plus a JSON (or photo multipart) body and a same-origin
   `Origin`, which blocks cross-site request forgery.
6. **Link previews** (`POST /api/preview`, editors only) are the one time the server fetches a URL it was
   given. It only speaks http(s), refuses hosts that resolve to private, loopback, link-local or other
   non-public addresses (re-checked on each of at most 3 redirects, and the connection goes to the address
   that was checked), and gives up after 5 seconds, 1 MB of page or 20 MB of image. The photo is
   downloaded and re-encoded like an upload, so the page never loads images from other sites.
7. **Private feed** for one trusted companion service on the LAN (a private gift planner that records which
   giver has been handed which wish, so shoppers here don't buy something already spoken for).
   `GET /api/feed` returns the tabs in `FEED_PEOPLE` with every item on them, including secret ideas, items
   the owner removed after a reservation or purchase, and who reserved or bought each one, with their
   email (archived items only with `?archived=1`). `POST /api/feed/items/<id>/reserve` with `{"by": "<name>"}`
   reserves under that name, or re-labels a reservation that has no email; `{"by": null}` releases one.
   `POST /api/feed/items/<id>/purchase` with `{"by": "<name>", "purchased": true}` marks it bought (ending a
   reservation that has no email); `"purchased": false` undoes a purchase that has no email and leaves the
   item open. A reservation or purchase by a signed-in shopper, or reserving a bought item, is 409; an item
   on another tab, archived or missing is 404. Releasing or undoing what nobody holds succeeds and changes
   nothing. Writes need a JSON body but no `X-Wishlist` header.
   Because the feed returns exactly what spoiler protection hides, it answers only a request that has
   `Authorization: Bearer <FEED_TOKEN>` (compared in constant time; never a query string or cookie), comes
   from a private address, and carries **no** Cloudflare header or `CF_Authorization` cookie, so it can't be
   reached through the tunnel. It ignores `LAN_ROLE` and identity entirely. Any other request to an
   `/api/feed` path, the right token included, gets exactly what a path that doesn't exist gets. Anything
   with no email (a reservation or purchase made by the feed, or typed in by a LAN visitor) counts as the
   feed's to change. Over plain `http://` the token crosses the LAN in clear text: keep the two
   containers on a trusted segment. The installer only writes `/etc/conf.d/wishlist` on a first install;
   on an existing one, add `FEED_TOKEN` and `FEED_PEOPLE` to it by hand and `rc-service wishlist restart`.

## Backups

Everything stateful is in `/var/lib/wishlist` (`wishlist.db` + `photos/`) and `/etc/conf.d/wishlist`.
SQLite runs in WAL mode; for a consistent copy while running use
`sqlite3 /var/lib/wishlist/wishlist.db ".backup /root/wishlist-$(date +%F).db"`.

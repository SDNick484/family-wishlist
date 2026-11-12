# Family Wish Lists

A small self-hosted wish list tracker for the family: one tab per person, online items (link + price)
and in-store items (store + price), sorting, photos, and "who got it" tracking that is hidden from
the person whose list it is. Runs on an Alpine LXC on Proxmox and is shared through a Cloudflare
Tunnel with Cloudflare Access (email one-time PIN) in front of it.

- **Backend:** Flask + SQLite (`app.py`), served by gunicorn under OpenRC
- **Frontend:** plain HTML/CSS/JS in `static/`, no build step, installable as a phone home-screen app
- **Photos:** Pillow; re-encoded to JPEG (max 1600 px + 400 px thumbnail), EXIF stripped

## Layout

```
app.py                       the whole server
static/                      index.html, app.js, style.css, icon.svg, manifest.json
deploy/setup-wishlist.sh.in  installer template (Alpine + OpenRC)
deploy/deploy.env.example    household defaults for the installer -> copy to deploy/deploy.env
tools/build_installer.py     builds dist/setup-wishlist.sh (app embedded as a base64 tarball)
tests/                       pytest suite
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

## How access works

1. Cloudflare Access decides who can reach the site at all (policy: Include → Emails; login
   method: One-time PIN, so family need no Cloudflare account).
2. The app verifies the `Cf-Access-Jwt-Assertion` JWT (signature via the team's JWKS, audience,
   issuer, expiry) and takes the email from it. A forged email header alone is never trusted.
3. Roles: emails in `ADMIN_EMAILS` and members marked "Can edit" in Settings are **editors**
   (add/edit/delete items, photos, tabs). Everyone else Access lets in is a **shopper** (view, mark purchased).
4. **Spoiler protection:** give a tab an owner email in Settings → Lists. When that person opens their
   own tab, purchase state, buyer names and bought counts are stripped server-side.
5. Mutating requests need the `X-Wishlist: 1` header plus a JSON (or photo multipart) body and a same-origin
   `Origin`, which blocks cross-site request forgery.

## Backups

Everything stateful is in `/var/lib/wishlist` (`wishlist.db` + `photos/`) and `/etc/conf.d/wishlist`.
SQLite runs in WAL mode; for a consistent copy while running use
`sqlite3 /var/lib/wishlist/wishlist.db ".backup /root/wishlist-$(date +%F).db"`.

# Family Wish Lists

Self-hosted wish list tracker: one tab per person, items with links/stores/prices/photos, and
"who got it" tracking hidden from the person whose list it is. See `README.md` for deployment.

## Layout

- `app.py` — the whole server (Flask + SQLite, ~1000 lines). Sections in order: database, identity,
  helpers, routes, photos, link preview, settings.
- `static/` — `index.html`, `app.js`, `style.css`. Plain browser JS, no build step, no dependencies.
- `deploy/setup-wishlist.sh.in` + `tools/build_installer.py` — single-file installer for Alpine/OpenRC.
- `tests/` — pytest suite; `frontend.test.mjs` holds the Node tests for `app.js`.
- `.github/workflows/ci.yml` — pytest on pip packages (Python 3.12 and latest, with Node) and on
  Alpine's `py3-*` packages. Dependabot keeps actions and pip pins current.

## Commands

```sh
python3 -m pytest -q                 # everything, including the Node tests if Node 20+ is installed
python3 -m pytest -q tests/test_lists.py -k Spoiler
node --test tests/frontend.test.mjs  # frontend only
SEED_PEOPLE="Alex,Sam" python3 app.py  # http://localhost:8766, LAN requests are editors
```

## Rules that must keep holding

- **Spoiler protection.** The owner of a tab (`people.owner_email` == signed-in email) must never
  learn what was bought or reserved for them, from any endpoint: no purchase or reservation state,
  buyer names, bought counts, or differences in status codes. `item_json(row, owner_view)` strips
  the fields; `list_people` omits `bought`/`reserved`/`remaining`. `can_see(item, owner)` decides
  which items exist for a user, and `get_item`/`tab_items` go through it, so a hidden item answers
  exactly like a missing id: items the owner removed after purchase or reservation
  (`removed_by_owner`) and secret ideas (`secret`; also hidden from LAN users) are hidden from the
  owner. Archiving (`archived_at`) is the one deliberate reveal: archived gifts show the owner who
  gave them. Any new field or endpoint that reveals gift state needs the same treatment and a test
  in `TestSpoilersMore`.
- **Identity.** Email comes only from a verified Cloudflare Access JWT. Requests with any Cloudflare
  header or token never fall back to the LAN role. LAN users have no email, so they are never owners.
- **Roles.** `editor` (in `ADMIN_EMAILS` or a member marked editor) can change things; everyone else
  is a `shopper` who can view, reserve, mark purchased and add secret ideas. Guard mutating routes
  with `@editor_required`, or `require_edit(item)` where the author of a secret idea may also act.
- **CSRF.** Mutations need the `X-Wishlist: 1` header and a JSON body (multipart only for photo upload).
- **Companion feed.** `/api/feed*` (`FEED_TOKEN`, `FEED_PEOPLE`) is the one deliberate exception to all of
  the above: a bearer-token caller on the LAN gets full gift state, no role, no `g.user`, and no
  `X-Wishlist`/`Origin` check. `feed_caller()` decides it per request (feed endpoint, token set and equal,
  private address, no Cloudflare header or cookie); anything else falls through to the normal checks and
  `@feed_required` answers 404 exactly like an unknown route (wrong verbs too). Keep it that narrow, and
  test any change in `TestSpoilersMore` against an unknown `/api` path. The contract is SDNick484/gift-tracker
  `docs/wishlist-feed.md`: honor it exactly. Feed writes never delete an owner-removed item (`orphaned()` hides
  it instead), because the tracker re-labels and rolls back as an undo followed by a new claim.
- **Input.** Read bodies with `json_body()` and string fields with `text(data, key)` so bad shapes
  return 400. Never store non-finite numbers: they serialize to invalid JSON.
- **Outbound fetches.** The server fetches URLs only through `_fetch` (link preview), which allows
  http(s) to public addresses only, re-checks every redirect, connects to the address it checked, and
  caps time and size. Tests stub `socket.getaddrinfo` and `_open`; never reach the real network.
- **Frontend.** Everything user-typed goes through `esc()` before it reaches `innerHTML`. The CSP
  allows no inline scripts and images only from this origin.

## Conventions

- Schema changes: add the column to `SCHEMA` and to the upgrade loop in `init_db()` (existing
  databases are migrated in place on start-up), and extend `test_upgrade_from_first_release_schema`.
- New runtime settings: read them in `app.py`, export them in the init script inside
  `deploy/setup-wishlist.sh.in`, and add them to the README table.
- Runtime dependencies must exist as Alpine `py3-*` packages; prefer the standard library.
- No personal data in the repo: use `example.com` emails, generic names (Alex, Sam, Jordan) and
  RFC 5737 addresses. Household values live in the git-ignored `deploy/deploy.env`.
- Commit messages use `type: summary` (`fix:`, `test:`, `docs:`, `chore:`).

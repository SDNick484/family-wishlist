# Family Wish Lists

Self-hosted wish list tracker: one tab per person, items with links/stores/prices/photos, and
"who got it" tracking hidden from the person whose list it is. See `README.md` for deployment.

## Layout

- `app.py` — the whole server (Flask + SQLite, ~700 lines). Sections in order: database, identity,
  helpers, routes, photos, settings.
- `static/` — `index.html`, `app.js`, `style.css`. Plain browser JS, no build step, no dependencies.
- `deploy/setup-wishlist.sh.in` + `tools/build_installer.py` — single-file installer for Alpine/OpenRC.
- `tests/` — pytest suite; `frontend.test.mjs` holds the Node tests for `app.js`.

## Commands

```sh
python3 -m pytest -q                 # everything, including the Node tests if Node 20+ is installed
python3 -m pytest -q tests/test_lists.py -k Spoiler
node --test tests/frontend.test.mjs  # frontend only
SEED_PEOPLE="Alex,Sam" python3 app.py  # http://localhost:8766, LAN requests are editors
```

## Rules that must keep holding

- **Spoiler protection.** The owner of a tab (`people.owner_email` == signed-in email) must never
  learn what was bought for them, from any endpoint: no purchase state, buyer names, bought counts,
  or differences in status codes. `item_json(row, owner_view)` strips the fields; `list_people`
  omits `bought`/`remaining`; items the owner removed after purchase (`removed_by_owner`) are hidden
  from the owner only. Any new field or endpoint that reveals gift state needs the same treatment
  and a test in `TestSpoilersMore`.
- **Identity.** Email comes only from a verified Cloudflare Access JWT. Requests with any Cloudflare
  header or token never fall back to the LAN role. LAN users have no email, so they are never owners.
- **Roles.** `editor` (in `ADMIN_EMAILS` or a member marked editor) can change things; everyone else
  is a `shopper` who can view and mark purchased. Guard mutating routes with `@editor_required`.
- **CSRF.** Mutations need the `X-Wishlist: 1` header and a JSON body (multipart only for photo upload).
- **Input.** Read bodies with `json_body()` and string fields with `text(data, key)` so bad shapes
  return 400. Never store non-finite numbers: they serialize to invalid JSON.
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

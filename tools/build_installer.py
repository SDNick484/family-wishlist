#!/usr/bin/env python3
"""Build dist/setup-wishlist.sh: a single self-contained installer for an Alpine LXC.

The app (app.py + static/) is packed as a base64 tarball inside the script, so the
container needs nothing but the one file:

    python3 tools/build_installer.py
    scp dist/setup-wishlist.sh root@<proxmox-host>:/root/
    ssh root@<proxmox-host> 'pct push <ctid> /root/setup-wishlist.sh /root/setup-wishlist.sh && \
                             pct exec <ctid> -- sh /root/setup-wishlist.sh'

Household defaults (admin emails, initial tabs, tab colors) come from deploy/deploy.env,
which is git-ignored. Copy deploy/deploy.env.example to start. They only apply on a
first install: re-running keeps /etc/conf.d/wishlist and the database as they are.
"""
import argparse
import base64
import io
import os
import re
import sys
import tarfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATE = os.path.join(ROOT, "deploy", "setup-wishlist.sh.in")
APP_FILES = ["app.py", "static"]
KEYS = ("ADMIN_EMAILS", "SEED_PEOPLE", "PERSON_COLORS")


def read_env(path):
    values = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            values[key.strip()] = val.strip().strip('"').strip("'")
    return values


def payload():
    """Deterministic tar.gz of the app (stable mtimes/owners -> reproducible output)."""
    def clean(ti):
        if "__pycache__" in ti.name or ti.name.endswith(".pyc"):
            return None
        ti.uid = ti.gid = 0
        ti.uname = ti.gname = "root"
        ti.mtime = 0
        return ti

    buf = io.BytesIO()
    import gzip
    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as gz:
        with tarfile.open(fileobj=gz, mode="w") as tar:
            for name in APP_FILES:
                tar.add(os.path.join(ROOT, name), arcname=name, filter=clean)
    return base64.encodebytes(buf.getvalue()).decode().rstrip("\n")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--env", default=os.path.join(ROOT, "deploy", "deploy.env"),
                    help="household settings file (default: deploy/deploy.env)")
    ap.add_argument("-o", "--output", default=os.path.join(ROOT, "dist", "setup-wishlist.sh"))
    args = ap.parse_args()

    env_path = args.env
    if not os.path.exists(env_path):
        example = os.path.join(ROOT, "deploy", "deploy.env.example")
        print(f"note: {os.path.relpath(env_path, ROOT)} not found, using placeholder values from "
              f"{os.path.relpath(example, ROOT)}", file=sys.stderr)
        env_path = example
    env = read_env(env_path)

    for key in KEYS:
        val = env.get(key, "")
        # values land inside "${VAR:-...}" in a shell script
        if re.search(r'["`$\\\n}]', val):
            sys.exit(f"{key} contains a character that isn't allowed here: {val!r}")
        env[key] = val

    s = open(TEMPLATE).read()
    s = s.replace("@@PAYLOAD@@", payload())
    for key in KEYS:
        s = s.replace(f"@@{key}@@", env[key])
    leftover = re.findall(r"@@[A-Z_]+@@", s)
    if leftover:
        sys.exit(f"template placeholders not filled: {leftover}")

    os.makedirs(os.path.dirname(args.output), exist_ok=True)
    with open(args.output, "w", newline="\n") as f:
        f.write(s)
    os.chmod(args.output, 0o755)
    print(f"wrote {os.path.relpath(args.output, ROOT)} ({len(s.encode()):,} bytes)")


if __name__ == "__main__":
    main()

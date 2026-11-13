"""tools/build_installer.py and the installer template it fills in."""
import base64
import importlib.util
import io
import os
import re
import shutil
import subprocess
import sys
import tarfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

spec = importlib.util.spec_from_file_location("build_installer", os.path.join(ROOT, "tools", "build_installer.py"))
build_installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build_installer)


def build(tmp_path, monkeypatch, env_text=None):
    out = tmp_path / "setup-wishlist.sh"
    env = tmp_path / "deploy.env"
    if env_text is not None:
        env.write_text(env_text)
    monkeypatch.setattr(sys, "argv", ["build_installer.py", "--env", str(env), "-o", str(out)])
    build_installer.main()
    return out.read_bytes().decode()


def unpack(script):
    b64 = re.search(r"<<'PAYLOAD_EOF'[^\n]*\n(.*?)\nPAYLOAD_EOF\n", script, re.S).group(1)
    return tarfile.open(fileobj=io.BytesIO(base64.b64decode(b64)), mode="r:gz")


ENV = 'ADMIN_EMAILS=me@example.com\nSEED_PEOPLE="Pat,Lee"\nPERSON_COLORS=Pat=#112233,Lee|Leigh=#445566\n'


class TestReadEnv:
    def test_parses_comments_quotes_and_blanks(self, tmp_path):
        f = tmp_path / "x.env"
        f.write_text("# comment\n\nA=1\n B = two words \nC=\"quoted\"\nD='single'\nnot a pair\nE=a=b\nF=\n")
        assert build_installer.read_env(f) == {"A": "1", "B": "two words", "C": "quoted", "D": "single",
                                               "E": "a=b", "F": ""}

    def test_example_file_has_every_key_and_only_placeholders(self):
        env = build_installer.read_env(os.path.join(ROOT, "deploy", "deploy.env.example"))
        assert set(env) == set(build_installer.KEYS)
        emails = [e for e in env["ADMIN_EMAILS"].split(",")]
        assert emails and all(e.endswith("@example.com") for e in emails)


class TestPayload:
    def test_is_reproducible(self):
        assert build_installer.payload() == build_installer.payload()

    def test_contains_the_app_and_nothing_private(self, tmp_path, monkeypatch):
        tar = unpack(build(tmp_path, monkeypatch, ENV))
        names = set(tar.getnames())
        assert {"app.py", "static/index.html", "static/app.js", "static/style.css",
                "static/manifest.json", "static/icon.svg"} <= names
        assert not [n for n in names if n.split("/")[0] not in ("app.py", "static")]
        assert not [n for n in names if "__pycache__" in n or n.endswith((".pyc", ".db", ".env"))]
        for member in tar.getmembers():
            assert (member.uid, member.gid, member.mtime) == (0, 0, 0)
            assert not member.name.startswith(("/", "..")) and not member.issym()

    def test_embedded_app_matches_the_working_tree(self, tmp_path, monkeypatch):
        tar = unpack(build(tmp_path, monkeypatch, ENV))
        for name in ("app.py", "static/app.js"):
            with open(os.path.join(ROOT, name), "rb") as f:
                assert tar.extractfile(name).read() == f.read()


class TestGeneratedScript:
    def test_values_are_substituted_and_no_placeholders_remain(self, tmp_path, monkeypatch):
        s = build(tmp_path, monkeypatch, ENV)
        assert 'ADMIN_EMAILS="${ADMIN_EMAILS:-me@example.com}"' in s
        assert 'SEED_PEOPLE="${SEED_PEOPLE:-Pat,Lee}"' in s
        assert ': ${PERSON_COLORS:="Pat=#112233,Lee|Leigh=#445566"}' in s
        assert not re.search(r"@@[A-Z_]+@@", s)

    def test_unix_line_endings_and_shebang(self, tmp_path, monkeypatch):
        s = build(tmp_path, monkeypatch, ENV)
        assert s.startswith("#!/bin/sh\n") and "\r" not in s

    @pytest.mark.skipif(not shutil.which("sh"), reason="needs a POSIX shell")
    def test_shell_syntax_is_valid(self, tmp_path, monkeypatch):
        build(tmp_path, monkeypatch, ENV)
        r = subprocess.run(["sh", "-n", str(tmp_path / "setup-wishlist.sh")], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr

    def test_missing_env_file_falls_back_to_the_example(self, tmp_path, monkeypatch, capsys):
        s = build(tmp_path, monkeypatch, env_text=None)
        assert "you@example.com" in s and "not found" in capsys.readouterr().err

    def test_missing_keys_become_empty(self, tmp_path, monkeypatch):
        s = build(tmp_path, monkeypatch, "ADMIN_EMAILS=me@example.com\n")
        assert 'SEED_PEOPLE="${SEED_PEOPLE:-}"' in s
        assert 'FEED_TOKEN="${FEED_TOKEN:-}"' in s  # no token, no feed

    def test_feed_settings_are_substituted(self, tmp_path, monkeypatch):
        s = build(tmp_path, monkeypatch, ENV + "FEED_TOKEN=0123abcd\nFEED_PEOPLE=Pat,Lee\n")
        assert 'FEED_TOKEN="${FEED_TOKEN:-0123abcd}"' in s and 'FEED_PEOPLE="${FEED_PEOPLE:-Pat,Lee}"' in s

    @pytest.mark.parametrize("bad", ['a"b', "a`id`", "$(id)", "a\\b", "Pat}Lee"])
    def test_shell_metacharacters_are_refused(self, tmp_path, monkeypatch, bad):
        with pytest.raises(SystemExit):
            build(tmp_path, monkeypatch, f"ADMIN_EMAILS=me@example.com\nSEED_PEOPLE={bad}\n")
        assert not (tmp_path / "setup-wishlist.sh").exists()


class TestTemplate:
    template = open(os.path.join(ROOT, "deploy", "setup-wishlist.sh.in"), encoding="utf-8").read()
    exported = re.search(r"^\s*export ((?:.*\\\n)*.*)$", template, re.M).group(1).replace("\\\n", " ").split()

    def test_service_never_overwrites_existing_config_or_data(self):
        assert 'if [ ! -f "$CONF" ]; then' in self.template
        assert not re.search(r"rm -rf [^\n]*(\$DATA_DIR|/var/lib/wishlist)", self.template)

    def test_service_runs_unprivileged(self):
        assert 'command_user="wishlist"' in self.template

    @pytest.mark.parametrize("var", ["ADMIN_EMAILS", "CF_TEAM_DOMAIN", "CF_AUD", "LAN_ROLE", "LAN_NAME",
                                     "SEED_PEOPLE", "PERSON_COLORS", "FEED_TOKEN", "FEED_PEOPLE"])
    def test_documented_settings_reach_the_app(self, var):
        # OpenRC only passes exported variables to the service
        assert var in self.exported

    def test_installed_packages_cover_requirements(self):
        apk = re.search(r"apk add --no-cache (.+?) >", self.template).group(1).split()
        for pkg in ("py3-flask", "py3-gunicorn", "py3-jwt", "py3-cryptography", "py3-pillow"):
            assert pkg in apk

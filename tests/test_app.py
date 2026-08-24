"""Basic smoke tests for app.py.

These need Flask (and the rest of requirements.txt) actually installed to
run - `pip install -r requirements.txt -r requirements-dev.txt && pytest`
from the project root. They're deliberately narrow: enough to catch a
route that 500s, a template that fails to render, or a security feature
(CSRF, org isolation, custom error pages) silently regressing - not a full
behavioral test suite.

Each test gets a brand-new SQLite database in a temp directory (via
PHX_DATA_DIR) and a fresh import of app.py, so tests never share state or
touch the real pvtracker.db.
"""
import importlib
import sys
import os

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture
def app_module(tmp_path, monkeypatch):
    monkeypatch.setenv("PHX_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("PHX_SECRET_KEY", "test-only-secret-key")
    # RENDER unset -> IS_PRODUCTION is False -> cookies work over plain http
    # test requests, matching local dev rather than the production config.
    monkeypatch.delenv("RENDER", raising=False)
    sys.modules.pop("app", None)
    mod = importlib.import_module("app")
    mod.app.config["TESTING"] = True
    yield mod
    sys.modules.pop("app", None)


@pytest.fixture
def client(app_module):
    with app_module.app.test_client() as c:
        yield c


@pytest.fixture
def org(app_module):
    """A minimal organization row, for routes that need one to resolve."""
    conn = app_module.get_db()
    conn.execute("INSERT INTO organizations (name, slug) VALUES (?, ?)", ("Test Org", "test-org"))
    conn.commit()
    conn.close()
    return "test-org"


def test_home_page_loads(client):
    resp = client.get("/home")
    assert resp.status_code == 200
    assert b"Mound HQ" in resp.data


def test_robots_txt(client):
    resp = client.get("/robots.txt")
    assert resp.status_code == 200
    assert b"Disallow: /" in resp.data
    assert b"Sitemap:" in resp.data


def test_sitemap_xml(client):
    resp = client.get("/sitemap.xml")
    assert resp.status_code == 200
    assert b"<urlset" in resp.data


def test_terms_and_privacy_pages_load(client):
    for path in ("/terms", "/privacy"):
        resp = client.get(path)
        assert resp.status_code == 200, path
        assert b"Mound HQ" in resp.data


def test_unknown_org_returns_custom_404(client):
    resp = client.get("/this-org-does-not-exist/")
    assert resp.status_code == 404
    assert b"Page not found" in resp.data


def test_platform_admin_area_requires_login(client):
    resp = client.get("/platform/organizations", follow_redirects=False)
    assert resp.status_code == 302
    assert "/platform/login" in resp.headers["Location"]


def test_coach_portal_requires_login(client):
    resp = client.get("/coach/players", follow_redirects=False)
    assert resp.status_code == 302
    assert "/coach/login" in resp.headers["Location"]


def test_csrf_blocks_post_without_token(client):
    """The platform admin login form posts name/password with no
    csrf_token field - CSRFProtect should reject it with a 400 before the
    view function (and any real login attempt) ever runs."""
    resp = client.post(
        "/platform/login",
        data={"email": "nobody@example.com", "password": "wrong-password"},
    )
    assert resp.status_code == 400


def test_login_page_renders_csrf_token(client, org):
    resp = client.get(f"/{org}/login")
    assert resp.status_code == 200
    assert b'name="csrf_token"' in resp.data


def test_org_scoped_page_requires_login(client, org):
    resp = client.get(f"/{org}/", follow_redirects=False)
    assert resp.status_code == 302
    assert f"/{org}/login" in resp.headers["Location"]

"""
test_session_and_assets.py — logging out must actually close the door, and
one asset version must serve every page.

Two things this covers:

  * The back/forward hole. `Cache-Control: no-store` is enough for a reload but
    not for the bfcache: Safari and Firefox keep a fully rendered page in
    memory and restore it on Back without asking the server. On a shared clinic
    machine that puts the previous doctor's patient list back on screen. The
    server side is asserted here; the client-side guard is a JS file, so what
    is checked is that every private page actually loads it and that the
    endpoint it depends on cannot be cached.

  * Asset versioning. Nine templates each pinned their own cache-buster, so
    whichever page was not being edited kept serving a stale number.
"""
import re
from datetime import datetime

import pytest

from tests.conftest import TestSessionLocal
from tests.helpers import (make_doctor, clinic_of, make_patient, make_appointment,
                           give_schedule, set_pin, register, login, PASSWORD)
from config import settings
from database.models import Doctor


@pytest.fixture
def doc(client):
    client.cookies.clear()
    email = f"sess-{datetime.utcnow().timestamp()}@test.com".replace(".", "-", 1)
    did = make_doctor(client, email)
    cid = clinic_of(did)
    give_schedule(did, cid)
    pid = make_patient(did, cid, name="Session Patient")
    return {"id": did, "clinic": cid, "patient": pid, "email": email}


# --------------------------------------------------------------------------- #
#  Cache headers on private pages                                               #
# --------------------------------------------------------------------------- #

class TestPrivatePagesAreNotCacheable:

    PRIVATE = ["/dashboard", "/patients", "/appointments", "/calendar",
               "/income", "/reports", "/doctors/settings"]

    @pytest.mark.parametrize("path", PRIVATE)
    def test_no_store_is_set(self, client, doc, path):
        r = client.get(path, follow_redirects=False)
        cc = r.headers.get("cache-control", "")
        assert "no-store" in cc, f"{path} is cacheable: {cc!r}"

    def test_public_pages_are_still_cacheable(self, client):
        """Blanket no-store would make the marketing pages needlessly slow."""
        client.cookies.clear()
        for path in ("/login", "/pricing"):
            cc = client.get(path).headers.get("cache-control", "")
            assert "no-store" not in cc, f"{path} became uncacheable: {cc!r}"


class TestAuthCheckCannotBeCached:
    """The bfcache guard asks this endpoint whether the session is real.

    /auth/ sits in _PUBLIC_PREFIXES, so the middleware's blanket no-store does
    NOT cover it. A cached 200 here would keep a logged-out doctor's patients
    on screen — the exact failure the guard exists to prevent.
    """

    def test_ok_response_is_uncacheable(self, client, doc):
        r = client.get("/auth/check")
        assert r.status_code == 200
        assert "no-store" in r.headers.get("cache-control", ""), (
            f"auth/check is cacheable: {r.headers.get('cache-control')!r}")

    def test_logged_out_gets_401_not_a_redirect(self, client):
        """The guard reads the status code; an HTML redirect would read as ok."""
        client.cookies.clear()
        r = client.get("/auth/check", follow_redirects=False)
        assert r.status_code == 401, (
            f"expected 401 for the guard to act on, got {r.status_code}")
        assert "no-store" in r.headers.get("cache-control", "")

    def test_401_is_json_not_a_login_page(self, client):
        client.cookies.clear()
        r = client.get("/auth/check", follow_redirects=False)
        assert r.headers["content-type"].startswith("application/json")


# --------------------------------------------------------------------------- #
#  Logout                                                                       #
# --------------------------------------------------------------------------- #

class TestLogoutClosesTheDoor:

    def test_every_auth_cookie_is_cleared(self, client, doc):
        set_pin(client)
        r = client.get("/logout", follow_redirects=False)
        expired = " ".join(v for k, v in r.headers.items()
                           if k.lower() == "set-cookie")
        for cookie in ("access_token", "pin_session", "clinic_admin_auth",
                       "active_clinic"):
            assert cookie in expired, f"{cookie} was not cleared on logout"

    def test_logout_asks_the_browser_to_drop_its_cache(self, client, doc):
        """Deleting cookies does not evict a bfcache entry; this does."""
        r = client.get("/logout", follow_redirects=False)
        assert r.headers.get("clear-site-data") == '"cache"', (
            f"Clear-Site-Data missing on logout: "
            f"{r.headers.get('clear-site-data')!r}")

    def test_storage_is_not_cleared(self, client, doc):
        """"storage" would wipe localStorage and the doctor's theme choice."""
        r = client.get("/logout", follow_redirects=False)
        assert "storage" not in (r.headers.get("clear-site-data") or "")

    def test_private_pages_are_dead_after_logout(self, client, doc):
        client.get("/logout", follow_redirects=False)
        for path in ("/dashboard", "/patients", "/reports"):
            r = client.get(path, follow_redirects=False)
            assert r.status_code in (302, 303), f"{path} still served after logout"

    def test_login_explains_an_expired_bounce(self, client):
        """Otherwise being ejected mid-shift looks like a random failure."""
        client.cookies.clear()
        body = client.get("/login?expired=1").text
        assert "session ended" in body.lower(), (
            "the guard bounces here with no explanation on screen")


# --------------------------------------------------------------------------- #
#  The client-side guard is actually wired up                                   #
# --------------------------------------------------------------------------- #

class TestSessionGuardIsLoaded:

    def test_private_pages_load_the_guard(self, client, doc):
        for path in ("/dashboard", "/patients", "/appointments"):
            assert "session-guard.js" in client.get(path).text, (
                f"{path} has no back/forward protection")

    def test_pages_with_their_own_head_load_it_too(self, client, doc):
        """These do not extend base.html, so they are easy to forget."""
        set_pin(client)
        client.cookies.delete("pin_session")
        assert "session-guard.js" in client.get("/pin-prompt").text

    def test_the_guard_file_is_served(self, client):
        client.cookies.clear()
        r = client.get(f"/static/js/session-guard.js?v={settings.ASSET_VERSION}")
        assert r.status_code == 200
        assert "persisted" in r.text, "the bfcache branch is missing"
        assert "location.replace" in r.text, (
            "the guard must replace, not push — otherwise Back bounces between "
            "stale private pages")


# --------------------------------------------------------------------------- #
#  Asset versioning                                                             #
# --------------------------------------------------------------------------- #

class TestAssetVersionIsSingleSourced:

    # /pricing is deliberately excluded: it carries its own inline styles
    # and links no main.css, so it has no versioned asset to check.
    PAGES_PUBLIC = ["/login", "/register", "/"]
    PAGES_PRIVATE = ["/dashboard", "/patients", "/calendar"]

    def _versions(self, body):
        return set(re.findall(r'(?:main\.css|\.js)\?v=([^"\'&]+)', body))

    @pytest.mark.parametrize("path", PAGES_PUBLIC)
    def test_public_pages_use_the_configured_version(self, client, path):
        client.cookies.clear()
        found = self._versions(client.get(path, follow_redirects=True).text)
        assert found, f"{path} references no versioned asset"
        assert found == {settings.ASSET_VERSION}, (
            f"{path} pins its own asset version {found}, expected "
            f"{settings.ASSET_VERSION}")

    @pytest.mark.parametrize("path", PAGES_PRIVATE)
    def test_private_pages_use_the_configured_version(self, client, doc, path):
        found = self._versions(client.get(path).text)
        assert found == {settings.ASSET_VERSION}, (
            f"{path} pins its own asset version {found}")

    def test_no_template_hardcodes_a_version(self, client):
        """The root cause: nine <head> blocks, each bumped independently."""
        import pathlib
        offenders = []
        root = pathlib.Path(__file__).resolve().parent.parent / "templates"
        for f in root.rglob("*.html"):
            for m in re.finditer(r'(?:main\.css|\.js)\?v=(\d+)', f.read_text()):
                offenders.append(f"{f.name}: v={m.group(1)}")
        assert offenders == [], (
            "templates still hardcode an asset version: " + ", ".join(offenders))

    def test_bumping_the_version_moves_every_page_at_once(self, client, doc,
                                                          monkeypatch):
        monkeypatch.setattr(settings, "ASSET_VERSION", "999999")
        for path in ("/dashboard", "/login"):
            client_cookies = None
            if path == "/login":
                client_cookies = dict(client.cookies)
                client.cookies.clear()
            body = client.get(path, follow_redirects=True).text
            assert "?v=999999" in body, f"{path} did not follow the bump"
            if client_cookies:
                for k, v in client_cookies.items():
                    client.cookies.set(k, v)


class TestMiddlewareDoesNotWorkOnStaticAssets:
    """A logged-in browser sends its cookie with every stylesheet and script.
    Both middlewares skip that work; this keeps them honest."""

    def test_static_requests_skip_the_per_request_setup(self, client, doc):
        import main
        calls = []
        real = None
        import services.payment_service as ps
        real = ps.price_display

        def counting():
            calls.append(1)
            return real()

        ps.price_display = counting
        try:
            client.get(f"/static/css/main.css?v={settings.ASSET_VERSION}")
            assert calls == [], "price_display ran for a static asset"
            client.get("/dashboard")
            assert calls, "price_display did not run for a real page"
        finally:
            ps.price_display = real

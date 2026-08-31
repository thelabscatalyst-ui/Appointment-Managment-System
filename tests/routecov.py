"""pytest plugin: which route templates does the suite actually hit?

Route coverage is not line coverage. A router can be 90% covered by line and
still have whole endpoints nobody ever calls — which is how this app reached
139 routes with 40% of them never exercised by a single test.

Run it:

    ./venv/bin/python -m pytest tests/ -q -p tests.routecov

It prints a percentage and lists every method+path template that no test
touched. Adding a route without a test shows up here immediately.
"""
import pytest
from starlette.testclient import TestClient

HITS = []
_orig = TestClient.request


def _patched(self, method, url, **kw):
    try:
        HITS.append((str(method).upper(), str(url)))
    except Exception:
        pass
    return _orig(self, method, url, **kw)


TestClient.request = _patched


def pytest_sessionfinish(session, exitstatus):
    try:
        import main
        from starlette.routing import Match, Route
    except Exception:
        return
    routes = [r for r in main.app.routes if isinstance(r, Route)]
    skip = {"/openapi.json", "/docs", "/redoc", "/docs/oauth2-redirect"}

    def match(method, raw):
        path = raw.split("?", 1)[0]
        if path.startswith("http"):
            path = "/" + path.split("/", 3)[3] if path.count("/") >= 3 else "/"
        scope = {"type": "http", "method": method, "path": path,
                 "root_path": "", "headers": [], "query_string": b""}
        partial = None
        for r in routes:
            m, _ = r.matches(scope)
            if m == Match.FULL:
                return (method, r.path)
            if m == Match.PARTIAL and partial is None:
                partial = (method, r.path)
        return partial

    hit = {h for h in (match(m, u) for m, u in HITS) if h}
    allr = {(m, r.path) for r in routes if r.path not in skip
            for m in sorted(r.methods - {"HEAD", "OPTIONS"})}
    missed = sorted(allr - hit, key=lambda x: (x[1], x[0]))
    covered = len(allr) - len(missed)
    print("\n" + "=" * 70)
    print(f"ROUTE COVERAGE: {covered}/{len(allr)} ({100*covered//max(1,len(allr))}%)")
    print("=" * 70)
    for m, p in missed:
        print(f"  MISS {m:6} {p}")

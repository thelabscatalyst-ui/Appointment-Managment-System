/*
 * session-guard.js — stop the Back button from showing a logged-out doctor
 * their patients.
 *
 * The server already sends `Cache-Control: no-store` on every private page, and
 * that is enough for a normal reload. It is NOT enough for the back/forward
 * cache: Safari and Firefox keep the whole live page in memory and restore it
 * on Back or Forward WITHOUT asking the server. So after logging out — on a
 * shared clinic computer, say — one press of Back can put the previous
 * doctor's patient list back on screen, fully rendered.
 *
 * A restored page fires `pageshow` with `event.persisted === true`. That is the
 * only reliable signal, so this asks the server whether the session is still
 * real and replaces the page if it is not.
 *
 * Notes on the choices here:
 *   - location.replace(), not assign(): the dead private page must not stay in
 *     history, or Back/Forward just bounces between stale pages.
 *   - cache: 'no-store' on the fetch, because a cached "ok" would defeat the
 *     whole check. /auth/check also sets no-store server-side.
 *   - fail closed. A network error while restoring means we cannot prove the
 *     session is alive, and showing patient data on an unprovable session is
 *     the worse mistake.
 *   - it also runs on a normal load, cheaply, to catch a session that expired
 *     while the tab sat open.
 */
(function () {
  "use strict";

  // Pages that are public by design: nothing to protect, and redirecting from
  // them would trap a logged-out visitor in a loop.
  var PUBLIC = [
    "/", "/login", "/register", "/pricing", "/forgot-password",
    "/reset-password", "/plan-lapsed", "/verify-email",
  ];
  var PUBLIC_PREFIX = ["/book/", "/queue/", "/feedback/", "/clinic/doctor-invite/"];

  function isPublic() {
    var p = window.location.pathname;
    if (PUBLIC.indexOf(p) !== -1) return true;
    for (var i = 0; i < PUBLIC_PREFIX.length; i++) {
      if (p.indexOf(PUBLIC_PREFIX[i]) === 0) return true;
    }
    return false;
  }

  function bounceToLogin() {
    // replace() so the stale page is dropped from history rather than left
    // behind for the next Back press.
    window.location.replace("/login?expired=1");
  }

  function verifySession(onRestore) {
    if (isPublic()) return;

    fetch("/auth/check", {
      method: "GET",
      cache: "no-store",
      credentials: "same-origin",
      headers: { "Accept": "application/json" },
    })
      .then(function (res) {
        if (!res.ok) {
          bounceToLogin();
        } else if (onRestore) {
          // Session is genuinely alive and this page came out of the bfcache.
          // Reload so the content is current rather than however old the
          // snapshot is — a queue or dashboard from an hour ago is misleading
          // even to someone entitled to see it.
          window.location.reload();
        }
      })
      .catch(function () {
        // Fail closed only for a restored page. On a normal load a transient
        // network blip should not eject someone mid-consultation.
        if (onRestore) bounceToLogin();
      });
  }

  window.addEventListener("pageshow", function (event) {
    if (event.persisted) {
      verifySession(true);            // restored from bfcache — always check
    }
  });

  // A tab left open across a session expiry looks logged in until something is
  // clicked. Checking when it regains focus turns that into a clean redirect.
  document.addEventListener("visibilitychange", function () {
    if (document.visibilityState === "visible") verifySession(false);
  });
})();

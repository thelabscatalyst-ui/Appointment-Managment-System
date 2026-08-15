"""
Password policy — server-side validation for account passwords.

Grounded in NIST SP 800-63B §5.1.1.2 and OWASP ASVS v4 §2.1, with one
deliberate deviation from NIST below.

Deliberate design notes:

  * We enforce **length** (12+). NIST recommends against mandating symbols —
    they push users toward predictable patterns ("P@ssw0rd1!") that add
    little real entropy while hurting usability.

  * **Symbol requirement is a product decision, not a NIST recommendation.**
    Med Track requires at least 2 symbols/special characters on top of the
    12-char minimum. This is an intentional override of the NIST-only
    stance for defense-in-depth on a medical-records product — length alone
    was judged insufficient here. Kept as a *count* (2+ from anywhere in the
    password), not a positional rule, so it doesn't push toward the
    "Capital-first, symbol-last" pattern NIST warns about.

  * We block context-specific values (the doctor's own name / email local
    part / clinic name) and a small set of well-known weak passwords. ASVS
    2.1.7 asks for a breach-corpus check; see `check_breached()` below —
    it is off by default because it makes an outbound network call.

  * `validate_password` returns **all** failures at once so the signup form
    can show a complete list instead of making the user resubmit repeatedly.
"""
import re

# Minimum length for a password-only authentication factor (ASVS 2.1.1).
MIN_LENGTH = 12

# Upper bound so a pathological input can't burn CPU in the hash function.
# NIST §5.1.1.2 requires accepting at least 64 characters.
MAX_LENGTH = 128

# Minimum count of symbol/special characters required (product decision,
# see module docstring). Any non-alphanumeric ASCII character counts.
MIN_SPECIAL_CHARS = 2
_SPECIAL_RE = re.compile(r"[^a-zA-Z0-9]")

# Small embedded blocklist. This is intentionally short — it catches the
# lazy cases without pretending to be a breach corpus. For real coverage,
# enable check_breached() (HaveIBeenPwned k-anonymity).
_COMMON_PASSWORDS = {
    "password", "password1", "password123", "passw0rd", "p@ssw0rd",
    "123456", "1234567", "12345678", "123456789", "1234567890",
    "qwerty", "qwertyuiop", "asdfghjkl", "zxcvbnm", "qwerty123",
    "letmein", "welcome", "welcome1", "admin", "administrator",
    "iloveyou", "monkey", "dragon", "sunshine", "princess",
    "abc123", "abcd1234", "111111", "000000", "123123",
    # India / product specific
    "india123", "bharat123", "clinic123", "doctor123", "hospital123",
    "medtrack", "medtrack123", "clinicos", "clinicos123",
}

# Sequential runs we treat as filler rather than entropy.
_SEQUENCES = (
    "abcdefghijklmnopqrstuvwxyz",
    "01234567890",
    "qwertyuiop",
    "asdfghjkl",
    "zxcvbnm",
)


def _has_long_run(pw: str, run: int = 4) -> bool:
    """True if pw contains `run`+ consecutive chars from a known sequence."""
    low = pw.lower()
    for seq in _SEQUENCES:
        for i in range(len(seq) - run + 1):
            if seq[i:i + run] in low:
                return True
            if seq[i:i + run][::-1] in low:
                return True
    return False


def _has_repeat(pw: str, run: int = 4) -> bool:
    """True if the same character repeats `run`+ times in a row."""
    return re.search(r"(.)\1{" + str(run - 1) + r",}", pw) is not None


def _context_tokens(*values: str) -> list[str]:
    """Split user-specific values into comparable lowercase tokens."""
    tokens: list[str] = []
    for value in values:
        if not value:
            continue
        cleaned = value.strip().lower()
        if not cleaned:
            continue
        # Email → use the local part only ("asha@x.com" → "asha")
        if "@" in cleaned:
            cleaned = cleaned.split("@", 1)[0]
        # Split on non-alphanumerics so "Asha Verma" → ["asha", "verma"]
        for part in re.split(r"[^a-z0-9]+", cleaned):
            if len(part) >= 4:      # ignore trivially short tokens
                tokens.append(part)
    return tokens


def validate_password(
    password: str,
    *,
    email: str = "",
    name: str = "",
    clinic_name: str = "",
) -> list[str]:
    """Validate a password. Returns a list of problems — empty means OK.

    All checks run so the caller can surface every failure in one pass.
    """
    problems: list[str] = []

    if password is None:
        return ["Password is required."]

    # Note: no .strip() — leading/trailing spaces are legitimate password
    # characters and NIST §5.1.1.2 requires accepting them.
    if len(password) < MIN_LENGTH:
        problems.append(
            f"Password must be at least {MIN_LENGTH} characters "
            f"(currently {len(password)})."
        )

    if len(password) > MAX_LENGTH:
        problems.append(f"Password must be {MAX_LENGTH} characters or fewer.")

    special_count = len(_SPECIAL_RE.findall(password))
    if special_count < MIN_SPECIAL_CHARS:
        problems.append(
            f"Password must include at least {MIN_SPECIAL_CHARS} special "
            f"characters or symbols (e.g. ! @ # $ % & * -) "
            f"(currently {special_count})."
        )

    if password.lower() in _COMMON_PASSWORDS:
        problems.append("That password is too common. Choose something less predictable.")

    if _has_long_run(password):
        problems.append("Avoid keyboard patterns or sequences like 'abcd' or '1234'.")

    if _has_repeat(password):
        problems.append("Avoid repeating the same character four or more times.")

    # Context check — a password containing the doctor's own name or email
    # is trivially guessable by anyone who knows them.
    for token in _context_tokens(email, name, clinic_name):
        if token in password.lower():
            problems.append("Password must not contain your name, email, or clinic name.")
            break

    # A password of one repeated class (all digits) is weak regardless of
    # length. We surface this as guidance, not a composition mandate.
    if password.isdigit():
        problems.append("Password must not be only numbers.")

    return problems


def check_breached(password: str, *, timeout: float = 2.0) -> bool | None:
    """Check a password against HaveIBeenPwned via k-anonymity (ASVS 2.1.7).

    Only the first 5 characters of the SHA-1 are sent — the password itself
    never leaves this process.

    Returns True if breached, False if clean, None if the check could not be
    completed (network error / timeout). Callers should **fail open** on None:
    a availability blip must not block a doctor from signing up.

    Not wired into validate_password() by default — enable deliberately, since
    it adds an outbound call to the registration path.
    """
    import hashlib
    try:
        import requests  # already a dependency
    except ImportError:
        return None

    digest = hashlib.sha1(password.encode("utf-8")).hexdigest().upper()
    prefix, suffix = digest[:5], digest[5:]
    try:
        resp = requests.get(
            f"https://api.pwnedpasswords.com/range/{prefix}",
            timeout=timeout,
            headers={"Add-Padding": "true"},
        )
        if resp.status_code != 200:
            return None
        for line in resp.text.splitlines():
            hash_suffix, _, _count = line.partition(":")
            if hash_suffix.strip().upper() == suffix:
                return True
        return False
    except Exception:
        return None

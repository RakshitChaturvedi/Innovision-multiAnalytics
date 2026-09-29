"""Obviously fake credentials for tests.

Every value is built from parts at runtime, so no source line holds a
credential-shaped literal (user:secret@host, password=..., KEY=value): secret
scanners flag those even in tests. Every fake value contains MARKER, so a
masking test can assert that MARKER appears nowhere in masked output, which
proves that every fake secret was hidden. Nothing here is or was a real secret.
"""

MARKER = "REDACTED"


def fake(label: str) -> str:
    """An obviously fake secret value, e.g. REDACTED-db."""
    return f"{MARKER}-{label}"


def userinfo_url(scheme: str, host: str, path: str = "", *, user: str = "user", label: str = "url") -> str:
    """A URL whose userinfo holds a fake secret: scheme, user, fake(label), host, path."""
    return f"{scheme}://{user}:{fake(label)}@{host}{path}"


def assignment(key: str, label: str, sep: str = "=") -> str:
    """KEY=<fake> (or KEY: <fake> with sep=': ')."""
    return f"{key}{sep}{fake(label)}"

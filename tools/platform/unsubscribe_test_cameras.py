"""Remove every use case from the platform's built-in test cameras.

    python -m tools.platform.unsubscribe_test_cameras [--dry-run] [--prefix "Test Camera"]
    (scripts\\unsubscribe_test_cameras.ps1 [-DryRun])

The platform creates "Test Camera UC1".."UC4", all subscribed to uc1 and pointing
at video files; left subscribed, detection processes them next to the real
cameras. For every camera whose name starts with the prefix and still has use
cases: PUT /cameras/{id}/config {"use_cases": []}. Prints what it changed.
Admin email/password are prompted (never stored); --dry-run needs no login.
"""
from __future__ import annotations

import argparse

from tools.platform.common import Camera, RegistryClient, RegistryError, Reporter, load_env
from tools.platform.register_camera import prompt_credentials

PREFIX = "Test Camera"


def test_cameras(cameras: list[Camera], prefix: str = PREFIX) -> list[Camera]:
    return sorted((c for c in cameras if c.name.startswith(prefix)), key=lambda c: c.name)


def unsubscribe(client: RegistryClient, r: Reporter, *, dry_run: bool, prefix: str = PREFIX,
                credentials=prompt_credentials) -> list[Camera]:
    """Returns the cameras changed (or, with dry_run, that would be)."""
    try:
        cams = test_cameras(client.active_cameras(), prefix)
    except RegistryError as exc:
        r.fail("registry", str(exc), exc.fix)
        return []
    if not cams:
        r.ok("test cameras", f"no camera named {prefix!r}*")
        return []
    todo = [c for c in cams if c.use_cases]
    for c in cams:
        if not c.use_cases:
            r.ok(c.name, f"{c.id} use_cases [] (unchanged)")
    if todo and not dry_run:
        try:
            client.login(*credentials())
        except RegistryError as exc:
            r.fail("login", str(exc), exc.fix)
            return []
    changed = []
    for c in todo:
        if dry_run:
            r.info(f"DRY-RUN {c.name}: {c.id} use_cases {c.use_cases} -> [] (not sent)")
            changed.append(c)
            continue
        try:
            client.update_camera(c.id, {"use_cases": []})
        except RegistryError as exc:
            r.fail(c.name, f"{c.id}: {exc}", exc.fix)
            continue
        r.ok(c.name, f"{c.id} use_cases {c.use_cases} -> []")
        changed.append(c)
    return changed


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="only print what would change")
    ap.add_argument("--prefix", default=PREFIX, help="camera name prefix (default %(default)r)")
    args = ap.parse_args(argv)
    r = Reporter()
    changed = unsubscribe(RegistryClient.from_env(load_env()), r, dry_run=args.dry_run,
                          prefix=args.prefix)
    verb = "would change" if args.dry_run else "changed"
    r.info(f"{len(changed)} camera(s) {verb}")
    if changed and not args.dry_run:
        r.info("Restart Alert Management so it reloads its camera list "
               "(docker compose -f <PlatformPath>\\infra\\docker-compose.yml restart <alert management service>)")
    return r.summary()


if __name__ == "__main__":
    raise SystemExit(main())

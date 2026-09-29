"""Register a camera in the platform camera registry for this use case.

    python -m tools.platform.register_camera --name "Phone 1" --location Lobby \\
        --url http://192.168.1.20:4747/video [--fps 5] [--update] [--platform-path DIR]
    (scripts\\register_cameras.ps1 -Name ... -Location ... -Url ... [-Fps 5] [-Update])

New name  -> log in (admin email/password prompted, never stored) and create it
             with use_cases [SOURCE_UC].
Name taken -> report its id and any difference in URL (masked), fps or use_cases.
             Only with --update: PUT /cameras/{id}/config with the new values,
             keeping every other use case and adding SOURCE_UC.
Then verify the camera is listed by GET /cameras/by-uc/{SOURCE_UC} and print the
command that restarts Alert Management (it loads cameras only at startup).
"""
from __future__ import annotations

import argparse
import getpass
import sys
import time
from pathlib import Path

from tools.platform.common import (
    Camera, RegistryClient, RegistryError, Reporter, compose_services, find_service,
    load_env, mask_url, platform_compose,
)


def prompt_credentials() -> tuple[str, str]:
    """Interactive: prompt. Piped (tests): two lines on stdin. Never stored."""
    if sys.stdin.isatty():
        email = input("Platform admin email: ").strip()
        return email, getpass.getpass("Platform admin password: ")
    lines = sys.stdin.read().splitlines() + ["", ""]
    return lines[0].strip(), lines[1]


def differences(cam: Camera, url: str, fps: int, uc: str) -> dict:
    """Changes to send with PUT /cameras/{id}/config (use_cases keeps the others)."""
    changes = {}
    if cam.rtsp_url != url:
        changes["rtsp_url"] = url
    if cam.fps != fps:
        changes["fps"] = fps
    if uc not in cam.use_cases:
        changes["use_cases"] = cam.use_cases + [uc]
    return changes


def describe(cam: Camera, changes: dict) -> list[str]:
    out = []
    if "rtsp_url" in changes:
        out.append(f"url {mask_url(cam.rtsp_url or '(none)')} -> {mask_url(changes['rtsp_url'])}")
    if "fps" in changes:
        out.append(f"fps {cam.fps} -> {changes['fps']}")
    if "use_cases" in changes:
        out.append(f"use_cases {cam.use_cases} -> {changes['use_cases']}")
    return out


def restart_command(platform_path: str | None, r: Reporter) -> None:
    if not platform_path:
        r.warn("restart", "no -PlatformPath given, cannot read the compose file",
               "docker compose -f <PlatformPath>\\infra\\docker-compose.yml restart <alert management service>")
        return
    compose = platform_compose(platform_path)
    if not compose.exists():
        r.warn("restart", f"{compose} not found", "pass -PlatformPath <platform repo folder>")
        return
    services = compose_services(compose)
    svc = find_service(services, "alert", "manag") or find_service(services, "alert")
    if svc is None:
        r.warn("restart", f"no alert management service in {compose} "
               f"(services: {', '.join(services)})", "restart the Alert Management service by hand")
        return
    r.info(f"NEXT: Alert Management loads cameras only at startup. Run:\n"
           f"  docker compose -f {compose} restart {svc}")


def wait_listed(client: RegistryClient, uc: str, cam_id: str, seconds: float = 10) -> bool:
    deadline = time.monotonic() + seconds
    while True:
        if cam_id in client.by_uc(uc):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(1)


def register(client: RegistryClient, r: Reporter, *, name, location, url, fps, uc,
             update=False, credentials=prompt_credentials, verify_s: float = 10) -> str | None:
    """Returns the camera id, or None when registration failed."""
    try:
        cameras = client.active_cameras()
    except RegistryError as exc:
        r.fail("registry", str(exc), exc.fix)
        return None
    same = [c for c in cameras if c.name == name]
    if len(same) > 1:
        r.fail("name", f"{len(same)} cameras are named {name!r} ({', '.join(c.id for c in same)})",
               "rename or delete the duplicates in the platform UI, then re-run")
        return None
    try:
        if same:
            cam = same[0]
            changes = differences(cam, url, fps, uc)
            if not changes:
                r.ok("camera", f"{name!r} already registered, id {cam.id}, nothing differs")
            elif not update:
                r.warn("camera", f"{name!r} already registered, id {cam.id}; differs: "
                       + "; ".join(describe(cam, changes)),
                       "re-run with -Update to apply these values")
            else:
                client.login(*credentials())
                client.update_camera(cam.id, changes)
                r.ok("camera", f"{name!r} id {cam.id} updated: " + "; ".join(describe(cam, changes)))
            cam_id = cam.id
        else:
            client.login(*credentials())
            cam = client.create_camera(name=name, location=location, rtsp_url=url,
                                       use_cases=[uc], fps=fps)
            r.ok("camera", f"{name!r} created, id {cam.id}, url {mask_url(url)}, fps {fps}, use_cases [{uc}]")
            cam_id = cam.id
        if wait_listed(client, uc, cam_id, verify_s):
            r.ok("discovery", f"{cam_id} is listed by /cameras/by-uc/{uc}")
        else:
            r.fail("discovery", f"{cam_id} is NOT listed by /cameras/by-uc/{uc}",
                   f"the camera needs {uc} in use_cases: re-run with -Update")
        return cam_id
    except RegistryError as exc:
        r.fail("registry", str(exc), exc.fix)
        return None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--name", required=True)
    ap.add_argument("--location", required=True)
    ap.add_argument("--url", required=True)
    ap.add_argument("--fps", type=int, default=5)
    ap.add_argument("--update", action="store_true", help="apply differences to an existing camera")
    ap.add_argument("--platform-path", help="platform repo folder (for the restart command)")
    args = ap.parse_args(argv)
    env = load_env()
    r = Reporter()
    cam_id = register(RegistryClient.from_env(env), r, name=args.name, location=args.location,
                      url=args.url, fps=args.fps, uc=env.get("SOURCE_UC", "uc1"), update=args.update)
    if cam_id:
        restart_command(args.platform_path or env.get("PLATFORM_PATH"), r)
    return r.summary()


if __name__ == "__main__":
    raise SystemExit(main())

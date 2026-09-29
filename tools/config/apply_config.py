"""Apply a hand-written config.yaml: zones per camera, people, enrollment photos,
authorized zones and the blocklist.

    python -m tools.config.apply_config config.yaml --dry-run
    python -m tools.config.apply_config config.yaml [--prune] [--retry-skipped]

See tools/config/config.example.yaml for the format. Rules:
  * cameras are resolved by name via the registry's internal endpoint; an
    unknown name FAILs and lists the registered names.
  * zones are upserted by (camera, zone name); polygons are validated with the
    same rules as the zone monitor (ZoneStore.validate_polygon).
  * zones of a listed camera that are not in the file are only reported;
    --prune deletes them (and their authorizations), except zones with an open
    intruder event or an open headcount breach, which are kept and reported.
  * for every person in the file the file is the source of truth: their
    authorized zones become exactly the listed set (removals are printed) and
    their blocklist entry follows `blocklisted`. People not in the file are
    untouched.
  * photos are enrolled with the recognition service's own face pipeline
    (tools/config/enrollment.py). A photo is remembered by its sha256 in
    enrolled_persons.metadata, so re-running never enrolls it twice; photos
    that were skipped are not retried unless --retry-skipped.
  * --dry-run prints the full plan and writes nothing (and loads no model).
  * after a real run the zone and enrollment caches are invalidated.
Everything runs in one transaction; on any FAIL nothing is written.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from sqlalchemy import text

from services.event_processing.src.workers.zone_monitor.zone_store import validate_polygon
from shared.schemas.enums import ZoneType
from tools.platform.common import Camera, RegistryClient, RegistryError, Reporter, load_env

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}
BLOCKLIST_REASON = "apply_config"
DEFAULT_DWELL_S = 60  # zones.dwell_threshold_seconds server default
LOCK_KEY = "innovision_apply_config"
ENROLL_INVALIDATE_CHANNEL = "cache:enrolled:invalidate"  # services/recognition/src/config.py

# ---------------------------------------------------------------- file format


class ZoneCfg(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=255)
    type: ZoneType
    polygon: list[list[float]]
    max_headcount: int | None = Field(default=None, ge=1)
    dwell_threshold_seconds: int = Field(default=DEFAULT_DWELL_S, ge=1)

    @field_validator("polygon", mode="before")
    @classmethod
    def _polygon(cls, v):
        points = validate_polygon(v)
        if points is None:
            raise ValueError("polygon needs at least 3 [x, y] points with x and y between 0.0 and 1.0")
        return points


class CameraCfg(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    zones: list[ZoneCfg] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique_zones(self):
        names = [z.name for z in self.zones]
        dup = sorted({n for n in names if names.count(n) > 1})
        if dup:
            raise ValueError(f"zone names repeated in camera {self.name!r}: {dup}")
        return self


class PersonCfg(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=255)
    images_dir: str | None = None
    authorized_zones: list[str] = Field(default_factory=list)
    blocklisted: bool = False

    @field_validator("authorized_zones")
    @classmethod
    def _refs(cls, v):
        bad = [ref for ref in v if "/" not in ref]
        if bad:
            raise ValueError(f"authorized_zones entries must be 'camera name/zone name', got {bad}")
        return v


class ConfigFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cameras: list[CameraCfg] = Field(default_factory=list)
    people: list[PersonCfg] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique(self):
        for label, names in (("camera", [c.name for c in self.cameras]),
                             ("person", [p.name for p in self.people])):
            dup = sorted({n for n in names if names.count(n) > 1})
            if dup:
                raise ValueError(f"{label} names repeated: {dup}")
        return self


def load_config(path: Path | str) -> ConfigFile:
    """Raises ValueError with every problem, one per line."""
    import yaml

    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"cannot read {path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ValueError(f"{path} is not valid YAML: {exc}") from exc
    try:
        return ConfigFile.model_validate(data or {})
    except ValidationError as exc:
        lines = []
        for err in exc.errors():
            loc = ".".join(str(p) for p in err["loc"])
            lines.append(f"{loc}: {err['msg'].removeprefix('Value error, ')}")
        raise ValueError("\n".join(lines)) from exc


# ---------------------------------------------------------------- plan


@dataclass
class Plan:
    lines: list[tuple[str, str]] = field(default_factory=list)   # (verb, text)
    errors: list[tuple[str, str]] = field(default_factory=list)  # (text, fix)
    warnings: list[tuple[str, str]] = field(default_factory=list)
    zone_creates: list[dict] = field(default_factory=list)
    zone_updates: list[dict] = field(default_factory=list)
    zone_deletes: list[dict] = field(default_factory=list)
    person_creates: list[dict] = field(default_factory=list)
    enroll: list[dict] = field(default_factory=list)       # {person_key, path, sha}
    auth_add: list[tuple[str, str, str]] = field(default_factory=list)     # (person_key, zone_id, label)
    auth_remove: list[tuple[str, str, str]] = field(default_factory=list)
    block_add: list[str] = field(default_factory=list)     # person keys
    block_remove: list[str] = field(default_factory=list)
    touched_cameras: set[str] = field(default_factory=set)

    def add(self, verb: str, what: str) -> None:
        self.lines.append((verb, what))

    @property
    def changes(self) -> int:
        return sum(1 for verb, _ in self.lines if verb not in ("UNCHANGED", "KEEP", "SKIP"))


def _polygon_of(value) -> list[list[float]] | None:
    return validate_polygon(value)


def _images(dir_: Path) -> list[Path]:
    return sorted(p for p in dir_.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


async def _zones_of(session, camera_id: str) -> list:
    rows = await session.execute(text(
        "SELECT id, name, type, polygon, max_headcount, dwell_threshold_seconds "
        "FROM zones WHERE camera_id = CAST(:c AS uuid) ORDER BY name"), {"c": camera_id})
    return rows.fetchall()


async def _open_events(session, zone_id: str) -> list[str]:
    out = []
    if (await session.execute(text(
            "SELECT 1 FROM intruder_events WHERE zone_id = CAST(:z AS uuid) AND resolved_at IS NULL "
            "LIMIT 1"), {"z": zone_id})).first():
        out.append("open intruder event")
    if (await session.execute(text(
            "SELECT 1 FROM headcount_breach_events WHERE zone_id = CAST(:z AS uuid) AND status = 'open' "
            "LIMIT 1"), {"z": zone_id})).first():
        out.append("open headcount breach")
    return out


async def build_plan(session, cfg: ConfigFile, registry_cameras: list[Camera], *,
                     prune: bool, retry_skipped: bool) -> Plan:
    plan = Plan()
    by_name: dict[str, list[Camera]] = {}
    for c in registry_cameras:
        by_name.setdefault(c.name, []).append(c)
    cam_name_by_id = {c.id: c.name for c in registry_cameras}
    known_names = ", ".join(sorted(repr(n) for n in by_name)) or "(none)"

    def camera(name: str) -> Camera | None:
        hits = by_name.get(name, [])
        if len(hits) == 1:
            return hits[0]
        if not hits:
            plan.errors.append((f"camera {name!r} is not registered; registered: {known_names}",
                                "fix the name in config.yaml or run scripts\\register_cameras.ps1"))
        else:
            plan.errors.append((f"camera name {name!r} is used by {len(hits)} cameras",
                                "give each camera a unique name in the platform"))
        return None

    # (camera name, zone name) -> zone id after this run
    final_zones: dict[tuple[str, str], str] = {}

    # ---- zones of the cameras in the file
    for cam_cfg in cfg.cameras:
        cam = camera(cam_cfg.name)
        if cam is None:
            continue
        rows = await _zones_of(session, cam.id)
        existing: dict[str, Any] = {}
        for row in rows:
            if row.name in existing:
                plan.errors.append((f"camera {cam.name!r} has two zones named {row.name!r} in the database",
                                    "delete one of them (or rename it) before applying"))
            existing[row.name] = row
        for z in cam_cfg.zones:
            label = f"{cam.name}/{z.name}"
            row = existing.get(z.name)
            want = {"type": z.type.value, "polygon": z.polygon, "max_headcount": z.max_headcount,
                    "dwell_threshold_seconds": z.dwell_threshold_seconds}
            if row is None:
                zid = str(uuid.uuid4())
                plan.zone_creates.append({"id": zid, "camera_id": cam.id, "name": z.name, **want})
                plan.touched_cameras.add(cam.id)
                plan.add("CREATE", f"zone {label} ({z.type.value})")
            else:
                zid = str(row.id)
                have = {"type": row.type, "polygon": _polygon_of(row.polygon),
                        "max_headcount": row.max_headcount,
                        "dwell_threshold_seconds": row.dwell_threshold_seconds}
                diff = [f"{k} {have[k]} -> {want[k]}" for k in want if have[k] != want[k]]
                if diff:
                    plan.zone_updates.append({"id": zid, **want})
                    plan.touched_cameras.add(cam.id)
                    plan.add("UPDATE", f"zone {label}: " + "; ".join(diff))
                else:
                    plan.add("UNCHANGED", f"zone {label}")
            final_zones[(cam.name, z.name)] = zid
        wanted = {z.name for z in cam_cfg.zones}
        for name, row in existing.items():
            if name in wanted:
                continue
            label = f"{cam.name}/{name}"
            if not prune:
                plan.add("KEEP", f"zone {label} is not in the file (--prune deletes it)")
                final_zones[(cam.name, name)] = str(row.id)
                continue
            blockers = await _open_events(session, str(row.id))
            if blockers:
                plan.warnings.append((f"zone {label} not pruned: {', '.join(blockers)}",
                                      "prune again after the event/breach is resolved"))
                plan.add("KEEP", f"zone {label}: {', '.join(blockers)}")
                final_zones[(cam.name, name)] = str(row.id)
                continue
            n = (await session.execute(text(
                "SELECT count(*) FROM zone_authorized_persons WHERE zone_id = CAST(:z AS uuid)"),
                {"z": str(row.id)})).scalar()
            plan.zone_deletes.append({"id": str(row.id), "label": label})
            plan.touched_cameras.add(cam.id)
            plan.add("DELETE", f"zone {label} and its {n} authorization(s)")

    listed = {c.name for c in cfg.cameras}
    deleted_ids = {d["id"] for d in plan.zone_deletes}

    async def zone_ref(ref: str) -> str | None:
        """'camera/zone' -> zone id after this run. Tries every '/' split."""
        hits = []
        for i, ch in enumerate(ref):
            if ch != "/":
                continue
            cname, zname = ref[:i], ref[i + 1:]
            if cname in listed:
                if (cname, zname) in final_zones:
                    hits.append(final_zones[(cname, zname)])
            elif len(by_name.get(cname, [])) == 1:
                rows = [row for row in await _zones_of(session, by_name[cname][0].id) if row.name == zname]
                if len(rows) == 1 and str(rows[0].id) not in deleted_ids:
                    hits.append(str(rows[0].id))
        if len(hits) == 1:
            return hits[0]
        plan.errors.append((f"authorized zone {ref!r} does not name exactly one zone",
                            "use 'camera name/zone name' exactly as in the file or the database"))
        return None

    async def zone_label(zone_id: str) -> str:
        row = (await session.execute(text(
            "SELECT camera_id, name FROM zones WHERE id = CAST(:z AS uuid)"), {"z": zone_id})).first()
        if row is None:
            return zone_id
        return f"{cam_name_by_id.get(str(row.camera_id), str(row.camera_id))}/{row.name}"

    # ---- people
    for p in cfg.people:
        rows = (await session.execute(text(
            "SELECT id, metadata FROM enrolled_persons WHERE name = :n"), {"n": p.name})).fetchall()
        if len(rows) > 1:
            plan.errors.append((f"{len(rows)} enrolled persons are named {p.name!r} in the database",
                                "merge or rename them before applying"))
            continue
        if rows:
            pid = str(rows[0].id)
            meta = rows[0].metadata or {}
            if isinstance(meta, str):
                meta = json.loads(meta)
            plan.add("UNCHANGED", f"person {p.name}")
        else:
            pid = str(uuid.uuid4())
            meta = {}
            plan.person_creates.append({"id": pid, "name": p.name})
            plan.add("CREATE", f"person {p.name}")

        # photos
        enrolled = meta.get("enrollment_images", {})
        skipped = meta.get("enrollment_skipped", {})
        if p.images_dir:
            d = Path(p.images_dir)
            if not d.is_dir():
                plan.errors.append((f"images_dir for {p.name!r} does not exist: {d}",
                                    "fix images_dir (use forward slashes: C:/innovision/enrollment/alice)"))
            else:
                photos = _images(d)
                if not photos:
                    plan.warnings.append((f"no photos in {d} for {p.name!r}",
                                          "add .jpg/.png photos with exactly one clear frontal face"))
                for photo in photos:
                    sha = _sha256(photo)
                    if sha in enrolled:
                        plan.add("UNCHANGED", f"photo {p.name}/{photo.name} (enrolled)")
                    elif sha in skipped and not retry_skipped:
                        plan.add("SKIP", f"photo {p.name}/{photo.name}: skipped before ({skipped[sha]}); "
                                 "--retry-skipped to try again")
                    else:
                        plan.enroll.append({"person_id": pid, "person": p.name, "path": photo, "sha": sha})
                        plan.add("ENROLL", f"photo {p.name}/{photo.name}")
        if not enrolled and not plan_has_enroll(plan, pid):
            plan.warnings.append((f"{p.name!r} has no enrolled photo: they are treated as unknown",
                                  "set images_dir to a folder with clear frontal photos"))

        # authorized zones: exact set
        target: dict[str, str] = {}
        for ref in p.authorized_zones:
            zid = await zone_ref(ref)
            if zid:
                target[zid] = ref
        current = set()
        if rows:
            current = {str(r.zone_id) for r in (await session.execute(text(
                "SELECT zone_id FROM zone_authorized_persons WHERE person_id = CAST(:p AS uuid)"),
                {"p": pid})).fetchall()}
        for zid, ref in target.items():
            if zid in current:
                plan.add("UNCHANGED", f"authorization {p.name} -> {ref}")
            else:
                plan.auth_add.append((pid, zid, ref))
                plan.add("AUTHORIZE", f"{p.name} -> {ref}")
        for zid in sorted(current - set(target)):
            label = await zone_label(zid)
            if zid in deleted_ids:
                continue  # removed with the pruned zone, already listed there
            plan.auth_remove.append((pid, zid, label))
            plan.add("REMOVE", f"authorization {p.name} -> {label}")

        # blocklist
        active = [] if not rows else (await session.execute(text(
            "SELECT reason FROM blocklist WHERE person_id = CAST(:p AS uuid) "
            "AND (expires_at IS NULL OR expires_at > now())"), {"p": pid})).scalars().all()
        ours = [a for a in active if a == BLOCKLIST_REASON]
        others = [a for a in active if a != BLOCKLIST_REASON]
        if p.blocklisted:
            if active:
                plan.add("UNCHANGED", f"blocklist {p.name}")
            else:
                plan.block_add.append(pid)
                plan.add("BLOCKLIST", p.name)
        else:
            if ours:
                plan.block_remove.append(pid)
                plan.add("UNBLOCKLIST", p.name)
            if others:
                plan.warnings.append((f"{p.name!r} stays blocklisted by entries not made by this tool "
                                      f"(reason: {', '.join(others)})",
                                      "remove those blocklist rows by hand if intended"))
    return plan


def plan_has_enroll(plan: Plan, person_id: str) -> bool:
    return any(e["person_id"] == person_id for e in plan.enroll)


# ---------------------------------------------------------------- apply


async def _write(session, plan: Plan, embeddings: dict[str, tuple], skipped: dict[str, str]) -> set[str]:
    """Execute the plan. Returns the person ids whose enrollment changed."""
    for z in plan.zone_creates:
        await session.execute(text(
            "INSERT INTO zones (id, camera_id, name, type, polygon, max_headcount, dwell_threshold_seconds) "
            "VALUES (CAST(:id AS uuid), CAST(:camera_id AS uuid), :name, :type, CAST(:polygon AS jsonb), "
            ":max_headcount, :dwell)"),
            {**z, "polygon": json.dumps(z["polygon"]), "dwell": z["dwell_threshold_seconds"]})
    for z in plan.zone_updates:
        await session.execute(text(
            "UPDATE zones SET type = :type, polygon = CAST(:polygon AS jsonb), max_headcount = :max_headcount, "
            "dwell_threshold_seconds = :dwell, updated_at = now() WHERE id = CAST(:id AS uuid)"),
            {**z, "polygon": json.dumps(z["polygon"]), "dwell": z["dwell_threshold_seconds"]})
    for z in plan.zone_deletes:
        await session.execute(text("DELETE FROM zone_authorized_persons WHERE zone_id = CAST(:z AS uuid)"),
                              {"z": z["id"]})
        await session.execute(text("DELETE FROM zones WHERE id = CAST(:z AS uuid)"), {"z": z["id"]})
    for p in plan.person_creates:
        await session.execute(text(
            "INSERT INTO enrolled_persons (id, name) VALUES (CAST(:id AS uuid), :name)"), p)

    changed: set[str] = set()
    for e in plan.enroll:
        pid, sha, name = e["person_id"], e["sha"], e["path"].name
        if sha in embeddings:
            emb, quality = embeddings[sha]
            eid = str(uuid.uuid4())
            await session.execute(text(
                "INSERT INTO face_embeddings (id, person_id, embedding, quality_score, is_enrollment) "
                "VALUES (CAST(:id AS uuid), CAST(:p AS uuid), CAST(:e AS vector), :q, true)"),
                {"id": eid, "p": pid, "e": "[" + ",".join(f"{float(v):.8f}" for v in emb) + "]",
                 "q": float(quality)})
            patch = {"enrollment_images": {sha: {"embedding_id": eid, "file": name}}}
            changed.add(pid)
        else:
            patch = {"enrollment_skipped": {sha: skipped[sha]}}
        await session.execute(text(
            "UPDATE enrolled_persons SET metadata = "
            "jsonb_set(jsonb_set(metadata, '{enrollment_images}', "
            "  COALESCE(metadata->'enrollment_images', '{}'::jsonb) || COALESCE(CAST(:p AS jsonb)->'enrollment_images', '{}'::jsonb)), "
            "  '{enrollment_skipped}', "
            "  (COALESCE(metadata->'enrollment_skipped', '{}'::jsonb) || COALESCE(CAST(:p AS jsonb)->'enrollment_skipped', '{}'::jsonb)) "
            "  - CAST(:done AS text[])) "
            "WHERE id = CAST(:id AS uuid)"),
            {"p": json.dumps(patch), "id": pid, "done": list(patch.get("enrollment_images", {}))})

    for pid, zid, _ in plan.auth_add:
        await session.execute(text(
            "INSERT INTO zone_authorized_persons (zone_id, person_id) VALUES (CAST(:z AS uuid), CAST(:p AS uuid)) "
            "ON CONFLICT DO NOTHING"), {"z": zid, "p": pid})
    for pid, zid, _ in plan.auth_remove:
        await session.execute(text(
            "DELETE FROM zone_authorized_persons WHERE zone_id = CAST(:z AS uuid) AND person_id = CAST(:p AS uuid)"),
            {"z": zid, "p": pid})
    for pid in plan.block_add:
        await session.execute(text(
            "INSERT INTO blocklist (person_id, reason) VALUES (CAST(:p AS uuid), :r)"),
            {"p": pid, "r": BLOCKLIST_REASON})
    for pid in plan.block_remove:
        await session.execute(text(
            "DELETE FROM blocklist WHERE person_id = CAST(:p AS uuid) AND reason = :r"),
            {"p": pid, "r": BLOCKLIST_REASON})
    return changed


def _read_image(path: Path):
    import cv2
    import numpy as np

    data = np.fromfile(str(path), dtype=np.uint8)  # handles non-ASCII Windows paths
    return cv2.imdecode(data, cv2.IMREAD_COLOR) if data.size else None


class _Abort(Exception):
    """Leave the transaction without writing (dry run or a FAIL in the plan)."""


async def run(cfg: ConfigFile, registry_cameras: list[Camera], session_factory, redis, r: Reporter, *,
              dry_run: bool, prune: bool = False, retry_skipped: bool = False, enroller=None) -> Plan | None:
    try:
        return await _run(cfg, registry_cameras, session_factory, redis, r, dry_run=dry_run,
                          prune=prune, retry_skipped=retry_skipped, enroller=enroller)
    except _Abort as stop:
        return stop.args[0]


async def _run(cfg, registry_cameras, session_factory, redis, r, *, dry_run, prune, retry_skipped,
               enroller) -> Plan:
    async with session_factory() as session:
        async with session.begin():
            await session.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": LOCK_KEY})
            plan = await build_plan(session, cfg, registry_cameras, prune=prune, retry_skipped=retry_skipped)

            r.info("PLAN" + (" (dry run, nothing is written)" if dry_run else ""))
            for verb, what in plan.lines:
                r.info(f"  {verb:<11} {what}")
            for what, fix in plan.warnings:
                r.warn("plan", what, fix)
            for what, fix in plan.errors:
                r.fail("config", what, fix)
            if plan.errors:
                r.info("Nothing was written.")
                raise _Abort(plan)
            if dry_run:
                r.ok("dry-run", f"{plan.changes} change(s) planned, nothing written")
                raise _Abort(plan)

            embeddings: dict[str, tuple] = {}
            skipped: dict[str, str] = {}
            if plan.enroll:
                if enroller is None:
                    from tools.config.enrollment import ServiceFaceEnroller

                    enroller = ServiceFaceEnroller()
                    r.info("Loading the face model (first photo only; this takes a while) ...")
                for e in plan.enroll:
                    image = await asyncio.to_thread(_read_image, e["path"])
                    if image is None:
                        res_reason, res = "not a readable image", None
                    else:
                        res = await asyncio.to_thread(enroller.embed, image)
                        res_reason = res.reason
                    if res is not None and res.embedding is not None:
                        embeddings[e["sha"]] = (res.embedding, res.quality_score)
                        r.ok("enroll", f"{e['person']}/{e['path'].name} (quality {res.quality_score:.2f})")
                    else:
                        skipped[e["sha"]] = res_reason or "rejected"
                        r.warn("enroll", f"{e['person']}/{e['path'].name} skipped: {skipped[e['sha']]}",
                               "use a sharp, well-lit, frontal photo with exactly one face")
            changed_people = await _write(session, plan, embeddings, skipped)

    # after COMMIT: tell the running services
    for cam_id in sorted(plan.touched_cameras):
        from services.event_processing.src.workers.zone_monitor.zone_store import publish_zone_invalidation

        await publish_zone_invalidation(redis, cam_id)
    for pid in sorted(changed_people):
        await redis.publish(ENROLL_INVALIDATE_CHANNEL, pid)
    r.ok("applied", f"{plan.changes} change(s); invalidated {len(plan.touched_cameras)} camera zone "
         f"cache(s), {len(changed_people)} enrollment(s)")
    return plan


async def _main(args) -> int:
    import redis.asyncio as aioredis
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from shared.config import settings

    r = Reporter()
    try:
        cfg = load_config(args.config)
    except ValueError as exc:
        for line in str(exc).splitlines():
            r.fail("config.yaml", line, "fix config.yaml (see tools/config/config.example.yaml)")
        return r.summary()
    r.ok("config.yaml", f"{len(cfg.cameras)} camera(s), {sum(len(c.zones) for c in cfg.cameras)} zone(s), "
         f"{len(cfg.people)} person(s)")
    env = load_env()
    try:
        cameras = RegistryClient.from_env(env).active_cameras()
    except RegistryError as exc:
        r.fail("registry", str(exc), exc.fix)
        return r.summary()
    engine = create_async_engine(env.get("DATABASE_URL", settings.DATABASE_URL))
    redis = aioredis.from_url(settings.redis_url())
    try:
        await run(cfg, cameras, async_sessionmaker(engine, expire_on_commit=False), redis, r,
                  dry_run=args.dry_run, prune=args.prune, retry_skipped=args.retry_skipped)
    finally:
        await redis.aclose()
        await engine.dispose()
    return r.summary()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("config", help="config.yaml")
    ap.add_argument("--dry-run", action="store_true", help="print the plan, write nothing")
    ap.add_argument("--prune", action="store_true",
                    help="delete zones of the listed cameras that are not in the file")
    ap.add_argument("--retry-skipped", action="store_true", help="retry photos skipped on earlier runs")
    return asyncio.run(_main(ap.parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())

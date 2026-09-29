"""Stream health for this use case (CLAUDE.md sections 5 and 7).

    python tools/check_health.py [--max-pending-age 60] [--redis-url redis://...]

Per stream (frames:*, events:*, alerts:live) and consumer group: length,
pending count, age of the oldest pending entry, lag; plus every {stream}:dlq
length. Exit 1 when a group has a pending entry older than --max-pending-age
seconds or a DLQ is not empty (use --allow-dlq N for deliberately injected
bad messages).
"""
import argparse
import asyncio
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

if __package__ in (None, ""):  # run as a script: make `shared` importable
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

FIXED_STREAMS = ("events:detections", "events:zone", "events:recognitions", "alerts:live")


def _s(v) -> str:
    return v.decode() if isinstance(v, bytes) else str(v)


def _id_ms(entry_id) -> int:
    return int(_s(entry_id).split("-")[0])


@dataclass
class GroupHealth:
    stream: str
    group: str
    pending: int
    oldest_pending_age_s: float | None
    lag: int | None


@dataclass
class Report:
    streams: dict[str, int] = field(default_factory=dict)
    groups: list[GroupHealth] = field(default_factory=list)
    dlq: dict[str, int] = field(default_factory=dict)

    def problems(self, max_pending_age_s: float, allow_dlq: int = 0) -> list[str]:
        out = [
            f"{g.stream}/{g.group}: oldest pending is {g.oldest_pending_age_s:.0f}s old "
            f"(> {max_pending_age_s:.0f}s), {g.pending} pending"
            for g in self.groups
            if g.oldest_pending_age_s is not None and g.oldest_pending_age_s > max_pending_age_s
        ]
        total = sum(self.dlq.values())
        if total > allow_dlq:
            out.append(f"{total} DLQ entries (allowed {allow_dlq}): {self.dlq}")
        return out


async def _keys(redis, pattern: str) -> list[str]:
    return sorted([_s(k) async for k in redis.scan_iter(match=pattern, _type="stream")])


async def collect(redis, now_ms: int | None = None) -> Report:
    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    frame_streams = [k for k in await _keys(redis, "frames:*") if not k.endswith(":dlq")]
    report = Report()
    for stream in (*frame_streams, *FIXED_STREAMS):
        if not await redis.exists(stream):
            continue
        report.streams[stream] = await redis.xlen(stream)
        for g in await redis.xinfo_groups(stream):
            name = _s(g["name"])
            pending = int(g["pending"])
            age = None
            if pending:
                summary = await redis.xpending(stream, name)
                age = max(0.0, (now_ms - _id_ms(summary["min"])) / 1000.0)
            lag = g.get("lag")
            report.groups.append(GroupHealth(stream, name, pending, age,
                                             None if lag is None else int(lag)))
    for dlq in await _keys(redis, "*:dlq"):
        report.dlq[dlq] = await redis.xlen(dlq)
    return report


def render(report: Report) -> str:
    lines = [f"{'stream':<52}{'len':>8}"]
    lines += [f"{s:<52}{n:>8}" for s, n in report.streams.items()]
    lines.append("")
    lines.append(f"{'stream/group':<60}{'pending':>9}{'oldest_s':>10}{'lag':>8}")
    for g in report.groups:
        age = "-" if g.oldest_pending_age_s is None else f"{g.oldest_pending_age_s:.0f}"
        lag = "-" if g.lag is None else str(g.lag)
        lines.append(f"{g.stream + '/' + g.group:<60}{g.pending:>9}{age:>10}{lag:>8}")
    lines.append("")
    lines.append("dlq: " + (", ".join(f"{k}={v}" for k, v in report.dlq.items()) or "none"))
    return "\n".join(lines)


async def run(args) -> int:
    import redis.asyncio as aioredis

    from shared.config import settings

    redis = aioredis.from_url(args.redis_url or settings.redis_url())
    try:
        report = await collect(redis)
    finally:
        await redis.aclose()
    print(render(report))
    problems = report.problems(args.max_pending_age, args.allow_dlq)
    for p in problems:
        print(f"UNHEALTHY: {p}")
    print("OK" if not problems else f"{len(problems)} problem(s)")
    return 1 if problems else 0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--max-pending-age", type=float, default=60.0)
    ap.add_argument("--allow-dlq", type=int, default=0)
    ap.add_argument("--redis-url", help="default: REDIS_HOST/PORT/DB from env")
    args = ap.parse_args()
    raise SystemExit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()

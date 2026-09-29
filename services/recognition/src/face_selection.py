"""
Which detected face belongs to which track.

The crop sent to the model is the top part of the person box (full width),
so with overlapping people it routinely contains SOMEONE ELSE's face as
well. Picking the highest det_score face then records that other person's
identity on this track (identity swap). Instead every detected face is
assigned to at most one track, and a track only ever uses the face assigned
to it:

  1. A face is a candidate for a track only if its center lies inside that
     track's estimated head region (see `head_region`).
  2. Among the candidate tracks the face goes to the one whose person-box
     top-center is nearest (pixels). An exact tie assigns it to nobody.
  3. If two or more faces end up assigned to the same track, the nearest one
     wins, unless the runner-up is within `ambiguity_ratio` of it: then the
     track gets no face at all (no guess).

All tracks of the same DetectionEvent take part, so a face that sits in the
head region of a neighbour is only kept by the track whose head it is
closest to. Overlapping head regions (people side by side) do not leave
both tracks without a face.
"""
import math
from collections.abc import Sequence
from dataclasses import dataclass

from shared.schemas.common import BoundingBox, TrackResult


@dataclass(frozen=True)
class HeadRegionConfig:
    # Central fraction of the person-box width the head can be in.
    width_frac: float = 0.7
    # How far above the box top (fraction of box height) still counts.
    top_margin_frac: float = 0.05
    # How far below the box top (fraction of box height) the head reaches.
    height_frac: float = 0.30
    # Two faces for one track are ambiguous if d2 <= ratio * d1.
    ambiguity_ratio: float = 1.25


@dataclass(frozen=True)
class FaceSelection:
    index: int | None
    reason: str  # "selected" | "no_face" | "no_face_in_head_region" | "ambiguous"


def head_region(
    bbox: BoundingBox, frame_w: int, frame_h: int, cfg: HeadRegionConfig
) -> tuple[float, float, float, float]:
    """Estimated head region of a person box, in frame pixels (x1, y1, x2, y2)."""
    x1, y1 = bbox.x1 * frame_w, bbox.y1 * frame_h
    x2, y2 = bbox.x2 * frame_w, bbox.y2 * frame_h
    w, h = x2 - x1, y2 - y1
    cx = (x1 + x2) / 2
    half = w * cfg.width_frac / 2
    return (cx - half, y1 - h * cfg.top_margin_frac, cx + half, y1 + h * cfg.height_frac)


def top_center(bbox: BoundingBox, frame_w: int, frame_h: int) -> tuple[float, float]:
    return ((bbox.x1 + bbox.x2) / 2 * frame_w, bbox.y1 * frame_h)


def _inside(point: tuple[float, float], region: tuple[float, float, float, float]) -> bool:
    x, y = point
    return region[0] <= x <= region[2] and region[1] <= y <= region[3]


def _owner(
    center: tuple[float, float],
    tracks: Sequence[TrackResult],
    frame_w: int,
    frame_h: int,
    cfg: HeadRegionConfig,
) -> int | None:
    """track_id the face at `center` is assigned to, or None."""
    candidates: list[tuple[float, int]] = []
    for t in tracks:
        if _inside(center, head_region(t.bbox, frame_w, frame_h, cfg)):
            candidates.append((math.dist(center, top_center(t.bbox, frame_w, frame_h)), t.track_id))
    if not candidates:
        return None
    candidates.sort()
    if len(candidates) > 1 and candidates[1][0] == candidates[0][0]:
        return None  # exact tie: nobody can claim it
    return candidates[0][1]


def select_face(
    face_centers: Sequence[tuple[float, float]],
    track: TrackResult,
    all_tracks: Sequence[TrackResult],
    frame_w: int,
    frame_h: int,
    cfg: HeadRegionConfig,
) -> FaceSelection:
    """
    Pick the face (index into `face_centers`, frame-pixel centers) that
    belongs to `track`, given every track of the same frame. `all_tracks`
    may or may not contain `track`; it is always considered.
    """
    if not face_centers:
        return FaceSelection(None, "no_face")

    tracks = [t for t in all_tracks if t.track_id != track.track_id] + [track]
    anchor = top_center(track.bbox, frame_w, frame_h)
    mine = sorted(
        (math.dist(c, anchor), i)
        for i, c in enumerate(face_centers)
        if _owner(c, tracks, frame_w, frame_h, cfg) == track.track_id
    )
    if not mine:
        return FaceSelection(None, "no_face_in_head_region")
    if len(mine) > 1 and mine[1][0] <= cfg.ambiguity_ratio * mine[0][0]:
        return FaceSelection(None, "ambiguous")
    return FaceSelection(mine[0][1], "selected")

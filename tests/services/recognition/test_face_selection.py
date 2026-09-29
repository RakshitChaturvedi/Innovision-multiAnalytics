"""Face -> track assignment (pure geometry, no model)."""
from services.recognition.src.face_selection import (
    HeadRegionConfig,
    head_region,
    select_face,
)
from shared.schemas.common import BoundingBox, TrackResult

W, H = 1000, 1000
CFG = HeadRegionConfig()


def track(track_id, x1, y1, x2, y2):
    box = BoundingBox(x1=x1, y1=y1, x2=x2, y2=y2)
    return TrackResult(
        track_id=track_id, bbox=box, confidence=0.9, class_label="person",
        has_face=True, face_bbox=box,
    )


def px(x, y):
    return (x * W, y * H)


def test_head_region_is_central_top_part_of_box():
    x1, y1, x2, y2 = head_region(BoundingBox(x1=0.2, y1=0.1, x2=0.4, y2=0.9), W, H, CFG)
    assert (round(x1), round(x2)) == (230, 370)  # central 70% of 200 px
    assert (round(y1), round(y2)) == (60, 340)  # 5% above, 30% below the top


def test_own_face_is_selected():
    a = track(1, 0.2, 0.1, 0.4, 0.9)
    sel = select_face([px(0.30, 0.16)], a, [a], W, H, CFG)
    assert sel.index == 0 and sel.reason == "selected"


def test_neighbours_face_in_crop_is_not_used():
    """Only the neighbour's face is in A's crop (A looks away): A gets nothing."""
    a = track(1, 0.20, 0.20, 0.55, 0.95)
    b = track(2, 0.45, 0.10, 0.80, 0.90)
    b_face = px(0.53, 0.20)  # inside A's crop (full-width top part), B's head
    sel = select_face([b_face], a, [a, b], W, H, CFG)
    assert sel.index is None and sel.reason == "no_face_in_head_region"
    assert select_face([b_face], b, [a, b], W, H, CFG).index == 0


def test_side_by_side_overlapping_heads_each_keep_their_own_face():
    a = track(1, 0.30, 0.10, 0.60, 0.90)  # head x 0.345..0.555
    b = track(2, 0.45, 0.10, 0.75, 0.90)  # head x 0.495..0.705
    a_face, b_face = px(0.43, 0.15), px(0.53, 0.15)  # b_face is in BOTH heads
    faces = [b_face, a_face]  # order must not matter
    assert select_face(faces, a, [a, b], W, H, CFG).index == 1
    assert select_face(faces, b, [a, b], W, H, CFG).index == 0


def test_face_in_overlap_goes_to_nearest_top_center_only():
    a = track(1, 0.30, 0.10, 0.60, 0.90)
    b = track(2, 0.45, 0.10, 0.75, 0.90)
    shared = px(0.53, 0.15)  # 0.08 from A's top-center, 0.07 from B's
    assert select_face([shared], a, [a, b], W, H, CFG).index is None
    assert select_face([shared], b, [a, b], W, H, CFG).index == 0


def test_exact_tie_between_tracks_assigns_to_nobody():
    # binary-exact coordinates: both top-centers are exactly 62.5 px away
    a = track(1, 0.25, 0.125, 0.5, 0.875)
    b = track(2, 0.375, 0.125, 0.625, 0.875)
    middle = px(0.4375, 0.125)
    assert select_face([middle], a, [a, b], W, H, CFG).index is None
    assert select_face([middle], b, [a, b], W, H, CFG).index is None


def test_two_similar_faces_for_one_track_are_ambiguous():
    a = track(1, 0.2, 0.1, 0.6, 0.9)  # top-center (0.40, 0.10)
    near, also_near = px(0.40, 0.20), px(0.45, 0.20)  # d = 0.100 vs 0.112
    sel = select_face([near, also_near], a, [a], W, H, CFG)
    assert sel.index is None and sel.reason == "ambiguous"


def test_clearly_nearer_face_wins_for_one_track():
    a = track(1, 0.2, 0.1, 0.6, 0.9)
    near, far = px(0.40, 0.14), px(0.52, 0.30)  # d = 0.04 vs 0.23
    assert select_face([far, near], a, [a], W, H, CFG).index == 1


def test_face_outside_every_head_region_is_ignored():
    a = track(1, 0.2, 0.1, 0.6, 0.9)
    torso = px(0.40, 0.60)
    sel = select_face([torso], a, [a], W, H, CFG)
    assert sel.index is None and sel.reason == "no_face_in_head_region"


def test_no_faces():
    a = track(1, 0.2, 0.1, 0.6, 0.9)
    assert select_face([], a, [a], W, H, CFG).reason == "no_face"

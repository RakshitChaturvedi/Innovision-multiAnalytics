"""Print blur scores for the synthetic calibration set.

    python -m tools.calibrate_blur [--threshold 15] [--eval-size 112] [--image face.jpg ...]

Shows, per case, the crop size, the old raw-crop score (Laplacian variance of
the crop as-is, used before 0009_blur_recalibration) and the current score
(services/recognition/src/quality_gate.py::blur_score), and whether the
current score passes the threshold. --image scores your own face crops too,
which is the way to tune DEFAULT_BLUR_THRESHOLD for a real camera.
"""
import argparse

import cv2

from services.recognition.src.config import config
from services.recognition.src.quality_gate import blur_score
from tools.blur_calibration import cases, platform_cases


def _old_score(crop) -> float:
    return float(cv2.Laplacian(cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY), cv2.CV_64F).var())


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--threshold", type=float, default=config.DEFAULT_BLUR_THRESHOLD)
    ap.add_argument("--eval-size", type=int, default=config.BLUR_EVAL_SIZE)
    ap.add_argument("--image", nargs="*", default=[], help="extra face crops to score")
    args = ap.parse_args()

    rows = [(c.name, c.crop) for c in cases() + platform_cases()]
    for path in args.image:
        img = cv2.imread(path, cv2.IMREAD_COLOR)
        if img is None:
            raise SystemExit(f"cannot read {path}")
        rows.append((path, img))

    print(f"threshold={args.threshold:g} eval_size={args.eval_size}")
    print(f"{'case':42s} {'crop':>9s} {'old raw':>9s} {'score':>7s}  result")
    for name, crop in rows:
        score = blur_score(crop, args.eval_size)
        h, w = crop.shape[:2]
        verdict = "pass" if score >= args.threshold else "REJECT"
        print(f"{name:42s} {w:>4d}x{h:<4d} {_old_score(crop):9.1f} {score:7.1f}  {verdict}")


if __name__ == "__main__":
    main()

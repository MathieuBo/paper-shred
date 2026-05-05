#!/usr/bin/env python3
"""Classify marker and docling images as scientific figures vs page decorations.

Both extractors emit images for every visual element they detect, including
page-running headers, journal logos, and tiny banner decorations. None of those
are scientific figures. This script inspects each image's dimensions and writes
a JSON audit so the skill can avoid copying decorations into figures/.

Heuristics (all must pass to be kept):
  - min(width, height) >= MIN_DIM (default 200 px)
  - max(w,h)/min(w,h) <= MAX_ASPECT (default 5)
  - width * height >= MIN_AREA (default 50,000 px^2)

Decoration patterns these catch in practice:
  - 822x90 page-banner strips (aspect ~9:1)
  - 25x29 partner-logo thumbnails (small dim + small area)
  - 416x92 logo blocks (aspect ~4.5:1 — borderline; tune if needed)

Usage:
    filter_figures.py <WORK_DIR>

where WORK_DIR is the _raw/ directory that extract.sh populated. The script
auto-discovers marker_out/<stem>/*.jpeg and docling_out/docling_pictures/*.png.

Writes <WORK_DIR>/figure_audit.json. Does NOT delete files — Claude reads the
audit and decides which to copy into figures/.
"""

import json
import sys
from pathlib import Path

from PIL import Image

MIN_DIM = 200
MAX_ASPECT = 5.0
MIN_AREA = 50_000


def classify(path: Path) -> dict:
    try:
        with Image.open(path) as im:
            w, h = im.size
    except Exception as e:
        return {"path": str(path), "error": str(e), "kept": False, "reason": "open_failed"}

    area = w * h
    aspect = max(w, h) / max(min(w, h), 1)

    reasons = []
    if min(w, h) < MIN_DIM:
        reasons.append(f"min_dim<{MIN_DIM}")
    if aspect > MAX_ASPECT:
        reasons.append(f"aspect>{MAX_ASPECT}")
    if area < MIN_AREA:
        reasons.append(f"area<{MIN_AREA}")

    return {
        "path": str(path),
        "w": w,
        "h": h,
        "area": area,
        "aspect": round(aspect, 2),
        "kept": not reasons,
        "reason": ",".join(reasons) if reasons else None,
    }


def collect_images(work_dir: Path) -> list[Path]:
    images: list[Path] = []
    marker_root = work_dir / "marker_out"
    if marker_root.is_dir():
        for sub in marker_root.iterdir():
            if sub.is_dir():
                images.extend(sorted(sub.glob("*.jpeg")))
                images.extend(sorted(sub.glob("*.jpg")))
                images.extend(sorted(sub.glob("*.png")))
    docling_pics = work_dir / "docling_out" / "docling_pictures"
    if docling_pics.is_dir():
        images.extend(sorted(docling_pics.glob("*.png")))
        images.extend(sorted(docling_pics.glob("*.jpeg")))
        images.extend(sorted(docling_pics.glob("*.jpg")))
    return images


def main():
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <WORK_DIR>", file=sys.stderr)
        sys.exit(2)

    work_dir = Path(sys.argv[1]).resolve()
    if not work_dir.is_dir():
        print(f"ERROR: {work_dir} is not a directory", file=sys.stderr)
        sys.exit(1)

    images = collect_images(work_dir)
    if not images:
        print("filter_figures: no images found", file=sys.stderr)
        (work_dir / "figure_audit.json").write_text(json.dumps({"images": [], "kept": 0, "dropped": 0}, indent=2))
        return

    results = [classify(p) for p in images]
    kept = [r for r in results if r["kept"]]
    dropped = [r for r in results if not r["kept"]]

    audit = {
        "thresholds": {"min_dim": MIN_DIM, "max_aspect": MAX_ASPECT, "min_area": MIN_AREA},
        "n_total": len(results),
        "n_kept": len(kept),
        "n_dropped": len(dropped),
        "images": results,
    }
    audit_path = work_dir / "figure_audit.json"
    audit_path.write_text(json.dumps(audit, indent=2))

    print(
        f"filter_figures: {len(kept)} kept / {len(dropped)} dropped of {len(results)} images. "
        f"Audit: {audit_path}",
        file=sys.stderr,
    )
    if dropped:
        # Show drop reasons summary
        reason_counts: dict[str, int] = {}
        for d in dropped:
            reason_counts[d.get("reason", "unknown")] = reason_counts.get(d.get("reason", "unknown"), 0) + 1
        for reason, n in sorted(reason_counts.items(), key=lambda kv: -kv[1]):
            print(f"  dropped ({n}): {reason}", file=sys.stderr)


if __name__ == "__main__":
    main()

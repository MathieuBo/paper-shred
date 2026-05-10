#!/usr/bin/env python3
"""Classify marker and docling images as scientific figures vs page decorations.

Both extractors emit images for every visual element they detect, including
page-running headers, journal logos, and tiny banner decorations. None of those
are scientific figures. This script inspects each image's dimensions and writes
a JSON audit so the skill can avoid copying decorations into figures/.

Heuristics (all must pass to be kept):
  - min(width, height) >= MIN_DIM (default 300 px)
  - max(w,h)/min(w,h) <= MAX_ASPECT (default 5)
  - width * height >= MIN_AREA (default 100,000 px^2)

Decoration patterns these catch in practice:
  - 822x90 page-banner strips (aspect ~9:1)
  - 25x29 partner-logo thumbnails (small dim + small area)
  - 416x92 logo blocks (aspect ~4.5:1 — borderline; tune if needed)
  - Sub-panel fragments from dense Nature/Cell figure layouts

Optional caption-presence gate (--require-caption <cleaned.md>): drops kept
images whose page (extracted from marker's `_page_<N>_Figure_<M>.jpeg`
filename) has no `Figure N` caption pattern in the cleaned markdown. Useful
on Nature/Cell papers where review figures dump dozens of un-captioned panel
fragments alongside the real figures.

Usage:
    filter_figures.py <WORK_DIR> [--require-caption <cleaned.md>]

where WORK_DIR is the _raw/ directory that extract.sh populated. The script
auto-discovers marker_out/<stem>/*.jpeg and docling_out/docling_pictures/*.png.

Writes <WORK_DIR>/figure_audit.json. Does NOT delete files — Claude reads the
audit and decides which to copy into figures/.
"""

import json
import re
import sys
from pathlib import Path

from PIL import Image

MIN_DIM = 300
MAX_ASPECT = 5.0
MIN_AREA = 100_000

# Match a *caption-style* line: `Figure 1.`, `**Figure 1.**`, `### Figure 1.`,
# anchored to start-of-line. Inline mentions like `(Figure 3A)` are excluded
# on purpose — they aren't captions, they're cross-references.
FIG_CAPTION_RE = re.compile(
    r"^(?:#{1,6}\s+)?\**\s*(?:Figure|Fig\.?|FIGURE)\s+(\d+)\b\s*[.|:](?!\d)",
    re.MULTILINE,
)
# Pull the page number from marker's filename: `_page_3_Figure_2.jpeg`
PAGE_FROM_NAME = re.compile(r"_page_(\d+)_(?:Figure|Picture)_\d+", re.IGNORECASE)


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


def has_caption_near(cleaned_md: Path, image_filename: str, window: int = 20) -> bool | None:
    """Check if cleaned.md has a `Figure N` caption within ±`window` lines of
    the image marker `![](image_filename)`.

    Returns:
        True/False if the marker is found and we can decide.
        None if the marker isn't in cleaned.md (e.g. docling-only image) — caller
        should keep that image (don't second-guess).
    """
    if not cleaned_md.is_file():
        return None
    text = cleaned_md.read_text()
    needle = f"]({image_filename}"
    pos = text.find(needle)
    if pos == -1:
        # Try a partial match by basename without extension
        stem = image_filename.rsplit(".", 1)[0]
        pos = text.find(stem)
        if pos == -1:
            return None
    # Compute the line number of pos
    line_no = text.count("\n", 0, pos)
    lines = text.splitlines()
    lo = max(0, line_no - window)
    hi = min(len(lines), line_no + window + 1)
    chunk = "\n".join(lines[lo:hi])
    return bool(FIG_CAPTION_RE.search(chunk))


def main():
    args = sys.argv[1:]
    require_caption: Path | None = None
    if "--require-caption" in args:
        i = args.index("--require-caption")
        if i + 1 >= len(args):
            print("ERROR: --require-caption requires a path argument", file=sys.stderr)
            sys.exit(2)
        require_caption = Path(args[i + 1])
        del args[i:i + 2]

    if len(args) != 1:
        print(f"Usage: {sys.argv[0]} <WORK_DIR> [--require-caption <cleaned.md>]", file=sys.stderr)
        sys.exit(2)

    work_dir = Path(args[0]).resolve()
    if not work_dir.is_dir():
        print(f"ERROR: {work_dir} is not a directory", file=sys.stderr)
        sys.exit(1)

    images = collect_images(work_dir)
    if not images:
        print("filter_figures: no images found", file=sys.stderr)
        (work_dir / "figure_audit.json").write_text(json.dumps({"images": [], "kept": 0, "dropped": 0}, indent=2))
        return

    results = [classify(p) for p in images]

    # Optional second-pass: drop kept images whose markdown reference in
    # cleaned.md has no `Figure N` caption within ±20 lines. Images that don't
    # appear in cleaned.md (e.g. docling-only) are left alone.
    if require_caption:
        for r in results:
            if not r.get("kept"):
                continue
            fname = Path(r["path"]).name
            verdict = has_caption_near(require_caption, fname)
            if verdict is False:
                r["kept"] = False
                existing = r.get("reason") or ""
                r["reason"] = f"{existing},no_caption" if existing else "no_caption"

    kept = [r for r in results if r["kept"]]
    dropped = [r for r in results if not r["kept"]]

    audit = {
        "thresholds": {"min_dim": MIN_DIM, "max_aspect": MAX_ASPECT, "min_area": MIN_AREA},
        "require_caption": str(require_caption) if require_caption else None,
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

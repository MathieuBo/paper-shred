#!/usr/bin/env python3
"""Run docling on a PDF and emit structural metadata.

Docling is run alongside marker-pdf in the paper-shred pipeline. We use
docling specifically for:
- Document title detection (marker rarely finds it)
- Heading scaffold (marker's heading hierarchy is inconsistent)
- Figure/table count cross-check

Body text and citations come from marker, NOT from this output.

Usage:
    extract_docling.py INPUT.pdf OUT_DIR
"""

import json
import re
import sys
import time
from pathlib import Path

from docling.datamodel.base_models import InputFormat
from docling.datamodel.pipeline_options import PdfPipelineOptions
from docling.document_converter import DocumentConverter, PdfFormatOption


def detect_title(doc, md: str) -> str | None:
    """Find the document title.

    Strategy:
    1. First heading (any level) that is reasonably title-like (3-25 words,
       no trailing period, not a section keyword).
    2. Else first non-empty line if it looks title-like.
    """
    section_keywords = {
        "abstract", "introduction", "methods", "results", "discussion",
        "references", "bibliography", "conclusion", "acknowledgements",
        "vision", "approach", "summary",
    }

    lines = md.splitlines()
    for line in lines:
        m = re.match(r"^#{1,6}\s+(.+?)\s*$", line)
        if not m:
            continue
        candidate = m.group(1).strip()
        words = candidate.split()
        if not (3 <= len(words) <= 25):
            continue
        if candidate.rstrip(".").lower() in section_keywords:
            continue
        if candidate.endswith("."):
            continue
        return candidate

    # Fallback: first non-empty non-heading line, if title-shaped
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("|"):
            continue
        words = line.split()
        if 3 <= len(words) <= 25 and not line.endswith("."):
            return line
        break
    return None


def extract_headings(md: str) -> list[dict]:
    out = []
    for line in md.splitlines():
        m = re.match(r"^(#{1,6})\s+(.+?)\s*$", line)
        if m:
            out.append({"level": len(m.group(1)), "text": m.group(2).strip()})
    return out


def main():
    if len(sys.argv) != 3:
        print(f"Usage: {sys.argv[0]} INPUT.pdf OUT_DIR", file=sys.stderr)
        sys.exit(2)

    pdf_path = Path(sys.argv[1])
    out_dir = Path(sys.argv[2])
    out_dir.mkdir(parents=True, exist_ok=True)

    if not pdf_path.is_file():
        print(f"ERROR: not a file: {pdf_path}", file=sys.stderr)
        sys.exit(1)

    opts = PdfPipelineOptions()
    opts.generate_picture_images = True
    opts.images_scale = 2.0

    converter = DocumentConverter(
        format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=opts)},
    )

    t0 = time.time()
    result = converter.convert(str(pdf_path))
    dt = time.time() - t0
    doc = result.document
    md = doc.export_to_markdown()

    # Save raw docling outputs for cross-check
    (out_dir / "docling.md").write_text(md)

    # Save figures as candidates (marker remains primary)
    pics_dir = out_dir / "docling_pictures"
    pics_dir.mkdir(exist_ok=True)
    saved_pics = []
    for i, pic in enumerate(doc.pictures, 1):
        img = pic.get_image(doc)
        if img is not None:
            p = pics_dir / f"figure_{i:02d}.png"
            img.save(p)
            saved_pics.append(p.name)

    title = detect_title(doc, md)
    headings = extract_headings(md)

    meta = {
        "title": title,
        "headings": headings,
        "n_pages": len(doc.pages),
        "n_pictures": len(doc.pictures),
        "n_tables": len(doc.tables),
        "n_texts": len(doc.texts),
        "saved_pictures": saved_pics,
        "runtime_seconds": round(dt, 1),
    }
    (out_dir / "docling_meta.json").write_text(json.dumps(meta, indent=2))

    # stdout = key=value paths for the calling shell script
    print(f"docling_md={out_dir / 'docling.md'}")
    print(f"docling_meta={out_dir / 'docling_meta.json'}")
    print(f"docling_pics={pics_dir}")
    print(f"docling_title={title or ''}")
    print(f"docling_runtime={dt:.1f}", file=sys.stderr)


if __name__ == "__main__":
    main()

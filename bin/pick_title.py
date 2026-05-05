#!/usr/bin/env python3
"""Recommend a document title for paper-shred meta.json.

Docling's title heuristic is unreliable on form-shaped documents (grants,
EoIs, application forms): it tends to pick the largest-text element on the
first page, which is often a section sub-heading or the form name rather
than the project title.

Strategy:
  1. Try to find an explicit `Project title` / `Project name` field in the body
     (cleaned markdown). This is a high-confidence signal that beats docling
     even when docling's output looks plausible.
  2. If no body field is found, accept docling's title — unless it matches a
     rejection pattern (section prefix, length bounds, all-caps banner shape).
  3. Last resort: filename stem with underscores → spaces.

Body field patterns recognised:
  - Markdown heading + sub-heading: `## **2. Project Title**` then
    `#### **2.1 Title (max N characters)**` then a bolded line of content.
  - Table row: `| Project title | <project title> |`.
  - Plain heading or label `Project Title` / `Project name` /
    `Title (max N characters)` followed by the actual title.

Output: JSON to stdout with fields:
  {
    "recommended": "<chosen title>",
    "source": "body_table | body_heading | docling | filename",
    "rejected_docling": "<original docling title or null>",
    "rejection_reason": "<why or null>",
    "warnings": [...]
  }

Usage:
    pick_title.py <docling_meta.json> <cleaned.md> <source_pdf_path>
"""

import json
import re
import sys
from pathlib import Path


REJECT_PREFIXES = re.compile(
    r"^(Judging Criterion|Section|Question|Annex|Part|Chapter|Appendix)\s+\d",
    re.IGNORECASE,
)


def reject_docling(title: str | None) -> str | None:
    """Return rejection reason, or None if the title is acceptable."""
    if not title or not title.strip():
        return "empty"
    t = title.strip()
    if REJECT_PREFIXES.match(t):
        return "matches section-prefix pattern"
    if len(t) < 15:
        return f"too short ({len(t)} chars)"
    if len(t) > 200:
        return f"too long ({len(t)} chars)"
    if t.isupper() and len(t) < 30:
        return "all-caps banner shape"
    return None


# Match a heading-like line that labels a project-title field.
# Catches:
#   ## **2. Project Title**
#   #### **2.1 Title (max 150 characters)**
#   ### Project name
#   Project Title
HEADING_LABEL = re.compile(
    r"^(?:#{1,6}\s+)?\**\s*(?:\d+(?:\.\d+)?\.?\s+)?"
    r"(Project Title|Project Name|Title \(max[^)]*\)|Title)\s*\**\s*$",
    re.IGNORECASE | re.MULTILINE,
)

# Match a markdown table row whose first cell is a project-title label.
TABLE_LABEL = re.compile(
    r"^\|\s*\**\s*(Project title|Project name|Title)\s*\**\s*\|\s*([^|]+?)\s*\|",
    re.IGNORECASE | re.MULTILINE,
)

PROMPT_PREFIXES = (
    "please ", "your ", "this ", "guidance", "note that", "we will",
    "applicants", "if ", "in the ", "in your ", "the form ", "do not ",
)


def _next_substantial_line(text: str, start: int) -> str | None:
    """Return the first non-empty, non-prompt-shaped line after `start`,
    skipping intermediate Title-label sub-headings (so a `## **2. Project
    Title**` heading drops into the actual title line, even if a
    `#### **2.1 Title (max...)**` sub-heading sits between them)."""
    for line in text[start:].splitlines():
        bare = line.strip().strip("*").strip()
        if not bare:
            continue
        if HEADING_LABEL.match(bare):
            # another title-shaped sub-heading — keep scanning
            continue
        if bare.lower().startswith(PROMPT_PREFIXES):
            continue
        if len(bare) < 15:
            return None
        if REJECT_PREFIXES.match(bare):
            return None
        return bare
    return None


def fallback_from_body(text: str) -> tuple[str | None, str]:
    """Return (title, source_tag) or (None, '') if no fallback found."""
    # Strategy 1: table row with project-title label
    m = TABLE_LABEL.search(text)
    if m:
        candidate = m.group(2).strip().strip("*").strip()
        if candidate and len(candidate) >= 15:
            return candidate, "body_table"

    # Strategy 2: heading or labelled line, take the next substantial line
    for m in HEADING_LABEL.finditer(text):
        candidate = _next_substantial_line(text, m.end())
        if candidate:
            return candidate, "body_heading"

    return None, ""


def filename_to_title(pdf_path: Path) -> str:
    stem = pdf_path.stem
    return re.sub(r"[_\s]+", " ", stem).strip()


def main():
    if len(sys.argv) != 4:
        print(
            f"Usage: {sys.argv[0]} <docling_meta.json> <marker.md> <source_pdf_path>",
            file=sys.stderr,
        )
        sys.exit(2)

    docling_meta_path = Path(sys.argv[1])
    cleaned_path = Path(sys.argv[2])
    pdf_path = Path(sys.argv[3])

    warnings: list[str] = []

    # Load docling title (may be missing if docling failed)
    docling_title: str | None = None
    if docling_meta_path.is_file():
        try:
            docling_meta = json.loads(docling_meta_path.read_text())
            docling_title = docling_meta.get("title")
        except Exception as e:
            warnings.append(f"failed to read docling_meta: {e}")
    else:
        warnings.append("docling_meta.json not found")

    cleaned_text = cleaned_path.read_text() if cleaned_path.is_file() else ""
    if not cleaned_text:
        warnings.append(f"cleaned.md not readable at {cleaned_path}")

    # Strategy: prefer an explicit body project-title field over docling, since
    # docling's heuristic is unreliable on form-shaped documents and a body
    # field is a higher-confidence signal.
    body_title, body_source = fallback_from_body(cleaned_text)
    if body_title:
        rejection = reject_docling(docling_title)
        result = {
            "recommended": body_title,
            "source": body_source,
            "rejected_docling": docling_title if rejection else None,
            "rejection_reason": rejection,
            "warnings": warnings,
        }
        if not rejection and docling_title and body_title.strip().lower() != docling_title.strip().lower():
            result["warnings"].append(
                f"body title differs from docling title (docling: {docling_title!r})"
            )
        print(json.dumps(result, indent=2))
        return

    # No body field found — fall back to docling if it passes rejection rules
    rejection = reject_docling(docling_title)
    if rejection is None and docling_title:
        print(json.dumps({
            "recommended": docling_title.strip(),
            "source": "docling",
            "rejected_docling": None,
            "rejection_reason": None,
            "warnings": warnings,
        }, indent=2))
        return

    # Last resort: filename
    fn_title = filename_to_title(pdf_path)
    warnings.append("no body title found and docling rejected; using filename stem")
    print(json.dumps({
        "recommended": fn_title,
        "source": "filename",
        "rejected_docling": docling_title,
        "rejection_reason": rejection,
        "warnings": warnings,
    }, indent=2))


if __name__ == "__main__":
    main()

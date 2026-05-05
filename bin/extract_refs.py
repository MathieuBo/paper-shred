#!/usr/bin/env python3
"""Find or reconstruct a reference list from cleaned.md.

Behaviour:
  1. If cleaned.md contains a `References` / `Bibliography` / `Literature Cited`
     heading, return its position so the skill can splice that block out into
     `references.md` directly. No parsing — the heading exists, trust it.
  2. Otherwise, scan the body for PMID and DOI tokens and emit a synthesised
     entry per unique identifier. Each entry includes the surrounding sentence
     fragment (the snippet containing the citation), so the user can audit
     whether two PMIDs in the same paragraph belong to the same paper or not.

Output: JSON to stdout with:
  {
    "has_references_section": bool,
    "section_heading": "## References" | null,
    "section_start_line": <int> | null,
    "n_pmids": <int>,
    "n_dois": <int>,
    "entries": [
      {"id": "PMID:33854069", "snippet": "Caballero et al. 2021..."},
      ...
    ]
  }

Usage:
    extract_refs.py <cleaned.md>
"""

import json
import re
import sys
from pathlib import Path


REFS_HEADING = re.compile(
    r"^#{1,6}\s+\**\s*(References|Bibliography|Literature\s+Cited|"
    r"Works\s+Cited|REFERENCES)\s*\**\s*$",
    re.MULTILINE | re.IGNORECASE,
)

PMID_RE = re.compile(r"\bPMID:?\s*(\d{5,9})\b", re.IGNORECASE)
DOI_RE = re.compile(r"\bdoi:?\s*(10\.\d{4,9}/[^\s)\]]+)|\b(10\.\d{4,9}/[^\s)\]]+)", re.IGNORECASE)


def find_refs_section(text: str):
    m = REFS_HEADING.search(text)
    if not m:
        return None
    line_no = text.count("\n", 0, m.start()) + 1
    return {"heading": m.group(0).strip(), "start_line": line_no}


def snippet_around(text: str, pos: int, span: int = 120) -> str:
    """Pull a one-sentence context around a position. Stops at sentence
    boundaries or paragraph breaks."""
    start = max(0, pos - span)
    end = min(len(text), pos + span)
    chunk = text[start:end]
    # Trim to nearest sentence/paragraph boundary
    for m in re.finditer(r"[.\n](?=\s|$)", chunk):
        if m.start() < (pos - start) - 20:
            start = start + m.end()
    chunk = text[start:end].strip()
    # Cut off trailing partial sentence
    last_dot = chunk.rfind(".")
    if last_dot > len(chunk) // 2:
        chunk = chunk[: last_dot + 1]
    return re.sub(r"\s+", " ", chunk).strip()


def collect_identifiers(text: str) -> list[dict]:
    seen: dict[str, dict] = {}

    for m in PMID_RE.finditer(text):
        pmid = m.group(1)
        key = f"PMID:{pmid}"
        if key in seen:
            continue
        seen[key] = {"id": key, "snippet": snippet_around(text, m.start())}

    for m in DOI_RE.finditer(text):
        doi = m.group(1) or m.group(2)
        # Trim trailing punctuation that often clings to DOIs
        doi = doi.rstrip(".,;:>)\\")
        key = f"doi:{doi}"
        if key in seen:
            continue
        seen[key] = {"id": key, "snippet": snippet_around(text, m.start())}

    return list(seen.values())


def main():
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <cleaned.md>", file=sys.stderr)
        sys.exit(2)

    src = Path(sys.argv[1])
    text = src.read_text()
    section = find_refs_section(text)
    entries = collect_identifiers(text)
    n_pmids = sum(1 for e in entries if e["id"].startswith("PMID:"))
    n_dois = sum(1 for e in entries if e["id"].startswith("doi:"))

    result = {
        "has_references_section": section is not None,
        "section_heading": section["heading"] if section else None,
        "section_start_line": section["start_line"] if section else None,
        "n_pmids": n_pmids,
        "n_dois": n_dois,
        "entries": entries,
    }
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()

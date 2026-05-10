#!/usr/bin/env python3
"""Find or reconstruct a reference list from cleaned.md.

Behaviour:
  1. If cleaned.md contains a `References` / `Bibliography` / `Literature Cited`
     heading, return its position so the skill can splice that block out into
     `references.md` directly. No parsing — the heading exists, trust it.
  2. Count reference-shaped lines in the refs slice (or the whole body when no
     heading is found) using four format-specific regexes — numbered, bracketed
     author-year (Cell Press), EMBO-style (`Surname FN, Surname2 RB (YYYY)`),
     and a loose `(YYYY)`-anywhere fallback. The caller picks the best estimate.
  3. Scan the body for PMID and DOI tokens and emit a synthesised entry per
     unique identifier. Used as the references.md content when there's no
     parseable reference section.

Output: JSON to stdout with:
  {
    "has_references_section": bool,
    "section_heading": "## References" | null,
    "section_start_line": <int> | null,
    "n_pmids": <int>,
    "n_dois": <int>,
    "n_refs_by_format": {
        "numbered": <int>,
        "bracketed_authoryear": <int>,
        "embo_style": <int>,
        "loose_year_paren": <int>
    },
    "n_refs_estimate": <int>,
    "likely_truncated": <bool>,
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

# Per-format counters (line-based). Each one tries to match a reference-shaped
# line; the caller picks the best estimate from the counts.
NUMBERED_RE = re.compile(r"^\s*[-*]?\s*\d+\.\s+\S")
BRACKETED_AUTHORYEAR_RE = re.compile(r"^\[?[A-Z][a-zA-Z'\-]+,\s+[A-Z]\.")
EMBO_STYLE_RE = re.compile(
    r"^\s*[-*]?\s*[A-Z][a-zA-Z'\-]+\s+[A-Z]{1,4}(?:,|\s).*\(\s*(?:19|20)\d{2}[a-z]?\s*\)"
)
# eLife style: `Author1 Init, Author2 Init. YEAR. Title.` — bare period-bounded year.
ELIFE_STYLE_RE = re.compile(
    r"^\s*[-*]?\s*[A-Z][a-zA-Z'\-]+\s+[A-Z]{1,4}(?:,|\.).*?\.\s*(?:19|20)\d{2}[a-z]?\.\s+[A-Z]"
)
LOOSE_YEAR_RE = re.compile(r"\(\s*(?:19|20)\d{2}[a-z]?\s*\)")


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


def count_refs_by_format(text: str) -> dict[str, int]:
    """Count reference-shaped lines under each of five format heuristics.

    The caller resolves the best estimate (typically max with an outlier guard)."""
    counts = {
        "numbered": 0,
        "bracketed_authoryear": 0,
        "embo_style": 0,
        "elife_style": 0,
        "loose_year_paren": 0,
    }
    for line in text.splitlines():
        if NUMBERED_RE.match(line):
            counts["numbered"] += 1
        if BRACKETED_AUTHORYEAR_RE.match(line):
            counts["bracketed_authoryear"] += 1
        if EMBO_STYLE_RE.match(line):
            counts["embo_style"] += 1
        if ELIFE_STYLE_RE.match(line):
            counts["elife_style"] += 1
        if LOOSE_YEAR_RE.search(line):
            counts["loose_year_paren"] += 1
    return counts


def best_ref_estimate(counts: dict[str, int]) -> int:
    """Pick the most credible count.

    Strategy: prefer a strict counter when it's nonzero. Fall through to looser
    counters only when the stricter ones return nothing. This avoids the loose
    `(YYYY)` matcher inflating the count by tagging body paragraphs that
    happen to mention years.
    """
    for key in ("numbered", "bracketed_authoryear", "embo_style", "elife_style", "loose_year_paren"):
        if counts.get(key, 0) >= 5:
            return counts[key]
    # Nothing reached the credible-list threshold — return the largest small count.
    return max(counts.values()) if counts else 0


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

    # Slice the text to count refs only inside the references section if found.
    if section is not None:
        # Find char offset of the heading line
        lines = text.splitlines()
        char_offset = sum(len(L) + 1 for L in lines[: section["start_line"]])
        refs_slice = text[char_offset:]
    else:
        refs_slice = text

    counts = count_refs_by_format(refs_slice)
    n_refs_estimate = best_ref_estimate(counts)

    # Likely-truncated heuristic: no refs heading AND no inline identifiers AND
    # no format counter found ≥5 lines — the bibliography may have been lost
    # by marker (last-page truncation is the common cause).
    likely_truncated = (
        section is None
        and not entries
        and max(counts.values(), default=0) < 5
    )

    result = {
        "has_references_section": section is not None,
        "section_heading": section["heading"] if section else None,
        "section_start_line": section["start_line"] if section else None,
        "n_pmids": n_pmids,
        "n_dois": n_dois,
        "n_refs_by_format": counts,
        "n_refs_estimate": n_refs_estimate,
        "likely_truncated": likely_truncated,
        "entries": entries,
    }
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()

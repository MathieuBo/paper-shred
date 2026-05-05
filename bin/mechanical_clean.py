#!/usr/bin/env python3
"""Mechanical cleanups on marker-pdf markdown output.

Deterministic regex-only pass. Does NOT attempt heading promotion, reference
splitting, or sub-section detection — those are delegated to the LLM stage.

Usage:
    mechanical_clean.py INPUT.md OUTPUT.md
"""

import pathlib
import re
import sys


def strip_zotero_urls(text: str) -> str:
    """Replace [N](zotero-url) with <sup>N</sup>; strip bare zotero wrappers."""
    text = re.sub(
        r"\[(\d+(?:[,\s]+\d+)*)\]\(https?://(?:www\.)?zotero\.org/[^)]+\)",
        r"<sup>\1</sup>",
        text,
    )
    text = re.sub(
        r"\[([^\]]+)\]\(https?://(?:www\.)?zotero\.org/[^)]+\)",
        r"\1",
        text,
    )
    return text


def strip_google_doc_urls(text: str) -> str:
    """Drop google-docs tracking wrappers that survive marker."""
    text = re.sub(
        r"\[([^\]]+)\]\(https?://docs\.google\.com/[^)]+\)",
        r"\1",
        text,
    )
    return text


def strip_paperpile_urls(text: str) -> str:
    """Replace Paperpile-wrapped citations with <sup>N</sup>.

    Paperpile is a Zotero-alternative used in Google Docs. Its citation links
    differ from Zotero in two ways that matter for cleanup:

      1. The trailing letter of the preceding word is pulled INTO the link text:
            processe[s1](paperpile-url)        → processes<sup>1</sup>
            transition[s29,](paperpile-url)    → transitions<sup>29</sup>,
            protein[s7–15.](paperpile-url)     → proteins<sup>7–15</sup>.
      2. Trailing punctuation may also be pulled in (comma, period above).

    Citations without leading-letter capture also occur:
            IDRs[6,7](paperpile-url)           → IDRs<sup>6,7</sup>
            mechanism[s45,46\)](paperpile-url) → mechanisms<sup>45,46</sup>)

    Generic non-citation Paperpile links (link text not digit-shaped) are left
    untouched.
    """
    text = re.sub(
        r"\[([a-z]?)([\d][\d,\-–\s]*?)([,.\)])?\]\(https?://(?:www\.)?paperpile\.com/[^)]+\)",
        r"\1<sup>\2</sup>\3",
        text,
    )
    # Generic fallback: residual paperpile links with non-digit text (single
    # trailing letters or stray punctuation pulled into separate links).
    text = re.sub(
        r"\[([^\]]+)\]\(https?://(?:www\.)?paperpile\.com/[^)]+\)",
        r"\1",
        text,
    )
    return text


def drop_page_break_markers(text: str) -> str:
    """marker emits `{0}-----`-style page separators that confuse parsing."""
    return re.sub(r"\{\d+\}-+\s*", "\n\n", text)


def replace_br_tags(text: str) -> str:
    return text.replace("<br>", " ").replace("<br/>", " ").replace("<br />", " ")


def strip_html_span_anchors(text: str) -> str:
    """Drop `<span id="..."></span>` HTML anchor tags.

    These are pure marker artefacts — anchor targets for intra-PDF cross-refs
    that don't resolve in standalone markdown. Universally cruft.
    """
    return re.sub(r'<span id="[^"]*"></span>', "", text)


def strip_dead_anchor_links(text: str) -> str:
    """Convert `[text](#page-N|#bib...|#sec...|#fig...|#tab...)` to plain `text`.

    These are markdown links pointing to PDF-internal anchors. The anchor
    targets are gone (or never existed in the markdown), so the link is dead.
    The visible link text is kept verbatim.
    """
    text = re.sub(
        r"\[([^\]]+)\]\(#(?:page|bib|sec|fig|tab|opt)[-\w]+\)",
        r"\1",
        text,
    )
    # Backslash-escaped variants (some markers escape closing parens).
    text = re.sub(
        r"\[([^\]]+)\]\(#[-\w]+\\?\)",
        r"\1",
        text,
    )
    return text


def strip_legend_markers(text: str) -> str:
    """Drop `*(legend on next page)*` / `*(legend continued on next page)*`.

    These are page-flow artefacts from journal layouts. Universally cruft.
    """
    return re.sub(
        r"\*\(legend (on|continued on) next page\)\*",
        "",
        text,
    )


def normalize_whitespace(text: str) -> str:
    text = re.sub(r"\s+<sup>", "<sup>", text)
    text = re.sub(r" {2,}", " ", text)
    text = re.sub(r" +\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip() + "\n"


# ---- Citation style detection ------------------------------------------

# <sup>N</sup> or <sup>N,M</sup> or <sup>N-M</sup>
SUP_CITE = re.compile(r"<sup>\d[\d,\-–\s]*</sup>")

# [N], [N,M], [N-M], [N, M, O] — bracketed numerics, with internal commas/dashes
# only. Excludes things like [Figure 1] (non-numeric content).
BRACKET_CITE = re.compile(r"\[\d+(?:[,\-–\s]+\d+)*\]")

# Author-year citations: matches "<LastName> ... <YYYY>" sequences anywhere in
# the body. Pattern requires a capitalized last name optionally followed by
# "et al.", "and <Name>", or "& <Name>", then a 4-digit year (with optional
# letter suffix), separated by whitespace/comma. Not anchored to parentheses
# because grant docs and theses often write inline citations as "shown by
# Smith et al. 2021 ..." without bracketing them.
#
# This will also count entries in a bibliography ("Smith J. 2021. Title…"),
# inflating the count. Acceptable: the goal is to identify the dominant
# citation style, not produce a precise inline-citation count.
AUTHORYEAR_CITE = re.compile(
    r"\b[A-Z][A-Za-z'\-]{2,}"
    r"(?:\s+(?:et\s+al\.?|(?:and|&)\s+[A-Z][A-Za-z'\-]+))?"
    r"\s*,?\s+\(?\d{4}[a-z]?\)?\b"
)


def count_citation_styles(text: str) -> dict:
    counts = {
        "sup": len(SUP_CITE.findall(text)),
        "bracketed": len(BRACKET_CITE.findall(text)),
        "authoryear": len(AUTHORYEAR_CITE.findall(text)),
    }
    dominant = max(counts, key=counts.get) if any(counts.values()) else None
    return {
        "counts": counts,
        "dominant": dominant,
        "n_dominant": counts[dominant] if dominant else 0,
    }


def main():
    if len(sys.argv) != 3:
        print(f"Usage: {sys.argv[0]} INPUT.md OUTPUT.md", file=sys.stderr)
        sys.exit(2)

    src = pathlib.Path(sys.argv[1])
    dst = pathlib.Path(sys.argv[2])
    text = src.read_text()

    text = strip_zotero_urls(text)
    text = strip_paperpile_urls(text)
    text = strip_google_doc_urls(text)
    text = strip_html_span_anchors(text)
    text = strip_dead_anchor_links(text)
    text = strip_legend_markers(text)
    text = drop_page_break_markers(text)
    text = replace_br_tags(text)
    text = normalize_whitespace(text)

    dst.write_text(text)

    chars = len(text)
    images = len(re.findall(r"!\[.*?\]\(.*?\)", text))
    tables = text.count("|---")
    cite = count_citation_styles(text)
    cite_summary = (
        f"sup={cite['counts']['sup']} bracket={cite['counts']['bracketed']} "
        f"authoryear={cite['counts']['authoryear']} → dominant={cite['dominant']}"
        if cite["dominant"]
        else "no inline citations detected"
    )
    print(
        f"cleaned: {chars:,} chars, {images} images, {tables} tables; "
        f"citations: {cite_summary}",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()

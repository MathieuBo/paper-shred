#!/usr/bin/env python3
"""Post-pass cleanup of section files written by the paper-shred skill.

Runs AFTER Claude has written all section files to `<OUT_DIR>/sections/`.
Strips context-dependent cruft that we can't safely remove during the
mechanical pre-pass (because Claude needs to see it to extract content
into figures/ sidecars and front-matter):

  1. Embedded `#### Figure N. ...` heading + caption blocks (now in
     figures/figure_N.md sidecars)
  2. Paragraph-form `Figure N. ...` captions that follow the same pattern
  3. Page-banner H1 headings (`# Article`, `# Authors`, `# In brief`,
     `# Correspondence`, `# Highlights`, `# Graphical abstract`, `# SUMMARY`).
     Note: `# Abstract` is intentionally NOT in this set — it's the legitimate
     H1 of paper-shaped section files, not a banner.
  4. Author affiliation footnote lines (`<sup>N</sup>Department of …`)
  5. Sentence joins where cruft removal left a paragraph break mid-sentence

Idempotent. Safe to run multiple times.

Usage:
    clean_sections.py <OUT_DIR>

where OUT_DIR contains the sections/ subdirectory.
"""

import re
import sys
from pathlib import Path


# ---- Strippers ----------------------------------------------------------

LEGEND_RE = re.compile(r"\*\(legend (on|continued on) next page\)\*")

FIGURE_HEADING_BLOCK_RE = re.compile(
    r"^#{1,6}\s+Figure\s+S?\d+\.\s.*?(?=^#{1,6}\s|\Z)",
    re.DOTALL | re.MULTILINE,
)

# Banner H1 headings — these are page-running headers that marker keeps as H1.
# Note: we only strip these when they appear as a STANDALONE H1 line.
# Do NOT add "Abstract" here: in a paper-shaped doc it's the legitimate H1 of
# the abstract section file, and stripping it silently removes the heading.
BANNER_HEADINGS = {
    "Article", "Authors", "Cell", "Correspondence", "Graphical abstract",
    "In brief", "Highlights", "SUMMARY",
}
BANNER_RE = re.compile(
    r"^#\s+(" + "|".join(re.escape(b) for b in BANNER_HEADINGS) + r")\s*$\n?",
    re.MULTILINE,
)

# Author affiliation footnote lines: <sup>N</sup>Department/Institute/...
AFFILIATION_RE = re.compile(
    r"^<sup>[\d\\\*]+</sup>"
    r"(?:Department|Institute|Departments|Section|Division|School|"
    r"Lab(?:oratory)?|Centre|Center|Faculty|Hospital|"
    r"Universit|Lead contact|Present address|"
    r"Correspondence:|Inst\.|Cellular|Pharma)"
    r".*$\n?(?:\n)?",  # consume one trailing blank line
    re.MULTILINE,
)

# Bare "*Correspondence:" line shape (e.g. <sup>*</sup>Correspondence: ...)
CORRESPONDENCE_RE = re.compile(
    r"^<sup>\\?\*</sup>\s*Correspondence:.*$\n?(?:\n)?",
    re.MULTILINE,
)


def strip_orphan_figure_paragraph(text: str) -> str:
    """Remove paragraph-form `Figure N. ...` caption blocks (no heading prefix).

    These run from the `Figure N.` line until the next blank line that's
    followed by sentence-shaped prose (i.e. NOT a sub-panel `(A)`, sub-caption
    continuation `Data are mean ± SEM`, etc.).
    """
    lines = text.splitlines()
    out = []
    skip = False
    for line in lines:
        if not skip and re.match(r"^Figure\s+S?\d+\.\s", line):
            skip = True
            continue
        if skip:
            stripped = line.strip()
            if (
                not stripped
                or re.match(r"^\([A-Z]+(\-[A-Z]+)?(,\s*[A-Z]+)*\)\s", line)
                or re.match(r"^\([A-Z]\)\s", line)
                or stripped.startswith((
                    "Data are mean", "Data represent mean", "Comparisons were",
                    "Statistical", "Scale bar", "Scale bars", "See also",
                    "*p <", "**p <", "Individual values", "n =",
                ))
                or re.match(r"^Figure\s+S?\d+\.\s", line)
            ):
                continue
            skip = False
        out.append(line)
    return "\n".join(out)


def join_broken_sentences(text: str) -> str:
    """Join paragraphs split mid-sentence by removed-cruft page boundaries.

    Pattern: word char (no sentence-final punctuation) + blank line +
    lowercase word → join with a single space. Conservative: requires the
    next paragraph to start with a lowercase letter, signalling continuation.
    """
    return re.sub(r"([a-zA-Z,])\n\n([a-z][a-z])", r"\1 \2", text)


def normalize_whitespace(text: str) -> str:
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r" {2,}", " ", text)
    text = re.sub(r" +\n", "\n", text)
    return text.strip() + "\n"


def clean(text: str) -> str:
    text = LEGEND_RE.sub("", text)
    text = FIGURE_HEADING_BLOCK_RE.sub("", text)
    text = strip_orphan_figure_paragraph(text)
    text = BANNER_RE.sub("", text)
    text = AFFILIATION_RE.sub("", text)
    text = CORRESPONDENCE_RE.sub("", text)
    text = join_broken_sentences(text)
    text = normalize_whitespace(text)
    return text


def main():
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <OUT_DIR>", file=sys.stderr)
        sys.exit(2)

    out_dir = Path(sys.argv[1])
    sections_dir = out_dir / "sections"
    if not sections_dir.is_dir():
        print(f"ERROR: {sections_dir} is not a directory", file=sys.stderr)
        sys.exit(1)

    total_before = 0
    total_after = 0
    changed = 0
    for f in sorted(sections_dir.glob("*.md")):
        before = f.read_text()
        after = clean(before)
        total_before += len(before)
        total_after += len(after)
        if before != after:
            f.write_text(after)
            changed += 1
            delta = len(before) - len(after)
            print(
                f"  {f.name:<40}  {len(before):>7,} → {len(after):>7,}  (-{delta:,})",
                file=sys.stderr,
            )

    print(
        f"clean_sections: {changed} files changed, "
        f"{total_before - total_after:,} chars removed",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()

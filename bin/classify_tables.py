#!/usr/bin/env python3
"""Classify markdown tables in cleaned.md as admin (form fields) vs data.

The skill writes data-bearing tables to `tables/table_NN.md` (and `.csv` when
rectangular). Form-field tables — applicant details, biosketch contact info,
budget summaries, declaration signatures — should stay inline in their section
file. The split was previously a judgment call; this script codifies it.

Heuristics for marking a table as ADMIN:
  - First-column cells match form-field labels:
      ^[A-Z]?\\d+(\\.\\d+)? \\b ...   e.g. "A1.1 Project lead name", "2.1 Title"
      ^\\b(Email|Phone|Name|Address|ORCID|Date|Signature)\\b
  - First-column cells frequently end with `?` (question fields)
  - Two-column shape (label, value) where first column has form-field shape
    in ≥50% of cells

Otherwise, treated as DATA: typically ≥3 columns, header row has noun-style
column names, body rows have heterogeneous cell types (numbers, IDs, etc.).

Output: <WORK>/table_audit.json with per-table classification, dimensions,
and the first row preview. Does NOT modify the source — Claude reads the
audit and decides.

Usage:
    classify_tables.py <cleaned.md> <output_audit.json>
"""

import json
import re
import sys
from pathlib import Path


FORM_LABEL_PREFIX = re.compile(r"^[A-Z]?\d+(\.\d+)?\.?\s+\S")
ADMIN_KEYWORDS = re.compile(
    r"^\s*(Email|Phone|Name|Full[\s_]+Name|Address|ORCID|Date|"
    r"Signature|Title|Position|Institution|Organisation|Organization|"
    r"Department|Role|Affiliation|Contact|Job\s+title|"
    r"Project\s+(title|lead|name)|Lead\s+applicant|Co[\s-]+applicant|"
    r"Budget|Estimate|Justification|Staff\s+costs|Running\s+costs)\b",
    re.IGNORECASE,
)


def parse_tables(text: str) -> list[dict]:
    """Find markdown table blocks. A table is one or more consecutive lines
    where the first non-whitespace char is `|`."""
    tables = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.lstrip().startswith("|"):
            start = i
            buf = []
            while i < len(lines) and lines[i].lstrip().startswith("|"):
                buf.append(lines[i])
                i += 1
            tables.append({"start_line": start + 1, "lines": buf})
        else:
            i += 1
    return tables


def split_row(row: str) -> list[str]:
    # Strip leading/trailing | and split on | (no escape handling — markdown tables
    # rarely escape |).
    inner = row.strip()
    if inner.startswith("|"):
        inner = inner[1:]
    if inner.endswith("|"):
        inner = inner[:-1]
    return [c.strip() for c in inner.split("|")]


def classify(table: dict) -> dict:
    rows = [split_row(L) for L in table["lines"]]
    # Drop separator rows (`|---|---|`)
    rows = [r for r in rows if not all(re.match(r"^[-:\s]*$", c) for c in r)]
    n_rows = len(rows)
    n_cols = max((len(r) for r in rows), default=0)

    if n_rows == 0:
        return {
            "start_line": table["start_line"],
            "n_rows": 0,
            "n_cols": 0,
            "verdict": "empty",
            "reasons": ["no rows"],
            "preview": "",
        }
    if n_cols <= 1:
        # marker sometimes wraps prose blocks (notably references lists) in
        # `| ... |` pipes; these aren't real tables.
        return {
            "start_line": table["start_line"],
            "n_rows": n_rows,
            "n_cols": n_cols,
            "verdict": "prose",
            "reasons": ["1-col block — prose wrapped in pipes, not a real table"],
            "preview": " ".join(rows[0])[:140] if rows else "",
        }

    first_col = [r[0] if r else "" for r in rows]
    # Skip empty first-col cells when scoring
    nonempty = [c for c in first_col if c.strip()]

    admin_score = 0
    data_score = 0
    reasons = []

    # Form-label prefix in first column
    n_form_label = sum(1 for c in nonempty if FORM_LABEL_PREFIX.match(c))
    # Admin keyword in first column
    n_admin_kw = sum(1 for c in nonempty if ADMIN_KEYWORDS.match(c))
    # Question-shaped cells (end with ?)
    n_question = sum(1 for c in nonempty if c.rstrip().endswith("?"))

    label_ratio = (n_form_label + n_admin_kw + n_question) / max(len(nonempty), 1)

    if label_ratio >= 0.4:
        admin_score += 3
        reasons.append(f"label-shaped first-col cells ({label_ratio:.0%})")

    # Two-column shape often signals form fields
    if n_cols == 2 and n_rows <= 12:
        admin_score += 1
        reasons.append("2-col, short → likely label/value")

    # Wider tables with multiple data rows lean data
    if n_cols >= 3 and n_rows >= 4:
        data_score += 2
        reasons.append(f"{n_cols} cols × {n_rows} rows")

    # Numeric/ID-shaped first column lean data
    n_numeric_first = sum(1 for c in nonempty if re.match(r"^[\d.]+$", c.strip()))
    if n_numeric_first >= 3 and n_numeric_first / max(len(nonempty), 1) >= 0.5:
        data_score += 2
        reasons.append(f"{n_numeric_first} numeric IDs in first col")

    if admin_score > data_score:
        verdict = "admin"
    elif data_score > admin_score:
        verdict = "data"
    else:
        # Tie-breaker: short tables default admin, larger default data
        verdict = "admin" if n_rows <= 6 else "data"
        reasons.append(f"tie-break by size ({n_rows} rows → {verdict})")

    preview = " | ".join(rows[0])[:140]
    return {
        "start_line": table["start_line"],
        "n_rows": n_rows,
        "n_cols": n_cols,
        "verdict": verdict,
        "admin_score": admin_score,
        "data_score": data_score,
        "reasons": reasons,
        "preview": preview,
    }


def main():
    if len(sys.argv) != 3:
        print(f"Usage: {sys.argv[0]} <cleaned.md> <output_audit.json>", file=sys.stderr)
        sys.exit(2)

    src = Path(sys.argv[1])
    dst = Path(sys.argv[2])

    text = src.read_text()
    tables = parse_tables(text)
    classified = [classify(t) for t in tables]
    n_admin = sum(1 for t in classified if t["verdict"] == "admin")
    n_data = sum(1 for t in classified if t["verdict"] == "data")
    n_prose = sum(1 for t in classified if t["verdict"] == "prose")

    audit = {
        "n_total": len(classified),
        "n_admin": n_admin,
        "n_data": n_data,
        "n_prose": n_prose,
        "tables": classified,
    }
    dst.write_text(json.dumps(audit, indent=2))
    print(
        f"classify_tables: {n_data} data / {n_admin} admin / {n_prose} prose of "
        f"{len(classified)} pipe-blocks. Audit: {dst}",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()

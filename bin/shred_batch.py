#!/usr/bin/env python3
"""Batch shredder for paper-shred skill.

Runs the full extract → clean → audit → split → write → post-pass pipeline
for one PDF or every PDF in a directory, using heuristics for section split
and metadata extraction. Designed to follow the skill's structural rules:
  - Body prose is never modified
  - Citations and figures are preserved verbatim
  - Section count capped at ~8

Idempotent: a folder with `meta.json` and `README.md` is skipped. The
extract.sh cache means re-runs after deleting the user-facing output (but
keeping `_raw/`) finish in seconds per paper.

Heuristics layered on top of the skill helpers:
  - Anchor selection: H1 if ≥3 non-banner; else H2 ≥3; else H3 ≥3; else single.
  - Drop title-only first section (PMC `# Title` / `# Abstract` sibling pattern).
  - Filename metadata parsing: `YYYY-MM_Journal_topic_(PMID|DOI)…`.

Usage:
    shred_batch.py <pdf_or_dir> [output_parent_dir]
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

SKILL_BIN = Path.home() / ".claude/skills/paper-shred/bin"
MARKER_VENV = Path.home() / ".venvs/pdf-pipeline"
DOCLING_VENV = Path.home() / ".venvs/docling"
SHRED_VERSION = "marker-pdf + paper-shred 0.2.0 (batch)"

# ---------- Filename metadata ----------

FN_RE = re.compile(
    r"^(?P<year>\d{4})-(?P<month>\d{2})_(?P<journal>[^_]+)_(?P<topic>.+?)_(?:PMID(?P<pmid>\d+)|DOI(?P<doi>[\d.]+))$"
)


def parse_filename(stem: str) -> dict:
    """Pull year, journal, identifier from `YYYY-MM_Journal_topic_PMID...` stems."""
    m = FN_RE.match(stem)
    if not m:
        return {}
    g = m.groupdict()
    return {
        "year": int(g["year"]),
        "month": int(g["month"]),
        "journal": g["journal"],
        "topic": g["topic"],
        "pmid": g["pmid"],
        "doi": g["doi"],
    }


# ---------- Heading detection ----------

HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)
BANNER_HEADINGS = re.compile(
    r"(europe pmc|funders group|author manuscript|received:|accepted:|published:|"
    r"copyright|cc[\s-]?by|published online|article| graphical abstract|in brief|highlights"
    r"|correspondence|summary|article info|^reference\s*$)",
    re.IGNORECASE,
)


def find_headings(text: str) -> list[dict]:
    """Return list of {level, text, line, start, end} for every #-heading."""
    out: list[dict] = []
    for m in HEADING_RE.finditer(text):
        level = len(m.group(1))
        title = m.group(2).strip().strip("*").strip()
        if not title:
            continue
        out.append({
            "level": level,
            "title": title,
            "line": text.count("\n", 0, m.start()) + 1,
            "start": m.start(),
            "end": m.end(),
        })
    return out


# ---------- Title fallback ----------
#
# pick_title.py (the skill helper) now does docling rejection + body-H1
# fallback itself, so this module no longer needs its own ad-hoc picker.
# The caller passes the helper's output through unchanged.


# ---------- Section split ----------

ABSTRACT_RE = re.compile(r"^\**\s*(abstract|summary)\s*\**\s*$", re.IGNORECASE)
INTRO_RE = re.compile(r"^\**\s*(introduction|background)\s*\**\s*$", re.IGNORECASE)
METHODS_RE = re.compile(
    r"^\**\s*(methods?|materials?\s+and\s+methods?|experimental\s+procedures?|"
    r"star\s*methods?)\s*\**\s*$",
    re.IGNORECASE,
)
RESULTS_RE = re.compile(r"^\**\s*results?\s*\**\s*$", re.IGNORECASE)
DISCUSSION_RE = re.compile(r"^\**\s*(discussion|conclusions?|concluding remarks)\s*\**\s*$", re.IGNORECASE)
ACK_RE = re.compile(r"^\**\s*(acknowledg(e?ments?|ments?)|funding|author contributions?)\s*\**\s*$", re.IGNORECASE)
REFS_RE = re.compile(r"^\**\s*(references?|bibliography|literature\s+cited)\s*\**\s*$", re.IGNORECASE)
SUPPL_RE = re.compile(r"^\**\s*(supplementary|supporting information|appendix)\s*\**\s*$", re.IGNORECASE)
GLOSSARY_RE = re.compile(r"^\**\s*(glossary)\s*\**\s*$", re.IGNORECASE)
BOX_RE = re.compile(r"^\**\s*box\s+\d", re.IGNORECASE)


def slugify(text: str, maxlen: int = 25) -> str:
    s = text.lower().strip().strip("*").strip()
    # Remove leading "The "
    s = re.sub(r"^(the|a|an)\s+", "", s)
    s = re.sub(r"[^a-z0-9]+", "_", s)
    s = re.sub(r"_+", "_", s).strip("_")
    if len(s) > maxlen:
        s = s[:maxlen].rstrip("_")
    return s or "section"


def detect_doc_type(text: str, headings: list[dict], journal: str | None) -> str:
    """paper / review / preprint / protocol / other."""
    head_titles = " | ".join(h["title"].lower() for h in headings)
    has_methods = any(METHODS_RE.match(h["title"]) for h in headings)
    has_results = any(RESULTS_RE.match(h["title"]) for h in headings)
    if "biorxiv" in text.lower()[:2000] or "medrxiv" in text.lower()[:2000]:
        return "preprint"
    if has_methods and has_results:
        return "paper"
    if journal and re.search(r"natrev|annurev|trends|cshpersp|curropin|review", journal, re.IGNORECASE):
        return "review"
    if has_results:
        return "paper"
    return "review"


def find_refs_split_point(text: str) -> int | None:
    """Return char offset where the references section starts, or None."""
    m = REFS_RE.search(text) if False else None
    # We need a heading-anchored search
    for hm in HEADING_RE.finditer(text):
        title = hm.group(2).strip().strip("*").strip()
        if REFS_RE.match(title):
            return hm.start()
    return None


def plan_sections(text: str, headings: list[dict], doc_type: str) -> tuple[list[dict], int | None]:
    """Plan the section split. Returns (sections, refs_split_offset).

    Each section = {slug, title, start, end} as char offsets into `text`.
    """
    refs_at = find_refs_split_point(text)
    body_end = refs_at if refs_at is not None else len(text)

    # Top-level boundaries: first try H1; if document mostly uses H2, use H2.
    h1s = [h for h in headings if h["level"] == 1 and h["start"] < body_end]
    h2s = [h for h in headings if h["level"] == 2 and h["start"] < body_end]

    # Filter banner H1/H2
    def keep(h):
        t = h["title"]
        if BANNER_HEADINGS.search(t):
            return False
        if len(t) < 3:
            return False
        return True

    h1s = [h for h in h1s if keep(h)]
    h2s = [h for h in h2s if keep(h)]

    h3s = [h for h in headings if h["level"] == 3 and h["start"] < body_end and keep(h)]
    if len(h1s) >= 3:
        anchors = h1s
    elif len(h2s) >= 3:
        anchors = h2s
    elif len(h3s) >= 3:
        # Cell Press style: marker emits everything as H3
        anchors = h3s
    else:
        anchors = sorted(h1s + h2s + h3s, key=lambda h: h["start"])

    # Sort and dedupe
    anchors = sorted(anchors, key=lambda h: h["start"])

    if not anchors:
        # Whole body becomes one section
        return ([{"slug": "01_body", "title": "Body", "start": 0, "end": body_end}], refs_at)

    # First section starts at offset 0 (captures abstract before first heading if any).
    sections: list[dict] = []
    prev_end = 0
    for i, a in enumerate(anchors):
        if i == 0:
            # Pre-anchor content: if non-trivial, treat as Abstract section (or skip if banner-only)
            head_text = text[: a["start"]].strip()
            # Only retain if it has substance beyond banner H2s
            stripped = re.sub(r"^#{1,6}.*$", "", head_text, flags=re.MULTILINE).strip()
            if len(stripped) > 200:
                # Identify as abstract/summary
                sec_title = "Abstract"
                sections.append({
                    "slug": "00_abstract",
                    "title": sec_title,
                    "start": 0,
                    "end": a["start"],
                })
        # This section spans [a.start, next anchor or body_end)
        next_start = anchors[i + 1]["start"] if i + 1 < len(anchors) else body_end
        sections.append({
            "slug": slugify(a["title"]),
            "title": a["title"],
            "start": a["start"],
            "end": next_start,
            "level": a["level"],
        })

    # Cap at 8: merge adjacent shortest sections
    while len(sections) > 8:
        # Find adjacent pair with minimum combined length, prefer merging short ones
        sizes = [(i, sections[i]["end"] - sections[i]["start"]) for i in range(len(sections))]
        # Don't merge an "abstract" or anything labeled abstract with what follows; merge by smallest length
        # Find smallest section index (not abstract if possible)
        non_abstract_idx = [(i, sz) for i, sz in sizes if sections[i]["slug"] != "00_abstract"]
        if not non_abstract_idx:
            break
        smallest = min(non_abstract_idx, key=lambda x: x[1])[0]
        # Merge with the right neighbour if exists, else left
        if smallest + 1 < len(sections) and sections[smallest + 1]["slug"] != "00_abstract":
            sections[smallest]["end"] = sections[smallest + 1]["end"]
            sections[smallest]["title"] = f"{sections[smallest]['title']} & {sections[smallest+1]['title']}"
            del sections[smallest + 1]
        elif smallest > 0:
            sections[smallest - 1]["end"] = sections[smallest]["end"]
            sections[smallest - 1]["title"] = f"{sections[smallest-1]['title']} & {sections[smallest]['title']}"
            del sections[smallest]
        else:
            break

    # Renumber slugs: 01_, 02_, ...
    final = []
    for i, s in enumerate(sections, 1):
        slug = re.sub(r"^\d+_?", "", s["slug"])
        slug = f"{i:02d}_{slug or 'section'}"
        final.append({**s, "slug": slug})

    return final, refs_at


def drop_title_section(sections: list[dict], title: str, text: str) -> list[dict]:
    """If the first section's title matches the document title and its body is
    < 600 chars, drop it (it's just the title block + authors)."""
    if not sections:
        return sections
    first = sections[0]
    body = text[first["start"]:first["end"]].strip()
    body_size = len(body)
    norm = lambda s: re.sub(r"\W+", "", s).lower()
    if norm(first["title"]) == norm(title) and body_size < 800:
        sections = sections[1:]
        # Renumber
        for i, s in enumerate(sections, 1):
            slug = re.sub(r"^\d+_?", "", s["slug"])
            s["slug"] = f"{i:02d}_{slug or 'section'}"
    return sections


# ---------- Figure caption sniffing ----------

FIG_CAPTION_RE = re.compile(
    r"^(?:#{1,6}\s+)?\**\s*(Figure|Fig\.?|FIGURE)\s+(\d+)\b[\s.:|]*\**\s*(.{0,400})",
    re.MULTILINE,
)


def sniff_captions(text: str) -> dict[int, str]:
    """Return {figure_number: first-sentence caption}."""
    out: dict[int, str] = {}
    for m in FIG_CAPTION_RE.finditer(text):
        try:
            n = int(m.group(2))
        except ValueError:
            continue
        cap_block = m.group(3).strip().strip("*").strip()
        if not cap_block:
            # caption text might be on next line; pull next 400 chars
            tail = text[m.end():m.end() + 400]
            cap_block = tail.strip().split("\n\n")[0].strip()
        if not cap_block:
            continue
        # Trim to first sentence (max 240 chars)
        sent = re.split(r"(?<=[.!?])\s+", cap_block, maxsplit=1)[0]
        if len(sent) > 240:
            sent = sent[:240].rsplit(" ", 1)[0] + "…"
        out.setdefault(n, sent)
    return out


# ---------- Folder writer ----------

README_TMPL = """\
---
title: "{title}"
authors: {authors_yaml}
year: {year_yaml}
type: {doc_type}
doi: {doi_yaml}
journal: {journal_yaml}
pmid: {pmid_yaml}
source: _raw/source.pdf
extracted: {extracted_date}
extractor: {extractor}
tags:
  - {doc_type}
---

# {title}

> **Source:** `{source_filename}`
> **Type:** {doc_type} · **Year:** {year_display} · **DOI:** {doi_display} · **PMID:** {pmid_display}

## Sections

{sections_list}

{figures_block}{tables_block}## References

[[references|{n_refs} references]]
"""


def yaml_str(value, fallback="null") -> str:
    if value is None or value == "":
        return fallback
    if isinstance(value, list):
        if not value:
            return fallback
        return "\n  - " + "\n  - ".join(json.dumps(v) for v in value)
    return json.dumps(str(value))


def shred_pdf(pdf_path: Path, out_parent: Path | None = None, log=print,
              require_caption: bool = False) -> dict:
    pdf_path = pdf_path.resolve()
    if out_parent is None:
        out_parent = pdf_path.parent
    raw_stem = pdf_path.stem
    stem = re.sub(r"\s+", "_", raw_stem)
    out_dir = out_parent / stem
    work = out_dir / "_raw"
    work.mkdir(parents=True, exist_ok=True)

    # Cache: if marker_out already exists, skip extract.sh
    cached_marker_dir = work / "marker_out" / stem
    cached_marker_md = cached_marker_dir / f"{stem}.md"
    cached_marker_meta = cached_marker_dir / f"{stem}_meta.json"
    cached_docling_meta = work / "docling_out" / "docling_meta.json"
    if cached_marker_md.is_file() and cached_marker_meta.is_file():
        log(f"  cached extract; restructuring → {out_dir.name}/")
        paths = {
            "marker_md": str(cached_marker_md),
            "marker_meta": str(cached_marker_meta),
            "marker_dir": str(cached_marker_dir),
            "docling_md": str(work / "docling_out" / "docling.md") if cached_docling_meta.is_file() else "",
            "docling_meta": str(cached_docling_meta) if cached_docling_meta.is_file() else "",
            "docling_pics": str(work / "docling_out" / "docling_pictures"),
            "docling_title": "",
            "stem": stem,
            "marker_venv": str(MARKER_VENV),
            "docling_venv": str(DOCLING_VENV),
        }
    else:
        log(f"  extracting → {out_dir.name}/")
        res = subprocess.run(
            ["bash", str(SKILL_BIN / "extract.sh"), str(pdf_path), str(work)],
            capture_output=True, text=True,
        )
        if res.returncode != 0:
            return {"status": "extract_failed", "stderr": res.stderr[-500:], "stem": stem}
        paths = {}
        for line in res.stdout.strip().splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                paths[k] = v

    marker_md = paths.get("marker_md")
    if not marker_md or not Path(marker_md).is_file():
        return {"status": "no_marker_md", "stem": stem}

    cleaned = work / "cleaned.md"

    # 2. mechanical_clean
    cln = subprocess.run(
        [str(MARKER_VENV / "bin/python"), str(SKILL_BIN / "mechanical_clean.py"),
         marker_md, str(cleaned)],
        capture_output=True, text=True,
    )
    clean_summary = cln.stderr.strip()
    # Parse citation style from summary line
    cit_style = "null"
    n_cit = 0
    m = re.search(r"sup=(\d+) bracket=(\d+) authoryear=(\d+) → dominant=(\w+)", clean_summary)
    if m:
        counts = {"sup": int(m.group(1)), "bracket": int(m.group(2)), "authoryear": int(m.group(3))}
        cit_style = m.group(4)
        n_cit = counts.get(cit_style, 0)

    # 3. audits (sequential — fast)
    fig_args = [str(MARKER_VENV / "bin/python"), str(SKILL_BIN / "filter_figures.py"), str(work)]
    if require_caption:
        fig_args += ["--require-caption", str(cleaned)]
    subprocess.run(fig_args, capture_output=True, text=True)
    subprocess.run([str(MARKER_VENV / "bin/python"), str(SKILL_BIN / "classify_tables.py"),
                    str(cleaned), str(work / "table_audit.json")],
                   capture_output=True, text=True)
    refs_run = subprocess.run([str(MARKER_VENV / "bin/python"), str(SKILL_BIN / "extract_refs.py"),
                               str(cleaned)],
                              capture_output=True, text=True)
    refs_data = json.loads(refs_run.stdout) if refs_run.returncode == 0 else {"entries": []}

    title_run = subprocess.run([str(MARKER_VENV / "bin/python"), str(SKILL_BIN / "pick_title.py"),
                                paths.get("docling_meta", ""), str(cleaned), str(pdf_path)],
                               capture_output=True, text=True)
    if title_run.returncode == 0:
        try:
            title_data = json.loads(title_run.stdout)
        except json.JSONDecodeError:
            title_data = {"recommended": stem.replace("_", " "), "source": "filename", "warnings": []}
    else:
        title_data = {"recommended": stem.replace("_", " "), "source": "filename", "warnings": []}

    # 4. read cleaned, plan structure
    text = cleaned.read_text()
    headings = find_headings(text)
    title = title_data.get("recommended", "") or stem.replace("_", " ")
    title_source = title_data.get("source", "filename")
    t_warnings: list[str] = list(title_data.get("warnings") or [])
    fnmeta = parse_filename(stem)
    journal = fnmeta.get("journal")
    doc_type = detect_doc_type(text, headings, journal)

    sections, refs_at = plan_sections(text, headings, doc_type)
    sections = drop_title_section(sections, title, text)

    # 5. figures from audit
    audit_path = work / "figure_audit.json"
    figs_audit = json.loads(audit_path.read_text()) if audit_path.is_file() else {"images": []}
    kept = [im for im in figs_audit.get("images", []) if im.get("kept")]
    # Prefer marker images first, then docling extras
    def is_marker(im): return "/marker_out/" in im["path"]
    kept_marker = [im for im in kept if is_marker(im)]
    kept_docling = [im for im in kept if not is_marker(im)]
    kept_ordered = kept_marker + kept_docling
    captions = sniff_captions(text)

    figures_dir = out_dir / "figures"
    figures_dir.mkdir(parents=True, exist_ok=True)
    fig_records = []
    for i, im in enumerate(kept_ordered, 1):
        src = Path(im["path"])
        ext = src.suffix.lower()
        dst_name = f"figure_{i:02d}{ext}"
        dst = figures_dir / dst_name
        try:
            shutil.copy2(src, dst)
        except Exception as e:
            log(f"  WARN: copy figure failed: {e}")
            continue
        cap = captions.get(i, "")
        sidecar = figures_dir / f"figure_{i:02d}.md"
        cap_short = cap if cap else "(no caption detected; see surrounding section text)"
        sidecar.write_text(
            f"# Figure {i}\n\n![Figure {i}]({dst_name})\n\n"
            f"**Caption (verbatim):** {cap_short}\n\n"
            f"**Description:** {cap_short}\n"
        )
        fig_records.append({"n": i, "caption": cap or ""})

    # 6. tables from audit
    tab_audit = work / "table_audit.json"
    tab_data = json.loads(tab_audit.read_text()) if tab_audit.is_file() else {"tables": []}
    data_tables = [t for t in tab_data.get("tables", []) if t.get("verdict") == "data"]
    tables_dir = out_dir / "tables"
    tables_dir.mkdir(parents=True, exist_ok=True)
    table_records = []
    if data_tables:
        text_lines = text.splitlines()
        for i, t in enumerate(data_tables, 1):
            start = t["start_line"] - 1
            # Walk forward to last contiguous pipe-line
            end = start
            while end < len(text_lines) and text_lines[end].lstrip().startswith("|"):
                end += 1
            block = "\n".join(text_lines[start:end])
            (tables_dir / f"table_{i:02d}.md").write_text(f"# Table {i}\n\n{block}\n")
            table_records.append({"n": i, "preview": t.get("preview", "")[:120]})
    # If no tables, leave empty dir; remove it
    if not table_records:
        try:
            tables_dir.rmdir()
        except OSError:
            pass

    # 7. references
    refs_md = out_dir / "references.md"
    if refs_at is not None:
        refs_block = text[refs_at:].strip()
        # Strip the heading line itself - keep its content
        first_nl = refs_block.find("\n")
        body = refs_block[first_nl + 1:].strip() if first_nl > 0 else refs_block
        refs_md.write_text(f"# References\n\n{body}\n")
        # Trust extract_refs.py's n_refs_estimate (multi-format-aware).
        n_refs = int(refs_data.get("n_refs_estimate") or 0)
    elif refs_data.get("entries"):
        lines = [f"{i}. `{e['id']}` — {e['snippet']}" for i, e in enumerate(refs_data["entries"], 1)]
        refs_md.write_text(
            "# References\n\n*Reconstructed from inline PMID/DOI tokens — "
            "the source PDF lacked a parseable bibliography section.*\n\n"
            + "\n".join(lines) + "\n"
        )
        n_refs = len(lines)
    else:
        refs_md.write_text("# References\n\n*No machine-parseable references found in source.*\n")
        n_refs = 0

    # 8. write section files
    sections_dir = out_dir / "sections"
    sections_dir.mkdir(parents=True, exist_ok=True)
    body_for_sections = text[: refs_at] if refs_at is not None else text
    for s in sections:
        # Cap end at refs_at if we set it earlier (already done in plan_sections)
        chunk = text[s["start"]:s["end"]].strip()
        # Promote first heading line: if it's H1, demote to H1; if H2, keep H2 — but file should start with H1.
        # Replace any leading heading with single H1.
        chunk_lines = chunk.splitlines()
        # Find first heading line
        out_lines = [f"# {s['title']}", ""]
        seen_first_heading = False
        for L in chunk_lines:
            if not seen_first_heading and HEADING_RE.match(L):
                # Drop the original leading heading (we wrote our own H1 above)
                seen_first_heading = True
                continue
            # Demote remaining H1s to H2 so the file still has only one H1
            m_h = HEADING_RE.match(L)
            if m_h and len(m_h.group(1)) == 1:
                L = "## " + m_h.group(2)
            out_lines.append(L)
        section_text = "\n".join(out_lines).rstrip() + "\n"
        (sections_dir / f"{s['slug']}.md").write_text(section_text)

    # 9. README, meta.json
    n_pages = None
    try:
        marker_meta = json.loads(Path(paths["marker_meta"]).read_text())
        n_pages = marker_meta.get("page_stats", [{}])
        if isinstance(n_pages, list):
            n_pages = len(n_pages)
        elif isinstance(n_pages, dict):
            n_pages = n_pages.get("n_pages")
    except Exception:
        n_pages = None

    sections_list = "\n".join(
        f"- [[sections/{s['slug']}|{s['title']}]]" for s in sections
    )
    figures_block = ""
    if fig_records:
        figs_lines = "\n".join(
            f"- [[figures/figure_{r['n']:02d}|Figure {r['n']}{(' — ' + r['caption']) if r['caption'] else ''}]]"
            for r in fig_records
        )
        figures_block = f"## Figures\n\n{figs_lines}\n\n"
    tables_block = ""
    if table_records:
        tabs_lines = "\n".join(
            f"- [[tables/table_{r['n']:02d}|Table {r['n']}{(' — ' + r['preview']) if r['preview'] else ''}]]"
            for r in table_records
        )
        tables_block = f"## Tables\n\n{tabs_lines}\n\n"

    extracted_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    extracted_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")

    pmid = fnmeta.get("pmid")
    doi = fnmeta.get("doi")
    year = fnmeta.get("year")

    readme = README_TMPL.format(
        title=title.replace('"', "'"),
        authors_yaml="null",
        year_yaml=year if year else "null",
        year_display=year if year else "—",
        doc_type=doc_type,
        doi_yaml=json.dumps(doi) if doi else "null",
        doi_display=doi if doi else "—",
        journal_yaml=json.dumps(journal) if journal else "null",
        pmid_yaml=pmid if pmid else "null",
        pmid_display=pmid if pmid else "—",
        extracted_date=extracted_date,
        extractor=SHRED_VERSION,
        source_filename=pdf_path.name,
        sections_list=sections_list,
        figures_block=figures_block,
        tables_block=tables_block,
        n_refs=n_refs,
    )
    (out_dir / "README.md").write_text(readme)

    meta = {
        "title": title,
        "title_source": title_source,
        "title_warnings": t_warnings,
        "authors": None,
        "year": year,
        "type": doc_type,
        "doi": doi,
        "pmid": pmid,
        "journal": journal,
        "funder": None,
        "grant_id": None,
        "source_pdf": "_raw/source.pdf",
        "extracted_at": extracted_iso,
        "extractor": SHRED_VERSION,
        "n_pages": n_pages,
        "n_sections": len(sections),
        "n_figures": len(fig_records),
        "n_tables": len(table_records),
        "n_references": n_refs,
        "refs_likely_truncated": bool(refs_data.get("likely_truncated")),
        "citation_style": cit_style if cit_style != "null" else None,
        "n_citations_inline": n_cit,
        "sections": [f"{s['slug']}.md" for s in sections],
    }
    (out_dir / "meta.json").write_text(json.dumps(meta, indent=2))

    # 10. _raw artifacts
    raw = out_dir / "_raw"
    raw.mkdir(exist_ok=True)
    src_link = raw / "source.pdf"
    if src_link.exists() or src_link.is_symlink():
        src_link.unlink()
    try:
        src_link.symlink_to(pdf_path)
    except OSError:
        shutil.copy2(pdf_path, src_link)
    # Move marker.md, marker_meta to _raw
    try:
        shutil.copy2(marker_md, raw / "marker.md")
        shutil.copy2(paths["marker_meta"], raw / "marker_meta.json")
    except Exception:
        pass

    # 11. clean_sections.py post-pass
    subprocess.run(
        [str(MARKER_VENV / "bin/python"), str(SKILL_BIN / "clean_sections.py"), str(out_dir)],
        capture_output=True, text=True,
    )

    return {
        "status": "ok",
        "stem": stem,
        "out_dir": str(out_dir),
        "n_sections": len(sections),
        "n_figures": len(fig_records),
        "n_tables": len(table_records),
        "n_refs": n_refs,
        "title": title,
        "title_source": title_source,
        "doc_type": doc_type,
    }


def main():
    args = sys.argv[1:]
    require_caption = False
    if "--require-caption" in args:
        require_caption = True
        args.remove("--require-caption")
    if not args:
        print("Usage: shred_batch.py <pdf_or_dir> [output_parent_dir] [--require-caption]", file=sys.stderr)
        sys.exit(2)
    target = Path(args[0]).resolve()
    out_parent = Path(args[1]).resolve() if len(args) > 1 else None

    pdfs: list[Path] = []
    if target.is_file():
        pdfs = [target]
    elif target.is_dir():
        for p in sorted(target.rglob("*.pdf")):
            # Skip symlinks (avoids picking up _raw/source.pdf)
            if p.is_symlink():
                continue
            # Skip anything under a `_raw` directory
            if "_raw" in p.parts:
                continue
            pdfs.append(p)
    else:
        print(f"Not found: {target}", file=sys.stderr)
        sys.exit(1)

    log_path = (out_parent or target if target.is_dir() else target.parent) / "_shred_log.jsonl"
    if log_path.is_dir():
        log_path = log_path / "_shred_log.jsonl"
    log_f = open(log_path, "a")
    def log(msg: str):
        print(msg, flush=True)

    print(f"Shredding {len(pdfs)} PDFs → log: {log_path}", flush=True)
    results = []
    for i, pdf in enumerate(pdfs, 1):
        # Skip already-shredded
        out_check = (pdf.parent if out_parent is None else out_parent) / re.sub(r"\s+", "_", pdf.stem)
        if (out_check / "meta.json").is_file() and (out_check / "README.md").is_file():
            print(f"[{i}/{len(pdfs)}] SKIP (already shredded): {pdf.name}", flush=True)
            r = {"status": "skipped", "pdf": str(pdf), "out_dir": str(out_check)}
            log_f.write(json.dumps(r) + "\n")
            log_f.flush()
            results.append(r)
            continue
        print(f"[{i}/{len(pdfs)}] {pdf.name}", flush=True)
        try:
            r = shred_pdf(pdf, out_parent=pdf.parent if out_parent is None else out_parent,
                          log=log, require_caption=require_caption)
        except Exception as e:
            r = {"status": "exception", "error": str(e), "pdf": str(pdf)}
        r["pdf"] = str(pdf)
        log_f.write(json.dumps(r) + "\n")
        log_f.flush()
        if r.get("status") == "ok":
            print(f"    ok: {r['n_sections']} sections, {r['n_figures']} figs, "
                  f"{r['n_tables']} tables, {r['n_refs']} refs")
        else:
            print(f"    FAIL: {r}")
        results.append(r)
    log_f.close()

    n_ok = sum(1 for r in results if r.get("status") == "ok")
    print(f"\nDone: {n_ok}/{len(results)} succeeded.")
    return 0 if n_ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())

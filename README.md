# paper-shred

A Claude Code skill that converts a scientific PDF into a structured folder of
markdown sections, figures, tables, and references — one paper, one folder, one
file per addressable component. Inspired by ["biomedical literature as a
filesystem"](https://gxl.ai/blog/biomedical-literature-as-a-filesystem).

## What it produces

```
<paper>/
├── README.md          # frontmatter + summary + TOC
├── meta.json          # title, authors, year, DOI, counts, citation_style
├── sections/          # 01_*.md, 02_*.md, ... (≤ 8 files)
├── figures/           # figure_NN.jpeg + figure_NN.md sidecar (caption + AI summary)
├── tables/            # table_NN.md (+ .csv when rectangular)
├── references.md      # one entry per line
└── _raw/              # marker.md, docling output, audit JSONs, source.pdf symlink
```

## How it works

Two extractors run in parallel:

- **[marker-pdf](https://github.com/datalab-to/marker)** — primary source for body text. Best at preserving citation markers, emphasis, scientific notation.
- **[docling](https://github.com/docling-project/docling)** — auxiliary source for document title, heading scaffold, picture inventory.

Claude reconciles them, plans a section split (capped at ~8 files), and writes
the folder. Four audit scripts run before Claude's planning step, each emitting
a JSON manifest in `_raw/` that Claude reads to make decisions:

| Script | Decides |
|---|---|
| `bin/pick_title.py` | Body `Project title` field → docling title (with rejection rules) → filename stem |
| `bin/filter_figures.py` | Drops images by min-dim/aspect/area thresholds (catches page banners + partner logos) |
| `bin/classify_tables.py` | Per-block `data` / `admin` / `prose` / `empty` (admin tables stay inline; prose blocks are mis-piped reference lists) |
| `bin/extract_refs.py` | Finds the References section, or falls back to PMID/DOI scan with snippets |

Two text-mutation scripts (run unchanged by Claude) clean up the raw markdown:

- `bin/mechanical_clean.py` — regex pre-pass on marker output. Strips Zotero/Paperpile/Google-docs URL wrappers, dead anchors, page-break markers, `<br>` tags. Reports a citation-style summary (`sup` / `bracketed` / `authoryear`).
- `bin/clean_sections.py` — post-pass on section files. Strips embedded figure captions, page-banner H1s (Article/Authors/SUMMARY/etc.), affiliation footnotes; joins paragraphs split mid-sentence. Idempotent.

## Install (for agents)

If you are an LLM agent installing this skill on a fresh machine, run the
steps below verbatim. They assume `git`, `bash`, and [`uv`](https://docs.astral.sh/uv/)
are already on `PATH`. If `uv` is missing, install it with
`curl -LsSf https://astral.sh/uv/install.sh | sh` first.

```bash
# 1. Clone into the location Claude Code auto-discovers skills.
#    For non-Claude-Code agents, clone anywhere and set CLAUDE_SKILL_DIR
#    to that path before invoking bin/*.
mkdir -p ~/.claude/skills
git clone https://github.com/<owner>/paper-shred ~/.claude/skills/paper-shred

# 2. Two virtualenvs, separate to avoid a torch version conflict between
#    marker-pdf and docling.
uv venv ~/.venvs/pdf-pipeline
~/.venvs/pdf-pipeline/bin/uv pip install marker-pdf pymupdf pillow

uv venv ~/.venvs/docling
~/.venvs/docling/bin/uv pip install docling

# 3. Smoke-test that both extractors import.
~/.venvs/pdf-pipeline/bin/python -c "import marker, pymupdf, PIL; print('marker ok')"
~/.venvs/docling/bin/python   -c "import docling; print('docling ok')"
```

`bin/extract.sh` looks for the venvs at `~/.venvs/pdf-pipeline/` and
`~/.venvs/docling/` by default. Override with the `MARKER_VENV` and
`DOCLING_VENV` env vars if you put them elsewhere.

First marker run downloads ~2 GB of surya layout models and caches them under
`~/.cache/datalab/`. Plan ~5–10 min the first time; ~1–7 min thereafter
depending on page count.

## Use

### From Claude Code

```
/paper-shred /path/to/document.pdf
```

Claude reads `SKILL.md` for the full workflow, runs the extractors and audit
scripts, then writes the structured folder. Output goes next to the source
PDF unless you pass a second argument.

### Batch mode (directories of >5 PDFs)

```
python bin/shred_batch.py <directory> [--require-caption]
```

Walks for `*.pdf` (skipping symlinks and `_raw/` paths), runs the full pipeline
per file with deterministic heuristics — no LLM call per paper. Idempotent
(skips folders that already have `meta.json + README.md`); the `extract.sh`
cache makes re-runs after deleting user-facing output finish in seconds. Per-
paper status streams to `<directory>/_shred_log.jsonl`. Expect 5–10 min/PDF on
CPU (50 PDFs ≈ 5–8 hours).

Use `--require-caption` to drop figure panels without an anchored
`Figure N.` caption nearby — useful on Cell/Nature collections where marker
extracts dozens of un-captioned panel fragments.

### From any agent (or by hand)

Each script is independent and prints its result to stdout (or a JSON file).
A minimal end-to-end run:

```bash
PDF=/path/to/document.pdf
OUT=/path/to/output_dir
WORK=$OUT/_raw
mkdir -p $WORK

# 1. Extract (parallel marker + docling). Sets stdout key=value vars.
CLAUDE_SKILL_DIR=~/.claude/skills/paper-shred \
  bash ~/.claude/skills/paper-shred/bin/extract.sh "$PDF" "$WORK"

# 2. Mechanical clean of marker output.
~/.venvs/pdf-pipeline/bin/python \
  ~/.claude/skills/paper-shred/bin/mechanical_clean.py \
  "$WORK/marker_out/<stem>/<stem>.md" "$WORK/cleaned.md"

# 3. Audit scripts (read-only; emit JSON for downstream LLM use).
PY=~/.venvs/pdf-pipeline/bin/python
SK=~/.claude/skills/paper-shred/bin
$PY $SK/filter_figures.py   "$WORK"
$PY $SK/classify_tables.py  "$WORK/cleaned.md" "$WORK/table_audit.json"
$PY $SK/pick_title.py       "$WORK/docling_out/docling_meta.json" \
                            "$WORK/cleaned.md" "$PDF" > "$WORK/title_audit.json"
$PY $SK/extract_refs.py     "$WORK/cleaned.md" > "$WORK/refs_audit.json"

# 4. (Agent step) Read cleaned.md + the four audit JSONs, write the folder.

# 5. Post-pass cleanup of section files.
$PY $SK/clean_sections.py "$OUT"
```

The four audit JSONs are the contract surface between the deterministic
pipeline and the LLM-driven section-splitting step. Each one is documented in
`SKILL.md` (the canonical workflow doc).

## Tested document types

- Journal research papers (with KEY RESOURCES TABLE — the Cell convention)
- bioRxiv / Nature-style preprints
- Research-council grant applications (multi-objective, multi-WP)
- Expression-of-interest (EoI) forms with Project Title field
- Competition-style submission forms with judging-criteria sections

## License

MIT — see [LICENSE](LICENSE).

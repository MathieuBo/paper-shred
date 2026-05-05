#!/usr/bin/env bash
# Extract a PDF with marker-pdf AND docling in parallel.
# Marker provides body text with citations + emphasis preserved.
# Docling provides title detection, heading scaffold, picture inventory.
# Prints absolute paths to: marker_md, marker_meta, marker_dir, docling_md,
# docling_meta, docling_pics, docling_title, stem, marker_venv, docling_venv.
#
# Usage: extract.sh <pdf_path> <work_dir>

set -euo pipefail

PDF="${1:?Usage: extract.sh <pdf_path> <work_dir>}"
WORK="${2:?Usage: extract.sh <pdf_path> <work_dir>}"

if [[ ! -f "$PDF" ]]; then
    echo "ERROR: PDF not found: $PDF" >&2
    exit 1
fi

# Resolve marker venv: prefer dedicated pipeline venv, fall back to Hermes.
if [[ -x "$HOME/.venvs/pdf-pipeline/bin/marker_single" ]]; then
    MARKER_VENV="$HOME/.venvs/pdf-pipeline"
elif [[ -x "$HOME/.hermes/hermes-agent/venv/bin/marker_single" ]]; then
    MARKER_VENV="$HOME/.hermes/hermes-agent/venv"
else
    echo "ERROR: marker_single not found. Install with:" >&2
    echo "  uv venv ~/.venvs/pdf-pipeline --python 3.11" >&2
    echo "  uv pip install --python ~/.venvs/pdf-pipeline/bin/python pymupdf pymupdf4llm marker-pdf" >&2
    exit 1
fi

# Resolve docling venv (separate, due to torch version conflicts with marker).
DOCLING_VENV=""
if [[ -x "$HOME/.venvs/docling/bin/python" ]]; then
    if "$HOME/.venvs/docling/bin/python" -c "import docling" 2>/dev/null; then
        DOCLING_VENV="$HOME/.venvs/docling"
    fi
fi

mkdir -p "$WORK"
MARKER_OUT="$WORK/marker_out"
DOCLING_OUT="$WORK/docling_out"
mkdir -p "$MARKER_OUT" "$DOCLING_OUT"

STEM="$(basename "$PDF" .pdf)"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Launch marker in background
"$MARKER_VENV/bin/marker_single" "$PDF" --output_dir "$MARKER_OUT" \
    >"$WORK/marker.log" 2>&1 &
MARKER_PID=$!

# Helper: detect surya layout-batch overflow that requires a retry with
# layout_batch_size=1 (slow but reliable on dense pages).
needs_layout_retry() {
    grep -qE "AcceleratorError|index 8192 is out of bounds|unpack_qkv_with_mask" \
        "$WORK/marker.log" 2>/dev/null
}

# Launch docling in background (if available)
DOCLING_PID=""
if [[ -n "$DOCLING_VENV" ]]; then
    "$DOCLING_VENV/bin/python" "$SCRIPT_DIR/extract_docling.py" "$PDF" "$DOCLING_OUT" \
        >"$WORK/docling.log" 2>"$WORK/docling.err" &
    DOCLING_PID=$!
else
    echo "WARN: docling venv not found at ~/.venvs/docling — skipping docling pass." >&2
    echo "WARN: install with: uv venv ~/.venvs/docling --python 3.11 && uv pip install --python ~/.venvs/docling/bin/python docling" >&2
fi

# Wait for both, capturing failures
MARKER_RC=0
wait $MARKER_PID || MARKER_RC=$?
DOCLING_RC=0
if [[ -n "$DOCLING_PID" ]]; then
    wait $DOCLING_PID || DOCLING_RC=$?
fi

if [[ $MARKER_RC -ne 0 ]]; then
    if needs_layout_retry; then
        echo "WARN: marker hit surya layout-batch overflow; retrying with layout_batch_size=1 (slower but reliable)..." >&2
        rm -rf "$MARKER_OUT"
        mkdir -p "$MARKER_OUT"
        "$MARKER_VENV/bin/marker_single" "$PDF" --output_dir "$MARKER_OUT" \
            --layout_batch_size 1 --detection_batch_size 1 \
            >"$WORK/marker.log" 2>&1
        MARKER_RC=$?
    fi
fi

if [[ $MARKER_RC -ne 0 ]]; then
    echo "ERROR: marker failed (rc=$MARKER_RC). Tail of log:" >&2
    tail -20 "$WORK/marker.log" >&2
    exit 1
fi

MARKER_DIR="$MARKER_OUT/$STEM"
MARKER_MD="$MARKER_DIR/$STEM.md"
MARKER_META="$MARKER_DIR/${STEM}_meta.json"

if [[ ! -f "$MARKER_MD" ]]; then
    echo "ERROR: marker did not produce $MARKER_MD" >&2
    exit 1
fi

# Read docling outputs (if it ran)
DOCLING_MD=""
DOCLING_META=""
DOCLING_PICS=""
DOCLING_TITLE=""
if [[ $DOCLING_RC -eq 0 && -f "$DOCLING_OUT/docling_meta.json" ]]; then
    DOCLING_MD="$DOCLING_OUT/docling.md"
    DOCLING_META="$DOCLING_OUT/docling_meta.json"
    DOCLING_PICS="$DOCLING_OUT/docling_pictures"
    DOCLING_TITLE=$(python3 -c "import json,sys; d=json.load(open('$DOCLING_META')); print(d.get('title') or '')" 2>/dev/null || echo "")
elif [[ -n "$DOCLING_PID" ]]; then
    echo "WARN: docling failed (rc=$DOCLING_RC). Tail of err log:" >&2
    tail -10 "$WORK/docling.err" >&2 || true
    echo "WARN: continuing with marker output only." >&2
fi

# Emit machine-readable paths
echo "marker_md=$MARKER_MD"
echo "marker_meta=$MARKER_META"
echo "marker_dir=$MARKER_DIR"
echo "docling_md=$DOCLING_MD"
echo "docling_meta=$DOCLING_META"
echo "docling_pics=$DOCLING_PICS"
echo "docling_title=$DOCLING_TITLE"
echo "stem=$STEM"
echo "marker_venv=$MARKER_VENV"
echo "docling_venv=$DOCLING_VENV"

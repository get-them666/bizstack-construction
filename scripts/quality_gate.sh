#!/usr/bin/env bash
# Canonical pre-commit gate. Run before every commit.
#   bash scripts/quality_gate.sh
set -euo pipefail
cd "$(dirname "$0")/.."

echo "[1/4] Full pytest suite"
./venv/bin/python -m pytest -q

echo "[2/4] Compile active Python entry points"
python3 -m py_compile \
  construction_main.py main.py bid_costing.py bid_opportunities.py \
  documents_service.py auto_reply.py followups.py inbound_email.py \
  kit_service.py lead_pipeline.py portal_service.py job_documents.py \
  bizstack_leads/*.py services/*.py scripts/*.py

echo "[3/4] Pyflakes blocking errors (undefined names / syntax)"
TMP="$(mktemp)"
trap 'rm -f "$TMP"' EXIT
python3 -m pyflakes \
  construction_main.py main.py bid_costing.py bid_opportunities.py \
  documents_service.py auto_reply.py followups.py inbound_email.py \
  kit_service.py lead_pipeline.py portal_service.py job_documents.py \
  bizstack_leads/*.py services/*.py scripts/*.py >"$TMP" 2>&1 || true
WARNINGS="$(wc -l <"$TMP" | tr -d ' ')"
if grep -qE 'undefined name|syntax error|invalid syntax' "$TMP"; then
  echo "BLOCKING pyflakes errors:"
  cat "$TMP"
  exit 1
fi
echo "pyflakes: 0 blocking errors, ${WARNINGS} non-blocking warning(s)"

echo "[4/4] Construction brand check"
if grep -rnE '908-7121|9087121|17579087121' \
    templates/construction construction_main.py >/tmp/brand_check.txt 2>&1; then
  echo "BRAND CONTAMINATION FOUND:"
  cat /tmp/brand_check.txt
  exit 1
fi
echo "brand check: construction templates/config contain no Broom number"

echo "Quality gate passed."

#!/usr/bin/env bash
# Runs every example scenario and writes outputs under runs/ (≈15 s total).
set -uo pipefail
cd "$(dirname "$0")/.."
PY=${PYTHON:-python3}
R=examples/requirements

echo "== 1. Greenfield (mandatory use case) =="
$PY -m sdlc_agent run -f $R/greenfield_url_shortener.md --run-id 01-greenfield --quiet

echo "== 2a. Injected transient fault + generated SQL drifting from the schema =="
$PY -m sdlc_agent run -f $R/greenfield_url_shortener.md --run-id 02a-recovery-schema-drift --quiet \
    --inject-fault transient:design_contract --inject-fault bug:gen_persistence

echo "== 2b. Injected API defect with no stack trace (wrong redirect status) =="
$PY -m sdlc_agent run -f $R/greenfield_url_shortener.md --run-id 02b-recovery-api-defect --quiet \
    --inject-fault bug:gen_api

echo "== 3. Brownfield (feature + bug fix on an existing repo) =="
$PY -m sdlc_agent run -f $R/brownfield_expiry_and_alias_bug.md --repo examples/legacy_links \
    --run-id 03-brownfield --quiet

echo "== 4a. Ambiguous requirement, autonomous policy (expected: escalated to a human) =="
$PY -m sdlc_agent run -f $R/ambiguous_campaign_links.md --run-id 04a-ambiguous-escalated --quiet

echo "== 4b. Ambiguous requirement, human answers recorded at the gates =="
$PY -m sdlc_agent run -f $R/ambiguous_campaign_links.md --run-id 04b-ambiguous-answered --quiet \
    --approval file --answers examples/answers/ambiguous_campaign_links.yaml

echo "== 5. Crash + resume from checkpoint =="
$PY -m sdlc_agent run -f $R/greenfield_url_shortener.md --run-id 05-crash-resume --quiet \
    --inject-fault crash:gen_service
$PY -m sdlc_agent resume runs/05-crash-resume --quiet
echo
echo "Done. Open runs/<id>/workspace/ENGINEERING_SUMMARY.md"

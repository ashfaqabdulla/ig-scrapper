#!/usr/bin/env bash
# End-to-end smoke test against a running container.
# Usage:  BASE=http://localhost:8099 ./tests/smoke.sh
set -euo pipefail
BASE="${BASE:-http://localhost:8099}"

echo "== health =="
curl -sf "$BASE/health" | tee /dev/stderr; echo

echo "== invalid collect (should be 422) =="
code=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$BASE/collect" \
       -H 'content-type: application/json' -d '{"users":[]}')
[ "$code" = "422" ] || { echo "expected 422, got $code"; exit 1; }

echo "== start a real collect job =="
JOB=$(curl -sf -X POST "$BASE/collect" \
      -H 'content-type: application/json' \
      -d '{"users":["instagram"],"max_per_user":1,"min_per_user":1,"concurrency":1,"user_timeout":90}' \
      | python -c 'import sys,json;print(json.load(sys.stdin)["id"])')
echo "job=$JOB"

for i in $(seq 1 60); do
  STATE=$(curl -sf "$BASE/jobs/$JOB" | python -c 'import sys,json;print(json.load(sys.stdin)["state"])')
  echo "  [$i] $STATE"
  [[ "$STATE" == "done" || "$STATE" == "failed" || "$STATE" == "cancelled" ]] && break
  sleep 2
done

curl -sf "$BASE/jobs/$JOB/result" | python -m json.tool

echo "== list =="
curl -sf "$BASE/jobs" | python -m json.tool | head -40

echo "OK"

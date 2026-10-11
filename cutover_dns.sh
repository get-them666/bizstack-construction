#!/usr/bin/env bash
# Move bizstackperks.com off the old shared Railway project and onto the two
# separate companies. Dry run by default; pass --apply to actually change it.
#
# Why this exists: both domains are proxied through Cloudflare and currently
# point at the old shared project's wildcard (bdceilsj.up.railway.app). Railway
# cannot reclaim a custom domain until DNS follows it.
#
#   Construction  construction.bizstackperks.com -> 3ut6mfik.up.railway.app
#   Hosts         bizstackperks.com              -> 3dwrys6b.up.railway.app
#
# Usage:
#   export CLOUDFLARE_API_TOKEN='...'      # zone:DNS:Edit on bizstackperks.com
#   ./cutover_dns.sh                       # inspect, change nothing
#   ./cutover_dns.sh --apply               # make the change
set -euo pipefail

ZONE_NAME="${ZONE_NAME:-bizstackperks.com}"
OLD_TARGET="${OLD_TARGET:-bdceilsj.up.railway.app}"
NEW_CONSTRUCTION="${NEW_CONSTRUCTION:-3ut6mfik.up.railway.app}"
NEW_HOSTS="${NEW_HOSTS:-3dwrys6b.up.railway.app}"
APPLY=0
[[ "${1:-}" == "--apply" ]] && APPLY=1

: "${CLOUDFLARE_API_TOKEN:?Set CLOUDFLARE_API_TOKEN first}"

api() {
  local path="$1" method="${2:-GET}" body="${3:-}"
  if [[ -n "$body" ]]; then
    curl -sS -X "$method" \
      -H "Authorization: Bearer ${CLOUDFLARE_API_TOKEN}" \
      -H "Content-Type: application/json" \
      --data "$body" \
      "https://api.cloudflare.com/client/v4${path}"
  else
    curl -sS -X "$method" \
      -H "Authorization: Bearer ${CLOUDFLARE_API_TOKEN}" \
      "https://api.cloudflare.com/client/v4${path}"
  fi
}

fail() { echo "ERROR: $*" >&2; exit 1; }

# --- token check -----------------------------------------------------------
tok=$(api "/user/tokens/verify")
echo "$tok" | grep -q '"success":true' || fail "token rejected: $(echo "$tok" | head -c 200)"

# --- zone ------------------------------------------------------------------
zone_json=$(api "/zones?name=${ZONE_NAME}")
zone_id=$(echo "$zone_json" | python3 -c 'import sys,json; d=json.load(sys.stdin); print((d.get("result") or [{}])[0].get("id",""))' 2>/dev/null || true)
[[ -n "$zone_id" ]] || fail "zone ${ZONE_NAME} not found — check the token has Zone:Read"
echo "zone ${ZONE_NAME}: ${zone_id}"

# --- current records -------------------------------------------------------
recs=$(api "/zones/${zone_id}/dns_records?per_page=500")
echo
echo "All current DNS records:"
echo "$recs" | python3 -c '
import sys, json
rows = json.load(sys.stdin).get("result", [])
if not rows:
    print("  (none)")
for r in rows:
    # Bind first, print second: keeps every quote out of the f-string, which is
    # what makes this survive the shell quoting.
    t = r.get("type", "?")
    n = r.get("name") or "@"
    c = r.get("content", "?")
    proxied = r.get("proxied")
    ttl = r.get("ttl")
    print("  %-6s %-34s -> %-36s proxied=%s ttl=%s" % (t, n, c, proxied, ttl))
'

# --- plan ------------------------------------------------------------------
# Cloudflare returns the apex record's "name" as the domain itself
# ("bizstackperks.com"), NOT as an empty string -- so apex has to be detected by
# comparing against the zone, not against "". Getting that wrong means the hosts
# domain silently never moves.
#
# Everything is passed via the environment rather than interpolated into the
# python source. Interpolating "${VAR}" inside a single-quoted python -c means
# escaping quotes inside quotes, which is where this went wrong twice.
plan=$(echo "$recs" | ZONE_NAME="$ZONE_NAME" OLD_TARGET="$OLD_TARGET" \
                NEW_CONSTRUCTION="$NEW_CONSTRUCTION" NEW_HOSTS="$NEW_HOSTS" \
  python3 -c '
import sys, json, os
zone = os.environ["ZONE_NAME"]
old = os.environ["OLD_TARGET"]
nc = os.environ["NEW_CONSTRUCTION"]
nh = os.environ["NEW_HOSTS"]
rows = []
for r in json.load(sys.stdin).get("result", []):
    name = (r.get("name") or "").rstrip(".")
    if r.get("content") != old:
        continue
    if name == zone:
        target = nh
    elif name == "construction." + zone:
        target = nc
    else:
        continue
    rows.append((r.get("id", ""), r.get("type", "CNAME"), name,
                 target, r.get("proxied"), r.get("ttl")))
for row in rows:
    print("\t".join(str(x) for x in row))
')

if [[ -z "$plan" ]]; then
  echo
  echo "Nothing pointing at ${OLD_TARGET} matched construction or the apex."
  echo "Already moved, or the records differ from what this expects. Nothing changed."
  exit 0
fi

echo
echo "Planned changes:"
while IFS=$'\t' read -r id type name target proxied ttl; do
  [[ -z "$id" ]] && continue
  echo "  ${type} ${name}  ->  ${target}   (proxied stays ${proxied})"
done <<< "$plan"

if [[ "$APPLY" -ne 1 ]]; then
  echo
  echo "Dry run. Re-run with --apply to make these changes."
  exit 0
fi

echo
echo "Applying..."
while IFS=$'\t' read -r id type name target proxied ttl; do
  [[ -z "$id" ]] && continue
  # Built via the environment, not by interpolating into python source: a
  # python3 -c "...json.dumps({'type':...})..." nested inside "$( )" inside a
  # double-quoted argument gets its quotes eaten by the shell and silently
  # runs five separate broken commands.
  payload=$(REC_TYPE="$type" REC_NAME="$name" REC_TARGET="$target" \
            REC_TTL="$ttl" REC_PROXIED="$proxied" python3 -c '
import json, os
print(json.dumps({
    "type": os.environ["REC_TYPE"],
    "name": os.environ["REC_NAME"],
    "content": os.environ["REC_TARGET"],
    "ttl": int(os.environ["REC_TTL"]),
    "proxied": os.environ["REC_PROXIED"] == "True",
}))
')
  resp=$(api "/zones/${zone_id}/dns_records/${id}" PATCH "$payload")
  if echo "$resp" | grep -q '"success":true'; then
    echo "  OK   ${type} ${name} -> ${target}"
  else
    echo "  FAIL ${type} ${name}: $(echo "$resp" | head -c 300)" >&2
    exit 1
  fi
done <<< "$plan"

echo
echo "Done. DNS will take a few minutes to propagate."
echo "Next: cut the domains over in Railway and verify."
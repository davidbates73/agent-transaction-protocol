#!/usr/bin/env bash
# One-shot, idempotent setup for the Hermes ATP reporting stack (run by the `setup` service in
# docker-compose.yml, as root inside the Elasticsearch image). Safe to re-run any time:
#   docker compose run --rm setup
#
#  1. TLS: self-signed CA + certificates for es01 and kibana (elasticsearch-certutil), if missing.
#  2. Waits for Elasticsearch; sets the kibana_system password.
#  3. Role hermes_exporter (create_doc + auto_configure on hermes-atp-records-*, nothing else)
#     and user hermes_exporter; the index template.
#  4. Waits for Kibana; role hermes_viewer (read-only: index read + dashboard/discover/visualize
#     read in the default space), user hermes_viewer; imports the saved objects of both
#     dashboards (Hermes ATP overview, Hermes ATP request lifecycle).
#
# Never prints a password: credentials are passed to curl through a config on a private fd,
# and request bodies that contain passwords are sent on stdin.
set -euo pipefail
umask 077

CERTS=/usr/share/elasticsearch/config/certs
CA="$CERTS/ca/ca.crt"
ES_URL="${ES_URL:-https://es01:9200}"
KIBANA_URL="${KIBANA_URL:-https://kibana:5601}"
HERE="$(cd "$(dirname "$0")" && pwd)"
KIBANA_DIR="${KIBANA_DIR:-/kibana}"

say() { printf '[setup] %s\n' "$*"; }
die() { printf '[setup] ERROR: %s\n' "$*" >&2; exit 1; }

for v in ELASTIC_PASSWORD KIBANA_PASSWORD HERMES_EXPORTER_PASSWORD HERMES_VIEWER_PASSWORD; do
  [ -n "${!v:-}" ] || die "$v is empty; set it in .env"
done

# JSON string literal for an arbitrary value (escapes backslash, quote and control chars).
json_str() {
  local s=$1
  s=${s//\\/\\\\}; s=${s//\"/\\\"}; s=${s//$'\n'/\\n}; s=${s//$'\r'/\\r}; s=${s//$'\t'/\\t}
  printf '"%s"' "$s"
}

# curl as `elastic`; the credentials never appear in argv or output.
#   req METHOD URL [extra curl args...]  -> prints "<http_code>" on the last line, body before it
req() {
  local method=$1 url=$2; shift 2
  local pw=${ELASTIC_PASSWORD//\\/\\\\}; pw=${pw//\"/\\\"}  # curl config quoting
  curl -sS --cacert "$CA" -X "$method" "$url" -K <(printf 'user = "elastic:%s"\n' "$pw") \
    -w '\n%{http_code}' "$@"
}
code_of() { tail -n1 <<<"$1"; }
body_of() { sed '$d' <<<"$1"; }
expect_2xx() {  # expect_2xx WHAT RESPONSE
  local c; c=$(code_of "$2")
  case "$c" in 2??) say "$1: ok ($c)";; *) die "$1 failed (HTTP $c): $(body_of "$2" | head -c 600)";; esac
}

# ---------------------------------------------------------------------------------------
# 1. certificates
# ---------------------------------------------------------------------------------------
cd /usr/share/elasticsearch
mkdir -p "$CERTS"
rm -f "$CERTS/.ready"
if [ ! -f "$CERTS/ca/ca.crt" ]; then
  say "creating a self-signed CA"
  bin/elasticsearch-certutil ca --silent --pem --days 3650 --out "$CERTS/ca.zip"
  unzip -oq "$CERTS/ca.zip" -d "$CERTS" && rm -f "$CERTS/ca.zip"
fi
if [ ! -f "$CERTS/es01/es01.crt" ] || [ ! -f "$CERTS/kibana/kibana.crt" ]; then
  say "creating certificates for es01 and kibana"
  {
    echo "instances:"
    echo "  - name: es01"
    echo "    dns: [es01, localhost${ES_PUBLIC_HOSTNAME:+, ${ES_PUBLIC_HOSTNAME//,/, }}]"
    echo "    ip: [127.0.0.1${ES_PUBLIC_IP:+, ${ES_PUBLIC_IP//,/, }}]"
    echo "  - name: kibana"
    echo "    dns: [kibana, localhost${KIBANA_PUBLIC_HOSTNAME:+, ${KIBANA_PUBLIC_HOSTNAME//,/, }}]"
    echo "    ip: [127.0.0.1]"
  } > "$CERTS/instances.yml"
  bin/elasticsearch-certutil cert --silent --pem --days 825 --in "$CERTS/instances.yml" \
    --out "$CERTS/certs.zip" --ca-cert "$CERTS/ca/ca.crt" --ca-key "$CERTS/ca/ca.key"
  unzip -oq "$CERTS/certs.zip" -d "$CERTS" && rm -f "$CERTS/certs.zip"
fi
# Elasticsearch and Kibana run as uid 1000 / gid 0 and need the certs and their own keys;
# the CA key stays root-only (re-run setup to issue new certificates).
chown -R root:root "$CERTS"
find "$CERTS" -type d -exec chmod 755 {} +
find "$CERTS" -type f -exec chmod 644 {} +
chown 1000:0 "$CERTS/es01/es01.key" "$CERTS/kibana/kibana.key"
chmod 600 "$CERTS/es01/es01.key" "$CERTS/kibana/kibana.key" "$CERTS/ca/ca.key"
touch "$CERTS/.ready"   # the setup healthcheck (es01 waits for it) -- only after permissions are right
say "certificates ready (CA: certs/ca/ca.crt -- copy it to the exporter host)"

# ---------------------------------------------------------------------------------------
# 2. Elasticsearch
# ---------------------------------------------------------------------------------------
say "waiting for Elasticsearch at $ES_URL"
until curl -s --cacert "$CA" "$ES_URL" | grep -q "missing authentication credentials"; do sleep 3; done
until [ "$(code_of "$(req GET "$ES_URL/_cluster/health?wait_for_status=yellow&timeout=5s" -o /dev/null)")" = 200 ]; do
  sleep 3
done

r=$(printf '{"password":%s}' "$(json_str "$KIBANA_PASSWORD")" |
    req POST "$ES_URL/_security/user/kibana_system/_password" -H 'Content-Type: application/json' --data-binary @-)
expect_2xx "kibana_system password" "$r"

r=$(req PUT "$ES_URL/_security/role/hermes_exporter" -H 'Content-Type: application/json' \
    --data-binary "@$HERE/role-hermes-exporter.json")
expect_2xx "role hermes_exporter" "$r"

r=$(printf '{"password":%s,"roles":["hermes_exporter"],"full_name":"Hermes ATP exporter (write-only)","metadata":{"managed_by":"hermes-atp-elastic"}}' \
      "$(json_str "$HERMES_EXPORTER_PASSWORD")" |
    req PUT "$ES_URL/_security/user/hermes_exporter" -H 'Content-Type: application/json' --data-binary @-)
expect_2xx "user hermes_exporter" "$r"

r=$(req PUT "$ES_URL/_index_template/hermes-atp-records" -H 'Content-Type: application/json' \
    --data-binary "@$HERE/index-template.json")
expect_2xx "index template hermes-atp-records" "$r"

r=$(req GET "$ES_URL/_license")
lic=$(body_of "$r" | grep -o '"type" *: *"[a-z]*"' | head -n1 | sed 's/.*"\([a-z]*\)"$/\1/')
[ "$lic" = basic ] || die "license type is '$lic', expected 'basic' (this stack must not use trial/paid features)"
say "license: basic"

# ---------------------------------------------------------------------------------------
# 3. Kibana
# ---------------------------------------------------------------------------------------
say "waiting for Kibana at $KIBANA_URL"
until body_of "$(req GET "$KIBANA_URL/api/status")" | grep -q '"level":"available"'; do sleep 5; done

r=$(req PUT "$KIBANA_URL/api/security/role/hermes_viewer" -H 'kbn-xsrf: hermes-atp-setup' \
    -H 'Content-Type: application/json' --data-binary "@$HERE/role-hermes-viewer.kibana.json")
expect_2xx "role hermes_viewer" "$r"

r=$(printf '{"password":%s,"roles":["hermes_viewer"],"full_name":"Hermes ATP viewer (read-only)","metadata":{"managed_by":"hermes-atp-elastic"}}' \
      "$(json_str "$HERMES_VIEWER_PASSWORD")" |
    req PUT "$ES_URL/_security/user/hermes_viewer" -H 'Content-Type: application/json' --data-binary @-)
expect_2xx "user hermes_viewer" "$r"

# Overview first: it carries the shared data view. Both files also import on their own.
for nd in hermes-atp-dashboard.ndjson hermes-atp-lifecycle.ndjson; do
  r=$(req POST "$KIBANA_URL/api/saved_objects/_import?overwrite=true" -H 'kbn-xsrf: hermes-atp-setup' \
      --form "file=@$KIBANA_DIR/$nd")
  expect_2xx "import saved objects $nd" "$r"
  body_of "$r" | grep -q '"success":true' || die "saved-object import of $nd reported errors: $(body_of "$r" | head -c 800)"
done

say "setup complete"

#!/usr/bin/env bash
# TEST-ONLY https server for the "Import from a link" walk-throughs (app_flow, service_flow,
# split_flow). Makes a throwaway CA (with the key-usage extensions Python 3.13 requires), a
# certificate for 127.0.0.1, and big.csv (the labelled sample repeated to 150,000 rows, so it is
# above AGENTMETER_MAX_CSV_ROWS=100000 and gets sampled), then serves it on https://127.0.0.1:8443.
#   bash tests/e2e/url_fixture.sh <dir>        # foreground; Ctrl-C to stop
# Then start the backend with the test-only switches (never in production):
#   AGENTMETER_TEST_ALLOW_LOOPBACK_URLS=1 AGENTMETER_TEST_URL_CAFILE=<dir>/ca.pem python scripts/serve.py ...
#   E2E_URL_BASE=https://127.0.0.1:8443 node tests/e2e/app_flow.js      (74 checks)
#   E2E_URL_BASE=https://127.0.0.1:8443 node tests/e2e/service_flow.js  (38 checks; backend with
#       AGENTMETER_MAX_UPLOAD_MB=1 AGENTMETER_MAX_CSV_ROWS=100000)
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
D="${1:?usage: url_fixture.sh <dir>}"; mkdir -p "$D/www"; cd "$D"
openssl req -x509 -newkey rsa:2048 -nodes -keyout ca.key -out ca.pem -days 2 -subj "/CN=agentmeter-e2e-ca" \
  -addext "basicConstraints=critical,CA:TRUE" -addext "keyUsage=critical,keyCertSign,cRLSign" 2>/dev/null
openssl req -newkey rsa:2048 -nodes -keyout srv.key -out srv.csr -subj "/CN=127.0.0.1" 2>/dev/null
printf "subjectAltName=IP:127.0.0.1,DNS:localhost\nkeyUsage=critical,digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\nbasicConstraints=CA:FALSE\nauthorityKeyIdentifier=keyid\nsubjectKeyIdentifier=hash\n" > ext.cnf
openssl x509 -req -in srv.csr -CA ca.pem -CAkey ca.key -CAcreateserial -out srv.pem -days 2 -extfile ext.cnf 2>/dev/null
python3 - "$REPO/data/sample_csv/cicids2017_sample.csv" www/big.csv <<'PY'
import csv, sys
rows = list(csv.reader(open(sys.argv[1], newline="")))
with open(sys.argv[2], "w", newline="") as f:
    w = csv.writer(f); w.writerow(rows[0]); n = 0
    while n < 150000:
        for r in rows[1:]:
            w.writerow(r); n += 1
PY
echo "serving https://127.0.0.1:8443/big.csv (CA: $D/ca.pem)"
exec python3 - "$D/www" <<'PY'
import functools, http.server, ssl, sys
srv = http.server.ThreadingHTTPServer(("127.0.0.1", 8443), functools.partial(http.server.SimpleHTTPRequestHandler, directory=sys.argv[1]))
ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); ctx.load_cert_chain("srv.pem", "srv.key")
srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
srv.serve_forever()
PY

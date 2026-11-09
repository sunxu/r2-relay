#!/usr/bin/env bash
# External Owner Mac acceptance for the deployed R2 Relay HTTPS endpoint.
# Requires the public CA PEM obtained over trusted SSH; never needs a token.
set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "Usage: $0 /absolute/path/to/r2-root.crt" >&2
  exit 2
fi

CA_FILE="$1"
EXPECTED_SHA256="2a5fd6545942b88f3b3c13f9db4e3e04cfc9e251ad39282440ba948440a2e80b"

if [[ ! -f "$CA_FILE" || -L "$CA_FILE" ]]; then
  echo "FAIL: CA file must be a regular, non-symlink file" >&2
  exit 2
fi
chmod 600 "$CA_FILE"
ACTUAL_SHA256="$(shasum -a 256 "$CA_FILE" | awk '{print $1}')"
if [[ "$ACTUAL_SHA256" != "$EXPECTED_SHA256" ]]; then
  echo "FAIL: CA SHA-256 mismatch"
  echo "expected=$EXPECTED_SHA256"
  echo "actual=$ACTUAL_SHA256"
  exit 1
fi
openssl x509 -in "$CA_FILE" -noout >/dev/null
echo "PASS: CA SHA-256 verified ($ACTUAL_SHA256)"

check() {
  local label="$1"
  local url="$2"
  local expected_code="$3"
  local result code verify remote_ip

  if ! result="$(curl --noproxy '*' --silent --show-error \
      --connect-timeout 10 --max-time 20 --cacert "$CA_FILE" \
      --output /dev/null \
      --write-out $'http_code=%{http_code}\nssl_verify_result=%{ssl_verify_result}\nremote_ip=%{remote_ip}\n' \
      "$url")"; then
    echo "FAIL: $label (connection, TLS chain, or IP SAN verification)"
    return 1
  fi

  code="$(printf '%s\n' "$result" | awk -F= '$1=="http_code"{print $2}')"
  verify="$(printf '%s\n' "$result" | awk -F= '$1=="ssl_verify_result"{print $2}')"
  remote_ip="$(printf '%s\n' "$result" | awk -F= '$1=="remote_ip"{print $2}')"

  if [[ "$code" != "$expected_code" || "$verify" != "0" ]]; then
    echo "FAIL: $label expected_http=$expected_code actual_http=$code ssl_verify_result=$verify remote_ip=$remote_ip"
    return 1
  fi
  echo "PASS: $label http=$code ssl_verify_result=$verify remote_ip=$remote_ip"
}

failed=0
check "IPv4 healthz" "https://104.224.168.192:8443/healthz" "200" || failed=1
check "IPv4 unauthenticated metrics" "https://104.224.168.192:8443/v1/metrics" "401" || failed=1
check "IPv6 healthz" "https://[2607:8700:5500:a47f::2]:8443/healthz" "200" || failed=1
check "IPv6 unauthenticated metrics" "https://[2607:8700:5500:a47f::2]:8443/v1/metrics" "401" || failed=1

if [[ "$failed" -ne 0 ]]; then
  echo "RESULT: EXTERNAL_HTTPS_ACCEPTANCE_FAILED"
  exit 1
fi
echo "RESULT: EXTERNAL_HTTPS_ACCEPTANCE_PASS"

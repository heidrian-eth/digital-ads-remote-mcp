#!/usr/bin/env bash
set -euo pipefail

# Resolve script directory (for venv + .env discovery)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Load configuration from .env (sibling to this script). Required keys:
#   GCP_PROJECT, PRIVATE_KEY_SECRET, API_KEY_SECRET, SERVICE_URL
# See .env.example for a template.
ENV_FILE="${SCRIPT_DIR}/.env"
if [[ ! -f "$ENV_FILE" ]]; then
    echo "Error: ${ENV_FILE} not found. Copy .env.example to .env and fill it in." >&2
    exit 1
fi
set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

for var in GCP_PROJECT PRIVATE_KEY_SECRET API_KEY_SECRET SERVICE_URL; do
    if [[ -z "${!var:-}" ]]; then
        echo "Error: ${var} is not set in ${ENV_FILE}" >&2
        exit 1
    fi
done

VENV_PYTHON="${SCRIPT_DIR}/.venv/bin/python3"

if [[ ! -x "$VENV_PYTHON" ]]; then
    echo "Error: venv not found at ${SCRIPT_DIR}/.venv" >&2
    echo "Run: python3 -m venv .venv && .venv/bin/pip install eciespy" >&2
    exit 1
fi

# Fetch secrets from GCP
echo "Fetching secrets from GCP..." >&2
PRIVATE_KEY_HEX=$(gcloud secrets versions access latest \
    --secret="$PRIVATE_KEY_SECRET" --project="$GCP_PROJECT")
API_KEY=$(gcloud secrets versions access latest \
    --secret="$API_KEY_SECRET" --project="$GCP_PROJECT")

# Derive public key
PUBLIC_KEY_HEX=$("$VENV_PYTHON" -c "
from coincurve import PrivateKey
sk = PrivateKey(bytes.fromhex('${PRIVATE_KEY_HEX}'))
print(sk.public_key.format(False).hex())
")
echo "Public key derived." >&2

encrypt() {
    local plaintext="$1"
    "$VENV_PYTHON" -c "
import ecies, base64, sys
ciphertext = ecies.encrypt('${PUBLIC_KEY_HEX}', sys.argv[1].encode())
print(base64.urlsafe_b64encode(ciphertext).decode().rstrip('='))
" "$plaintext"
}

encrypt_file() {
    local filepath="$1"
    "$VENV_PYTHON" -c "
import ecies, base64, sys
with open(sys.argv[1], 'rb') as f:
    content = f.read()
ciphertext = ecies.encrypt('${PUBLIC_KEY_HEX}', content)
print(base64.urlsafe_b64encode(ciphertext).decode().rstrip('='))
" "$filepath"
}

usage() {
    echo "Usage: $0 <facebook|googleads> [options]"
    echo
    echo "Commands:"
    echo "  facebook   Generate Facebook Ads MCP URL"
    echo "    --token TOKEN           Facebook access token (or set FB_ACCESS_TOKEN)"
    echo
    echo "  googleads  Generate Google Ads MCP URL"
    echo "    --developer-token TOKEN Developer token (or set GOOGLE_ADS_DEVELOPER_TOKEN)"
    echo "    --customer-id ID        Login customer ID (optional)"
    echo "    --credentials FILE      Service account JSON file (optional)"
    echo
    exit 1
}

generate_facebook() {
    local token="${FB_ACCESS_TOKEN:-}"

    while [[ $# -gt 0 ]]; do
        case "$1" in
            --token) token="$2"; shift 2 ;;
            *) echo "Unknown option: $1" >&2; usage ;;
        esac
    done

    if [[ -z "$token" ]]; then
        read -rsp "Facebook access token: " token
        echo >&2
    fi

    local enc_token
    enc_token=$(encrypt "$token")

    echo "${SERVICE_URL}/facebookads/mcp?api_key=${API_KEY}&ENC_FB_ACCESS_TOKEN=${enc_token}"
}

generate_googleads() {
    local dev_token="${GOOGLE_ADS_DEVELOPER_TOKEN:-}"
    local customer_id="${GOOGLE_ADS_LOGIN_CUSTOMER_ID:-}"
    local credentials_file=""

    while [[ $# -gt 0 ]]; do
        case "$1" in
            --developer-token) dev_token="$2"; shift 2 ;;
            --customer-id) customer_id="$2"; shift 2 ;;
            --credentials) credentials_file="$2"; shift 2 ;;
            *) echo "Unknown option: $1" >&2; usage ;;
        esac
    done

    if [[ -z "$dev_token" ]]; then
        read -rsp "Google Ads developer token: " dev_token
        echo >&2
    fi

    local enc_dev_token
    enc_dev_token=$(encrypt "$dev_token")

    local url="${SERVICE_URL}/googleads/mcp?api_key=${API_KEY}&ENC_GOOGLE_ADS_DEVELOPER_TOKEN=${enc_dev_token}"

    if [[ -n "$customer_id" ]]; then
        url="${url}&PLAIN_GOOGLE_ADS_LOGIN_CUSTOMER_ID=${customer_id}"
    fi

    if [[ -n "$credentials_file" ]]; then
        if [[ ! -f "$credentials_file" ]]; then
            echo "Error: credentials file not found: $credentials_file" >&2
            exit 1
        fi
        local enc_creds
        enc_creds=$(encrypt_file "$credentials_file")
        url="${url}&ENCFILE_GOOGLE_APPLICATION_CREDENTIALS=${enc_creds}"
    fi

    echo "$url"
}

# Main
[[ $# -lt 1 ]] && usage

command="$1"; shift

case "$command" in
    facebook)  generate_facebook "$@" ;;
    googleads) generate_googleads "$@" ;;
    *)         usage ;;
esac

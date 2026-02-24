# Digital Ads Remote MCP

Remote deployment wrapper for digital ads MCP servers. Currently supports:
- [Google Ads MCP](https://github.com/googleads/google-ads-mcp) → `/googleads/mcp`
- [Google Analytics MCP](https://github.com/googleanalytics/google-analytics-mcp) → `/analytics/mcp`
- [Facebook Ads MCP](https://github.com/gomarble-ai/facebook-ads-mcp-server) → `/facebookads/mcp`

## Features

- **HTTP JSON-RPC Transport**: Cloud-native HTTP-based MCP server
- **Stateless Architecture**: No session storage, scales to zero
- **API Key Authentication**: Validate clients via API keys
- **Per-Request Credentials**: Users provide their own platform-specific credentials (e.g., Google Ads developer tokens)
- **Multi-Cloud Ready**: Deploy to any container platform
- **Auto-Updates**: Uses git submodule to track upstream changes

## Architecture

This repository wraps digital ads MCP servers (such as `google-ads-mcp`) as git submodules and adds:
- Remote server transport (HTTP JSON-RPC)
- Authentication layer (API keys)
- Credential injection (per-request platform credentials)
- Container deployment configuration

## Local Development

```bash
# Clone with submodule
git clone --recurse-submodules <your-repo-url>
cd digital-ads-remote-mcp

# Build Docker image
docker build -t digital-ads-remote-mcp .

# Run locally for testing
docker run -p 8080:8080 \
  -e ALLOWED_API_KEYS="test-key-1,test-key-2" \
  -e GOOGLE_APPLICATION_CREDENTIALS="/path/to/credentials.json" \
  -v /path/to/credentials.json:/path/to/credentials.json:ro \
  digital-ads-remote-mcp
```

## Deployment

### Google Cloud Run

```bash
# Build and push to Google Container Registry
gcloud builds submit --tag gcr.io/PROJECT_ID/digital-ads-remote-mcp

# Deploy to Cloud Run
gcloud run deploy digital-ads-remote-mcp \
  --image=gcr.io/PROJECT_ID/digital-ads-remote-mcp \
  --platform=managed \
  --region=us-central1 \
  --allow-unauthenticated \
  --set-env-vars=ALLOWED_API_KEYS="key1,key2"
```

### AWS Fargate

```bash
# Build and push to ECR
aws ecr get-login-password --region us-east-1 | docker login --username AWS --password-stdin ACCOUNT_ID.dkr.ecr.us-east-1.amazonaws.com
docker build -t digital-ads-remote-mcp .
docker tag digital-ads-remote-mcp:latest ACCOUNT_ID.dkr.ecr.us-east-1.amazonaws.com/digital-ads-remote-mcp:latest
docker push ACCOUNT_ID.dkr.ecr.us-east-1.amazonaws.com/digital-ads-remote-mcp:latest

# Deploy via AWS CLI (ensure task definition and service exist)
aws ecs update-service \
  --cluster your-cluster \
  --service digital-ads-remote-mcp \
  --force-new-deployment
```

### Azure Container Apps

```bash
# Build and push to Azure Container Registry
az acr build --registry YOUR_ACR_NAME --image digital-ads-remote-mcp:latest .

# Deploy to Azure Container Apps
az containerapp create \
  --name digital-ads-remote-mcp \
  --resource-group YOUR_RESOURCE_GROUP \
  --environment YOUR_CONTAINER_APP_ENV \
  --image YOUR_ACR_NAME.azurecr.io/digital-ads-remote-mcp:latest \
  --target-port 8080 \
  --ingress external \
  --env-vars ALLOWED_API_KEYS="key1,key2"
```

## Encryption Setup

Environment variables can be encrypted using ECIES (Elliptic Curve Integrated Encryption Scheme) with secp256k1.

### Generate Key Pair

```bash
# Install eciespy (uses coincurve under the hood)
pip install eciespy

# Generate a new key pair
python3 -c "
from coincurve import PrivateKey
import secrets

sk = PrivateKey(secrets.token_bytes(32))
print(f'Private key (keep secret): {sk.secret.hex()}')
print(f'Public key (for clients):  {sk.public_key.format(False).hex()}')
"

# Derive public key from an existing hex private key
python3 -c "
from coincurve import PrivateKey
private_key_hex = 'YOUR_PRIVATE_KEY_HEX'
sk = PrivateKey(bytes.fromhex(private_key_hex))
print(sk.public_key.format(False).hex())
"
```

### Encrypt Values (Client-Side)

```bash
# Encrypt a string
python3 -c "
import ecies
import base64
import sys

public_key = 'YOUR_PUBLIC_KEY_HEX'
plaintext = sys.argv[1]

ciphertext = ecies.encrypt(public_key, plaintext.encode())
print(base64.urlsafe_b64encode(ciphertext).decode().rstrip('='))
" "your-secret-value"

# Encrypt a file
python3 -c "
import ecies
import base64
import sys

public_key = 'YOUR_PUBLIC_KEY_HEX'
with open(sys.argv[1], 'rb') as f:
    content = f.read()

ciphertext = ecies.encrypt(public_key, content)
print(base64.urlsafe_b64encode(ciphertext).decode().rstrip('='))
" credentials.json
```

## Client Configuration

Environment variables are passed via query parameters with type prefixes:

| Prefix | Description | Example |
|--------|-------------|---------|
| `PLAIN_` | Plaintext value | `?PLAIN_FOO=bar` → `FOO=bar` |
| `FILE_` | Content written to temp file | `?FILE_CREDS={}` → `CREDS=/tmp/xxx.json` |
| `ENC_` | Encrypted value (base64url) | `?ENC_TOKEN=abc...` → `TOKEN=decrypted` |
| `ENCFILE_` | Encrypted content to temp file | `?ENCFILE_CREDS=abc...` → `CREDS=/tmp/xxx.json` |

### MCP Client Configuration

```json
{
  "mcpServers": {
    "google-ads": {
      "url": "https://your-service-url/googleads/mcp?api_key=your-api-key&ENC_GOOGLE_ADS_DEVELOPER_TOKEN=abc123...&PLAIN_GOOGLE_ADS_LOGIN_CUSTOMER_ID=1234567890",
      "transport": "http"
    },
    "google-analytics": {
      "url": "https://your-service-url/analytics/mcp?api_key=your-api-key&ENCFILE_GOOGLE_APPLICATION_CREDENTIALS=xyz789...",
      "transport": "http"
    },
    "facebook-ads": {
      "url": "https://your-service-url/facebookads/mcp?api_key=your-api-key&ENC_FB_ACCESS_TOKEN=abc123...",
      "transport": "http"
    }
  }
}
```

### Required Parameters

**Google Ads MCP** (`/googleads/mcp`):
- `GOOGLE_ADS_DEVELOPER_TOKEN`: Your Google Ads API developer token
- `GOOGLE_ADS_LOGIN_CUSTOMER_ID`: (Optional) Manager customer ID
- `GOOGLE_APPLICATION_CREDENTIALS`: (Optional) Path to service account JSON (use `FILE_` or `ENCFILE_` prefix)

**Google Analytics MCP** (`/analytics/mcp`):
- `GOOGLE_APPLICATION_CREDENTIALS`: Path to service account JSON (use `FILE_` or `ENCFILE_` prefix)
- `GOOGLE_PROJECT_ID`: Google Cloud project ID
- `ANALYTICS_PROPERTY_ID`: (Optional) Default GA4 property ID

**Facebook Ads MCP** (`/facebookads/mcp`):
- `FB_ACCESS_TOKEN`: Facebook User Access Token with `ads_read` permission (required)

## Keeping Up-to-Date

The MCP servers are included as git submodules. To update:

```bash
# Update all submodules to latest version
git submodule update --remote

# Commit the updates
git add google-ads-mcp google-analytics-mcp facebook-ads-mcp
git commit -m "Update MCP submodules to latest"

# Redeploy to your platform
```

## Environment Variables

Server-side environment variables:

- `ALLOWED_API_KEYS`: Comma-separated list of valid API keys
- `ECIES_PRIVATE_KEY`: Hex-encoded secp256k1 private key for decrypting `ENC_*` and `ENCFILE_*` parameters

## Security Considerations

1. **API Keys**: Store in your platform's secret manager, rotate regularly
2. **Developer Tokens**: Never stored server-side, passed per-request
3. **HTTPS Only**: Ensure your platform enforces HTTPS
4. **Service Account**: Use minimal permissions for Google Ads API access
5. **CORS**: Configure `allow_origins` in production

## Testing

```bash
# Health check
curl https://your-service-url/health

# Google Ads - list tools
curl -X POST "https://your-service-url/googleads/mcp?api_key=your-api-key&PLAIN_GOOGLE_ADS_DEVELOPER_TOKEN=your-dev-token" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"tools/list","id":1}'

# Google Analytics - list tools (with service account file)
curl -X POST "https://your-service-url/analytics/mcp?api_key=your-api-key&FILE_GOOGLE_APPLICATION_CREDENTIALS=$(cat sa.json)" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"tools/list","id":1}'

# Facebook Ads - list tools
curl -X POST "https://your-service-url/facebookads/mcp?api_key=your-api-key&PLAIN_FB_ACCESS_TOKEN=your-fb-token" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"tools/list","id":1}'
```

## Troubleshooting

- **401 Unauthorized**: Check `api_key` query param matches `ALLOWED_API_KEYS`
- **400 Invalid params**: Check encryption/decryption - ensure `ECIES_PRIVATE_KEY` is set
- **500 Internal Error**: Check container logs for MCP or API errors

## License

Apache 2.0 / MIT (same as upstream MCP servers:
[google-ads-mcp](https://github.com/googleads/google-ads-mcp),
[google-analytics-mcp](https://github.com/googleanalytics/google-analytics-mcp),
[facebook-ads-mcp](https://github.com/gomarble-ai/facebook-ads-mcp-server))

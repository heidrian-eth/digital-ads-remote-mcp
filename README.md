# Google Ads MCP - Remote Server

Remote deployment wrapper for [Google Ads MCP](https://github.com/googleads/google-ads-mcp).

## Features

- **HTTP JSON-RPC Transport**: Cloud-native HTTP-based MCP server
- **Stateless Architecture**: No session storage, scales to zero
- **API Key Authentication**: Validate clients via API keys
- **Per-Request Credentials**: Users provide their own Google Ads developer tokens
- **Multi-Cloud Ready**: Deploy to any container platform
- **Auto-Updates**: Uses git submodule to track upstream changes

## Architecture

This repository wraps the original `google-ads-mcp` as a git submodule and adds:
- Remote server transport (HTTP JSON-RPC)
- Authentication layer (API keys)
- Credential injection (per-request developer tokens)
- Container deployment configuration

## Local Development

```bash
# Clone with submodule
git clone --recurse-submodules <your-repo-url>
cd google-ads-mcp-remote

# Build Docker image
docker build -t google-ads-mcp-remote .

# Run locally for testing
docker run -p 8080:8080 \
  -e ALLOWED_API_KEYS="test-key-1,test-key-2" \
  -e GOOGLE_APPLICATION_CREDENTIALS="/path/to/credentials.json" \
  -v /path/to/credentials.json:/path/to/credentials.json:ro \
  google-ads-mcp-remote
```

## Deployment

### Google Cloud Run

```bash
# Build and push to Google Container Registry
gcloud builds submit --tag gcr.io/PROJECT_ID/google-ads-mcp-remote

# Deploy to Cloud Run
gcloud run deploy google-ads-mcp-remote \
  --image=gcr.io/PROJECT_ID/google-ads-mcp-remote \
  --platform=managed \
  --region=us-central1 \
  --allow-unauthenticated \
  --set-env-vars=ALLOWED_API_KEYS="key1,key2"
```

### AWS Fargate

```bash
# Build and push to ECR
aws ecr get-login-password --region us-east-1 | docker login --username AWS --password-stdin ACCOUNT_ID.dkr.ecr.us-east-1.amazonaws.com
docker build -t google-ads-mcp-remote .
docker tag google-ads-mcp-remote:latest ACCOUNT_ID.dkr.ecr.us-east-1.amazonaws.com/google-ads-mcp-remote:latest
docker push ACCOUNT_ID.dkr.ecr.us-east-1.amazonaws.com/google-ads-mcp-remote:latest

# Deploy via AWS CLI (ensure task definition and service exist)
aws ecs update-service \
  --cluster your-cluster \
  --service google-ads-mcp-remote \
  --force-new-deployment
```

### Azure Container Apps

```bash
# Build and push to Azure Container Registry
az acr build --registry YOUR_ACR_NAME --image google-ads-mcp-remote:latest .

# Deploy to Azure Container Apps
az containerapp create \
  --name google-ads-mcp-remote \
  --resource-group YOUR_RESOURCE_GROUP \
  --environment YOUR_CONTAINER_APP_ENV \
  --image YOUR_ACR_NAME.azurecr.io/google-ads-mcp-remote:latest \
  --target-port 8080 \
  --ingress external \
  --env-vars ALLOWED_API_KEYS="key1,key2"
```

## Client Configuration

Configure MCP clients (Claude Desktop, etc.) to connect:

```json
{
  "mcpServers": {
    "google-ads-remote": {
      "url": "https://your-service-url/googleads/mcp",
      "transport": "http",
      "headers": {
        "X-API-Key": "your-api-key",
        "X-Developer-Token": "your-google-ads-developer-token",
        "X-Login-Customer-ID": "optional-manager-customer-id"
      }
    }
  }
}
```

### Required Headers

- `X-API-Key`: API key for authentication (validated against `ALLOWED_API_KEYS`)
- `X-Developer-Token`: Google Ads API developer token (per-user)
- `X-Login-Customer-ID`: (Optional) Manager customer ID for account access

## Keeping Up-to-Date

The original `google-ads-mcp` is included as a git submodule. To update:

```bash
# Update submodule to latest version
git submodule update --remote

# Commit the update
git add google-ads-mcp
git commit -m "Update google-ads-mcp submodule to latest"

# Redeploy to your platform
```

## Environment Variables

- `ALLOWED_API_KEYS`: Comma-separated list of valid API keys
- `GOOGLE_APPLICATION_CREDENTIALS`: Path to service account credentials (auto-set by most container platforms)

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

# Test MCP endpoint
curl -X POST https://your-service-url/googleads/mcp \
  -H "X-API-Key: your-api-key" \
  -H "X-Developer-Token: your-dev-token" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"tools/list","id":1}'
```

## Troubleshooting

- **401 Unauthorized**: Check API key in `ALLOWED_API_KEYS`
- **400 Missing Token**: Include `X-Developer-Token` header
- **500 Internal Error**: Check container logs for Google Ads API errors

## License

Same as [google-ads-mcp](https://github.com/googleads/google-ads-mcp) - Apache 2.0

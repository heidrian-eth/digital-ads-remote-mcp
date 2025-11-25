"""
Remote MCP Server Wrapper for Google Ads MCP.

This wrapper:
- Calls the original google-ads-mcp via subprocess with injected credentials
- Adds HTTP JSON-RPC transport (replacing stdio)
- Validates API keys from X-API-Key header
- Injects per-request credentials as subprocess environment variables
- Deployable to any container platform (Cloud Run, Fargate, Azure Container Apps, etc.)
"""

import os
import sys
import json
import logging
import subprocess

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.routing import Route
from starlette.requests import Request
from starlette.responses import JSONResponse

logger = logging.getLogger(__name__)

# Path to the worker script
WORKER_SCRIPT = os.path.join(os.path.dirname(__file__), 'worker.py')

# Add submodule to PYTHONPATH for the worker subprocess
SUBMODULE_PATH = os.path.join(os.path.dirname(__file__), 'google-ads-mcp')


def validate_api_key(api_key: str) -> bool:
    """Validate API key against allowed list."""
    allowed_keys = os.environ.get('ALLOWED_API_KEYS', '').split(',')
    allowed_keys = [k.strip() for k in allowed_keys if k.strip()]

    if not allowed_keys:
        logger.warning("ALLOWED_API_KEYS not configured - rejecting all requests")
        return False

    return api_key in allowed_keys


def execute_mcp_request(body: dict, dev_token: str, login_customer_id: str = None) -> dict:
    """
    Execute MCP request in a subprocess with injected credentials.

    This is like running:
        GOOGLE_ADS_DEVELOPER_TOKEN=xxx python worker.py
    """
    # Build environment for subprocess - inherit current env and add credentials
    env = os.environ.copy()
    env['GOOGLE_ADS_DEVELOPER_TOKEN'] = dev_token
    if login_customer_id:
        env['GOOGLE_ADS_LOGIN_CUSTOMER_ID'] = login_customer_id
    elif 'GOOGLE_ADS_LOGIN_CUSTOMER_ID' in env:
        del env['GOOGLE_ADS_LOGIN_CUSTOMER_ID']

    # Add submodule to PYTHONPATH
    python_path = env.get('PYTHONPATH', '')
    env['PYTHONPATH'] = f"{SUBMODULE_PATH}:{python_path}" if python_path else SUBMODULE_PATH

    # Run worker script with request as stdin
    result = subprocess.run(
        [sys.executable, WORKER_SCRIPT],
        input=json.dumps(body),
        capture_output=True,
        text=True,
        env=env,
        timeout=300,  # 5 minute timeout
    )

    if result.returncode != 0:
        logger.error(f"Worker stderr: {result.stderr}")
        return {
            'jsonrpc': '2.0',
            'id': body.get('id'),
            'error': {'code': -32603, 'message': f'Worker error: {result.stderr}'}
        }

    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as e:
        logger.error(f"Invalid JSON from worker: {result.stdout}")
        return {
            'jsonrpc': '2.0',
            'id': body.get('id'),
            'error': {'code': -32603, 'message': f'Invalid response from worker: {str(e)}'}
        }


async def health_check(request: Request) -> JSONResponse:
    """Health check endpoint for container platforms."""
    return JSONResponse({'status': 'healthy', 'service': 'google-ads-mcp-remote'})


async def handle_mcp_request(request: Request) -> JSONResponse:
    """
    Handle MCP request with authentication and credential injection.

    Credentials are passed as environment variables to a subprocess,
    ensuring complete isolation (like: ENV=value ./command).
    """
    # Extract API key
    api_key = request.headers.get('x-api-key', '')
    if not validate_api_key(api_key):
        logger.warning("Invalid API key attempted")
        return JSONResponse(
            {'error': 'Unauthorized: Invalid API key'},
            status_code=401
        )

    # Extract Google Ads credentials
    dev_token = request.headers.get('x-developer-token', '')
    login_customer_id = request.headers.get('x-login-customer-id', '')

    if not dev_token:
        logger.warning("Missing X-Developer-Token header")
        return JSONResponse(
            {'error': 'Bad Request: X-Developer-Token header required'},
            status_code=400
        )

    try:
        body = await request.json()

        if not isinstance(body, dict):
            return JSONResponse({
                'jsonrpc': '2.0',
                'id': None,
                'error': {'code': -32600, 'message': 'Invalid request'}
            }, status_code=400)

        # Execute in subprocess with injected credentials
        result = execute_mcp_request(body, dev_token, login_customer_id)
        return JSONResponse(result)

    except subprocess.TimeoutExpired:
        logger.error("Worker timeout")
        return JSONResponse({
            'jsonrpc': '2.0',
            'id': None,
            'error': {'code': -32603, 'message': 'Request timeout'}
        }, status_code=504)

    except Exception as e:
        logger.exception(f"Error handling request: {e}")
        return JSONResponse({
            'jsonrpc': '2.0',
            'id': None,
            'error': {'code': -32603, 'message': f'Internal error: {str(e)}'}
        }, status_code=500)


# Create Starlette ASGI application
app = Starlette(
    debug=False,
    routes=[
        Route('/', endpoint=health_check, methods=['GET']),
        Route('/health', endpoint=health_check, methods=['GET']),
        Route('/googleads/mcp', endpoint=handle_mcp_request, methods=['POST']),
    ],
    middleware=[
        Middleware(
            CORSMiddleware,
            allow_origins=['*'],  # Configure appropriately for production
            allow_methods=['GET', 'POST', 'OPTIONS'],
            allow_headers=['*'],
            expose_headers=['*'],
        )
    ]
)


def run(host: str = '0.0.0.0', port: int = 8080):
    """Run the remote MCP server."""
    import uvicorn

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    logger.info(f"Starting Google Ads MCP Remote Server on {host}:{port}")
    logger.info("Transport: HTTP JSON-RPC (subprocess isolation)")
    logger.info("Authentication: API Key + Per-request Developer Token")

    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level='info',
    )


if __name__ == '__main__':
    run()

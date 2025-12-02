"""
Remote MCP Server Wrapper for Google Ads MCP.

This wrapper:
- Spawns the google-ads-mcp command via subprocess with injected credentials
- Adds HTTP JSON-RPC transport (replacing stdio)
- Validates API keys from X-API-Key header
- Injects per-request credentials as subprocess environment variables
- Deployable to any container platform (Cloud Run, Fargate, Azure Container Apps, etc.)
"""

import os
import json
import logging
import asyncio
import shutil
import base64

import ecies

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.routing import Route
from starlette.requests import Request
from starlette.responses import JSONResponse

logger = logging.getLogger(__name__)

# Private key for decryption (hex-encoded secp256k1 private key)
DECRYPT_PRIVATE_KEY = os.environ.get('ECIES_PRIVATE_KEY', '')


def decrypt_value(encrypted_b64url: str) -> str:
    """
    Decrypt a base64url-encoded ECIES ciphertext.

    Args:
        encrypted_b64url: Base64url-encoded ciphertext

    Returns:
        Decrypted plaintext string

    Raises:
        ValueError: If decryption fails or private key not configured
    """
    if not DECRYPT_PRIVATE_KEY:
        raise ValueError("ECIES_PRIVATE_KEY environment variable not set")

    # Decode base64url to bytes
    ciphertext = base64.urlsafe_b64decode(encrypted_b64url)

    # Decrypt using ECIES
    plaintext = ecies.decrypt(DECRYPT_PRIVATE_KEY, ciphertext)
    return plaintext.decode('utf-8')


def parse_env_params(query_params: dict) -> tuple[dict[str, str], dict[str, str]]:
    """
    Parse query parameters into environment variables.

    Supports 4 prefixes:
    - PLAIN_X=value     -> env_vars[X] = value
    - FILE_X=content    -> env_file_vars[X] = content (written to temp file)
    - ENC_X=encrypted   -> env_vars[X] = decrypt(encrypted)
    - ENCFILE_X=encrypted -> env_file_vars[X] = decrypt(encrypted)

    Args:
        query_params: Dictionary of query parameters

    Returns:
        Tuple of (env_vars, env_file_vars)
    """
    env_vars = {}
    env_file_vars = {}

    for key, value in query_params.items():
        if key.startswith('PLAIN_'):
            var_name = key[6:]  # Strip 'PLAIN_'
            env_vars[var_name] = value

        elif key.startswith('FILE_'):
            var_name = key[5:]  # Strip 'FILE_'
            env_file_vars[var_name] = value

        elif key.startswith('ENC_'):
            var_name = key[4:]  # Strip 'ENC_'
            env_vars[var_name] = decrypt_value(value)

        elif key.startswith('ENCFILE_'):
            var_name = key[8:]  # Strip 'ENCFILE_'
            env_file_vars[var_name] = decrypt_value(value)

    return env_vars, env_file_vars


def validate_api_key(api_key: str) -> bool:
    """Validate API key against allowed list."""
    allowed_keys = os.environ.get('ALLOWED_API_KEYS', '').split(',')
    allowed_keys = [k.strip() for k in allowed_keys if k.strip()]

    if not allowed_keys:
        logger.warning("ALLOWED_API_KEYS not configured - rejecting all requests")
        return False

    return api_key in allowed_keys


async def execute_mcp_command(
    command: str,
    body: dict,
    env_vars: dict[str, str] | None = None,
    env_file_vars: dict[str, str] | None = None,
    timeout: int = 300,
) -> dict:
    """
    Execute an MCP command with the given request body.

    Args:
        command: Command to execute (e.g., "google-ads-mcp" or "/path/to/mcp --flag")
        body: JSON-RPC request body to send
        env_vars: Environment variables to pass directly to the subprocess
        env_file_vars: Environment variables whose values should be written to
                       temporary files; the env var will contain the file path
        timeout: Timeout in seconds (default: 300)

    Returns:
        JSON-RPC response dict
    """
    import shlex
    import tempfile

    # Parse command into args
    args = shlex.split(command)
    if not args:
        return {
            'jsonrpc': '2.0',
            'id': body.get('id'),
            'error': {'code': -32603, 'message': 'Empty command'}
        }

    # Resolve command path
    executable = shutil.which(args[0]) or args[0]
    args[0] = executable

    # Build environment
    env = os.environ.copy()
    if env_vars:
        env.update(env_vars)

    # Create temp files for file-based env vars
    temp_files = []
    if env_file_vars:
        for var_name, content in env_file_vars.items():
            tmp = tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.json')
            tmp.write(content)
            tmp.close()
            temp_files.append(tmp.name)
            env[var_name] = tmp.name

    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )

        # Forward the JSON-RPC body directly (client handles MCP protocol)
        stdin_data = json.dumps(body) + '\n'

        stdout, stderr = await asyncio.wait_for(
            proc.communicate(input=stdin_data.encode()),
            timeout=timeout
        )

        if proc.returncode != 0 and stderr:
            logger.error(f"MCP server stderr: {stderr.decode()}")

        # Parse response (expect single JSON-RPC response)
        output = stdout.decode().strip()
        if output:
            for line in output.split('\n'):
                if line.strip():
                    try:
                        return json.loads(line)
                    except json.JSONDecodeError:
                        continue

        return {
            'jsonrpc': '2.0',
            'id': body.get('id'),
            'error': {'code': -32603, 'message': 'No response from MCP server'}
        }

    except asyncio.TimeoutError:
        if proc:
            proc.kill()
        raise
    except Exception as e:
        logger.exception(f"Error executing MCP command: {e}")
        return {
            'jsonrpc': '2.0',
            'id': body.get('id'),
            'error': {'code': -32603, 'message': f'MCP execution error: {str(e)}'}
        }
    finally:
        # Clean up temp files
        for path in temp_files:
            try:
                os.unlink(path)
            except OSError:
                pass


async def health_check(request: Request) -> JSONResponse:
    """Health check endpoint for container platforms."""
    return JSONResponse({'status': 'healthy', 'service': 'digital-ads-remote-mcp'})


async def handle_googleads_mcp(request: Request) -> JSONResponse:
    """
    Handle Google Ads MCP requests.

    Query parameters:
    - api_key            -> API key for authentication
    - PLAIN_X=value      -> env var X=value
    - FILE_X=content     -> env var X=/tmp/... (content written to file)
    - ENC_X=encrypted    -> env var X=decrypt(encrypted)
    - ENCFILE_X=encrypted -> env var X=/tmp/... (decrypted content in file)
    """
    try:
        # Parse query params
        query_params = dict(request.query_params)

        # Validate API key from query param
        api_key = query_params.pop('api_key', '')
        if not validate_api_key(api_key):
            logger.warning("Invalid API key attempted")
            return JSONResponse(
                {'error': 'Unauthorized: Invalid API key'},
                status_code=401
            )

        # Parse environment variables from remaining query params
        env_vars, env_file_vars = parse_env_params(query_params)

        # Parse JSON-RPC body
        body = await request.json()
        if not isinstance(body, dict):
            return JSONResponse({
                'jsonrpc': '2.0',
                'id': None,
                'error': {'code': -32600, 'message': 'Invalid request'}
            }, status_code=400)

        # Execute MCP command
        result = await execute_mcp_command(
            command='google-ads-mcp',
            body=body,
            env_vars=env_vars,
            env_file_vars=env_file_vars,
        )
        return JSONResponse(result)

    except ValueError as e:
        # Decryption or parsing errors
        logger.error(f"Parameter error: {e}")
        return JSONResponse({
            'jsonrpc': '2.0',
            'id': None,
            'error': {'code': -32602, 'message': f'Invalid params: {str(e)}'}
        }, status_code=400)

    except asyncio.TimeoutError:
        logger.error("MCP server timeout")
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


async def handle_analytics_mcp(request: Request) -> JSONResponse:
    """
    Handle Google Analytics MCP requests.

    Query parameters:
    - api_key            -> API key for authentication
    - PLAIN_X=value      -> env var X=value
    - FILE_X=content     -> env var X=/tmp/... (content written to file)
    - ENC_X=encrypted    -> env var X=decrypt(encrypted)
    - ENCFILE_X=encrypted -> env var X=/tmp/... (decrypted content in file)
    """
    try:
        # Parse query params
        query_params = dict(request.query_params)

        # Validate API key from query param
        api_key = query_params.pop('api_key', '')
        if not validate_api_key(api_key):
            logger.warning("Invalid API key attempted")
            return JSONResponse(
                {'error': 'Unauthorized: Invalid API key'},
                status_code=401
            )

        # Parse environment variables from remaining query params
        env_vars, env_file_vars = parse_env_params(query_params)

        # Parse JSON-RPC body
        body = await request.json()
        if not isinstance(body, dict):
            return JSONResponse({
                'jsonrpc': '2.0',
                'id': None,
                'error': {'code': -32600, 'message': 'Invalid request'}
            }, status_code=400)

        # Execute MCP command
        result = await execute_mcp_command(
            command='analytics-mcp',
            body=body,
            env_vars=env_vars,
            env_file_vars=env_file_vars,
        )
        return JSONResponse(result)

    except ValueError as e:
        # Decryption or parsing errors
        logger.error(f"Parameter error: {e}")
        return JSONResponse({
            'jsonrpc': '2.0',
            'id': None,
            'error': {'code': -32602, 'message': f'Invalid params: {str(e)}'}
        }, status_code=400)

    except asyncio.TimeoutError:
        logger.error("MCP server timeout")
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


async def handle_facebookads_mcp(request: Request) -> JSONResponse:
    """
    Handle Facebook Ads MCP requests.

    Query parameters:
    - api_key            -> API key for authentication
    - PLAIN_X=value      -> env var X=value
    - FILE_X=content     -> env var X=/tmp/... (content written to file)
    - ENC_X=encrypted    -> env var X=decrypt(encrypted)
    - ENCFILE_X=encrypted -> env var X=/tmp/... (decrypted content in file)

    Note: FB_ACCESS_TOKEN is passed via --fb-token command line argument.
    """
    try:
        # Parse query params
        query_params = dict(request.query_params)

        # Validate API key from query param
        api_key = query_params.pop('api_key', '')
        if not validate_api_key(api_key):
            logger.warning("Invalid API key attempted")
            return JSONResponse(
                {'error': 'Unauthorized: Invalid API key'},
                status_code=401
            )

        # Parse environment variables from remaining query params
        env_vars, env_file_vars = parse_env_params(query_params)

        # Extract Facebook token (required) - check both PLAIN_ and ENC_ versions
        fb_token = env_vars.pop('FB_ACCESS_TOKEN', None)
        if not fb_token:
            return JSONResponse({
                'jsonrpc': '2.0',
                'id': None,
                'error': {'code': -32602, 'message': 'FB_ACCESS_TOKEN required (use PLAIN_FB_ACCESS_TOKEN or ENC_FB_ACCESS_TOKEN)'}
            }, status_code=400)

        # Parse JSON-RPC body
        body = await request.json()
        if not isinstance(body, dict):
            return JSONResponse({
                'jsonrpc': '2.0',
                'id': None,
                'error': {'code': -32600, 'message': 'Invalid request'}
            }, status_code=400)

        # Execute MCP command (token passed via command line argument)
        import shlex
        result = await execute_mcp_command(
            command=f'python /app/facebook-ads-mcp/server.py --fb-token {shlex.quote(fb_token)}',
            body=body,
            env_vars=env_vars,
            env_file_vars=env_file_vars,
        )
        return JSONResponse(result)

    except ValueError as e:
        # Decryption or parsing errors
        logger.error(f"Parameter error: {e}")
        return JSONResponse({
            'jsonrpc': '2.0',
            'id': None,
            'error': {'code': -32602, 'message': f'Invalid params: {str(e)}'}
        }, status_code=400)

    except asyncio.TimeoutError:
        logger.error("MCP server timeout")
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
        Route('/googleads/mcp', endpoint=handle_googleads_mcp, methods=['POST']),
        Route('/analytics/mcp', endpoint=handle_analytics_mcp, methods=['POST']),
        Route('/facebookads/mcp', endpoint=handle_facebookads_mcp, methods=['POST']),
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

    logger.info(f"Starting Digital Ads Remote MCP Server on {host}:{port}")
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

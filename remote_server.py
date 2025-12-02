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
from starlette.responses import JSONResponse, StreamingResponse
import uuid
import tempfile
import shlex

logger = logging.getLogger(__name__)


# =============================================================================
# SSE Session Management
# =============================================================================

class MCPSession:
    """Manages a persistent MCP subprocess session."""

    def __init__(
        self,
        session_id: str,
        proc: asyncio.subprocess.Process,
        temp_files: list[str],
    ):
        self.session_id = session_id
        self.proc = proc
        self.temp_files = temp_files
        self.lock = asyncio.Lock()
        self._closed = False
        self._read_buffer = b''  # Buffer for incomplete lines

    async def send_message(self, message: dict) -> None:
        """Send a JSON-RPC message to the subprocess stdin."""
        if self._closed or self.proc.stdin is None:
            raise RuntimeError("Session is closed")
        async with self.lock:
            data = json.dumps(message) + '\n'
            print(f"[DEBUG] Session {self.session_id} SENDING: {data.strip()}")
            self.proc.stdin.write(data.encode())
            await self.proc.stdin.drain()

    async def read_line(self) -> str | None:
        """Read a line from subprocess stdout.

        Handles large JSON responses that exceed readline()'s default 64KB limit
        by reading in chunks and maintaining a buffer.
        """
        if self._closed or self.proc.stdout is None:
            print(f"[DEBUG] Session {self.session_id} read_line: closed or no stdout")
            return None
        try:
            # Keep reading until we have a complete line
            while b'\n' not in self._read_buffer:
                chunk = await self.proc.stdout.read(65536)  # Read 64KB at a time
                if not chunk:
                    # EOF - return whatever we have buffered
                    if self._read_buffer:
                        line = self._read_buffer
                        self._read_buffer = b''
                        decoded = line.decode().strip()
                        print(f"[DEBUG] Session {self.session_id} RECEIVED EOF ({len(decoded)} bytes): {decoded[:200]}...")
                        return decoded
                    print(f"[DEBUG] Session {self.session_id} read_line: EOF")
                    return None
                self._read_buffer += chunk

            # Split on first newline
            line, self._read_buffer = self._read_buffer.split(b'\n', 1)
            decoded = line.decode().strip()
            print(f"[DEBUG] Session {self.session_id} RECEIVED ({len(decoded)} bytes): {decoded[:200]}...")
            return decoded
        except Exception as e:
            print(f"[DEBUG] Session {self.session_id} read_line error: {e}")
            return None

    async def close(self) -> None:
        """Close the session and cleanup resources."""
        if self._closed:
            return
        self._closed = True

        try:
            if self.proc.stdin:
                self.proc.stdin.close()
            self.proc.terminate()
            try:
                await asyncio.wait_for(self.proc.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                self.proc.kill()
        except Exception as e:
            logger.warning(f"Error closing session {self.session_id}: {e}")

        # Clean up temp files
        for path in self.temp_files:
            try:
                os.unlink(path)
            except OSError:
                pass


# Global session registry
_sessions: dict[str, MCPSession] = {}
_sessions_lock = asyncio.Lock()


async def create_mcp_session(
    command: str,
    env_vars: dict[str, str] | None = None,
    env_file_vars: dict[str, str] | None = None,
) -> MCPSession:
    """Create a new MCP subprocess session."""
    print(f"[DEBUG] create_mcp_session: command={command}")
    args = shlex.split(command)
    if not args:
        raise ValueError("Empty command")

    # Resolve command path
    executable = shutil.which(args[0]) or args[0]
    args[0] = executable
    print(f"[DEBUG] Resolved executable: {executable}")

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

    print(f"[DEBUG] Spawning subprocess: {args}")
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    print(f"[DEBUG] Subprocess spawned, PID: {proc.pid}")

    session_id = str(uuid.uuid4())
    session = MCPSession(session_id, proc, temp_files)

    async with _sessions_lock:
        _sessions[session_id] = session

    print(f"[DEBUG] Session registered: {session_id}")
    logger.info(f"Created MCP session {session_id} for command: {command}")
    return session


async def get_session(session_id: str) -> MCPSession | None:
    """Get a session by ID."""
    async with _sessions_lock:
        return _sessions.get(session_id)


async def remove_session(session_id: str) -> None:
    """Remove and close a session."""
    async with _sessions_lock:
        session = _sessions.pop(session_id, None)
    if session:
        await session.close()
        logger.info(f"Removed MCP session {session_id}")

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

    # Validate base64url string length
    # Valid lengths are 4n, 4n+2, or 4n+3 (after stripping padding)
    # 4n+1 is NEVER valid as it represents an impossible partial byte
    remainder = len(encrypted_b64url) % 4
    if remainder == 1:
        raise ValueError(
            f"Invalid base64url-encoded string: length {len(encrypted_b64url)} "
            "is not valid (cannot be 1 more than a multiple of 4)"
        )

    # Decode base64url to bytes (add padding if stripped)
    if remainder == 2:
        encrypted_b64url += '=='
    elif remainder == 3:
        encrypted_b64url += '='
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


async def health_check(request: Request) -> JSONResponse:
    """Health check endpoint for container platforms."""
    return JSONResponse({'status': 'healthy', 'service': 'digital-ads-remote-mcp'})


# =============================================================================
# SSE Transport Handlers
# =============================================================================

async def handle_analytics_sse(request: Request) -> StreamingResponse:
    """
    SSE endpoint for Google Analytics MCP.

    Opens a persistent connection:
    1. Spawns analytics-mcp subprocess
    2. Streams subprocess stdout as SSE events
    3. Returns session_id in endpoint event for client to use with POST

    Query parameters:
    - api_key            -> API key for authentication
    - PLAIN_X=value      -> env var X=value
    - FILE_X=content     -> env var X=/tmp/... (content written to file)
    - ENC_X=encrypted    -> env var X=decrypt(encrypted)
    - ENCFILE_X=encrypted -> env var X=/tmp/... (decrypted content in file)
    """
    print(f"[DEBUG] GET /analytics/sse called")

    # Parse query params
    query_params = dict(request.query_params)
    print(f"[DEBUG] Query params: {list(query_params.keys())}")

    # Validate API key
    api_key = query_params.pop('api_key', '')
    if not validate_api_key(api_key):
        logger.warning("Invalid API key attempted for SSE")
        return JSONResponse(
            {'error': 'Unauthorized: Invalid API key'},
            status_code=401
        )

    # Parse environment variables
    try:
        env_vars, env_file_vars = parse_env_params(query_params)
    except ValueError as e:
        return JSONResponse({
            'jsonrpc': '2.0',
            'id': None,
            'error': {'code': -32602, 'message': f'Invalid params: {str(e)}'}
        }, status_code=400)

    # Create session
    print(f"[DEBUG] Creating MCP session...")
    try:
        session = await create_mcp_session(
            command='analytics-mcp',
            env_vars=env_vars,
            env_file_vars=env_file_vars,
        )
        print(f"[DEBUG] Session created: {session.session_id}")
    except Exception as e:
        print(f"[DEBUG] Failed to create session: {e}")
        logger.exception(f"Failed to create MCP session: {e}")
        return JSONResponse({
            'jsonrpc': '2.0',
            'id': None,
            'error': {'code': -32603, 'message': f'Failed to start MCP server: {str(e)}'}
        }, status_code=500)

    async def event_stream():
        """Generate SSE events from subprocess stdout."""
        try:
            # First, send the endpoint event with session info
            # This tells the client where to POST messages
            # MCP SSE spec: endpoint event data is just the raw URI, not JSON
            endpoint_uri = f'/analytics/message?session_id={session.session_id}'
            print(f"[DEBUG] SSE sending endpoint event: {endpoint_uri}")
            yield f'event: endpoint\ndata: {endpoint_uri}\n\n'

            # Stream stdout lines as SSE message events
            while True:
                print(f"[DEBUG] SSE waiting for line from session {session.session_id}...")
                line = await session.read_line()
                if line is None:
                    # Process ended or error
                    print(f"[DEBUG] SSE got None from read_line, ending stream")
                    break
                if line:
                    # Send as SSE message event
                    print(f"[DEBUG] SSE sending message event: {line[:200]}...")
                    yield f'event: message\ndata: {line}\n\n'

        except asyncio.CancelledError:
            print(f"[DEBUG] SSE connection cancelled for session {session.session_id}")
            logger.info(f"SSE connection cancelled for session {session.session_id}")
        except Exception as e:
            print(f"[DEBUG] SSE error for session {session.session_id}: {e}")
            logger.exception(f"Error in SSE stream for session {session.session_id}: {e}")
        finally:
            print(f"[DEBUG] SSE cleanup for session {session.session_id}")
            await remove_session(session.session_id)

    return StreamingResponse(
        event_stream(),
        media_type='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'Connection': 'keep-alive',
            'X-Accel-Buffering': 'no',  # Disable nginx buffering
        }
    )


async def handle_analytics_message(request: Request) -> JSONResponse:
    """
    POST endpoint to send messages to an MCP session.

    Query parameters:
    - session_id: The session ID returned in SSE endpoint event

    Body: JSON-RPC message to send to the MCP server
    """
    print(f"[DEBUG] POST /analytics/message called")
    print(f"[DEBUG] Query params: {dict(request.query_params)}")

    session_id = request.query_params.get('session_id')
    if not session_id:
        print(f"[DEBUG] No session_id provided")
        return JSONResponse({
            'jsonrpc': '2.0',
            'id': None,
            'error': {'code': -32602, 'message': 'session_id required'}
        }, status_code=400)

    print(f"[DEBUG] Looking up session: {session_id}")
    session = await get_session(session_id)
    if not session:
        print(f"[DEBUG] Session not found: {session_id}")
        return JSONResponse({
            'jsonrpc': '2.0',
            'id': None,
            'error': {'code': -32603, 'message': 'Session not found or expired'}
        }, status_code=404)

    try:
        body = await request.json()
        print(f"[DEBUG] Message body: {json.dumps(body)[:500]}")

        if not isinstance(body, dict):
            return JSONResponse({
                'jsonrpc': '2.0',
                'id': None,
                'error': {'code': -32600, 'message': 'Invalid request'}
            }, status_code=400)

        # Normalize: ensure 'params' exists
        if 'params' not in body:
            body['params'] = {}

        await session.send_message(body)
        print(f"[DEBUG] Message sent successfully")
        return JSONResponse({'status': 'sent'})

    except RuntimeError as e:
        return JSONResponse({
            'jsonrpc': '2.0',
            'id': None,
            'error': {'code': -32603, 'message': str(e)}
        }, status_code=500)

    except Exception as e:
        logger.exception(f"Error sending message to session {session_id}: {e}")
        return JSONResponse({
            'jsonrpc': '2.0',
            'id': None,
            'error': {'code': -32603, 'message': f'Internal error: {str(e)}'}
        }, status_code=500)


# -----------------------------------------------------------------------------
# Google Ads SSE Handlers
# -----------------------------------------------------------------------------

async def handle_googleads_sse(request: Request) -> StreamingResponse:
    """
    SSE endpoint for Google Ads MCP.

    Query parameters:
    - api_key            -> API key for authentication
    - PLAIN_X=value      -> env var X=value
    - FILE_X=content     -> env var X=/tmp/... (content written to file)
    - ENC_X=encrypted    -> env var X=decrypt(encrypted)
    - ENCFILE_X=encrypted -> env var X=/tmp/... (decrypted content in file)
    """
    print(f"[DEBUG] GET /googleads/sse called")

    query_params = dict(request.query_params)
    print(f"[DEBUG] Query params: {list(query_params.keys())}")

    api_key = query_params.pop('api_key', '')
    if not validate_api_key(api_key):
        logger.warning("Invalid API key attempted for Google Ads SSE")
        return JSONResponse({'error': 'Unauthorized: Invalid API key'}, status_code=401)

    try:
        env_vars, env_file_vars = parse_env_params(query_params)
    except ValueError as e:
        return JSONResponse({
            'jsonrpc': '2.0', 'id': None,
            'error': {'code': -32602, 'message': f'Invalid params: {str(e)}'}
        }, status_code=400)

    print(f"[DEBUG] Creating Google Ads MCP session...")
    try:
        session = await create_mcp_session(
            command='google-ads-mcp',
            env_vars=env_vars,
            env_file_vars=env_file_vars,
        )
        print(f"[DEBUG] Session created: {session.session_id}")
    except Exception as e:
        print(f"[DEBUG] Failed to create session: {e}")
        logger.exception(f"Failed to create Google Ads MCP session: {e}")
        return JSONResponse({
            'jsonrpc': '2.0', 'id': None,
            'error': {'code': -32603, 'message': f'Failed to start MCP server: {str(e)}'}
        }, status_code=500)

    async def event_stream():
        try:
            endpoint_uri = f'/googleads/message?session_id={session.session_id}'
            print(f"[DEBUG] SSE sending endpoint event: {endpoint_uri}")
            yield f'event: endpoint\ndata: {endpoint_uri}\n\n'

            while True:
                line = await session.read_line()
                if line is None:
                    break
                if line:
                    print(f"[DEBUG] SSE sending message event: {line[:200]}...")
                    yield f'event: message\ndata: {line}\n\n'

        except asyncio.CancelledError:
            logger.info(f"SSE connection cancelled for session {session.session_id}")
        except Exception as e:
            logger.exception(f"Error in SSE stream for session {session.session_id}: {e}")
        finally:
            await remove_session(session.session_id)

    return StreamingResponse(
        event_stream(),
        media_type='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'Connection': 'keep-alive', 'X-Accel-Buffering': 'no'}
    )


async def handle_googleads_message(request: Request) -> JSONResponse:
    """POST endpoint to send messages to a Google Ads MCP session."""
    session_id = request.query_params.get('session_id')
    if not session_id:
        return JSONResponse({
            'jsonrpc': '2.0', 'id': None,
            'error': {'code': -32602, 'message': 'session_id required'}
        }, status_code=400)

    session = await get_session(session_id)
    if not session:
        return JSONResponse({
            'jsonrpc': '2.0', 'id': None,
            'error': {'code': -32603, 'message': 'Session not found or expired'}
        }, status_code=404)

    try:
        body = await request.json()
        if not isinstance(body, dict):
            return JSONResponse({
                'jsonrpc': '2.0', 'id': None,
                'error': {'code': -32600, 'message': 'Invalid request'}
            }, status_code=400)

        if 'params' not in body:
            body['params'] = {}

        await session.send_message(body)
        return JSONResponse({'status': 'sent'})

    except RuntimeError as e:
        return JSONResponse({
            'jsonrpc': '2.0', 'id': None,
            'error': {'code': -32603, 'message': str(e)}
        }, status_code=500)
    except Exception as e:
        logger.exception(f"Error sending message to session {session_id}: {e}")
        return JSONResponse({
            'jsonrpc': '2.0', 'id': None,
            'error': {'code': -32603, 'message': f'Internal error: {str(e)}'}
        }, status_code=500)


# -----------------------------------------------------------------------------
# Facebook Ads SSE Handlers
# -----------------------------------------------------------------------------

async def handle_facebookads_sse(request: Request) -> StreamingResponse:
    """
    SSE endpoint for Facebook Ads MCP.

    Query parameters:
    - api_key            -> API key for authentication
    - PLAIN_FB_ACCESS_TOKEN or ENC_FB_ACCESS_TOKEN -> Required Facebook token
    - PLAIN_X=value      -> env var X=value
    - FILE_X=content     -> env var X=/tmp/... (content written to file)
    - ENC_X=encrypted    -> env var X=decrypt(encrypted)
    - ENCFILE_X=encrypted -> env var X=/tmp/... (decrypted content in file)
    """
    print(f"[DEBUG] GET /facebookads/sse called")

    query_params = dict(request.query_params)
    print(f"[DEBUG] Query params: {list(query_params.keys())}")

    api_key = query_params.pop('api_key', '')
    if not validate_api_key(api_key):
        logger.warning("Invalid API key attempted for Facebook Ads SSE")
        return JSONResponse({'error': 'Unauthorized: Invalid API key'}, status_code=401)

    try:
        env_vars, env_file_vars = parse_env_params(query_params)
    except ValueError as e:
        return JSONResponse({
            'jsonrpc': '2.0', 'id': None,
            'error': {'code': -32602, 'message': f'Invalid params: {str(e)}'}
        }, status_code=400)

    # Extract Facebook token (required)
    fb_token = env_vars.pop('FB_ACCESS_TOKEN', None)
    if not fb_token:
        return JSONResponse({
            'jsonrpc': '2.0', 'id': None,
            'error': {'code': -32602, 'message': 'FB_ACCESS_TOKEN required (use PLAIN_FB_ACCESS_TOKEN or ENC_FB_ACCESS_TOKEN)'}
        }, status_code=400)

    print(f"[DEBUG] Creating Facebook Ads MCP session...")
    try:
        # Token passed via command line argument
        session = await create_mcp_session(
            command=f'python /app/facebook-ads-mcp/server.py --fb-token {shlex.quote(fb_token)}',
            env_vars=env_vars,
            env_file_vars=env_file_vars,
        )
        print(f"[DEBUG] Session created: {session.session_id}")
    except Exception as e:
        print(f"[DEBUG] Failed to create session: {e}")
        logger.exception(f"Failed to create Facebook Ads MCP session: {e}")
        return JSONResponse({
            'jsonrpc': '2.0', 'id': None,
            'error': {'code': -32603, 'message': f'Failed to start MCP server: {str(e)}'}
        }, status_code=500)

    async def event_stream():
        try:
            endpoint_uri = f'/facebookads/message?session_id={session.session_id}'
            print(f"[DEBUG] SSE sending endpoint event: {endpoint_uri}")
            yield f'event: endpoint\ndata: {endpoint_uri}\n\n'

            while True:
                line = await session.read_line()
                if line is None:
                    break
                if line:
                    print(f"[DEBUG] SSE sending message event: {line[:200]}...")
                    yield f'event: message\ndata: {line}\n\n'

        except asyncio.CancelledError:
            logger.info(f"SSE connection cancelled for session {session.session_id}")
        except Exception as e:
            logger.exception(f"Error in SSE stream for session {session.session_id}: {e}")
        finally:
            await remove_session(session.session_id)

    return StreamingResponse(
        event_stream(),
        media_type='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'Connection': 'keep-alive', 'X-Accel-Buffering': 'no'}
    )


async def handle_facebookads_message(request: Request) -> JSONResponse:
    """POST endpoint to send messages to a Facebook Ads MCP session."""
    session_id = request.query_params.get('session_id')
    if not session_id:
        return JSONResponse({
            'jsonrpc': '2.0', 'id': None,
            'error': {'code': -32602, 'message': 'session_id required'}
        }, status_code=400)

    session = await get_session(session_id)
    if not session:
        return JSONResponse({
            'jsonrpc': '2.0', 'id': None,
            'error': {'code': -32603, 'message': 'Session not found or expired'}
        }, status_code=404)

    try:
        body = await request.json()
        if not isinstance(body, dict):
            return JSONResponse({
                'jsonrpc': '2.0', 'id': None,
                'error': {'code': -32600, 'message': 'Invalid request'}
            }, status_code=400)

        if 'params' not in body:
            body['params'] = {}

        await session.send_message(body)
        return JSONResponse({'status': 'sent'})

    except RuntimeError as e:
        return JSONResponse({
            'jsonrpc': '2.0', 'id': None,
            'error': {'code': -32603, 'message': str(e)}
        }, status_code=500)
    except Exception as e:
        logger.exception(f"Error sending message to session {session_id}: {e}")
        return JSONResponse({
            'jsonrpc': '2.0', 'id': None,
            'error': {'code': -32603, 'message': f'Internal error: {str(e)}'}
        }, status_code=500)


# Create Starlette ASGI application
app = Starlette(
    debug=False,
    routes=[
        Route('/', endpoint=health_check, methods=['GET']),
        Route('/health', endpoint=health_check, methods=['GET']),
        # Google Analytics SSE transport
        Route('/analytics/sse', endpoint=handle_analytics_sse, methods=['GET']),
        Route('/analytics/message', endpoint=handle_analytics_message, methods=['POST']),
        # Google Ads SSE transport
        Route('/googleads/sse', endpoint=handle_googleads_sse, methods=['GET']),
        Route('/googleads/message', endpoint=handle_googleads_message, methods=['POST']),
        # Facebook Ads SSE transport
        Route('/facebookads/sse', endpoint=handle_facebookads_sse, methods=['GET']),
        Route('/facebookads/message', endpoint=handle_facebookads_message, methods=['POST']),
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

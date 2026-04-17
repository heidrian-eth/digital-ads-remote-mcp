"""
Remote MCP Server Wrapper (Streamable HTTP transport).

Exposes each wrapped MCP server at a single `/mcp` endpoint:

- `POST /{service}/mcp` — send a JSON-RPC request, notification, or batch.
  - For requests: response body is a `text/event-stream` whose final event
    carries the matching JSON-RPC response. Intermediate events may carry
    progress notifications emitted by the underlying MCP server.
  - For notifications only: returns 202 Accepted with no body.
- `DELETE /{service}/mcp` — close the session identified by `Mcp-Session-Id`.

Session lifecycle:
- The first request (`initialize`) is sent without an `Mcp-Session-Id` header.
  The server spawns the underlying MCP command as a stdio subprocess, forwards
  the request, and returns the subprocess's response stream. The response
  includes an `Mcp-Session-Id` header the client MUST send on all subsequent
  requests for the same session.
- Env-injection query parameters (`PLAIN_*`, `FILE_*`, `ENC_*`, `ENCFILE_*`)
  are read only on the initialize request; they configure the subprocess env.

Auth: `api_key` query parameter, validated against `ALLOWED_API_KEYS`.
"""

import os
import json
import logging
import asyncio
import shutil
import base64
import uuid
import tempfile
import shlex
from typing import Any, AsyncIterator, Awaitable, Callable

import ecies

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.routing import Route
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse

logger = logging.getLogger(__name__)


# =============================================================================
# MCP stdio subprocess session
# =============================================================================

class MCPSession:
    """Persistent stdio subprocess bridging JSON-RPC over HTTP."""

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
        self._read_buffer = b''
        self._stderr_task = asyncio.create_task(self._drain_stderr())

    async def _drain_stderr(self) -> None:
        if self.proc.stderr is None:
            return
        try:
            while True:
                line = await self.proc.stderr.readline()
                if not line:
                    break
                print(f"[STDERR:{self.session_id[:8]}] {line.decode().rstrip()}")
        except Exception as e:
            print(f"[DEBUG] stderr drain error for {self.session_id}: {e}")

    async def _read_line(self) -> str | None:
        """Read one line from subprocess stdout, buffered to bypass 64KB readline cap."""
        if self._closed or self.proc.stdout is None:
            return None
        try:
            while b'\n' not in self._read_buffer:
                chunk = await self.proc.stdout.read(65536)
                if not chunk:
                    if self._read_buffer:
                        line = self._read_buffer
                        self._read_buffer = b''
                        return line.decode().strip()
                    return None
                self._read_buffer += chunk
            line, self._read_buffer = self._read_buffer.split(b'\n', 1)
            return line.decode().strip()
        except Exception as e:
            print(f"[DEBUG] Session {self.session_id} read error: {e}")
            return None

    async def process_jsonrpc(self, body: Any) -> AsyncIterator[str]:
        """Send a JSON-RPC message/batch; yield stdout lines until all expected responses arrive.

        Holds the session lock for the full round-trip so concurrent POSTs on the
        same session are serialized against the stdio pipes.
        """
        if self._closed or self.proc.stdin is None:
            raise RuntimeError("Session is closed")

        expected_ids = _collect_request_ids(body)

        async with self.lock:
            data = (json.dumps(body) + '\n').encode()
            print(f"[DEBUG] Session {self.session_id} -> {data[:200]!r}")
            self.proc.stdin.write(data)
            await self.proc.stdin.drain()

            if not expected_ids:
                return

            seen_ids: set = set()
            while seen_ids < expected_ids:
                line = await self._read_line()
                if line is None:
                    break
                if not line:
                    continue
                print(f"[DEBUG] Session {self.session_id} <- {line[:200]}")
                yield line
                try:
                    parsed = json.loads(line)
                except json.JSONDecodeError:
                    continue
                msgs = parsed if isinstance(parsed, list) else [parsed]
                for m in msgs:
                    if (
                        isinstance(m, dict)
                        and 'id' in m
                        and ('result' in m or 'error' in m)
                    ):
                        seen_ids.add(m['id'])

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True

        self._stderr_task.cancel()
        try:
            await self._stderr_task
        except asyncio.CancelledError:
            pass

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

        for path in self.temp_files:
            try:
                os.unlink(path)
            except OSError:
                pass


_sessions: dict[str, MCPSession] = {}
_sessions_lock = asyncio.Lock()


async def create_mcp_session(
    command: str,
    env_vars: dict[str, str] | None = None,
    env_file_vars: dict[str, str] | None = None,
) -> MCPSession:
    args = shlex.split(command)
    if not args:
        raise ValueError("Empty command")

    executable = shutil.which(args[0]) or args[0]
    args[0] = executable

    env = os.environ.copy()
    if env_vars:
        env.update(env_vars)

    temp_files: list[str] = []
    if env_file_vars:
        for var_name, content in env_file_vars.items():
            tmp = tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.json')
            tmp.write(content)
            tmp.close()
            temp_files.append(tmp.name)
            env[var_name] = tmp.name

    proc = await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )

    session_id = str(uuid.uuid4())
    session = MCPSession(session_id, proc, temp_files)

    async with _sessions_lock:
        _sessions[session_id] = session

    logger.info(f"Created MCP session {session_id} for command: {command}")
    return session


async def get_session(session_id: str) -> MCPSession | None:
    async with _sessions_lock:
        return _sessions.get(session_id)


async def remove_session(session_id: str) -> None:
    async with _sessions_lock:
        session = _sessions.pop(session_id, None)
    if session:
        await session.close()
        logger.info(f"Removed MCP session {session_id}")


# =============================================================================
# Crypto + env param parsing
# =============================================================================

DECRYPT_PRIVATE_KEY = os.environ.get('ECIES_PRIVATE_KEY', '')


def decrypt_value(encrypted_b64url: str) -> str:
    if not DECRYPT_PRIVATE_KEY:
        raise ValueError("ECIES_PRIVATE_KEY environment variable not set")

    remainder = len(encrypted_b64url) % 4
    if remainder == 1:
        raise ValueError(
            f"Invalid base64url-encoded string: length {len(encrypted_b64url)} "
            "is not valid (cannot be 1 more than a multiple of 4)"
        )
    if remainder == 2:
        encrypted_b64url += '=='
    elif remainder == 3:
        encrypted_b64url += '='
    ciphertext = base64.urlsafe_b64decode(encrypted_b64url)

    plaintext = ecies.decrypt(DECRYPT_PRIVATE_KEY, ciphertext)
    return plaintext.decode('utf-8')


def parse_env_params(query_params: dict) -> tuple[dict[str, str], dict[str, str]]:
    env_vars: dict[str, str] = {}
    env_file_vars: dict[str, str] = {}

    for key, value in query_params.items():
        if key.startswith('PLAIN_'):
            env_vars[key[6:]] = value
        elif key.startswith('FILE_'):
            env_file_vars[key[5:]] = value
        elif key.startswith('ENC_'):
            env_vars[key[4:]] = decrypt_value(value)
        elif key.startswith('ENCFILE_'):
            env_file_vars[key[8:]] = decrypt_value(value)

    return env_vars, env_file_vars


def validate_api_key(api_key: str) -> bool:
    allowed_keys = [k.strip() for k in os.environ.get('ALLOWED_API_KEYS', '').split(',') if k.strip()]
    if not allowed_keys:
        logger.warning("ALLOWED_API_KEYS not configured - rejecting all requests")
        return False
    return api_key in allowed_keys


# =============================================================================
# JSON-RPC helpers
# =============================================================================

def _collect_request_ids(body: Any) -> set:
    """Return the set of JSON-RPC ids that require a response (notifications have no id)."""
    msgs = body if isinstance(body, list) else [body]
    ids: set = set()
    for m in msgs:
        if isinstance(m, dict) and 'id' in m and m['id'] is not None:
            ids.add(m['id'])
    return ids


def _is_initialize(body: Any) -> bool:
    msgs = body if isinstance(body, list) else [body]
    return any(isinstance(m, dict) and m.get('method') == 'initialize' for m in msgs)


def _jsonrpc_error(status: int, code: int, message: str) -> JSONResponse:
    return JSONResponse(
        {'jsonrpc': '2.0', 'id': None, 'error': {'code': code, 'message': message}},
        status_code=status,
    )


# =============================================================================
# Per-service command builders
# =============================================================================

def _build_analytics_command(env_vars: dict[str, str]) -> str:
    return 'analytics-mcp'


def _build_googleads_command(env_vars: dict[str, str]) -> str:
    return 'google-ads-mcp'


def _build_facebookads_command(env_vars: dict[str, str]) -> str:
    token = env_vars.pop('FB_ACCESS_TOKEN', None)
    if not token:
        raise ValueError('FB_ACCESS_TOKEN required (use PLAIN_FB_ACCESS_TOKEN or ENC_FB_ACCESS_TOKEN)')
    return f'python /app/facebook-ads-mcp/server.py --fb-token {shlex.quote(token)}'


# =============================================================================
# HTTP handlers
# =============================================================================

async def health_check(request: Request) -> JSONResponse:
    return JSONResponse({'status': 'healthy', 'service': 'digital-ads-remote-mcp'})


CommandBuilder = Callable[[dict[str, str]], str]


async def _handle_mcp(
    request: Request,
    service_name: str,
    build_command: CommandBuilder,
) -> Response:
    """Streamable HTTP handler for POST/DELETE on /{service}/mcp."""

    api_key = request.query_params.get('api_key', '')
    if not validate_api_key(api_key):
        logger.warning(f"Invalid API key attempted for {service_name}/mcp")
        return JSONResponse({'error': 'Unauthorized: Invalid API key'}, status_code=401)

    header_session_id = request.headers.get('mcp-session-id')

    if request.method == 'DELETE':
        if header_session_id:
            await remove_session(header_session_id)
        return Response(status_code=204)

    try:
        body = await request.json()
    except Exception:
        return _jsonrpc_error(400, -32700, 'Parse error: body is not valid JSON')

    new_session = False
    if header_session_id:
        session = await get_session(header_session_id)
        if not session:
            return _jsonrpc_error(404, -32603, 'Session not found or expired')
    else:
        if not _is_initialize(body):
            return _jsonrpc_error(
                400, -32600,
                'Missing Mcp-Session-Id header; the first request must be an "initialize" request'
            )

        query_params = {k: v for k, v in request.query_params.items() if k != 'api_key'}
        try:
            env_vars, env_file_vars = parse_env_params(query_params)
        except ValueError as e:
            return _jsonrpc_error(400, -32602, f'Invalid params: {e}')

        try:
            command = build_command(env_vars)
        except ValueError as e:
            return _jsonrpc_error(400, -32602, str(e))

        try:
            session = await create_mcp_session(command, env_vars, env_file_vars)
        except Exception as e:
            logger.exception(f"Failed to create {service_name} MCP session: {e}")
            return _jsonrpc_error(500, -32603, f'Failed to start MCP server: {e}')

        new_session = True

    session_id = session.session_id
    expected_ids = _collect_request_ids(body)

    if not expected_ids:
        try:
            async for _ in session.process_jsonrpc(body):
                pass
        except RuntimeError as e:
            return _jsonrpc_error(500, -32603, str(e))
        return Response(status_code=202, headers={'Mcp-Session-Id': session_id})

    async def event_stream() -> AsyncIterator[str]:
        delivered = False
        try:
            async for line in session.process_jsonrpc(body):
                delivered = True
                yield f'event: message\ndata: {line}\n\n'
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception(f"Error streaming /mcp for session {session_id}: {e}")
        finally:
            if new_session and not delivered:
                await remove_session(session_id)

    return StreamingResponse(
        event_stream(),
        media_type='text/event-stream',
        headers={
            'Mcp-Session-Id': session_id,
            'Cache-Control': 'no-cache',
            'Connection': 'keep-alive',
            'X-Accel-Buffering': 'no',
        },
    )


async def handle_analytics_mcp(request: Request) -> Response:
    return await _handle_mcp(request, 'analytics', _build_analytics_command)


async def handle_googleads_mcp(request: Request) -> Response:
    return await _handle_mcp(request, 'googleads', _build_googleads_command)


async def handle_facebookads_mcp(request: Request) -> Response:
    return await _handle_mcp(request, 'facebookads', _build_facebookads_command)


# =============================================================================
# ASGI app
# =============================================================================

app = Starlette(
    debug=False,
    routes=[
        Route('/', endpoint=health_check, methods=['GET']),
        Route('/health', endpoint=health_check, methods=['GET']),
        Route('/analytics/mcp', endpoint=handle_analytics_mcp, methods=['POST', 'DELETE']),
        Route('/googleads/mcp', endpoint=handle_googleads_mcp, methods=['POST', 'DELETE']),
        Route('/facebookads/mcp', endpoint=handle_facebookads_mcp, methods=['POST', 'DELETE']),
    ],
    middleware=[
        Middleware(
            CORSMiddleware,
            allow_origins=['*'],
            allow_methods=['GET', 'POST', 'DELETE', 'OPTIONS'],
            allow_headers=['*', 'Mcp-Session-Id'],
            expose_headers=['Mcp-Session-Id'],
        )
    ],
)


def run(host: str = '0.0.0.0', port: int = 8080):
    import uvicorn

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    )

    logger.info(f"Starting Digital Ads Remote MCP Server on {host}:{port}")
    logger.info("Transport: Streamable HTTP (/mcp) over stdio subprocess")
    logger.info("Authentication: API Key (api_key query param)")

    uvicorn.run(app, host=host, port=port, log_level='info')


if __name__ == '__main__':
    run()

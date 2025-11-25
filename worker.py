#!/usr/bin/env python3
"""
Worker script that handles a single MCP request.
Called via subprocess with credentials injected as environment variables.

Usage: echo '{"method": "tools/list", ...}' | python worker.py
"""

import sys
import json
import asyncio


def handle_request(body: dict) -> dict:
    """Handle a single MCP JSON-RPC request."""
    # Import ads_mcp - this will read credentials from environment
    from ads_mcp.coordinator import mcp
    from ads_mcp.tools import search, core  # noqa: F401 (registers tools)

    method = body.get('method', '')
    params = body.get('params', {})
    request_id = body.get('id')

    try:
        if method == 'initialize':
            result = {
                'protocolVersion': '2024-11-05',
                'capabilities': {
                    'tools': {'listChanged': False},
                },
                'serverInfo': {
                    'name': 'digital-ads-remote-mcp',
                    'version': '0.1.0'
                }
            }
        elif method == 'tools/list':
            tools = []
            for name, tool in mcp._tool_manager._tools.items():
                tools.append({
                    'name': name,
                    'description': tool.description or '',
                    'inputSchema': tool.parameters.model_json_schema() if tool.parameters else {'type': 'object', 'properties': {}}
                })
            result = {'tools': tools}
        elif method == 'tools/call':
            tool_name = params.get('name', '')
            tool_args = params.get('arguments', {})

            tool = mcp._tool_manager._tools.get(tool_name)
            if not tool:
                return {
                    'jsonrpc': '2.0',
                    'id': request_id,
                    'error': {'code': -32601, 'message': f'Tool not found: {tool_name}'}
                }

            # Execute the tool (handle both sync and async)
            tool_result = tool.fn(**tool_args)
            if asyncio.iscoroutine(tool_result):
                tool_result = asyncio.run(tool_result)

            # Format result as MCP content
            if isinstance(tool_result, str):
                content = [{'type': 'text', 'text': tool_result}]
            elif isinstance(tool_result, list):
                content = tool_result
            else:
                content = [{'type': 'text', 'text': str(tool_result)}]

            result = {'content': content, 'isError': False}
        elif method == 'notifications/initialized':
            result = {}
        else:
            return {
                'jsonrpc': '2.0',
                'id': request_id,
                'error': {'code': -32601, 'message': f'Method not found: {method}'}
            }

        return {
            'jsonrpc': '2.0',
            'id': request_id,
            'result': result
        }

    except Exception as e:
        return {
            'jsonrpc': '2.0',
            'id': request_id,
            'error': {'code': -32603, 'message': f'Internal error: {str(e)}'}
        }


if __name__ == '__main__':
    # Read JSON request from stdin
    input_data = sys.stdin.read()
    body = json.loads(input_data)

    # Process and output result
    result = handle_request(body)
    print(json.dumps(result))

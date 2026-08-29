"""Minimal ACP JSON-RPC peer used by the recorded offline adapter fixture."""

from __future__ import annotations

import json
import sys
import time


def reply(request_id: int, result: dict[str, object]) -> None:
    print(
        json.dumps(
            {"jsonrpc": "2.0", "id": request_id, "result": result},
            separators=(",", ":"),
        ),
        flush=True,
    )


for raw_line in sys.stdin:
    request = json.loads(raw_line)
    request_id = request["id"]
    method = request["method"]
    params = request["params"]
    if method == "initialize":
        if params != {"protocolVersion": 1, "clientCapabilities": {}}:
            raise RuntimeError("client capabilities were not empty")
        reply(request_id, {"authMethods": [{"id": "xai.api_key"}]})
    elif method == "authenticate":
        if params != {"methodId": "xai.api_key", "_meta": {"headless": True}}:
            raise RuntimeError("unexpected authentication contract")
        reply(request_id, {})
    elif method == "session/new":
        if params.get("mcpServers") != []:
            raise RuntimeError("MCP capability was advertised")
        reply(request_id, {"sessionId": "recorded-acp-session"})
    elif method == "session/prompt":
        if "--out-of-sequence" in sys.argv:
            reply(999, {})
        update = {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "sessionId": "recorded-acp-session",
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"text": '{"answer":"ok"}'},
                },
            },
        }
        print(json.dumps(update, separators=(",", ":")), flush=True)
        reply(
            request_id,
            {
                "stopReason": "end_turn",
                "model": "grok-model",
                "usage": {"output_tokens": 1},
            },
        )
        if "--overflow-after-prompt" in sys.argv:
            sys.stderr.write("x" * 8192)
            sys.stderr.flush()
    else:
        raise RuntimeError(f"unexpected ACP method: {method}")

if "--linger-after-eof" in sys.argv:
    time.sleep(30)

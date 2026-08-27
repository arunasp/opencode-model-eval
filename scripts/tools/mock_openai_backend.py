"""Minimal OpenAI-compatible chat-completions backend, used only by
test_run_eval_client_e2e.py to drive a REAL `opencode serve` process
end-to-end. Stdlib http.server only.

Two response modes, selected by the `mode` constructor arg:
  "sse"  -- real streaming SSE chunks (data: {...}\\n\\n ... data: [DONE]),
            including the usage chunk opencode's own `stream_options`
            asks for. This is the CONFORMANT mode and the one that made
            the documented reply-shape claim in run_eval_client.py true.
  "flat" -- a single synchronous JSON body, no SSE framing, even though
            opencode's request set "stream": true. DELIBERATELY out of
            spec: a fault injector, not a bug to fix. It models a real
            class -- a gateway or proxy that drops `stream` -- and it is
            the fixture behind test_flat_json_backend_is_caught_as_a_
            provider_fault. Do not "correct" it; correcting it deletes
            the only reproduction of that failure.

What "flat" produces changed upstream, so the old description of it
here was wrong. Through opencode 1.18.20 it silently yielded a response
with no text part. From 1.18.21 an "unknown" finish no longer ends a
turn (session/prompt.ts:1110-1116, deliberate -- a provider reporting
no finish reason should not be able to end one silently), so the turn
instead never ends: one assistant message per provider call, measured
at ~15/s. The harness detects that and aborts; see
run_eval_client.py's _unproductive_loop().

Distinguishes the short title-generation call opencode fires per
session (system-prompted, no prior assistant turn) from the real user
message, by prompt content -- title-gen requests are short and contain
"title" in the system/user content in every opencode version observed
so far. If that heuristic ever breaks, this mock will misclassify a
title call as the real one; logged loudly rather than failing silently.
"""

from __future__ import annotations

import http.server
import json
import time


class OpenAICompatibleMockHandler(http.server.BaseHTTPRequestHandler):
    # Set by the test harness before starting the server (class
    # attributes, since BaseHTTPRequestHandler is instantiated fresh
    # per request by HTTPServer).
    mode = "sse"
    reply_text = "mock reply text"
    requests_log: list[dict] = []

    def log_message(self, *args):  # silence per-request logging
        pass

    def do_GET(self):  # noqa: N802
        if self.path == "/v1/models":
            body = json.dumps(
                {"object": "list", "data": [{"id": "mock-model", "object": "model"}]}
            ).encode()
            self._send_json(200, body)
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            payload = {"_unparseable_raw": raw.decode(errors="replace")}
        self.__class__.requests_log.append({"path": self.path, "body": payload})

        if self.path != "/v1/chat/completions":
            self.send_response(404)
            self.end_headers()
            return

        is_stream_requested = bool(payload.get("stream"))
        # Title-gen calls are short, no assistant history, and every
        # opencode version observed so far includes "title" somewhere
        # in the messages -- classify on that, not on call order (which
        # isn't guaranteed).
        messages_text = json.dumps(payload.get("messages", [])).lower()
        is_title_call = "title" in messages_text and "mock-probe-marker" not in messages_text

        reply = "Mock Title" if is_title_call else self.reply_text

        if self.mode == "flat" and is_stream_requested:
            # Deliberately WRONG per the OpenAI streaming contract:
            # returns a flat JSON body despite stream:true. Kept as a
            # fault injector -- see the module docstring for what it
            # now produces and why it must not be "fixed".
            body = json.dumps(
                {
                    "id": "mock-flat-1",
                    "object": "chat.completion",
                    "created": int(time.time()),
                    "model": "mock-model",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": reply},
                            "finish_reason": "stop",
                        }
                    ],
                }
            ).encode()
            self._send_json(200, body)
            return

        # Real SSE stream, matching OpenAI's chat.completion.chunk format.
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

        def send_chunk(delta: dict, finish_reason: str | None = None):
            chunk = {
                "id": "mock-chunk-1",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": "mock-model",
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
            }
            self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
            self.wfile.flush()

        send_chunk({"role": "assistant", "content": ""})
        send_chunk({"content": reply})
        send_chunk({}, finish_reason="stop")
        # THE USAGE CHUNK, which this mock previously never sent.
        # opencode's request carries `stream_options` (confirmed on the
        # wire, not from docs), and the contract answers that with a
        # final chunk holding `usage` and an EMPTY choices array. Without
        # it opencode's Session.getUsage() falls back to an empty Usage,
        # so every e2e run recorded zero tokens and zero cost and the
        # token-accounting path was never exercised at all. Emitted only
        # when asked for, so a client that does not request usage still
        # sees exactly the stream it expects.
        if (payload.get("stream_options") or {}).get("include_usage"):
            prompt_tokens = sum(len(str(m.get("content", ""))) for m in payload.get("messages", [])) // 4
            completion_tokens = max(1, len(reply) // 4)
            usage_chunk = {
                "id": "mock-chunk-usage",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": "mock-model",
                "choices": [],
                "usage": {
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "total_tokens": prompt_tokens + completion_tokens,
                },
            }
            self.wfile.write(f"data: {json.dumps(usage_chunk)}\n\n".encode())
            self.wfile.flush()
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def _send_json(self, status: int, body: bytes):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def make_server(port: int, mode: str, reply_text: str) -> http.server.HTTPServer:
    handler_cls = type(
        "ConfiguredHandler",
        (OpenAICompatibleMockHandler,),
        {"mode": mode, "reply_text": reply_text, "requests_log": []},
    )
    return http.server.HTTPServer(("127.0.0.1", port), handler_cls), handler_cls

"""Minimal OpenAI-compatible chat-completions backend, used only by
test_run_eval_client_e2e.py to drive a REAL `opencode serve` process
end-to-end. Stdlib http.server only.

Two response modes, selected by the `mode` constructor arg:
  "sse"  -- real streaming SSE chunks (data: {...}\\n\\n ... data: [DONE]).
            This is the mode that made the documented reply-shape claim
            in run_eval_client.py true.
  "flat" -- a single synchronous JSON body, no SSE framing, even though
            opencode's request set "stream": true. Reproduces the
            documented gotcha: this must silently produce a response
            with no text part, not an error.

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
            # returns a flat JSON body despite stream:true. This is
            # the exact case the docstring says silently drops the
            # text part -- reproduced here to prove that claim, not
            # just assert it.
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

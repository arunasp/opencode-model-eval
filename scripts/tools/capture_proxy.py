#!/usr/bin/env python3
"""capture_proxy.py -- a forward proxy that records what opencode sends
to the model provider, without changing what the provider sees or what
opencode receives.

WHY THIS EXISTS. The system prompt and tool definitions opencode sends
are assembled per request (session/prompt.ts) and handed straight to the
provider; they are never persisted, so no endpoint and no message chain
can yield them. The outbound provider request is the only place the
resolved instruction set exists as bytes. Capturing it is the difference
between recording what a model answered and recording what it was asked.

HOW IT IS REACHED. opencode needs no configuration change: Bun's fetch
honours `http_proxy` / `all_proxy`, confirmed live -- the provider
request arrives here in absolute-URI form, which only a proxy-aware
client sends. (opencode's own vendored getProxyForUrl is wired solely
into plugin/openai/ws.ts, so this works below its TS, at the runtime.)
Set the variable on the server container for a capture run and leave it
unset otherwise, so nothing sits in the inference path by default.

THE DESIGN RULE: PARSE THE REQUEST, PIPE THE RESPONSE.
The request must be parsed -- that is the artifact being captured, and
the target has to be read off the request line. The response is copied
byte for byte and never parsed, so chunked encoding, SSE framing and
incremental delivery are preserved exactly. A proxy that buffered a
response and re-sent it with its own Content-Length would hold every
token until generation finished, turning a streaming reply into a batch
one and altering the very timing this harness measures. An observer
that changes what it observes is the failure this project exists to
catch; the cheapest way not to commit it is to relay bytes rather than
re-frame them. Verified with a mock that delivers token-by-token with
real delays: the reply arrives incrementally and reassembles intact.

HTTP conformance, per RFC 9110/9112:
  - absolute-URI request targets are accepted and rewritten to
    origin-form for the upstream leg (9112 s3.2.2)
  - hop-by-hop headers are stripped in both directions, including any
    field named by the Connection header (9110 s7.6.1)
  - a Via entry is appended, as a proxy should (9110 s7.6.3)
  - CONNECT is honoured as a blind tunnel (9110 s9.3.6), so an HTTPS
    provider still works -- the body is opaque in that case and the
    record says so rather than pretending otherwise
  - Content-Length and Transfer-Encoding on the response are left
    exactly as the origin set them

SECRETS ARE NEVER WRITTEN. Authorization and api-key headers are
redacted before anything reaches disk: this repo is public, its results
directory is bind-mounted, and a captured provider request carries
credentials in plain text.

SCOPE, stated rather than discovered later: a plain-HTTP provider (the
local Ollama path) is fully visible. An HTTPS provider reaches this as
CONNECT and its body stays encrypted, so cloud runs record the fact of
the request and not its contents.

Usage:
    python3 scripts/tools/capture_proxy.py --port 8888 --output capture.jsonl
"""
from __future__ import annotations

import argparse
import json
import socket
import socketserver
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

# RFC 9110 s7.6.1: connection-specific fields, meaningful only for a
# single hop. A proxy that forwards these corrupts the next connection's
# framing -- Transfer-Encoding especially, since the upstream leg is a
# different connection with its own encoding.
HOP_BY_HOP = frozenset({
    "connection", "proxy-connection", "keep-alive", "te", "trailer",
    "transfer-encoding", "upgrade", "proxy-authenticate", "proxy-authorization",
})

# Redacted before write. Matched case-insensitively against the whole
# field name, plus a substring pass for vendor spellings.
SECRET_HEADERS = frozenset({"authorization", "api-key", "x-api-key", "proxy-authorization", "cookie"})
SECRET_SUBSTRINGS = ("api-key", "apikey", "token", "secret", "authorization")

VIA_TOKEN = "1.1 opencode-model-eval-capture"


def is_secret_header(name: str) -> bool:
    lowered = name.lower()
    if lowered in SECRET_HEADERS:
        return True
    return any(marker in lowered for marker in SECRET_SUBSTRINGS)


def redact_headers(headers: list[tuple[str, str]]) -> dict:
    """Header names kept, secret values replaced.

    The NAMES matter for the record -- which auth scheme was used, which
    vendor headers were set -- and the values never do.
    """
    return {name: ("<redacted>" if is_secret_header(name) else value) for name, value in headers}


def strip_hop_by_hop(headers: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Drop hop-by-hop fields, including any listed in Connection.

    RFC 9110 s7.6.1: Connection names additional fields that are
    themselves single-hop. Forwarding one of those is the classic proxy
    bug that shows up as a mysteriously broken keep-alive three hops
    later.
    """
    connection_named = set()
    for name, value in headers:
        if name.lower() == "connection":
            connection_named.update(token.strip().lower() for token in value.split(","))
    return [(name, value) for name, value in headers
            if name.lower() not in HOP_BY_HOP and name.lower() not in connection_named]


def decode_body(body: bytes, content_type: str) -> tuple[object, str | None]:
    """The parsed body when it is JSON, otherwise a stated reason.

    Never guesses: a body that is not JSON is recorded as its length and
    type rather than as a mangled string, because a capture that quietly
    corrupts what it captures is worse than one that declines.
    """
    if not body:
        return None, "empty body"
    if "json" not in (content_type or "").lower():
        return None, f"not JSON ({content_type or 'no content-type'}, {len(body)} bytes)"
    try:
        return json.loads(body.decode("utf-8")), None
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return None, f"undecodable JSON: {type(exc).__name__}: {exc}"


class _Recorder:
    """Append-only JSONL sink, one line per captured request.

    Line-buffered and lock-guarded: the proxy is threaded, and a
    half-written line in the middle of a run is an artifact nobody can
    parse afterwards.
    """

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self.path.open("a", encoding="utf-8")

    def write(self, record: dict) -> None:
        with self._lock:
            self._fh.write(json.dumps(record, default=str) + "\n")
            self._fh.flush()

    def close(self) -> None:
        with self._lock:
            self._fh.close()


class ProxyHandler(socketserver.StreamRequestHandler):
    recorder: _Recorder | None = None
    verbose = False
    # UNBUFFERED, deliberately. StreamRequestHandler defaults rfile to a
    # BufferedReader, which reads ahead past the request headers -- so a
    # client that pipelines its TLS ClientHello in the same write as the
    # CONNECT has those bytes sitting in a Python buffer while the
    # tunnel pipes directly from the socket, and they are lost. Measured:
    # the origin received b"" and the handshake could never complete.
    # A raw rfile reads exactly what is asked for and nothing more.
    rbufsize = 0

    @staticmethod
    def _enable_keepalive(sock: socket.socket) -> None:
        """No idle deadline on a relayed connection, but not blind either.

        create_connection(timeout=N) leaves the socket in timeout mode,
        so recv() raises after N seconds of silence -- and _pipe treats
        that as end-of-stream, silently truncating a response mid-flight.
        A model that thinks for longer than the timeout before its first
        token would be cut off and look like a short answer; a 236s
        single request has already been measured on this hardware.
        Clearing the timeout removes the deadline; TCP keepalive is what
        still detects a genuinely dead peer.
        """
        sock.settimeout(None)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        except OSError:
            pass

    def handle(self):
        try:
            request_line = self.rfile.readline(65536)
            if not request_line:
                return
            parts = request_line.decode("latin-1").rstrip("\r\n").split()
            if len(parts) != 3:
                self._simple_response(400, b"malformed request line")
                return
            method, target, version = parts
            headers = self._read_headers()
            if method.upper() == "CONNECT":
                self._tunnel(target, headers)
                return
            self._forward(method, target, version, headers)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:                       # noqa: BLE001
            if self.verbose:
                print(f"[capture-proxy] {type(exc).__name__}: {exc}", file=sys.stderr)

    def _read_headers(self) -> list[tuple[str, str]]:
        headers: list[tuple[str, str]] = []
        while True:
            line = self.rfile.readline(65536)
            if not line or line in (b"\r\n", b"\n"):
                break
            decoded = line.decode("latin-1").rstrip("\r\n")
            if ":" not in decoded:
                continue
            name, _, value = decoded.partition(":")
            headers.append((name.strip(), value.strip()))
        return headers

    def _header(self, headers: list[tuple[str, str]], name: str) -> str:
        for key, value in headers:
            if key.lower() == name.lower():
                return value
        return ""

    def _read_exact(self, count: int) -> bytes:
        """Read exactly count bytes, or as many as arrive before EOF.

        Needed because rfile is raw (see rbufsize): a raw read() returns
        what one recv() produced, which for a large request body is
        routinely less than asked for. The buffered reader used to hide
        this, and removing the buffer to fix the CONNECT bug exposes it.
        """
        chunks: list[bytes] = []
        remaining = count
        while remaining > 0:
            chunk = self.rfile.read(remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def _read_body(self, headers: list[tuple[str, str]]) -> bytes:
        """Request body only. Length-delimited or chunked, per RFC 9112.

        The request is the artifact, so it is read fully before
        forwarding. That costs nothing: a chat-completions request is
        already complete before it is sent.
        """
        if self._header(headers, "transfer-encoding").lower().find("chunked") >= 0:
            chunks = []
            while True:
                size_line = self.rfile.readline(65536).strip()
                if not size_line:
                    break
                try:
                    size = int(size_line.split(b";")[0], 16)
                except ValueError:
                    break
                if size == 0:
                    self.rfile.readline(65536)         # trailing CRLF
                    break
                chunks.append(self._read_exact(size))
                self.rfile.readline(65536)
            return b"".join(chunks)
        length = self._header(headers, "content-length")
        if length.isdigit():
            return self._read_exact(int(length))
        return b""

    def _forward(self, method: str, target: str, version: str,
                 headers: list[tuple[str, str]]) -> None:
        split = urlsplit(target)
        if not split.netloc:
            self._simple_response(400, b"proxy requires an absolute-URI request target")
            return
        host = split.hostname or ""
        port = split.port or (443 if split.scheme == "https" else 80)
        origin_form = split.path or "/"
        if split.query:
            origin_form += "?" + split.query

        body = self._read_body(headers)
        self._record(method, target, headers, body, tunnelled=False)

        upstream = socket.create_connection((host, port), timeout=30)
        self._enable_keepalive(upstream)
        try:
            forwarded = strip_hop_by_hop(headers)
            forwarded = [(k, v) for k, v in forwarded if k.lower() != "host"]
            lines = [f"{method} {origin_form} {version}\r\n",
                     f"Host: {split.netloc}\r\n"]
            for name, value in forwarded:
                lines.append(f"{name}: {value}\r\n")
            # RFC 9110 s7.6.3 -- a proxy identifies itself.
            existing_via = self._header(headers, "via")
            lines.append(f"Via: {existing_via + ', ' if existing_via else ''}{VIA_TOKEN}\r\n")
            if body:
                lines.append(f"Content-Length: {len(body)}\r\n")
            lines.append("Connection: close\r\n\r\n")
            upstream.sendall("".join(lines).encode("latin-1"))
            if body:
                upstream.sendall(body)

            # THE RESPONSE IS NEVER PARSED. Bytes are relayed as the
            # origin framed them, so chunked encoding and SSE delivery
            # arrive at opencode exactly as they would without this hop.
            self._pipe(upstream, self.connection)
        finally:
            upstream.close()

    def _tunnel(self, target: str, headers: list[tuple[str, str]]) -> None:
        """CONNECT, per RFC 9110 s9.3.6: a blind byte tunnel.

        An HTTPS provider still works through this proxy, but its body
        is encrypted end to end and cannot be captured without a MITM
        certificate. The record says so explicitly rather than leaving a
        reader to infer that a cloud run simply produced no requests.
        """
        # RFC 9110 s9.3.6: the target is authority-form, host:port, and
        # the port is REQUIRED. A malformed target used to reach int()
        # unguarded and raise ValueError, which the outer handler
        # swallowed -- the client saw its connection close with no
        # response at all, which is indistinguishable from the proxy
        # being down.
        host, _, port = target.rpartition(":")
        if not host or not port.isdigit():
            self._simple_response(400, b"CONNECT requires an authority-form target (host:port)")
            return
        self._record("CONNECT", target, headers, b"", tunnelled=True)
        try:
            upstream = socket.create_connection((host, int(port)), timeout=30)
        except OSError as exc:
            self._simple_response(502, f"tunnel failed: {exc}".encode())
            return
        self._enable_keepalive(upstream)
        self._enable_keepalive(self.connection)
        self.connection.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        pending = self._drain_buffered()
        if pending:
            upstream.sendall(pending)
        client_to_upstream = threading.Thread(
            target=self._pipe, args=(self.connection, upstream), daemon=True)
        client_to_upstream.start()
        self._pipe(upstream, self.connection)
        client_to_upstream.join(timeout=5)
        upstream.close()

    def _drain_buffered(self) -> bytes:
        """Anything rfile read ahead of the headers.

        With rbufsize = 0 there should be nothing, so this normally
        returns empty -- it exists because losing a pipelined ClientHello
        is silent and fatal, and a belt-and-braces drain costs one call.
        """
        buffered = getattr(self.rfile, "peek", None)
        if buffered is None:
            return b""
        try:
            available = self.rfile.peek(0)
        except (OSError, ValueError):
            return b""
        return self._read_exact(len(available)) if available else b""

    @staticmethod
    def _pipe(src: socket.socket, dst: socket.socket) -> None:
        """Byte-for-byte relay, flushed as it arrives.

        No buffering beyond one read: holding a streaming response until
        it completes would delay every token and change the timing the
        harness measures.
        """
        try:
            while True:
                data = src.recv(65536)
                if not data:
                    break
                dst.sendall(data)
        except OSError:
            pass
        finally:
            # Propagate end-of-stream instead of leaving the peer waiting
            # on a direction that will never produce another byte. Without
            # this a half-closed tunnel hangs until something else times
            # out, and the other pipe thread never returns.
            try:
                dst.shutdown(socket.SHUT_WR)
            except OSError:
                pass

    def _record(self, method: str, url: str, headers: list[tuple[str, str]],
                body: bytes, tunnelled: bool) -> None:
        if self.recorder is None:
            return
        content_type = self._header(headers, "content-type")
        parsed, reason = decode_body(body, content_type)
        record = {
            "captured_utc": datetime.now(timezone.utc).isoformat(),
            "monotonic": round(time.monotonic(), 6),
            "method": method,
            "url": url,
            "headers": redact_headers(headers),
            "body_bytes": len(body),
            "body": parsed,
            "body_note": ("encrypted CONNECT tunnel -- request bodies are not "
                          "visible without a MITM certificate" if tunnelled else reason),
        }
        self.recorder.write(record)
        if self.verbose:
            print(f"[capture-proxy] {method} {url} ({len(body)} bytes)", file=sys.stderr)

    def _simple_response(self, status: int, message: bytes) -> None:
        self.connection.sendall(
            f"HTTP/1.1 {status} \r\nContent-Length: {len(message)}\r\n"
            f"Connection: close\r\n\r\n".encode("latin-1") + message
        )


class ThreadingProxy(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=8888)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--output", default="results/capture/provider-requests.jsonl")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    ProxyHandler.recorder = _Recorder(Path(args.output))
    # RUN BOUNDARY. The output is append-only and outlives any single
    # container, which is a genuine trap: 192 CONNECT records read as
    # the cost of one reply until the timestamps showed they spanned
    # three hours and five container recreates. A marker per start makes
    # "this run" a thing a reader can actually delimit.
    ProxyHandler.recorder.write({
        "type": "proxy_start",
        "captured_utc": datetime.now(timezone.utc).isoformat(),
        "monotonic": round(time.monotonic(), 6),
        "listening": f"{args.host}:{args.port}",
    })
    ProxyHandler.verbose = args.verbose
    server = ThreadingProxy((args.host, args.port), ProxyHandler)
    print(f"[capture-proxy] listening on {args.host}:{args.port}, recording to {args.output}",
          file=sys.stderr)
    print("[capture-proxy] point opencode at it with "
          f"http_proxy=http://<host>:{args.port}", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        if ProxyHandler.recorder:
            ProxyHandler.recorder.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

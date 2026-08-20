#!/usr/bin/env python3
"""Does an image attached to an opencode session reach a vision model?

Three things can go wrong and they look identical from the outside, so
this runs both paths against the SAME bytes:

  A. opencode drops or mishandles the file part (upstream issue 20802
     reports exactly this for custom OpenAI-compatible providers, which
     local/ollama is).
  B. Ollama rejects the encoding (/v1 accepts only base64 data URLs for
     jpeg/jpg/png/webp; http(s) URLs are refused outright).
  C. The model genuinely cannot see it.

The control (direct /api/chat with images=[...]) settles B and C. If
the control describes the image and the opencode path does not, the
fault is A.

The test image is generated here rather than shipped: a 96x96 PNG in
three horizontal bands, red then green then blue, top to bottom.
Naming that order correctly is not guessable from the prompt, and the
prompt never states the colours.

Written with stdlib only (zlib + struct for the PNG), matching
docs/CODEGEN.md.
"""
import argparse
import base64
import json
import struct
import sys
import time
import urllib.error
import urllib.request
import zlib

BANDS = [(255, 0, 0), (0, 255, 0), (0, 0, 255)]   # red, green, blue
EXPECTED = ["red", "green", "blue"]
PROMPT = ("Look at the attached image. It has three horizontal bands. "
          "Name the colour of each band from top to bottom. "
          "Answer with three words only, comma separated.")


def make_png(size=96):
    """Three horizontal bands, no dependencies."""
    rows = []
    for y in range(size):
        r, g, b = BANDS[min(y * len(BANDS) // size, len(BANDS) - 1)]
        rows.append(b"\x00" + bytes([r, g, b]) * size)   # filter byte 0
    raw = b"".join(rows)

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)  # 8-bit RGB
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def post(url, body, timeout, headers=None):
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/json", **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def score(reply):
    """Did it name the three bands in order?"""
    lowered = reply.lower()
    positions = []
    for colour in EXPECTED:
        idx = lowered.find(colour)
        if idx < 0:
            return False, f"{colour!r} not mentioned"
        positions.append(idx)
    if positions != sorted(positions):
        return False, f"colours named out of order: {positions}"
    return True, "all three named, in order"


def via_opencode(base_url, provider, model_id, png, timeout):
    session = post(f"{base_url}/session", {}, 60)
    session_id = session["id"]
    body = {
        "providerID": provider,
        "modelID": model_id,
        "parts": [
            {"type": "text", "text": PROMPT},
            {"type": "file", "mime": "image/png", "filename": "bands.png",
             "url": "data:image/png;base64," + base64.b64encode(png).decode()},
        ],
    }
    started = time.monotonic()
    response = post(f"{base_url}/session/{session_id}/message", body, timeout)
    elapsed = round(time.monotonic() - started, 1)
    parts = response.get("parts") or []
    text = "".join(p.get("text", "") for p in parts if p.get("type") == "text")
    return {
        "reply": text.strip(),
        "elapsed_s": elapsed,
        "finish": (response.get("info") or {}).get("finish"),
        "part_types_returned": sorted({p.get("type") for p in parts}),
        "session": session_id,
    }


def via_ollama(ollama_url, model_id, png, timeout):
    started = time.monotonic()
    response = post(f"{ollama_url}/api/chat", {
        "model": model_id,
        "messages": [{"role": "user", "content": PROMPT,
                      "images": [base64.b64encode(png).decode()]}],
        "stream": False,
    }, timeout)
    return {
        "reply": (response.get("message") or {}).get("content", "").strip(),
        "elapsed_s": round(time.monotonic() - started, 1),
        "done_reason": response.get("done_reason"),
        "eval_count": response.get("eval_count"),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--server-url", default="http://opencode-model-eval-server:4096")
    ap.add_argument("--ollama-url", default="http://host.docker.internal:11434")
    ap.add_argument("--provider", default="local/ollama")
    ap.add_argument("--model", default="qwen3.8:latest")
    ap.add_argument("--timeout", type=int, default=1800)
    args = ap.parse_args()

    png = make_png()
    print(f"test image: {len(png)} bytes, bands {EXPECTED} top to bottom",
          flush=True)

    result = {"model": f"{args.provider}/{args.model}", "image_bytes": len(png)}

    print("\n=== CONTROL: direct /api/chat with images=[...] ===", flush=True)
    try:
        control = via_ollama(args.ollama_url, args.model, png, args.timeout)
        ok, why = score(control["reply"])
        control["sees_image"] = ok
        control["verdict"] = why
        result["control"] = control
        print(json.dumps(control, indent=2), flush=True)
    except Exception as exc:                            # noqa: BLE001
        result["control"] = {"error": f"{type(exc).__name__}: {exc}"}
        print(result["control"], flush=True)

    print("\n=== OPENCODE: session file part, same bytes ===", flush=True)
    try:
        through = via_opencode(args.server_url, args.provider, args.model,
                               png, args.timeout)
        ok, why = score(through["reply"])
        through["sees_image"] = ok
        through["verdict"] = why
        result["opencode"] = through
        print(json.dumps(through, indent=2), flush=True)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:600]
        result["opencode"] = {"error": f"HTTP {exc.code}", "body": detail}
        print(result["opencode"], flush=True)
    except Exception as exc:                            # noqa: BLE001
        result["opencode"] = {"error": f"{type(exc).__name__}: {exc}"}
        print(result["opencode"], flush=True)

    control_ok = result.get("control", {}).get("sees_image")
    opencode_ok = result.get("opencode", {}).get("sees_image")
    if control_ok and opencode_ok:
        conclusion = "BOTH PATHS WORK -- opencode delivers the image"
    elif control_ok and not opencode_ok:
        conclusion = ("ONLY THE CONTROL WORKS -- the model and Ollama handle "
                      "the image, opencode does not deliver it (issue 20802 shape)")
    elif not control_ok and opencode_ok:
        conclusion = "ONLY OPENCODE WORKS -- unexpected, re-check the control"
    else:
        conclusion = ("NEITHER PATH WORKS -- model or encoding, not opencode; "
                      "the control rules opencode out")
    result["conclusion"] = conclusion
    print("\n=== CONCLUSION ===\n" + conclusion, flush=True)
    print("\n" + json.dumps(result), flush=True)
    return 0 if control_ok is not None else 1


if __name__ == "__main__":
    sys.exit(main())

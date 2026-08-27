#!/usr/bin/env python3
"""Where the host is, seen from inside a container.

One home for this, deliberately. A cicd_runner worker joins the default
bridge with no --add-host, so `host.docker.internal` does not resolve
there and the route table is the only answer -- while the compose
services DO get that alias, and a developer shell is not in a container
at all. Every candidate is right somewhere and wrong elsewhere, so the
answer is a chain rather than a constant. Two copies of that chain would
drift exactly as two copies of "is the server answering" would.

CLI, for shell callers:

    python3 scripts/hostnet.py                        # print the gateway
    python3 scripts/hostnet.py --urls 11434 /api/tags [override]
    python3 scripts/hostnet.py --reachable 11434 /api/tags [override]

--reachable prints the first candidate that answers and exits 0; exits 1
when none does, so a caller can tell "nothing to talk to" (skip) from "it
answered" (a real check) without writing its own sweep.
"""
import sys
import urllib.error
import urllib.request
from pathlib import Path

TIMEOUT_S = 2


def default_gateway():
    """The host's address as seen from inside a container, or None.

    Reads the default route (destination 00000000) out of /proc/net/route
    and unpacks its little-endian hex gateway field.
    """
    try:
        for line in Path("/proc/net/route").read_text().splitlines()[1:]:
            fields = line.split()
            if len(fields) > 2 and fields[1] == "00000000":
                hex_ip = fields[2]
                return ".".join(str(int(hex_ip[i:i + 2], 16)) for i in (6, 4, 2, 0))
    except OSError:
        pass
    return None


def host_candidates(port, path="", explicit=""):
    """Every URL worth trying for a service listening on the host.

    Cheapest and most specific first: an explicit override wins outright,
    then the loopback forms, then the docker alias, then the gateway.
    """
    if explicit:
        return [explicit]
    urls = [
        f"http://localhost:{port}{path}",
        f"http://127.0.0.1:{port}{path}",
        f"http://host.docker.internal:{port}{path}",
    ]
    gateway = default_gateway()
    if gateway:
        urls.append(f"http://{gateway}:{port}{path}")
    return urls


def first_reachable(candidates, timeout=TIMEOUT_S):
    """The first candidate that answers at all, or None.

    Any HTTP answer counts, including an error status: this establishes
    that something is listening and speaking HTTP, not that the endpoint
    is happy. The caller decides what a good answer looks like.
    """
    for url in candidates:
        try:
            urllib.request.urlopen(url, timeout=timeout)
            return url
        except urllib.error.HTTPError:
            return url
        except Exception:
            continue
    return None


def _cli(argv):
    if not argv:
        gateway = default_gateway()
        if not gateway:
            return 1
        print(gateway)
        return 0

    mode = argv[0]
    if mode not in ("--urls", "--reachable"):
        print(__doc__.strip(), file=sys.stderr)
        return 2
    if len(argv) < 2:
        print("a port is required", file=sys.stderr)
        return 2

    port = argv[1]
    path = argv[2] if len(argv) > 2 else ""
    explicit = argv[3] if len(argv) > 3 else ""
    candidates = host_candidates(port, path, explicit)

    if mode == "--urls":
        print("\n".join(candidates))
        return 0

    found = first_reachable(candidates)
    if not found:
        return 1
    print(found)
    return 0


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))

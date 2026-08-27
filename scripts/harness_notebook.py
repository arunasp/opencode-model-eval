#!/usr/bin/env python3
"""Notebook-facing interfaces to the harness: opencode and Ollama.

A notebook cell should not reimplement session handling or model
warm-up. Both already exist in run_eval_client.py, hardened by real
failures -- extract_reply() raising on info.finish == "error" after a
context overflow was once scored a clean PASS against an empty
transcript, abort_session() running on every exception path after
sessions were left retrying server-side forever, and the Ollama
warm-up existing because a cold model ate a tier's whole budget. This
module wraps those rather than restating them, so a notebook inherits
the fixes instead of repeating the bugs.

Two interfaces, deliberately separate:

    OpencodeSession -- talk to `opencode serve` over HTTP, which is
        what the real harness does. Tool use, permissions and the
        provider routing all behave as they do in a real eval run.

    OllamaModel -- load and unload a local model directly, for the
        cases where the point IS residency: measuring cold-start,
        freeing VRAM between runs, or checking what is resident.

WHICH TO USE. Prefer OpencodeSession for anything meant to resemble an
eval. Going to Ollama's own /api/chat bypasses opencode entirely -- no
tool use, no permission profile, no provider config -- so results from
it are not comparable with harness results, and the CVV categories
that depend on tool use will misfire. OllamaModel is for residency
control, not for asking models questions.

Stdlib only (urllib), matching docs/CODEGEN.md.

Usage:

    from harness_notebook import OpencodeSession, OllamaModel

    with OpencodeSession(provider="local/ollama",
                         model_id="qwen2.5-coder:7b") as s:
        print(s.ask("hi"))

    with OllamaModel("qwen2.5-coder:7b"):   # warm before, unload after
        ...
"""
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# The harness scripts are mounted read-only into the jupyter container
# and are importable from a normal checkout too. Both paths are added
# rather than assuming one, so the same notebook runs in either place.
_HERE = Path(__file__).resolve().parent
for _candidate in (_HERE, Path("/opt/harness/scripts")):
    if _candidate.is_dir() and str(_candidate) not in sys.path:
        sys.path.insert(0, str(_candidate))

from run_eval_client import (  # noqa: E402
    abort_session,
    create_session,
    extract_reply,
    ollama_ps,
    send_message,
    unload_local_model,
    warm_up_local_model,
)
from e2e_session_probe import (  # noqa: E402
    candidate_urls,
    delete_session,
    wait_for_server,
)
from discover_and_select_model import (  # noqa: E402
    is_free,
    parse_models,
    run_opencode_models_verbose,
)
from discover_local_ollama_models import fetch_ollama_model_names  # noqa: E402
sys.path.insert(0, str(_HERE / "tools"))
from read_local_ollama_models import load_local_ollama_models  # noqa: E402

DEFAULT_READY_TIMEOUT_S = int(os.environ.get("E2E_READY_TIMEOUT_S", "120"))
DEFAULT_OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL",
                                         "http://host.docker.internal:11434")
DEFAULT_OLLAMA_TAGS_URL = os.environ.get(
    "OPENCODE_OLLAMA_TAGS_URL", DEFAULT_OLLAMA_BASE_URL.rstrip("/") + "/api/tags")
OLLAMA_PROVIDER = os.environ.get("OPENCODE_OLLAMA_PROVIDER_KEY", "local/ollama")
DISCOVERED_ENV = Path(__file__).resolve().parent.parent / "results" / "discovered" / "discovered-model.env"


class HarnessError(RuntimeError):
    """Raised for a condition the notebook author needs to act on."""


def _split_full_id(full_id):
    """Split provider/model, honouring the two-segment provider key.

    `local/ollama` is ONE provider name containing a slash, so a naive
    partition on the first slash yields provider="local" and a modelID
    that starts with "ollama/" -- accepted silently by every caller and
    rejected by the server at the first message. The same special case
    already exists in scripts/tf-select-and-run-eval.sh; it lives here
    too because parse_models() does the naive split.
    """
    if full_id.startswith(OLLAMA_PROVIDER + "/"):
        return OLLAMA_PROVIDER, full_id[len(OLLAMA_PROVIDER) + 1:]
    provider, _, model_id = full_id.partition("/")
    return provider, model_id


def list_models(opencode=True, ollama=True, ollama_tags_url=None):
    """Every model reachable right now, from both sources.

    Returns dicts with provider, model, full_id, free and source.

    Two sources, and they answer different questions. `opencode models
    --verbose` is what the server will actually accept, including cloud
    providers and whatever the global config declares -- that is the
    authoritative list for an eval. Ollama's own /api/tags is what is
    installed on the host, which can include models opencode has no
    provider entry for, so they are listed but marked, since selecting
    one that opencode cannot route to fails at the first message.

    Neither source is faked when unavailable: a source that cannot be
    reached contributes nothing and is reported in `sources_failed` on
    the returned list, rather than silently shortening the results.
    """
    found = []
    failed = []

    if opencode:
        try:
            records = parse_models(run_opencode_models_verbose())
            for r in records:
                provider, model_id = _split_full_id(r["full_id"])
                found.append({"provider": provider, "model": model_id,
                              "full_id": r["full_id"], "free": is_free(r),
                              "source": "opencode"})
        except Exception as exc:                       # noqa: BLE001
            failed.append(f"opencode: {type(exc).__name__}: {exc}")

    if ollama:
        try:
            names = fetch_ollama_model_names(ollama_tags_url or DEFAULT_OLLAMA_TAGS_URL,
                                             timeout=3)
            known = {m["full_id"] for m in found}
            for name in names or []:
                full_id = f"{OLLAMA_PROVIDER}/{name}"
                if full_id in known:
                    continue
                found.append({"provider": OLLAMA_PROVIDER, "model": name,
                              "full_id": full_id, "free": True,
                              "source": "ollama-only"})
        except Exception as exc:                       # noqa: BLE001
            failed.append(f"ollama: {type(exc).__name__}: {exc}")

    found.sort(key=lambda m: (m["source"], m["provider"], m["model"]))
    found = _ModelList(found)
    found.sources_failed = failed
    return found


class _ModelList(list):
    """A list that also carries which sources could not be reached."""
    sources_failed = ()

    def show(self):
        """Print the list, grouped, for reading in a notebook cell."""
        for src in sorted({m["source"] for m in self}):
            rows = [m for m in self if m["source"] == src]
            print(f"{src} ({len(rows)}):")
            for m in rows:
                cost = "free" if m["free"] else "paid"
                print(f"  {m['full_id']}  [{cost}]")
        for problem in self.sources_failed:
            print(f"source unavailable -- {problem}")


def select_model(match=None, source=None, models=None):
    """Resolve one provider/model pair, or fail with the candidates.

    Resolution order: an explicit `match` narrowing the list, then
    OPENCODE_MODEL_PROVIDER/OPENCODE_MODEL_ID, then the env file a
    discovery run wrote.

    AMBIGUITY IS AN ERROR, not an invitation to pick. A notebook that
    quietly selected a different model than intended would produce
    results attributed to the wrong one, and nothing downstream would
    show it -- so more than one match raises, listing them. That is a
    deliberate difference from the unattended picker in
    discover_and_select_model.py, which has no one to ask.

    Interactive selection is left to the author assigning the result,
    rather than a widget: a notebook that needs a click cannot run
    headless under papermill, which is the path this library exists to
    keep open.
    """
    if match:
        candidates = models if models is not None else list_models()
        hits = [m for m in candidates
                if match in m["full_id"] and (source is None or m["source"] == source)]
        if not hits:
            raise HarnessError(
                f"no model matches {match!r}. Available: "
                + ", ".join(m["full_id"] for m in candidates) or "(none found)")
        if len(hits) > 1:
            raise HarnessError(
                f"{match!r} matches {len(hits)} models, narrow it: "
                + ", ".join(m["full_id"] for m in hits))
        return hits[0]["provider"], hits[0]["model"]

    provider = os.environ.get("OPENCODE_MODEL_PROVIDER", "")
    model_id = os.environ.get("OPENCODE_MODEL_ID", "")
    if provider and model_id:
        return provider, model_id

    if DISCOVERED_ENV.is_file():
        values = {}
        for line in DISCOVERED_ENV.read_text().splitlines():
            if "=" in line and not line.strip().startswith("#"):
                key, _, value = line.partition("=")
                values[key.strip()] = value.strip().strip('"').strip("'")
        provider = values.get("OPENCODE_MODEL_PROVIDER", "")
        model_id = values.get("OPENCODE_MODEL_ID", "")
        if provider and model_id:
            return provider, model_id

    raise HarnessError(
        "no model selected. Pass match=..., set OPENCODE_MODEL_PROVIDER and "
        "OPENCODE_MODEL_ID, or run discovery first. list_models().show() "
        "prints what is available.")


class ModelPicker:
    """A dropdown for choosing a model in Jupyter.

    Interactive convenience only. It SETS a choice; it never decides
    one. `resolve()` below applies precedence, so a headless run is not
    at the mercy of whatever the dropdown happened to default to --
    which is the failure a widget invites: nobody clicks it, and the run
    silently uses the first entry.

    Requires ipywidgets. Where it is missing, construction raises and
    the notebook falls back to `resolve()` with no picker, so the same
    cell works in an image without it.
    """

    def __init__(self, models=None, default=None):
        try:
            import ipywidgets as widgets      # noqa: PLC0415
        except ImportError as exc:
            raise HarnessError(
                "ipywidgets is not installed -- use resolve() or select_model(match=...) "
                "instead, or add ipywidgets to the image") from exc

        self.models = models if models is not None else list_models()
        if not self.models:
            raise HarnessError("no models discovered, nothing to pick from")

        options = [(f"{m['full_id']}  [{m['source']}, {'free' if m['free'] else 'paid'}]",
                    m["full_id"]) for m in self.models]
        self.filter = widgets.Text(description="filter", placeholder="substring, e.g. qwen")
        self.dropdown = widgets.Dropdown(options=options, description="model",
                                         value=default or options[0][1],
                                         layout=widgets.Layout(width="60%"))
        self.filter.observe(self._apply_filter, names="value")
        self.box = widgets.VBox([self.filter, self.dropdown])

    def _apply_filter(self, _change):
        text = self.filter.value.strip()
        rows = [m for m in self.models if text in m["full_id"]] if text else self.models
        options = [(f"{m['full_id']}  [{m['source']}, {'free' if m['free'] else 'paid'}]",
                    m["full_id"]) for m in rows]
        self.dropdown.options = options or [("(no match)", None)]

    @property
    def selection(self):
        """(provider, model_id) for the current choice, or None."""
        full_id = self.dropdown.value
        if not full_id:
            return None
        for m in self.models:
            if m["full_id"] == full_id:
                return m["provider"], m["model"]
        return None

    def _ipython_display_(self):
        from IPython.display import display   # noqa: PLC0415
        display(self.box)


def resolve(parameter=None, picker=None):
    """Decide which model to use, by precedence rather than by luck.

    Order, highest first:
      1. `parameter` -- a papermill-injected value from the notebook's
         parameters cell. An automated run must win over anything a
         widget is showing, or the run is not reproducible.
      2. `picker` -- what a human chose in this session.
      3. environment, then the discovery env file (see select_model).

    Accepts either "provider/model" or a (provider, model) pair for
    `parameter`, since a papermill argument is a single string.
    """
    if parameter:
        if isinstance(parameter, (tuple, list)) and len(parameter) == 2:
            return tuple(parameter)
        provider, model_id = _split_full_id(parameter)
        if not provider or not model_id:
            raise HarnessError(f"cannot parse {parameter!r} as provider/model")
        return provider, model_id

    if picker is not None:
        chosen = picker.selection
        if chosen:
            return chosen

    return select_model()


class OpencodeSession:
    """One session against a running `opencode serve`.

    Use as a context manager. Exit closes the session on every path,
    including an exception -- a session left open keeps retrying
    server-side, and for a local model keeps it resident.

    The server is resolved rather than assumed: an explicit base_url
    wins, otherwise the same candidate list the probe uses, and a
    server that is listening but still starting is waited for rather
    than reported absent.
    """

    def __init__(self, provider=None, model_id=None, base_url=None,
                 timeout=300, ready_timeout=DEFAULT_READY_TIMEOUT_S):
        self.provider = provider or os.environ.get("OPENCODE_MODEL_PROVIDER", "")
        self.model_id = model_id or os.environ.get("OPENCODE_MODEL_ID", "")
        if not self.provider or not self.model_id:
            raise HarnessError(
                "provider and model_id are required -- pass them, or set "
                "OPENCODE_MODEL_PROVIDER and OPENCODE_MODEL_ID")
        self._requested_url = base_url or os.environ.get("OPENCODE_SERVER_URL", "")
        self.timeout = timeout
        self.ready_timeout = ready_timeout
        self.base_url = ""
        self.session_id = None
        self.replies = []

    def __enter__(self):
        candidates = candidate_urls(self._requested_url)
        self.base_url = wait_for_server(candidates, self.ready_timeout)
        if not self.base_url:
            raise HarnessError(
                "no opencode serve instance answered. Tried: "
                + ", ".join(candidates)
                + ". Start one with `make server-up`, or set "
                "OPENCODE_SERVER_URL.")
        self.session_id = create_session(self.base_url)
        return self

    def ask(self, text):
        """Send one message, return the reply text.

        Raises on an error finish rather than returning the empty
        string -- a silent empty transcript is how a real failure once
        scored as a pass.
        """
        if not self.session_id:
            raise HarnessError("session is not open -- use OpencodeSession as a context manager")
        response = send_message(self.base_url, self.session_id, self.provider,
                                self.model_id, text, timeout=self.timeout)
        reply, parts = extract_reply(response)
        self.replies.append({"prompt": text, "reply": reply, "parts": parts,
                             "finish": response.get("info", {}).get("finish")})
        return reply

    def close(self):
        if not self.session_id:
            return
        session_id, self.session_id = self.session_id, None
        try:
            abort_session(self.base_url, session_id)
        finally:
            delete_session(self.base_url, session_id)

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False


class OllamaModel:
    """Direct access to one local Ollama model: chat, residency, detail.

    Complete enough that a notebook never writes its own HTTP. Where a
    method duplicates something the harness already does, it calls the
    harness rather than reimplementing it.

    THE PATH MATTERS AND IS RECORDED. `chat()` talks to Ollama's own
    /api/chat, which bypasses opencode entirely -- no tool use, no
    permission profile, no provider routing -- so its results are NOT
    comparable with harness or OpencodeSession results, and CVV
    categories that depend on tool use will over-fire against them.
    That is a legitimate thing to want (raw model behaviour, cold-start
    measurement, prompt iteration without the harness in the way), so
    it is supported rather than discouraged, and every reply carries
    `via="ollama-direct"` so a mixed set of results stays
    distinguishable after the fact.

    Use OpencodeSession when the question is about an eval.
    """

    def __init__(self, model_id, provider=OLLAMA_PROVIDER, base_url=None,
                 ollama_base_url=DEFAULT_OLLAMA_BASE_URL, unload_on_exit=True):
        self.model_id = model_id
        self.provider = provider
        self._requested_url = base_url or os.environ.get("OPENCODE_SERVER_URL", "")
        self.ollama_base_url = ollama_base_url.rstrip("/")
        self.unload_on_exit = unload_on_exit
        self.replies = []

    # -- discovery ------------------------------------------------------

    @staticmethod
    def installed(ollama_tags_url=None):
        """Model names installed on the host, from /api/tags."""
        return fetch_ollama_model_names(ollama_tags_url or DEFAULT_OLLAMA_TAGS_URL,
                                        timeout=3) or []

    @staticmethod
    def declared(config_path=None):
        """Models the global opencode config declares for this provider.

        Installed and declared are different sets: a model can be
        pulled but absent from the config, in which case opencode
        cannot route to it, or declared but not pulled, in which case
        the first request fails at Ollama.
        """
        path = config_path or os.environ.get(
            "OPENCODE_GLOBAL_CONFIG", str(Path.home() / ".config/opencode/opencode.json"))
        try:
            return load_local_ollama_models(path) or []
        except Exception:                              # noqa: BLE001
            return []

    @classmethod
    def discover(cls, ollama_tags_url=None, config_path=None):
        """Both sets and their disagreement, which is the useful part.

        Returns installed, declared, and the two differences -- a model
        pulled but undeclared cannot be reached through opencode, and a
        model declared but not pulled fails at the first request.
        """
        installed = set(cls.installed(ollama_tags_url))
        declared = set(cls.declared(config_path))
        return {
            "installed": sorted(installed),
            "declared": sorted(declared),
            "installed_not_declared": sorted(installed - declared),
            "declared_not_installed": sorted(declared - installed),
        }

    def show(self):
        """Everything known about this model right now, printed."""
        d = self.discover()
        print(f"model: {self.model_id}")
        print(f"  installed on host: {self.model_id in d['installed']}")
        print(f"  declared in config: {self.model_id in d['declared']}")
        loaded = [entry.get("name") for entry in self.resident()]
        print(f"  resident now: {self.model_id in loaded}")
        if d["installed_not_declared"]:
            print(f"  pulled but not declared (opencode cannot route): {d['installed_not_declared']}")
        if d["declared_not_installed"]:
            print(f"  declared but not pulled (first request will fail): {d['declared_not_installed']}")

    def details(self):
        """Ollama's own /api/show for this model: parameters, template,
        context length and quantisation.

        num_ctx is worth reading before a run. A model whose context is
        smaller than opencode's real prompt fails every message and
        looks exactly like a hang from the client side -- that is a
        failure this project has already spent a day on.
        """
        return self._post("/api/show", {"model": self.model_id})

    # -- residency ------------------------------------------------------

    def resident(self):
        """Models currently loaded, as a list of dicts from /api/ps.

        Residency only. Ollama exposes no field for "mid-request", so
        this cannot tell you whether a model is busy.

        Normalised: run_eval_client.ollama_ps() returns the bare list,
        while Ollama's own endpoint wraps it in {"models": [...]}. Both
        shapes reach here depending on the caller, and a check that
        tolerates one by silently taking a branch for the other reports
        "nothing loaded" for a type error -- which is how this was
        missed the first time.
        """
        raw = ollama_ps(self.ollama_base_url)
        if isinstance(raw, dict):
            return raw.get("models") or []
        return raw or []

    def is_resident(self):
        """Whether this model is loaded right now."""
        return any(entry.get("name") == self.model_id for entry in self.resident())

    def warm_up(self):
        """Load the model by making a real request THROUGH opencode.

        Deliberately not a direct call: the first request through the
        real path is what pays the load cost, so that is the one to
        make on purpose rather than discover inside a timed tier.
        """
        base_url = wait_for_server(candidate_urls(self._requested_url),
                                   DEFAULT_READY_TIMEOUT_S)
        if not base_url:
            raise HarnessError("no opencode serve instance answered, cannot warm up")
        return warm_up_local_model(base_url, self.provider, self.model_id)

    def unload(self):
        """Free the model (keep_alive: 0), confirmed against /api/ps."""
        return unload_local_model(self.ollama_base_url, self.model_id)

    # -- direct inference ----------------------------------------------

    def chat(self, prompt, system=None, options=None, timeout=300):
        """One /api/chat exchange, straight to Ollama.

        Returns the reply text. The full record, including via and the
        options sent, is appended to self.replies.

        NOT an eval. See the class docstring: this bypasses opencode,
        so nothing here is comparable with harness results.
        """
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        body = {"model": self.model_id, "messages": messages, "stream": False}
        if options:
            body["options"] = options

        started = time.monotonic()
        response = self._post("/api/chat", body, timeout=timeout)
        reply = (response.get("message") or {}).get("content", "")
        self.replies.append({
            "via": "ollama-direct",
            "model": self.model_id,
            "prompt": prompt,
            "system": system,
            "options": options,
            "reply": reply,
            "elapsed_s": round(time.monotonic() - started, 2),
            "done_reason": response.get("done_reason"),
            "eval_count": response.get("eval_count"),
        })
        if not reply.strip():
            raise HarnessError(
                f"/api/chat returned an empty reply for {self.model_id} "
                f"(done_reason={response.get('done_reason')!r}) -- an empty "
                "transcript scored as a pass is a failure this project has "
                "already had once")
        return reply

    def _post(self, path, body, timeout=60):
        data = json.dumps(body).encode()
        req = urllib.request.Request(
            f"{self.ollama_base_url}{path}", data=data, method="POST",
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:400]
            raise HarnessError(
                f"Ollama {path} failed: HTTP {exc.code}: {detail}") from exc
        except Exception as exc:
            raise HarnessError(
                f"Ollama {path} unreachable at {self.ollama_base_url}: "
                f"{type(exc).__name__}: {exc}") from exc

    def __enter__(self):
        self.warm_up()
        return self

    def __exit__(self, exc_type, exc, tb):
        if self.unload_on_exit:
            self.unload()
        return False

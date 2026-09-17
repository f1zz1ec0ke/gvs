"""Pluggable web-search providers for the researcher role.

A :class:`SearchProvider` answers ``search(query) -> list[dict]`` where each dict
has ``title``, ``url`` and ``snippet``. Three config-driven providers are shipped
so users can plug in anything:

- ``brave``   the Brave Search API (default)
- ``http``    a generic JSON search endpoint (URL template + headers + field mapping)
- ``command`` a local command/script whose stdout is a JSON list of results
"""

import json
import subprocess
import urllib.parse
import urllib.request

MAX_RESULTS = 8
SEARCH_TIMEOUT = 30   # seconds


class SearchError(Exception):
    """Raised when a search cannot be performed."""


# --- shared helpers ----------------------------------------------------------

def _http_get(url, headers=None, timeout=SEARCH_TIMEOUT):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _clean(results):
    out = []
    for r in (results or [])[:MAX_RESULTS]:
        if not isinstance(r, dict):
            continue
        out.append({"title": str(r.get("title", "")),
                    "url": str(r.get("url", "")),
                    "snippet": str(r.get("snippet", ""))})
    return out


def _dig(obj, path):
    """Fetch a (possibly nested) field by a dot path, e.g. ``source.url``."""
    cur = obj
    for part in str(path).split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return ""
    return str(cur) if cur is not None else ""


def _find_items(data):
    """Locate the results list in an arbitrary JSON response."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("results", "items", "data", "organic", "searchResults", "web"):
            v = data.get(key)
            if isinstance(v, list):
                return v
            if isinstance(v, dict) and isinstance(v.get("results"), list):
                return v["results"]
    return []


def _split_command(cmd):
    """Split a command string into args, respecting double quotes.

    Unlike ``shlex.split``, this does NOT treat backslashes as escape
    characters, so Windows paths (``C:\\...``) survive intact.
    """
    args, cur, in_quote = [], [], False
    for c in cmd:
        if c == '"':
            in_quote = not in_quote
        elif c in (" ", "\t") and not in_quote:
            if cur:
                args.append("".join(cur))
                cur = []
        else:
            cur.append(c)
    if cur:
        args.append("".join(cur))
    return args


def _build_args(template, query):
    """Split ``template`` and substitute the ``{query}`` placeholder with the
    query as a single clean argument."""
    out = []
    for tok in _split_command(template):
        if tok == "{query}":
            out.append(str(query))
        elif "{query}" in tok:
            out.append(tok.replace("{query}", str(query)))
        else:
            out.append(tok)
    return out


# --- providers ---------------------------------------------------------------

class BraveSearch:
    """Brave Search API: https://api.search.brave.com/res/v1/web/search"""

    def __init__(self, api_key="", base_url="https://api.search.brave.com/res/v1/web/search"):
        self.api_key = api_key
        self.base_url = base_url

    def search(self, query):
        if not self.api_key:
            raise SearchError("the brave provider needs an API key (search_api_key)")
        url = self.base_url + "?" + urllib.parse.urlencode({"q": query, "count": MAX_RESULTS})
        raw = _http_get(url, headers={"X-Subscription-Token": self.api_key,
                                      "Accept": "application/json"})
        data = json.loads(raw.decode("utf-8"))
        items = _find_items(data)
        results = [{"title": r.get("title", ""), "url": r.get("url", ""),
                    "snippet": r.get("description", "")} for r in items if isinstance(r, dict)]
        return _clean(results)


class HttpSearch:
    """A generic JSON search endpoint.

    Configure a URL template containing ``{query}``, optional request headers
    (for auth), and a field mapping for title/url/snippet (dot paths allowed).
    Works with any JSON search API.
    """

    def __init__(self, url_template, headers=None, mapping=None, timeout=SEARCH_TIMEOUT):
        if not url_template or "{query}" not in url_template:
            raise SearchError("the http provider needs a url_template containing {query}")
        self.url_template = url_template
        self.headers = headers or {}
        self.mapping = mapping or {}
        self.timeout = timeout

    def search(self, query):
        url = self.url_template.replace("{query}", urllib.parse.quote(str(query)))
        raw = _http_get(url, headers=self.headers, timeout=self.timeout)
        data = json.loads(raw.decode("utf-8"))
        items = _find_items(data)
        m = self.mapping
        results = [{"title": _dig(r, m.get("title", "title")),
                    "url": _dig(r, m.get("url", "url")),
                    "snippet": _dig(r, m.get("snippet", "snippet"))}
                   for r in items if isinstance(r, dict)]
        return _clean(results)


class CommandSearch:
    """Run a local command/script with the query substituted; parse stdout as a
    JSON list of results (or an object with a ``results`` list)."""

    def __init__(self, command_template, timeout=SEARCH_TIMEOUT, cwd=None):
        if not command_template or "{query}" not in command_template:
            raise SearchError("the command provider needs a command_template containing {query}")
        self.command_template = command_template
        self.timeout = timeout
        self.cwd = cwd

    def search(self, query):
        args = _build_args(self.command_template, query)
        try:
            r = subprocess.run(args, capture_output=True, text=True,
                               timeout=self.timeout, cwd=self.cwd)
        except subprocess.TimeoutExpired:
            raise SearchError(f"search command timed out (> {self.timeout}s)")
        except FileNotFoundError as e:
            raise SearchError(f"search command not found: {e}")
        if r.returncode != 0:
            raise SearchError(f"search command failed ({r.returncode}): {r.stderr[:200]}")
        try:
            data = json.loads(r.stdout)
        except json.JSONDecodeError:
            raise SearchError("search command did not return valid JSON")
        items = _find_items(data)
        results = [{"title": r.get("title", ""), "url": r.get("url", ""),
                    "snippet": r.get("snippet", "")} for r in items if isinstance(r, dict)]
        return _clean(results)


# --- factory -----------------------------------------------------------------

def make_search_provider(cfg):
    """Build a SearchProvider from a config dict (the web GUI's Config fields).

    Recognised keys: ``search_provider`` (brave|http|command), ``search_api_key``,
    ``search_url``, ``search_headers``, ``search_mapping``, ``search_command``.
    Returns ``None`` when no usable provider is configured.
    """
    kind = (cfg.get("search_provider") or "").strip().lower()
    if not kind:
        return None
    if kind == "brave":
        if not cfg.get("search_api_key"):
            return None
        return BraveSearch(api_key=cfg["search_api_key"])
    if kind == "http":
        if not cfg.get("search_url"):
            return None
        return HttpSearch(cfg["search_url"], headers=cfg.get("search_headers") or {},
                          mapping=cfg.get("search_mapping") or {})
    if kind == "command":
        if not cfg.get("search_command"):
            return None
        return CommandSearch(cfg["search_command"])
    return None


def format_results(results):
    """Render search results as a compact, model-readable block."""
    if not results:
        return "(no results)"
    lines = []
    for i, r in enumerate(results, 1):
        lines.append(f"{i}. {r['title']}\n   URL: {r['url']}\n   {r['snippet']}")
    return "\n".join(lines)

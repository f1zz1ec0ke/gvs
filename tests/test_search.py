"""Unit tests for the pluggable search providers.

Run:  python tests/test_search.py
"""

import os
import sys
import json
import tempfile

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import gvs_tui.search as search

failures = 0


def check(name, cond):
    global failures
    if cond:
        print("  ok  " + name)
    else:
        print("  FAIL " + name)
        failures += 1


def main():
    print("helpers")
    check("_clean caps + normalises",
          len(search._clean([{"title": "a", "url": "u", "snippet": "s"}] * 20)) == search.MAX_RESULTS)
    check("_dig nested", search._dig({"source": {"url": "x"}}, "source.url") == "x")
    check("_find_items top-level list", search._find_items([{"a": 1}]) == [{"a": 1}])
    check("_find_items under results", search._find_items({"results": [{"a": 1}]}) == [{"a": 1}])
    check("_find_items under web.results", search._find_items({"web": {"results": [{"a": 1}]}}) == [{"a": 1}])

    print("BraveSearch (mocked HTTP)")
    orig = search._http_get
    brave_body = json.dumps({"web": {"results": [
        {"title": "T1", "url": "https://a", "description": "D1"},
        {"title": "T2", "url": "https://b", "description": "D2"},
    ]}}).encode()
    search._http_get = lambda url, headers=None, timeout=30: brave_body
    try:
        b = search.BraveSearch(api_key="k")
        r = b.search("python asyncio")
        check("brave returns 2 results", len(r) == 2)
        check("brave maps description->snippet", r[0]["snippet"] == "D1" and r[0]["url"] == "https://a")
        nokey = search.BraveSearch(api_key="")
        try:
            nokey.search("x")
            check("brave without key raises", False)
        except search.SearchError:
            check("brave without key raises", True)
    finally:
        search._http_get = orig

    print("HttpSearch (mocked HTTP)")
    body = json.dumps({"results": [{"name": "N1", "link": "https://c", "desc": "D3"}]}).encode()
    search._http_get = lambda url, headers=None, timeout=30: body
    try:
        h = search.HttpSearch("https://api.test/search?q={query}",
                              headers={"Authorization": "Bearer z"},
                              mapping={"title": "name", "url": "link", "snippet": "desc"})
        r = h.search("hello world")
        check("http maps custom fields", r[0]["title"] == "N1" and r[0]["url"] == "https://c" and r[0]["snippet"] == "D3")
        try:
            search.HttpSearch("https://no-placeholder.com")
            check("http requires {query}", False)
        except search.SearchError:
            check("http requires {query}", True)
    finally:
        search._http_get = orig

    print("CommandSearch (fake command)")
    # a tiny python 'search tool' that echoes a JSON list
    script = os.path.join(tempfile.mkdtemp(), "fake_search.py")
    with open(script, "w", encoding="utf-8") as f:
        f.write("import sys, json\n"
                "q = sys.argv[1] if len(sys.argv) > 1 else ''\n"
                "print(json.dumps([{'title': 'C1', 'url': 'https://d', 'snippet': 'got ' + q}]))\n")
    c = search.CommandSearch(f"{sys.executable} {script} {{query}}")
    r = c.search("asyncio")
    check("command returns a result", len(r) == 1 and r[0]["title"] == "C1")
    check("command substituted the query", "got asyncio" in r[0]["snippet"])
    bad = search.CommandSearch(f"{sys.executable} {script} {{query}} --badflag")
    # the fake script ignores extra args, so this still works; test a truly failing one
    failcmd = search.CommandSearch(f"{sys.executable} -c \"import sys; sys.exit(3)\" {{query}}")
    try:
        failcmd.search("x")
        check("command failure raises", False)
    except search.SearchError:
        check("command failure raises", True)

    print("make_search_provider factory")
    check("factory brave", isinstance(search.make_search_provider({"search_provider": "brave", "search_api_key": "k"}), search.BraveSearch))
    check("factory http", isinstance(search.make_search_provider({"search_provider": "http", "search_url": "u?q={query}"}), search.HttpSearch))
    check("factory command", isinstance(search.make_search_provider({"search_provider": "command", "search_command": "x {query}"}), search.CommandSearch))
    check("factory none when empty", search.make_search_provider({}) is None)
    check("factory none when brave has no key", search.make_search_provider({"search_provider": "brave"}) is None)
    check("factory none for unknown", search.make_search_provider({"search_provider": "bogus"}) is None)

    print()
    if failures:
        print(f"{failures} SEARCH TEST(S) FAILED")
        sys.exit(1)
    print("ALL SEARCH TESTS PASSED")


if __name__ == "__main__":
    main()

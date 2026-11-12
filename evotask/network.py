from __future__ import annotations

import html.parser
import ipaddress
import json
import os
import socket
import urllib.error
import urllib.parse
import urllib.request

from .config import Config


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("HTTP redirects are disabled; request the explicit final URL")


def public_url(url: str) -> None:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Only public HTTPS URLs without credentials are supported")
    if parsed.port not in {None, 443}:
        raise ValueError("Only HTTPS port 443 is supported")
    for result in socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM):
        address = ipaddress.ip_address(result[4][0])
        if not address.is_global:
            raise ValueError("Private, local or reserved network addresses are not allowed")


def get(url: str, timeout: int, limit: int, headers: dict | None = None) -> bytes:
    public_url(url)
    request = urllib.request.Request(url, headers={"User-Agent": "EvoTask/0.1 (course prototype)", **(headers or {})})
    with urllib.request.build_opener(NoRedirect()).open(request, timeout=timeout) as response:
        data = response.read(limit + 1)
    if len(data) > limit:
        raise ValueError("HTTP response exceeded size limit")
    return data


class PlainHTML(html.parser.HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "noscript"}:
            self.hidden += 1

    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript"} and self.hidden:
            self.hidden -= 1

    def handle_data(self, data):
        if not self.hidden and data.strip():
            self.parts.append(data.strip())


def search_web(query: str, config: Config) -> dict:
    if not isinstance(query, str) or not 1 <= len(query.strip()) <= 500:
        raise ValueError("Search query must contain 1–500 characters")
    if config.search_provider == "brave":
        key = os.environ.get(config.search_key_env)
        if not key:
            raise ValueError(f"Configure {config.search_key_env} for Brave search")
        url = "https://api.search.brave.com/res/v1/web/search?" + urllib.parse.urlencode({"q": query, "count": 5})
        body = json.loads(get(url, config.tool_timeout, 1_000_000, {"X-Subscription-Token": key}))
        results = [{"title": r["title"], "url": r["url"], "snippet": r.get("description", "")}
                   for r in body.get("web", {}).get("results", [])[:5]]
    else:
        url = "https://en.wikipedia.org/w/api.php?" + urllib.parse.urlencode({
            "action": "query", "list": "search", "srsearch": query, "srlimit": 5, "format": "json"})
        body = json.loads(get(url, config.tool_timeout, 1_000_000))
        results = [{"title": r["title"], "url": f"https://en.wikipedia.org/?curid={r['pageid']}",
                    "snippet": r.get("snippet", "")} for r in body.get("query", {}).get("search", [])]
    return {"provider": config.search_provider, "results": results,
            "scope": "encyclopedia only" if config.search_provider == "wikipedia" else "web"}


def fetch_text(url: str, config: Config) -> dict:
    if urllib.parse.urlparse(url).path.lower().endswith((".pdf", ".doc", ".docx", ".ppt", ".pptx")):
        raise ValueError("PDF/Office document processing is outside the project scope")
    body = get(url, config.tool_timeout, 500_000).decode("utf-8", errors="replace")
    parser = PlainHTML()
    parser.feed(body)
    return {"url": url, "text": "\n".join(parser.parts)[:12000], "untrusted_source": True}

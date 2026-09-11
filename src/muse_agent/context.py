"""Bounded context ingestion; source content is data, never agent instructions."""
import hashlib
from html.parser import HTMLParser
import ipaddress
import os
from pathlib import Path
import socket
import urllib.parse
import urllib.request

MAX_BYTES = 1_000_000
MAX_CONTEXT = 48000
EXTENSIONS = {".md", ".txt", ".rst", ".json", ".toml", ".py", ".js", ".ts", ".tsx", ".c", ".h", ".yaml", ".yml", ".html", ".css"}
SKIP = {".git", ".codex", ".claude", ".agents", ".venv", "venv", "node_modules", "vendor", "dist", "build", "__pycache__", "runs", "output", ".tools"}


class VisibleText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.hidden, self.parts = 0, []

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript", "template"):
            self.hidden += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript", "template"):
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, data):
        if not self.hidden and data.strip():
            self.parts.append(data.strip())


def visible(text):
    parser = VisibleText()
    parser.feed(text)
    return "\n".join(parser.parts)


def validate_url(url):
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password:
        raise ValueError("Use a public HTTP(S) URL without credentials")
    if parts.port not in (None, 80, 443):
        raise ValueError("Only standard HTTP(S) ports are supported")
    addresses = socket.getaddrinfo(parts.hostname, parts.port or (443 if parts.scheme == "https" else 80), type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
        raise ValueError("Web sources must resolve to public internet addresses")


class PublicRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def webpage(url):
    validate_url(url)
    request = urllib.request.Request(url, headers={"User-Agent": "muse-agent/0.1 (composition context)", "Accept": "text/html,text/plain"})
    opener = urllib.request.build_opener(PublicRedirect())
    with opener.open(request, timeout=20) as response:
        kind = response.headers.get_content_type()
        if kind not in ("text/html", "text/plain", "application/xhtml+xml"):
            raise ValueError(f"Unsupported webpage content type: {kind}")
        data = response.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            raise ValueError("Webpage exceeds 1 MB limit")
        text = data.decode(response.headers.get_content_charset() or "utf-8", "replace")
        return visible(text) if "html" in kind else text


def read_text(path, limit=MAX_BYTES):
    with path.open("rb") as handle:
        data = handle.read(limit + 1)
    if b"\0" in data:
        raise ValueError(f"Not a text source: {path.name}")
    return data[:limit].decode("utf-8", "replace"), len(data) > limit


def source(value):
    if value.startswith(("https://", "http://")):
        text = webpage(value)
        return {"kind": "webpage", "source": value, "text": text[:MAX_CONTEXT], "truncated": len(text) > MAX_CONTEXT}
    path = Path(value).expanduser().resolve()
    if not path.exists():
        raise ValueError(f"Source does not exist: {value}; put free-text ideas in the prompt")
    if path.is_file():
        text, truncated = read_text(path, MAX_CONTEXT)
        if path.suffix.lower() in (".html", ".htm"):
            text = visible(text)
        return {"kind": "file", "source": str(path), "text": text, "truncated": truncated}
    candidates = []
    visited = 0
    for directory, dirs, files in os.walk(path, followlinks=False):
        visited += 1
        dirs[:] = sorted(d for d in dirs if d not in SKIP and not d.startswith(".") and not (Path(directory) / d).is_symlink())
        for name in sorted(files):
            p = Path(directory) / name
            if p.is_symlink() or name.startswith(".") or p.suffix.lower() not in EXTENSIONS:
                continue
            if any(word in name.lower() for word in ("secret", "credential", "lock", ".min.", "private_key")):
                continue
            relative = p.relative_to(path).as_posix()
            priority = (0 if name.lower().startswith("readme") else 1 if p.suffix.lower() == ".md" else 2)
            candidates.append((priority, relative, p))
        if visited >= 500 or len(candidates) >= 2000:
            break
    chunks, included, remaining = [], [], MAX_CONTEXT
    for _, relative, p in sorted(candidates)[:30]:
        text, truncated = read_text(p, min(7000, remaining))
        header = f"\n--- {relative} ---\n"
        chunks.append(header + text)
        included.append({"path": relative, "truncated": truncated})
        remaining -= len(header) + len(text)
        if remaining < 500:
            break
    return {"kind": "repository", "source": str(path), "files": included,
            "text": "".join(chunks)[:MAX_CONTEXT], "truncated": len(included) < len(candidates) or any(f["truncated"] for f in included)}


def collect(idea, sources):
    if len(sources) > 8:
        raise ValueError("At most eight context sources per request")
    items = [source(s) for s in sources]
    for item in items:
        item["sha256"] = hashlib.sha256(item["text"].encode()).hexdigest()
    if not idea.strip() and not items:
        raise ValueError("Provide an idea or at least one --source")
    return {"idea": idea, "sources": items}

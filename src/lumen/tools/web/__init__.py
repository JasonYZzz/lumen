"""Bounded web access tools: ``web_fetch`` and config-gated ``web_search``.

Both tools are declared ``Risk.EXTERNAL`` (the operator approves an outbound
network action) with ``EffectKind.OBSERVE`` for effect tracking. They currently
use the default exclusive concurrency policy; read risk/effect does not itself
grant parallel execution. SSRF hardening: only public http(s)
hosts are reachable, redirects are re-validated per hop, and responses are
size-capped before parsing.
"""

from __future__ import annotations

from lumen.tools.web.download import build_download_file_spec
from lumen.tools.web.fetch import build_web_fetch_spec
from lumen.tools.web.http import is_public_host, validate_public_url
from lumen.tools.web.models import SearchResultItem, WebFetchResult, WebSearchResult
from lumen.tools.web.search import MAX_CONTENT_CHARS, build_web_search_spec

__all__ = [
    "MAX_CONTENT_CHARS",
    "SearchResultItem",
    "WebFetchResult",
    "WebSearchResult",
    "build_download_file_spec",
    "build_web_fetch_spec",
    "build_web_search_spec",
    "is_public_host",
    "validate_public_url",
]

"""HTML body extraction: Trafilatura fast path with a stdlib fallback.

The fast path uses the optional ``web`` extra (Trafilatura) to extract article
body text as Markdown with document metadata. Pages that yield no usable body
(JS shells, thin pages, extraction errors) fall back to the dependency-free
:class:`_TextExtractor`, which is never worse than the pre-upgrade behavior.
"""

from __future__ import annotations

from dataclasses import dataclass
from html.parser import HTMLParser
from importlib import import_module
from types import ModuleType
from urllib.parse import urljoin, urlparse

from lumen.tools.web.models import FetchStrategy

_BLOCK_TAGS = frozenset(
    {"p", "div", "br", "li", "tr", "section", "article", "h1", "h2", "h3", "h4", "h5", "h6"}
)
_SKIP_TAGS = frozenset({"script", "style", "noscript", "template"})

#: Below this many characters an extracted "article" is treated as a JS shell
#: or thin page and the stdlib fallback extractor takes over instead.
MIN_EXTRACT_CHARS = 200


class _TextExtractor(HTMLParser):
    """Convert HTML to readable plain text without third-party dependencies."""

    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self._base_url = base_url
        self._chunks: list[str] = []
        self._skip_depth = 0
        self._links: list[tuple[str, int]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
        elif tag in _BLOCK_TAGS:
            self._chunks.append("\n")
        if tag == "a" and not self._skip_depth:
            href = next((value for name, value in attrs if name == "href" and value), None)
            if href:
                target = urljoin(self._base_url, href)
                if urlparse(target).scheme in {"http", "https"}:
                    self._links.append((target, len(self._chunks)))

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1
        elif tag == "a" and self._links and not self._skip_depth:
            target, start = self._links.pop()
            label = "".join(self._chunks[start:]).strip()
            if label and target not in label:
                self._chunks.append(f" ({target})")

    def handle_data(self, data: str) -> None:
        if not self._skip_depth and data.strip():
            self._chunks.append(data)

    def text(self) -> str:
        joined = "".join(self._chunks)
        lines = [line.strip() for line in joined.splitlines()]
        return "\n".join(line for line in lines if line)


@dataclass(frozen=True, slots=True)
class ExtractedPage:
    """Body text plus the observability fields of the fetch strategy chain."""

    text: str
    fetch_strategy: FetchStrategy
    fallback_reason: str | None = None
    title: str | None = None
    author: str | None = None
    date: str | None = None
    sitename: str | None = None


# Trafilatura is an optional dependency (the ``web`` extra); it is imported
# lazily on first use so importing this module stays cheap and always works.
_trafilatura: ModuleType | None = None
_trafilatura_checked = False


def _load_trafilatura() -> ModuleType | None:
    global _trafilatura, _trafilatura_checked
    if not _trafilatura_checked:
        _trafilatura_checked = True
        try:
            _trafilatura = import_module("trafilatura")
        except ImportError:
            pass
    return _trafilatura


def extract_page_text(html: str, *, url: str) -> ExtractedPage:
    """Extract readable body text from HTML, fast path first.

    Falls back to the stdlib extractor when Trafilatura is unavailable, fails,
    or returns a body below :data:`MIN_EXTRACT_CHARS`; the strategy and reason
    stay observable on the returned page.
    """

    fast, reason = _fast_extract(html, url)
    if fast is not None:
        return fast
    extractor = _TextExtractor(url)
    extractor.feed(html)
    text = extractor.text()
    # A page with thin static text that ships script bundles is a JS shell:
    # the browser tier (when enabled) is the right next step, and the reason
    # tells the model so.
    if len(text.strip()) < MIN_EXTRACT_CHARS and "<script" in html.lower():
        reason = "js_shell"
    return ExtractedPage(
        text,
        fetch_strategy="text_fallback",
        fallback_reason=reason,
    )


def _fast_extract(html: str, url: str) -> tuple[ExtractedPage | None, str]:
    """Try the Trafilatura fast path; on failure return the fallback reason."""

    trafilatura = _load_trafilatura()
    if trafilatura is None:
        return None, "extract_failed"
    metadata: dict[str, str | None] = {}
    try:
        text = trafilatura.extract(  # type: ignore[attr-defined]
            html, url=url, output_format="markdown", include_links=True, deduplicate=True
        )
        document = trafilatura.bare_extraction(  # type: ignore[attr-defined]
            html, url=url, with_metadata=True
        )
        if document is not None:
            metadata = {
                key: getattr(document, key, None) or None
                for key in ("title", "author", "date", "sitename")
            }
    except Exception:  # Trafilatura parses hostile markup; any failure degrades
        return None, "extract_failed"
    if text is None:
        return None, "extract_failed"
    if len(text.strip()) < MIN_EXTRACT_CHARS:
        return None, "thin_content"
    return ExtractedPage(
        text,
        fetch_strategy="fast",
        title=metadata.get("title"),
        author=metadata.get("author"),
        date=metadata.get("date"),
        sitename=metadata.get("sitename"),
    ), ""

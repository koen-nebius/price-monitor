"""
Convert the HTML+ dialect our renderers emit into Confluence storage format.

diff.py and capacity/render.py were written for the Atlassian MCP
updateConfluencePage tool, whose "HTML+" accepts nodes such as
<span data-type="status" data-color="red">, <div data-type="panel-warning">
and <div data-type="expand" data-title="...">. The Confluence REST API
(body.storage, representation "storage") does NOT understand those: it strips
the data-* attributes and leaves plain text. Measured 2026-09-07: the live
capacity page, published via REST since 2026-08-12, had 0 of its 410 status
lozenges on read-back, while the MCP-published pricing page kept all 369.

to_storage() rewrites exactly those nodes into the equivalent storage-format
macros and leaves everything else untouched (tables incl. data-layout,
headings, em/strong, links). Named HTML entities other than the five XML ones
are turned into numeric references so the body stays well-formed XML.
"""
import re
from html.entities import name2codepoint

_COLOUR = {
    "grey": "Grey", "gray": "Grey", "neutral": "Grey",
    "red": "Red", "yellow": "Yellow", "green": "Green",
    "blue": "Blue", "purple": "Purple",
}
# Legacy macro names render as the modern panel colours: info=blue, tip=green,
# note=yellow (HTML+ "panel-warning"), warning=red (HTML+ "panel-error").
_PANEL = {
    "panel-info": "info", "panel-note": "info", "panel-success": "tip",
    "panel-warning": "note", "panel-error": "warning", "panel": "info",
}
_XML_ENTITIES = {"amp", "lt", "gt", "quot", "apos"}

_STATUS_RE = re.compile(r'<span\b([^>]*\bdata-type="status"[^>]*)>(.*?)</span>', re.S | re.I)
_BLOCK_OPEN_RE = re.compile(r'<div\b([^>]*\bdata-type="(panel(?:-[a-z]+)?|expand)"[^>]*)>', re.I)
_DIV_TAG_RE = re.compile(r'<div\b[^>]*>|</div\s*>', re.I)
_TAG_RE = re.compile(r'<[^>]+>')
_ENTITY_RE = re.compile(r'&([A-Za-z][A-Za-z0-9]*);')
_BARE_LT_RE = re.compile(r'<(?![A-Za-z/!?])')                      # '<' not starting a tag
_BARE_AMP_RE = re.compile(r'&(?![A-Za-z][A-Za-z0-9]*;|#\d+;|#x[0-9a-fA-F]+;)')  # '&' not an entity


def _attr(attrs: str, name: str) -> str:
    m = re.search(rf'\b{name}="([^"]*)"', attrs)
    return m.group(1) if m else ""


def _status(m: re.Match) -> str:
    colour = _COLOUR.get(_attr(m.group(1), "data-color").lower(), "Grey")
    title = _TAG_RE.sub("", m.group(2)).strip()
    return (
        '<ac:structured-macro ac:name="status" ac:schema-version="1">'
        f'<ac:parameter ac:name="colour">{colour}</ac:parameter>'
        f'<ac:parameter ac:name="title">{title}</ac:parameter>'
        '</ac:structured-macro>'
    )


def _convert_blocks(html: str) -> str:
    """Rewrite <div data-type="panel-*|expand"> ... </div> (nesting-aware)."""
    out, i = [], 0
    while True:
        m = _BLOCK_OPEN_RE.search(html, i)
        if not m:
            out.append(html[i:])
            return "".join(out)
        out.append(html[i:m.start()])
        depth, j, close = 1, m.end(), None
        while depth:
            t = _DIV_TAG_RE.search(html, j)
            if not t:
                raise ValueError("unbalanced <div> inside data-type block")
            depth += 1 if t.group(0).lower().startswith("<div") else -1
            j = t.end()
            close = t
        inner = _convert_blocks(html[m.end():close.start()])
        attrs, kind = m.group(1), m.group(2).lower()
        if kind == "expand":
            title = _attr(attrs, "data-title")
            param = f'<ac:parameter ac:name="title">{title}</ac:parameter>' if title else ""
            out.append(
                '<ac:structured-macro ac:name="expand" ac:schema-version="1">'
                f'{param}<ac:rich-text-body>{inner}</ac:rich-text-body></ac:structured-macro>'
            )
        else:
            out.append(
                f'<ac:structured-macro ac:name="{_PANEL.get(kind, "info")}" ac:schema-version="1">'
                f'<ac:rich-text-body>{inner}</ac:rich-text-body></ac:structured-macro>'
            )
        i = j


def _numeric_entities(html: str) -> str:
    def rep(m: re.Match) -> str:
        name = m.group(1)
        if name in _XML_ENTITIES:
            return m.group(0)
        cp = name2codepoint.get(name)
        return f"&#{cp};" if cp else m.group(0)
    return _ENTITY_RE.sub(rep, html)


def to_storage(html: str) -> str:
    """HTML+ (Atlassian MCP dialect) -> Confluence storage format."""
    html = _STATUS_RE.sub(_status, html)
    html = _convert_blocks(html)
    html = re.sub(r"<(br|hr)\s*>", r"<\1/>", html, flags=re.I)
    # Defensive XML hygiene: the renderers occasionally emit a bare "<" in text
    # (e.g. the reserve-wins term bucket "short<=8mo") or a bare "&". The MCP's
    # HTML parser tolerated that; the REST storage parser is XML and would 400.
    html = _BARE_LT_RE.sub("&lt;", html)
    html = _BARE_AMP_RE.sub("&amp;", html)
    return _numeric_entities(html)


def validate_xml(storage_html: str):
    """Return None if the body parses as XML (with ac:/ri: namespaces), else the error."""
    import xml.etree.ElementTree as ET
    wrapped = (
        '<root xmlns:ac="http://atlassian.com/content" '
        'xmlns:ri="http://atlassian.com/resource/identifier">'
        f"{storage_html}</root>"
    )
    try:
        ET.fromstring(wrapped)
        return None
    except ET.ParseError as e:  # pragma: no cover
        return str(e)

"""Pure URL construction for local endpoints.

No network probing happens here; the scheme comes from user/builtin rules or
defaults to ``http``.
"""

from __future__ import annotations

from .models import AddressFamily, PortEntry, Protocol, SocketState

DEFAULT_SCHEME = "http"
ALLOWED_SCHEMES = ("http", "https")
WILDCARD_V4 = {"0.0.0.0", "", "*"}
WILDCARD_V6 = {"::", "", "*"}


def sanitize_scheme(scheme: str) -> str:
    """Return a scheme safe to hand to the OS URL handler.

    Only ``http`` and ``https`` are accepted; anything else (``file``,
    ``javascript``, empty, malformed) falls back to ``http``. This is the one
    place that guarantees no arbitrary scheme reaches ``os.startfile``.
    """
    candidate = (scheme or "").strip().rstrip(":").lower()
    if candidate in ALLOWED_SCHEMES:
        return candidate
    return DEFAULT_SCHEME


def _strip_scope(address: str) -> str:
    return address.split("%", 1)[0]


def host_for_url(address: str, family: AddressFamily) -> str:
    """Map a bind address to a browser-reachable host, bracketing IPv6."""
    address = (address or "").strip()
    if family == AddressFamily.IPV6:
        base = _strip_scope(address)
        if base in WILDCARD_V6:
            base = "::1"
        return f"[{base}]"
    if address in WILDCARD_V4:
        return "127.0.0.1"
    return address


def build_url(
    address: str,
    port: int,
    family: AddressFamily = AddressFamily.IPV4,
    scheme: str = DEFAULT_SCHEME,
) -> str:
    scheme = sanitize_scheme(scheme)
    host = host_for_url(address, family)
    return f"{scheme}://{host}:{int(port)}/"


def url_for_entry(entry: PortEntry) -> str:
    scheme = DEFAULT_SCHEME
    if entry.category is not None and entry.category.scheme:
        scheme = entry.category.scheme
    return build_url(entry.local_address, entry.port, entry.family, scheme)


def is_openable(entry: PortEntry) -> bool:
    """Only TCP **LISTEN** endpoints can be opened in a browser.

    Connected TCP rows (ESTABLISHED / TIME_WAIT / CLOSE_WAIT / anything else)
    and UDP rows have no server semantics to open. A category can additionally
    opt out with ``open_in_browser = false``.
    """
    if entry.protocol != Protocol.TCP:
        return False
    if entry.socket_state != SocketState.LISTEN:
        return False
    if entry.category is not None and not entry.category.open_in_browser:
        return False
    return True

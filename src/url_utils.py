"""Small URL helpers used for trusted-domain decisions."""

from __future__ import annotations

from urllib.parse import SplitResult, urlsplit


def host_matches_domain(host: str | None, domain: str) -> bool:
    """Return whether *host* is *domain* or one of its subdomains."""
    normalized_host = str(host or "").casefold().rstrip(".")
    normalized_domain = str(domain or "").casefold().strip().rstrip(".")
    if not normalized_host or not normalized_domain:
        return False
    return normalized_host == normalized_domain or normalized_host.endswith(f".{normalized_domain}")


def split_url(value: str, *, allow_bare_host: bool = False) -> SplitResult | None:
    """Parse a URL, optionally treating ``domain/path`` as a scheme-relative URL."""
    candidate = str(value or "").strip()
    if not candidate:
        return None
    if allow_bare_host and "://" not in candidate and not candidate.startswith(("/", "#", "?")):
        candidate = f"//{candidate}"
    try:
        return urlsplit(candidate)
    except ValueError:
        return None


def url_matches_domain(value: str, domain: str, *, allow_bare_host: bool = False) -> bool:
    """Return whether a parsed URL belongs to *domain* or a subdomain."""
    parsed = split_url(value, allow_bare_host=allow_bare_host)
    return parsed is not None and host_matches_domain(parsed.hostname, domain)


def path_for_domain(value: str, domain: str, *, allow_bare_host: bool = False) -> str | None:
    """Return a URL path only when the parsed host belongs to *domain*."""
    parsed = split_url(value, allow_bare_host=allow_bare_host)
    if parsed is None or not host_matches_domain(parsed.hostname, domain):
        return None
    return parsed.path

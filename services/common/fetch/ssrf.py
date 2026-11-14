"""SSRF guard for outbound scraper requests (Security doc §8).

`check_url` validates scheme, userinfo, port and host, resolves every A/AAAA record and
rejects the URL if ANY address is not globally routable. It returns the validated IP so
the caller connects to exactly that address (IP pinning: defeats DNS rebinding between
check and connect). Never bypass this for a test; patch `resolve` instead.
"""

import asyncio
import ipaddress
import re
import socket
from dataclasses import dataclass

import httpx2

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address

ALLOWED_SCHEMES = frozenset({"http", "https"})
ALLOWED_PORTS = frozenset({80, 443})
_HOSTNAME = re.compile(r"^[a-z0-9-]+(\.[a-z0-9-]+)*\.?$")

# The doc's list plus everything else that must never be reached; `is_global` covers
# most of it already, this list keeps the doc's ranges explicit and version-independent.
BLOCKED_NETWORKS = tuple(
    ipaddress.ip_network(n)
    for n in (
        "0.0.0.0/8",
        "10.0.0.0/8",
        "100.64.0.0/10",  # CGNAT
        "127.0.0.0/8",
        "169.254.0.0/16",  # link-local + cloud metadata (169.254.169.254)
        "172.16.0.0/12",
        "192.0.0.0/24",
        "192.168.0.0/16",
        "198.18.0.0/15",
        "224.0.0.0/4",  # multicast
        "240.0.0.0/4",  # reserved + broadcast
        "::/128",
        "::1/128",
        "fc00::/7",
        "fe80::/10",
        "fec0::/10",
        "ff00::/8",
    )
)
NAT64 = ipaddress.ip_network("64:ff9b::/96")


class BlockedURL(Exception):
    """The URL (or a redirect hop) is not allowed to be fetched."""


@dataclass(frozen=True)
class Target:
    url: httpx2.URL  # the logical URL (hostname kept, for Host header, SNI and redirects)
    ip: IPAddress  # the validated address to connect to

    def pinned_url(self) -> httpx2.URL:
        return self.url.copy_with(host=str(self.ip))


def _embedded_v4(ip: ipaddress.IPv6Address) -> ipaddress.IPv4Address | None:
    """The IPv4 address hidden in IPv4-mapped, 6to4, NAT64 or IPv4-compatible forms."""
    if ip.ipv4_mapped:
        return ip.ipv4_mapped
    if ip.sixtofour:
        return ip.sixtofour
    if ip in NAT64 or (int(ip) >> 32 == 0 and int(ip) > 1):  # 64:ff9b::/96 or ::a.b.c.d
        return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    return None


def is_blocked(ip: IPAddress) -> bool:
    if isinstance(ip, ipaddress.IPv6Address):
        ip = ip.ipv4_mapped or ipaddress.IPv6Address(ip.packed)  # drop any %scope
        v4 = _embedded_v4(ip) if isinstance(ip, ipaddress.IPv6Address) else None
        if v4 is not None and is_blocked(v4):
            return True
    return (
        not ip.is_global
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_unspecified
        or any(ip.version == n.version and ip in n for n in BLOCKED_NETWORKS)
    )


def _ip_literal(host: str) -> IPAddress | None:
    """Parse any IP spelling, including the ones browsers accept and `ipaddress` does not
    (decimal 2130706433, hex 0x7f.1, short 127.1) via inet_aton."""
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        pass
    try:
        return ipaddress.IPv4Address(socket.inet_aton(host))
    except OSError:
        return None


async def resolve(host: str, port: int) -> list[IPAddress]:
    """Every A/AAAA address for `host`. Tests patch this; nothing else does DNS."""
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [ipaddress.ip_address(str(info[4][0]).split("%")[0]) for info in infos]


async def check_url(
    url: str | httpx2.URL, *, allowed_ports: frozenset[int] = ALLOWED_PORTS
) -> Target:
    """Validate `url` and return the IP to pin; raise BlockedURL otherwise."""
    try:
        parsed = httpx2.URL(url)
    except httpx2.InvalidURL as e:  # e.g. octal 0177.0.0.1
        raise BlockedURL(f"invalid URL: {e}") from None
    if parsed.scheme not in ALLOWED_SCHEMES:
        raise BlockedURL(f"scheme not allowed: {parsed.scheme!r}")
    if parsed.userinfo:
        raise BlockedURL("credentials in URL not allowed")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if port not in allowed_ports:
        raise BlockedURL(f"port not allowed: {port}")
    host = parsed.host
    if not host:
        raise BlockedURL("missing host")

    literal = _ip_literal(host)
    if literal is not None:
        addresses = [literal]
    elif not _HOSTNAME.match(host):
        raise BlockedURL(f"invalid host: {host!r}")
    else:
        try:
            addresses = await resolve(host, port)
        except OSError as e:
            raise BlockedURL(f"cannot resolve {host!r}: {e}") from None
    if not addresses:
        raise BlockedURL(f"no addresses for {host!r}")
    bad = [a for a in addresses if is_blocked(a)]
    if bad:
        raise BlockedURL(f"{host!r} resolves to a non-public address: {bad[0]}")
    return Target(url=parsed, ip=addresses[0])

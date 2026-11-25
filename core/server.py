"""Run Uvicorn with exact proxy peers resolved before it accepts traffic."""
from __future__ import annotations

import ipaddress
import socket

from config import settings


def resolve_proxy_ips(value: str) -> list[str]:
    """Resolve configured Docker DNS peers; never trust arbitrary private networks."""
    peers: set[str] = set()
    for item in value.split(","):
        host = item.strip()
        if not host:
            continue
        if "*" in host or "/" in host:
            raise ValueError("FORWARDED_ALLOW_IPS requires exact proxy IPs or DNS names, not wildcards/networks")
        try:
            peers.add(str(ipaddress.ip_address(host)))
        except ValueError:
            try:
                addresses = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
            except OSError as exc:
                raise RuntimeError(f"Cannot resolve trusted proxy {host!r}; refusing ambiguous client-IP rate limits") from exc
            if not addresses:
                raise RuntimeError(f"Trusted proxy {host!r} has no addresses")
            peers.update(str(ipaddress.ip_address(address[4][0])) for address in addresses)
    return sorted(peers)


def run_server() -> None:
    import uvicorn

    uvicorn.run(
        "main:app", host=settings.HOST, port=settings.PORT,
        proxy_headers=True, forwarded_allow_ips=resolve_proxy_ips(settings.FORWARDED_ALLOW_IPS),
    )


if __name__ == "__main__":
    run_server()

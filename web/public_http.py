"""Restricted resolver transport. Public HTTP(S) only, including redirects.

Keep the hostname for HTTP Host and TLS SNI/certificate verification, but open
sockets only to the numeric sockaddr validated by this connection attempt.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3.connection import HTTPConnection, HTTPSConnection
from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool
from urllib3.exceptions import NewConnectionError


class DestinationBlocked(Exception):
    """A policy refusal, not a retryable socket/Requests I/O error.

    RequestException inherits OSError and urllib3 would wrap it as a connection
    failure, losing the resolver's immediate no-retry policy. Keep this error
    outside that hierarchy so it reaches the explicit policy handler intact.
    """


def _public(address):
    ip = ipaddress.ip_address(address)
    # Reject mapped IPv4 and transition ranges too; global classification alone
    # differs across Python releases and is insufficient for multicast.
    translation = (
        ipaddress.ip_network("64:ff9b::/96"),
        ipaddress.ip_network("64:ff9b:1::/48"),
    )
    # Conservative exclusions also cover ranges whose standard-library
    # classification changed between supported Python releases.
    if isinstance(ip, ipaddress.IPv4Address) and any(
        ip in ipaddress.ip_network(block)
        for block in ("192.0.0.0/24", "192.88.99.0/24")
    ):
        return False
    if isinstance(ip, ipaddress.IPv6Address) and any(
        ip in block
        for block in translation
        + (
            ipaddress.ip_network("2001::/23"),
            ipaddress.ip_network("3fff::/20"),
            ipaddress.ip_network("5f00::/16"),
        )
    ):
        return False
    return (
        ip.is_global
        and not ip.is_multicast
        and not ip.is_unspecified
        and not ip.is_reserved
        and not ip.is_loopback
        and not ip.is_link_local
        and not (
            isinstance(ip, ipaddress.IPv6Address)
            and (
                ip.ipv4_mapped is not None
                or ip.sixtofour is not None
                or ip.teredo is not None
            )
        )
    )


def validate_url(url):
    if (
        not isinstance(url, str)
        or any(ord(c) <= 32 or ord(c) == 127 for c in url)
        or "\\" in url
    ):
        raise DestinationBlocked("Invalid destination URL")
    try:
        parsed = urlsplit(url)
        if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname:
            raise ValueError("scheme or host")
        if (
            parsed.username is not None
            or parsed.password is not None
            or "%" in parsed.hostname
        ):
            raise ValueError("credentials or scoped host")
        port = (
            parsed.port
            if parsed.port is not None
            else (443 if parsed.scheme.lower() == "https" else 80)
        )
        if port not in (80, 443):
            raise ValueError("port")
        host = parsed.hostname
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            ip = None
        if ip is not None and not _public(str(ip)):
            raise ValueError("nonpublic literal")
        # Non-standard numeric forms (127.1, hex, decimal) are not accepted as
        # hostnames: their platform-dependent interpretation is unsafe.
        if ip is None and (
            host.replace(".", "").isdigit() or host.lower().startswith("0x")
        ):
            raise ValueError("ambiguous numeric host")
    except (ValueError, UnicodeError):
        raise DestinationBlocked("Blocked destination URL") from None
    return host, port


def _addresses(host, port):
    try:
        entries = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        if not entries:
            raise ValueError("empty DNS")
        if any(not _public(entry[4][0]) for entry in entries):
            raise ValueError("nonpublic DNS")
    except (OSError, ValueError):
        raise DestinationBlocked(
            "Destination did not resolve exclusively to public addresses"
        ) from None
    return entries


class _PublicConnection:
    def _new_conn(self):
        # Resolve once, reject a mixed public/private answer in full, then use
        # that exact sockaddr. Never let the HTTP library re-resolve the name.
        entries = _addresses(self._dns_host, self.port)
        last = None
        for family, socktype, proto, _, sockaddr in entries:
            sock = None
            try:
                sock = socket.socket(family, socktype, proto)
                if self.timeout is not socket._GLOBAL_DEFAULT_TIMEOUT:
                    sock.settimeout(self.timeout)
                for option in self.socket_options or []:
                    sock.setsockopt(*option)
                if self.source_address:
                    sock.bind(self.source_address)
                sock.connect(sockaddr)
                return sock
            except OSError as error:
                last = error
                if sock is not None:
                    sock.close()
        raise NewConnectionError(
            self, "Unable to connect to validated public destination"
        ) from last


class PublicHTTPConnection(_PublicConnection, HTTPConnection):
    pass


class PublicHTTPSConnection(_PublicConnection, HTTPSConnection):
    pass


class PublicHTTPPool(HTTPConnectionPool):
    ConnectionCls = PublicHTTPConnection


class PublicHTTPSPool(HTTPSConnectionPool):
    ConnectionCls = PublicHTTPSConnection


class PublicAdapter(HTTPAdapter):
    def init_poolmanager(self, *args, **kwargs):
        super().init_poolmanager(*args, **kwargs)
        # Per-manager copy, never mutate urllib3's global class map.
        self.poolmanager.pool_classes_by_scheme = {
            "http": PublicHTTPPool,
            "https": PublicHTTPSPool,
        }

    def send(self, request, **kwargs):
        validate_url(request.url)  # Requests calls send again for every redirect.
        if kwargs.get("proxies") or kwargs.get("verify") is False:
            raise DestinationBlocked(
                "Proxy or disabled certificate verification is not allowed"
            )
        return super().send(request, **kwargs)


class PublicSession(requests.Session):
    def rebuild_auth(self, prepared_request, response):
        super().rebuild_auth(prepared_request, response)
        old = urlsplit(response.request.url)
        new = urlsplit(prepared_request.url)
        if (old.scheme, old.hostname, old.port) != (new.scheme, new.hostname, new.port):
            # API-specific credentials are not covered by Requests' ordinary
            # Authorization stripping. Never forward them to another origin.
            safe = {"user-agent", "accept", "accept-encoding", "connection", "range"}
            for key in list(prepared_request.headers):
                if key.lower() not in safe:
                    del prepared_request.headers[key]


def get(url, **kwargs):
    validate_url(url)
    if any(k in kwargs for k in ("proxies", "auth", "cert", "verify", "stream")):
        raise DestinationBlocked("Unsupported transport override")
    with PublicSession() as session:
        session.trust_env = False  # no proxy variables, netrc or ambient CA changes
        session.max_redirects = 5
        session.mount("http://", PublicAdapter())
        session.mount("https://", PublicAdapter())
        return session.get(url, **kwargs)

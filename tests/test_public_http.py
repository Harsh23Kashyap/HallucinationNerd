import socket
import sys
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "web"))
import public_http as p


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/x",
        "http://10.1.1.1/",
        "http://172.16.1.1/",
        "http://192.168.1.1/",
        "http://169.254.169.254/",
        "http://0.0.0.0/",
        "http://[::1]/",
        "http://[fc00::1]/",
        "http://[fe80::1]/",
        "http://[::ffff:8.8.8.8]/",
        "http://224.0.0.1/",
        "http://[ff02::1]/",
        "http://127.1/",
        "http://2130706433/",
        "http://0x7f000001/",
        "file:///etc/passwd",
        "ftp://example.org/",
        "http://u:p@example.org/",
        "http://example.org:22/",
        "http://[fe80::1%25eth0]/",
        "http://example.org\\@127.0.0.1/",
        "http://example.org/\n",
        "http:///empty",
        "http://example.org:bad/",
    ],
)
def test_block_bad_url_without_socket(url):
    with patch.object(p.socket, "getaddrinfo") as dns, patch.object(
        p.socket, "socket"
    ) as sock:
        with pytest.raises(p.DestinationBlocked):
            p.get(url)
        dns.assert_not_called()
        sock.assert_not_called()


def answer(ip):
    return (
        socket.AF_INET6 if ":" in ip else socket.AF_INET,
        socket.SOCK_STREAM,
        socket.IPPROTO_TCP,
        "",
        (ip, 443, 0, 0) if ":" in ip else (ip, 443),
    )


@pytest.mark.parametrize(
    "ips",
    [
        ["127.0.0.1"],
        ["10.0.0.1"],
        ["::1"],
        ["169.254.169.254"],
        ["8.8.8.8", "192.168.1.1"],
        ["8.8.8.8", "::1"],
        [],
    ],
)
def test_dns_all_answers_fail_closed(ips):
    with patch.object(
        p.socket, "getaddrinfo", return_value=[answer(ip) for ip in ips]
    ), patch.object(p.socket, "socket") as sock:
        with pytest.raises(p.DestinationBlocked):
            p.PublicHTTPSConnection("source.example", 443, timeout=3)._new_conn()
        sock.assert_not_called()


def test_dns_failure():
    with patch.object(p.socket, "getaddrinfo", side_effect=socket.gaierror):
        with pytest.raises(p.DestinationBlocked):
            p._addresses("source.example", 443)


@pytest.mark.parametrize("cls", [p.PublicHTTPConnection, p.PublicHTTPSConnection])
def test_numeric_socket_pinning_preserves_host(cls):
    sock = Mock()
    with patch.object(
        p.socket,
        "getaddrinfo",
        side_effect=[[answer("8.8.8.8")], [answer("127.0.0.1")]],
    ) as dns, patch.object(p.socket, "socket", return_value=sock):
        conn = cls("source.example", 443, timeout=3)
        assert conn._new_conn() is sock
        assert conn.host == "source.example" and conn._dns_host == "source.example"
        sock.connect.assert_called_once_with(("8.8.8.8", 443))
        assert dns.call_count == 1
        with pytest.raises(p.DestinationBlocked):
            conn._new_conn()


def test_socket_failure_close_and_public_fallback():
    a, b = Mock(), Mock()
    a.connect.side_effect = OSError("fixture")
    with patch.object(
        p.socket, "getaddrinfo", return_value=[answer("8.8.8.8"), answer("1.1.1.1")]
    ), patch.object(p.socket, "socket", side_effect=[a, b]):
        assert p.PublicHTTPSConnection("example.org", 443, timeout=3)._new_conn() is b
        a.close.assert_called_once()
        b.connect.assert_called_once_with(("1.1.1.1", 443))


def test_redirect_target_checked_by_adapter_before_send():
    adapter = p.PublicAdapter()
    request = p.requests.Request("GET", "http://127.0.0.1/internal").prepare()
    with patch.object(p.HTTPAdapter, "send") as send:
        with pytest.raises(p.DestinationBlocked):
            adapter.send(request)
        send.assert_not_called()


@pytest.mark.parametrize(
    "override", [{"proxies": {"http": "http://proxy"}}, {"verify": False}]
)
def test_adapter_transport_override(override):
    adapter = p.PublicAdapter()
    req = p.requests.Request("GET", "https://example.org/").prepare()
    with patch.object(p.HTTPAdapter, "send") as send:
        with pytest.raises(p.DestinationBlocked):
            adapter.send(req, **override)
        send.assert_not_called()


def test_pool_configuration_is_local_and_tls_uses_original_host():
    from urllib3.poolmanager import pool_classes_by_scheme

    before = dict(pool_classes_by_scheme)
    adapter = p.PublicAdapter()
    assert pool_classes_by_scheme == before
    pool = adapter.poolmanager.connection_from_url("https://source.example/path")
    assert (
        pool.ConnectionCls is p.PublicHTTPSConnection and pool.host == "source.example"
    )
    conn = pool._new_conn()
    assert conn.host == "source.example" and conn.cert_reqs is not None


def test_session_disables_proxy_netrc_limits_redirects_and_closes():
    session = Mock()
    session.__enter__ = Mock(return_value=session)
    session.__exit__ = Mock()
    with patch.object(p, "PublicSession", return_value=session):
        assert p.get("https://example.org/", timeout=3) is session.get.return_value
        assert session.trust_env is False and session.max_redirects == 5
        assert session.mount.call_count == 2
        session.__exit__.assert_called_once()


@pytest.mark.parametrize("key", ["proxies", "auth", "cert", "verify", "stream"])
def test_get_reject_overrides(key):
    with pytest.raises(p.DestinationBlocked):
        p.get("https://example.org/", **{key: False})


def test_redirect_public_to_private_real_requests_dispatch():
    # Real Requests redirect loop, fake adapter lower send. No DNS/network.
    from requests import Response

    reqs = []

    def fake(self, request, **kwargs):
        reqs.append(request.url)
        response = Response()
        response.status_code = 302
        response.headers["Location"] = "http://169.254.169.254/latest/"
        response._content = b""
        response.url = request.url
        response.request = request
        return response

    with patch.object(p.HTTPAdapter, "send", fake):
        with pytest.raises(p.DestinationBlocked):
            p.get("https://example.org/")
    assert reqs == ["https://example.org/"]


def test_redirect_loop_is_bounded():
    from requests import Response

    calls = []

    def fake(self, request, **kwargs):
        calls.append(request.url)
        r = Response()
        r.status_code = 302
        r.headers["Location"] = "/again"
        r._content = b""
        r.url = request.url
        r.request = request
        return r

    with patch.object(p.HTTPAdapter, "send", fake):
        with pytest.raises(p.requests.TooManyRedirects):
            p.get("https://example.org/")
    assert len(calls) == 6


@pytest.mark.parametrize(
    "address",
    [
        "64:ff9b::a00:1",
        "64:ff9b:1::a00:1",
        "2002:0808:0808::1",
        "2001:0:4136:e378:8000:63bf:3fff:fdd2",
    ],
)
def test_transition_addresses_blocked(address):
    assert not p._public(address)


def test_private_source_through_actual_resolver_no_http_no_retry():
    import citation_resolver as cr

    with patch.object(p.requests.Session, "get") as get, patch.object(
        cr, "_rate_limit"
    ), patch.object(cr._time, "sleep") as sleep:
        assert (
            cr.fetch_source_content({"url": "http://169.254.169.254/latest/"}) is None
        )
        get.assert_not_called()
        sleep.assert_not_called()


def test_public_source_remains_extractable():
    import citation_resolver as cr

    response = Mock(
        status_code=200,
        text="<html><body>" + ("Fictional paper sentence. " * 30) + "</body></html>",
    )
    with patch.object(cr, "public_get", return_value=response) as get, patch.object(
        cr, "_rate_limit"
    ):
        assert "Fictional paper sentence." in cr.fetch_source_content(
            {"url": "https://example.org/paper"}
        )
        get.assert_called_once()


def test_default_https_connection_retains_certificate_hostname():
    # Exercise installed urllib3 HTTPS connect with fake sockets/TLS. No network.
    import ssl
    import urllib3.connection as uc
    from collections import namedtuple

    sock = Mock()
    conn = p.PublicHTTPSPool(
        "source.example", 443, cert_reqs="CERT_REQUIRED"
    )._new_conn()
    conn._new_conn = Mock(return_value=sock)
    if hasattr(uc, "_ssl_wrap_socket_and_match_hostname"):
        wrapped = namedtuple("TLSResult", "socket is_verified")(sock, True)
        with patch.object(
            uc, "_ssl_wrap_socket_and_match_hostname", return_value=wrapped
        ) as tls:
            conn.connect()
            kwargs = tls.call_args.kwargs
            assert kwargs["server_hostname"] == "source.example"
            assert kwargs["cert_reqs"] == "CERT_REQUIRED"
            assert kwargs["assert_hostname"] is None
    else:
        # urllib3 1.26 verifies the hostname after its TLS wrapper returns.
        sock.getpeercert.return_value = {"subjectAltName": (("DNS", "source.example"),)}
        with patch.object(
            uc, "ssl_wrap_socket", return_value=sock
        ) as tls, patch.object(uc, "_match_hostname") as match:
            conn.connect()
            kwargs = tls.call_args.kwargs
            assert kwargs["server_hostname"] == "source.example"
            assert kwargs["ssl_context"].verify_mode == ssl.CERT_REQUIRED
            match.assert_called_once_with(
                sock.getpeercert.return_value, "source.example"
            )
    assert conn.is_verified


def test_engine_url_fetch_cannot_bypass_resolver_guard(capsys):
    import verify_hallucinations as engine

    with patch.object(p.requests.Session, "get") as get:
        assert engine._fetch_url("http://127.0.0.1/internal") == ""
        get.assert_not_called()


def test_custom_backup_template_private_destination():
    import citation_resolver as cr

    with patch.object(p.requests.Session, "get") as get, patch.object(
        cr, "_rate_limit"
    ):
        content, source = cr.backup_search(
            "fictional claim", [], "http://10.0.0.1/search?q={query}"
        )
        assert not content
        get.assert_not_called()


def test_public_ipv6_exact_sockaddr():
    address = "2606:4700:4700::1111"
    sock = Mock()
    with patch.object(
        p.socket, "getaddrinfo", return_value=[answer(address)]
    ), patch.object(p.socket, "socket", return_value=sock):
        assert (
            p.PublicHTTPSConnection("source.example", 443, timeout=3)._new_conn()
            is sock
        )
        sock.connect.assert_called_once_with((address, 443, 0, 0))


def test_http_host_header_is_original_hostname():
    conn = p.PublicHTTPConnection("source.example", 80, timeout=3)
    conn._new_conn = Mock(return_value=Mock())
    conn.request("GET", "/paper")
    sent = b"".join(call.args[0] for call in conn.sock.sendall.call_args_list)
    assert b"Host: source.example\r\n" in sent


@pytest.mark.parametrize(
    "response_ip",
    [
        "100.64.0.1",
        "192.0.0.1",
        "198.18.0.1",
        "192.0.2.1",
        "198.51.100.1",
        "203.0.113.1",
        "240.0.0.1",
        "255.255.255.255",
    ],
)
def test_special_ipv4_ranges(response_ip):
    assert not p._public(response_ip)


def test_cross_origin_redirect_drops_api_credentials():
    from requests import Response

    sent = []

    def fake(self, request, **kwargs):
        sent.append(dict(request.headers))
        r = Response()
        r._content = b""
        r.url = request.url
        r.request = request
        r.status_code = 302 if len(sent) == 1 else 200
        if len(sent) == 1:
            r.headers["Location"] = "https://other.example/paper"
        return r

    with patch.object(p.HTTPAdapter, "send", fake):
        assert (
            p.get(
                "https://source.example/",
                headers={"X-API-Key": "fixture-not-a-secret", "Accept": "text/html"},
            ).status_code
            == 200
        )
    assert sent[0]["X-API-Key"] == "fixture-not-a-secret"
    assert "X-API-Key" not in sent[1] and sent[1]["Accept"] == "text/html"


def test_same_origin_redirect_retains_api_credentials():
    from requests import Response

    sent = []

    def fake(self, request, **kwargs):
        sent.append(dict(request.headers))
        r = Response()
        r._content = b""
        r.url = request.url
        r.request = request
        r.status_code = 302 if len(sent) == 1 else 200
        if len(sent) == 1:
            r.headers["Location"] = "/paper"
        return r

    with patch.object(p.HTTPAdapter, "send", fake):
        p.get("https://source.example/", headers={"X-API-Key": "fixture-not-a-secret"})
    assert sent[1]["X-API-Key"] == "fixture-not-a-secret"


@pytest.mark.parametrize("url", ["http://example.org:0/", "https://example.org:65535/"])
def test_explicit_disallowed_port(url):
    with patch.object(p.socket, "getaddrinfo") as dns:
        with pytest.raises(p.DestinationBlocked):
            p.get(url)
        dns.assert_not_called()


@pytest.mark.parametrize("address", ["192.0.0.8", "192.0.0.9", "192.88.99.1"])
def test_conservative_special_use_exclusions(address):
    assert not p._public(address)


@pytest.mark.parametrize("address", ["2001:20::1", "3fff::1", "5f00::1"])
def test_conservative_ipv6_special_use_exclusions(address):
    assert not p._public(address)


@pytest.mark.parametrize("scheme", ["http", "https"])
def test_full_requests_adapter_dns_socket_response_chain(scheme):
    import io
    import ssl
    import urllib3.connection as uc
    from collections import namedtuple
    from contextlib import ExitStack

    sock = Mock()
    sock.makefile.return_value = io.BytesIO(
        b"HTTP/1.1 200 OK\r\nContent-Length: 7\r\n\r\nfixture"
    )
    sock.getpeercert.return_value = {"subjectAltName": (("DNS", "source.example"),)}
    port = 443 if scheme == "https" else 80
    sockaddr = ("8.8.8.8", port)
    entry = (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", sockaddr)
    with ExitStack() as stack:
        dns = stack.enter_context(
            patch.object(p.socket, "getaddrinfo", return_value=[entry])
        )
        create = stack.enter_context(
            patch.object(p.socket, "socket", return_value=sock)
        )
        tls = None
        if scheme == "https":
            if hasattr(uc, "_ssl_wrap_socket_and_match_hostname"):
                wrapped = namedtuple("TLSResult", "socket is_verified")(sock, True)
                tls = stack.enter_context(
                    patch.object(
                        uc, "_ssl_wrap_socket_and_match_hostname", return_value=wrapped
                    )
                )
            else:
                tls = stack.enter_context(
                    patch.object(uc, "ssl_wrap_socket", return_value=sock)
                )
                match = stack.enter_context(patch.object(uc, "_match_hostname"))
        response = p.get(scheme + "://source.example/paper", timeout=3)
        assert response.status_code == 200 and response.text == "fixture"
        assert dns.call_count == 1 and create.call_count == 1
        sock.connect.assert_called_once_with(sockaddr)
        sent = b"".join(call.args[0] for call in sock.sendall.call_args_list)
        assert b"Host: source.example\r\n" in sent
        if tls:
            assert tls.call_args.kwargs["server_hostname"] == "source.example"
            context = tls.call_args.kwargs["ssl_context"]
            assert (
                context.verify_mode == ssl.CERT_REQUIRED
                if context is not None
                else tls.call_args.kwargs["cert_reqs"]
                in ("CERT_REQUIRED", ssl.CERT_REQUIRED)
            )
            if not hasattr(uc, "_ssl_wrap_socket_and_match_hostname"):
                match.assert_called_once_with(
                    sock.getpeercert.return_value, "source.example"
                )


def test_full_requests_redirect_to_private_dns_never_opens_second_socket():
    import io

    sock = Mock()
    sock.makefile.return_value = io.BytesIO(
        b"HTTP/1.1 302 Found\r\nLocation: http://internal.example/secret\r\nContent-Length: 0\r\n\r\n"
    )
    entries = [
        [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("8.8.8.8", 80))],
        [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("127.0.0.1", 80),
            )
        ],
    ]
    with patch.object(
        p.socket, "getaddrinfo", side_effect=entries
    ) as dns, patch.object(p.socket, "socket", return_value=sock) as create:
        with pytest.raises(p.DestinationBlocked):
            p.get("http://source.example/paper", timeout=3)
        assert dns.call_count == 2 and create.call_count == 1
        sock.connect.assert_called_once_with(("8.8.8.8", 80))


def test_actual_dns_policy_refusal_survives_requests_without_retry():
    import citation_resolver as cr

    with patch.object(
        p.socket, "getaddrinfo", return_value=[answer("127.0.0.1")]
    ) as dns, patch.object(p.socket, "socket") as create, patch.object(
        cr, "_rate_limit"
    ), patch.object(
        cr._time, "sleep"
    ) as sleep:
        assert (
            cr._get_with_backoff("https://internal.example/secret", timeout=3) is None
        )
        assert dns.call_count == 1
        create.assert_not_called()
        sleep.assert_not_called()

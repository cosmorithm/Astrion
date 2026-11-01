"""Bounded public HTTP transport with DNS pinning and redirect validation."""

from __future__ import annotations
import http.client
import ipaddress
import json
import socket
import ssl
import time
from urllib.parse import urlsplit, urljoin


class ServiceError(ValueError):
    def __init__(self, code, message, retry_after=None):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.retry_after = retry_after


class PublicHTTP:
    def __init__(self, timeout=15, max_bytes=20_000_000):
        self.timeout = timeout
        self.max_bytes = max_bytes

    def fetch(self, url, *, headers=None):
        started = time.monotonic()
        for _ in range(5):
            parts = urlsplit(url)
            if (
                parts.scheme not in {"http", "https"}
                or not parts.hostname
                or parts.username
                or parts.password
            ):
                raise ServiceError("invalid_url", "only public HTTP(S), without credentials")
            port = parts.port or (443 if parts.scheme == "https" else 80)
            if port not in {80, 443}:
                raise ServiceError("blocked_address", "only ports 80/443")
            try:
                addresses = socket.getaddrinfo(parts.hostname, port, type=socket.SOCK_STREAM)
            except OSError as exc:
                raise ServiceError("dns_failure", "host could not be resolved") from exc
            ips = {a[4][0] for a in addresses}
            if not ips or any(
                not ipaddress.ip_address(ip).is_global or ipaddress.ip_address(ip).is_multicast
                for ip in ips
            ):
                raise ServiceError(
                    "blocked_address", "private, loopback and special addresses are forbidden"
                )
            ip = sorted(ips)[0]
            remaining = self.timeout - (time.monotonic() - started)
            if remaining <= 0:
                raise ServiceError("timeout", "request deadline exceeded")
            connection = (
                http.client.HTTPSConnection(
                    parts.hostname, port, timeout=remaining, context=ssl.create_default_context()
                )
                if parts.scheme == "https"
                else http.client.HTTPConnection(parts.hostname, port, timeout=remaining)
            )
            # Pin the validated IP while preserving Host and TLS SNI.
            connection._create_connection = (
                lambda address, timeout, source_address=None: socket.create_connection(
                    (ip, port), timeout, source_address
                )
            )
            try:
                connection.request(
                    "GET",
                    (parts.path or "/") + ("?" + parts.query if parts.query else ""),
                    headers={
                        "User-Agent": "Astrion-Research/1.0",
                        "Accept-Encoding": "identity",
                        **(headers or {}),
                    },
                )
                response = connection.getresponse()
                status = response.status
                if status in {301, 302, 303, 307, 308}:
                    location = response.getheader("Location")
                    if not location:
                        raise ServiceError("invalid_redirect", "missing Location")
                    target = urljoin(url, location)
                    # Never forward an authorization header across origins.
                    if urlsplit(target).netloc != parts.netloc:
                        headers = {}
                    url = target
                    continue
                if status == 429:
                    raise ServiceError(
                        "rate_limited", "provider rate limit", response.getheader("Retry-After")
                    )
                if status >= 400:
                    raise ServiceError(
                        "access_denied" if status in {401, 403} else "http_error", f"HTTP {status}"
                    )
                body = bytearray()
                while True:
                    remaining = self.timeout - (time.monotonic() - started)
                    if remaining <= 0:
                        raise ServiceError("timeout", "request deadline exceeded")
                    if connection.sock:
                        connection.sock.settimeout(remaining)
                    chunk = response.read(min(65536, self.max_bytes + 1 - len(body)))
                    if not chunk:
                        break
                    body.extend(chunk)
                    if len(body) > self.max_bytes:
                        raise ServiceError(
                            "response_too_large", "body exceeds configured byte limit"
                        )
                return {
                    "url": url,
                    "status": status,
                    "content_type": response.getheader("Content-Type", "").split(";")[0].lower(),
                    "body": bytes(body),
                    "headers": dict(response.getheaders()),
                }
            except (TimeoutError, socket.timeout) as exc:
                raise ServiceError("timeout", "request deadline exceeded") from exc
            except (OSError, http.client.HTTPException) as exc:
                raise ServiceError("network_failure", type(exc).__name__) from exc
            finally:
                connection.close()
        raise ServiceError("redirect_limit", "too many redirects")

    def json(self, url, headers=None):
        for attempt in range(2):
            try:
                return json.loads(self.fetch(url, headers=headers)["body"])
            except ServiceError as exc:
                if exc.code != "rate_limited" or attempt:
                    raise
                try:
                    delay = float(exc.retry_after or "1")
                except ValueError:
                    raise exc
                if not 0 <= delay <= 1:
                    raise exc
                time.sleep(delay)
            except (ValueError, UnicodeError) as exc:
                raise ServiceError("invalid_response", "expected JSON") from exc

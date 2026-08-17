from __future__ import annotations

import ipaddress
import re
import socket
import unicodedata
from typing import Any, Callable
from urllib.parse import urlsplit


def is_public_ip_address(value: Any) -> bool:
    try:
        return ipaddress.ip_address(value).is_global
    except ValueError:
        return False


def is_valid_https_url(value: Any) -> bool:
    if not isinstance(value, str) or not value or any(
        character.isspace() or unicodedata.category(character).startswith("C")
        for character in value
    ):
        return False
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        return False
    host_valid = False
    if hostname:
        try:
            ipaddress.ip_address(hostname)
            host_valid = True
        except ValueError:
            try:
                ascii_hostname = hostname.encode("idna").decode("ascii").removesuffix(".")
            except UnicodeError:
                ascii_hostname = ""
            host_valid = bool(
                ascii_hostname
                and len(ascii_hostname) <= 253
                and all(
                    len(label) <= 63
                    and re.fullmatch(
                        r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?", label
                    )
                    for label in ascii_hostname.split(".")
                )
            )
    return bool(
        parsed.scheme == "https"
        and host_valid
        and parsed.netloc
        and port != 0
        and parsed.username is None
        and parsed.password is None
    )


def is_safe_public_https_url(
    value: Any,
    *,
    resolve_dns: bool = False,
    resolver: Callable[..., Any] = socket.getaddrinfo,
) -> bool:
    """Validate an HTTPS fetch target and optionally require public DNS answers."""
    if not is_valid_https_url(value):
        return False
    hostname = urlsplit(value).hostname
    if not hostname or hostname.casefold().rstrip(".") == "localhost":
        return False
    try:
        literal = ipaddress.ip_address(hostname)
    except ValueError:
        literal = None
    if literal is not None:
        return is_public_ip_address(str(literal))
    if not resolve_dns:
        return True
    try:
        answers = resolver(hostname, None, type=socket.SOCK_STREAM)
        addresses = {
            ipaddress.ip_address(answer[4][0])
            for answer in answers
            if isinstance(answer, tuple)
            and len(answer) >= 5
            and isinstance(answer[4], tuple)
            and answer[4]
        }
    except (OSError, TypeError, ValueError):
        return False
    return bool(addresses) and all(address.is_global for address in addresses)

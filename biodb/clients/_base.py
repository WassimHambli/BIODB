"""Shared client plumbing."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from typing import Any, TypeVar

from ..errors import ResponseValidationError, excerpt
from ..models._base import BioModel, parse_model
from ..transport import Params, Response, Transport

T = TypeVar("T", bound=BioModel)
R = TypeVar("R")

_HTML_RE = re.compile(r"<!doctype html|<html[\s>]|<head[\s>]|<body[\s>]", re.IGNORECASE)


def reject_html(resp: Response, service: str) -> None:
    """Raise ``ResponseValidationError`` if a 2xx body is an HTML page (a proxy or maintenance
    page served with status 200), for services whose payload is plain text."""
    head = resp.content[:1024].decode("utf-8", errors="replace").lstrip("\ufeff \t\r\n")
    if head.startswith("<") and _HTML_RE.search(head):
        raise ResponseValidationError(
            "expected a plain-text payload but received an HTML page",
            service=service,
            url=resp.url,
            status=resp.status,
            body_excerpt=excerpt(resp.content),
        )


def require_fasta(resp: Response, service: str) -> None:
    """Raise ``ResponseValidationError`` unless the (non-blank) body starts like FASTA."""
    if not resp.text.lstrip().startswith(">"):
        raise ResponseValidationError(
            "expected FASTA but the body does not start with '>'",
            service=service,
            url=resp.url,
            status=resp.status,
            body_excerpt=excerpt(resp.content),
        )


class BaseClient:
    """Base class: holds the shared transport and the service name."""

    service: str = ""
    base_url: str = ""

    def __init__(self, transport: Transport) -> None:
        self.transport = transport

    def _request(
        self,
        method: str,
        url: str,
        *,
        params: Params | None = None,
        json_body: Any = None,
        form: Params | None = None,
        headers: Mapping[str, str] | None = None,
        cache: bool = True,
        ttl: float | None = None,
        check: Callable[[Response], None] | None = None,
    ) -> Response:
        return self.transport.request(
            method,
            url,
            service=self.service,
            params=params,
            json_body=json_body,
            form=form,
            headers=headers,
            cache=cache,
            ttl=ttl,
            check=check,
        )

    def _call(
        self,
        method: str,
        url: str,
        parse: Callable[[Response], R],
        *,
        params: Params | None = None,
        json_body: Any = None,
        form: Params | None = None,
        headers: Mapping[str, str] | None = None,
        cache: bool = True,
        ttl: float | None = None,
    ) -> R:
        """Request ``url`` and return ``parse(response)``.

        ``parse`` is the transport's validation hook: it runs before a live body is cached and
        on every cached body (an invalid cached body is discarded and refetched once), so only
        bodies that parse are ever cached. Whatever it raises propagates unchanged."""
        box: list[R] = []

        def check(resp: Response) -> None:
            box.append(parse(resp))

        self._request(
            method,
            url,
            params=params,
            json_body=json_body,
            form=form,
            headers=headers,
            cache=cache,
            ttl=ttl,
            check=check,
        )
        return box[-1]

    def _parse(self, model: type[T], data: Any, url: str) -> T:
        return parse_model(model, data, service=self.service, url=url)

"""Errors exposed by the HTTP service."""

from __future__ import annotations

from typing import Mapping


class R1ServiceError(Exception):
    """Expected, safe-to-expose service failure at the HTTP boundary."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int = 400,
        code: int | None = None,
        headers: Mapping[str, str] | None = None,
    ):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = status_code if code is None else code
        self.headers = dict(headers or {})

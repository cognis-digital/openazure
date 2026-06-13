"""Common exception types for openazure services."""

from __future__ import annotations


class OpenAzureError(Exception):
    """Base error. Carries an HTTP status code and an Azure-style code."""

    http_status = 400
    code = "BadRequest"

    def __init__(self, message: str | None = None, *, code: str | None = None,
                 http_status: int | None = None):
        super().__init__(message or self.code)
        self.message = message or self.code
        if code is not None:
            self.code = code
        if http_status is not None:
            self.http_status = http_status


class NotFound(OpenAzureError):
    http_status = 404
    code = "ResourceNotFound"


class Conflict(OpenAzureError):
    http_status = 409
    code = "ResourceAlreadyExists"


class BadRequest(OpenAzureError):
    http_status = 400
    code = "BadRequest"

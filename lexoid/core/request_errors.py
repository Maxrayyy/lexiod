"""Classify service outages without exposing provider responses or credentials."""

from urllib.error import URLError


def request_status(error):
    return getattr(error, "status_code", None) or getattr(error, "code", None)


def is_request_failure_name(name):
    name = name.lower()
    return ("connection" in name or "timeout" in name or name in {
        "urlerror", "ratelimiterror", "internalservererror", "authenticationerror",
        "permissiondeniederror", "modelunavailableerror"})


def is_request_failure(error):
    status = request_status(error)
    return (isinstance(error, (ConnectionError, TimeoutError, URLError, ModelUnavailableError))
            or is_request_failure_name(type(error).__name__)
            or status in (401, 403, 408, 429)
            or isinstance(status, int) and status >= 500)


class ModelUnavailableError(RuntimeError):
    def __init__(self, page, error):
        self.page = page
        self.error_type = type(error).__name__
        self.status_code = request_status(error)
        super().__init__(f"Model service unavailable at page {page}: {self.error_type}; "
                         "progress retained, resume after service recovery")

"""Error types, exit codes and HTTP statuses shared by every part of easy-ui.

Every error a person or an agent can see is an ``EasyUIError``. It carries
three plain-language parts, so the message always says what happened, why it
matters and what to do next:

- ``message``: what happened and why it matters.
- ``hint``: what to do next (may be ``None``).
- ``code``: a short stable machine word, for example ``"not_found"``.

Each class has a fixed command-line exit code (``exit_code``) and a fixed HTTP
status (``http_status``), so the CLI and the server report the same error the
same way.

Public API
----------
``ExitCode``
    ``IntEnum``: ``OK=0``, ``ERROR=1``, ``USAGE=2``, ``NOT_FOUND=3``,
    ``NOT_ALLOWED=4``, ``REFUSED=5``, ``NEEDS_HUMAN=6``, ``TEMPORARY=75``.

``EasyUIError(message, *, hint=None, code=None, detail=None, problems=None, status=None)``
    Base class (exit 1, HTTP 500, code ``"error"``). Attributes: ``message``,
    ``hint``, ``code``, ``detail`` (optional technical text, never needed to
    act), ``problems`` (optional list of dicts, e.g. validation problems),
    ``exit_code``, ``http_status``. ``to_dict()`` returns
    ``{"error": {"code", "message", "hint"[, "detail"][, "problems"]}}``.

Subclasses (exit code, HTTP status, default code):

- ``UsageError``        2, 400, ``"usage"``: the request or command line was not understood.
- ``ParamError``        2, 400, ``"bad_param"``: a dashboard control value is not acceptable.
- ``NotFound``          3, 404, ``"not_found"``: the thing asked for does not exist.
- ``NoData``            3, 503, ``"no_data"``: no dashboard data has been built yet.
- ``NotAllowed``        4, 403, ``"not_allowed"``: the caller may not do this.
- ``SignInRequired``    4, 401, ``"sign_in_required"``: sign in first.
- ``Refused``           5, 403, ``"refused"``: refused for safety.
- ``QueryRefused``      5, 400, ``"query_refused"``: the sandbox refused a query.
- ``NeedsHuman``        6, 409, ``"needs_human"``: only the person can do the next step.
- ``TemporaryFailure`` 75, 503, ``"temporary"``: try again later.
- ``QueryTimeout``     75, 504, ``"query_timeout"``: a query ran past its time limit.
- ``ConfigError``       1, 500, ``"config_invalid"``: ``app.toml`` is wrong.
- ``SpecError``         1, 422, ``"spec_invalid"``: a dashboard file is wrong (see ``problems``).
- ``QueryError``        1, 400, ``"query_failed"``: a query could not run (bad SQL, missing column).
- ``DataBuildError``    1, 500, ``"data_build_failed"``: the data copy could not be built.
- ``NotAvailable``      1, 501, ``"not_available"``: that part is not installed in this copy.
"""

from __future__ import annotations

from enum import IntEnum
from typing import Any


class ExitCode(IntEnum):
    OK = 0
    ERROR = 1
    USAGE = 2
    NOT_FOUND = 3
    NOT_ALLOWED = 4
    REFUSED = 5
    NEEDS_HUMAN = 6
    TEMPORARY = 75


class EasyUIError(Exception):
    """An error with a plain-language message, a hint, an exit code and an HTTP status."""

    exit_code: int = ExitCode.ERROR
    http_status: int = 500
    default_code: str = "error"

    def __init__(
        self,
        message: str,
        *,
        hint: str | None = None,
        code: str | None = None,
        detail: str | None = None,
        problems: list[dict[str, Any]] | None = None,
        status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint
        self.code = code or self.default_code
        self.detail = detail
        self.problems = problems
        if status is not None:
            self.http_status = status

    def to_dict(self) -> dict[str, Any]:
        err: dict[str, Any] = {"code": self.code, "message": self.message, "hint": self.hint}
        if self.detail:
            err["detail"] = self.detail
        if self.problems:
            err["problems"] = self.problems
        return {"error": err}

    def __str__(self) -> str:
        return self.message if not self.hint else f"{self.message} {self.hint}"


class UsageError(EasyUIError):
    exit_code = ExitCode.USAGE
    http_status = 400
    default_code = "usage"


class ParamError(UsageError):
    default_code = "bad_param"


class NotFound(EasyUIError):
    exit_code = ExitCode.NOT_FOUND
    http_status = 404
    default_code = "not_found"


class NoData(NotFound):
    http_status = 503
    default_code = "no_data"


class NotAllowed(EasyUIError):
    exit_code = ExitCode.NOT_ALLOWED
    http_status = 403
    default_code = "not_allowed"


class SignInRequired(NotAllowed):
    http_status = 401
    default_code = "sign_in_required"


class Refused(EasyUIError):
    exit_code = ExitCode.REFUSED
    http_status = 403
    default_code = "refused"


class QueryRefused(Refused):
    http_status = 400
    default_code = "query_refused"


class NeedsHuman(EasyUIError):
    exit_code = ExitCode.NEEDS_HUMAN
    http_status = 409
    default_code = "needs_human"


class TemporaryFailure(EasyUIError):
    exit_code = ExitCode.TEMPORARY
    http_status = 503
    default_code = "temporary"


class QueryTimeout(TemporaryFailure):
    http_status = 504
    default_code = "query_timeout"


class ConfigError(EasyUIError):
    default_code = "config_invalid"


class SpecError(EasyUIError):
    http_status = 422
    default_code = "spec_invalid"


class QueryError(EasyUIError):
    http_status = 400
    default_code = "query_failed"


class DataBuildError(EasyUIError):
    default_code = "data_build_failed"


class NotAvailable(EasyUIError):
    http_status = 501
    default_code = "not_available"

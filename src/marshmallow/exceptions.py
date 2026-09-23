"""Exception classes for marshmallow-related errors."""

from __future__ import annotations

import dataclasses
import typing

# Key used for schema-level validation errors
SCHEMA = "_schema"

#: Code assigned to issues whose ValidationError did not carry an explicit
#: code. Custom/user-raised errors fall into this category; codes are never
#: reverse-engineered from message text.
USER_ERROR_CODE = "user_error"

#: Issue stage: raised while converting input data (field deserialization,
#: including required/null/type checks).
STAGE_DESERIALIZE = "deserialize"
#: Issue stage: raised by field-level validators (validate= callables and
#: marshmallow.validates methods).
STAGE_VALIDATE = "validate"
#: Issue stage: raised by schema-level validators
#: (marshmallow.validates_schema methods).
STAGE_SCHEMA_VALIDATE = "schema_validate"


class MarshmallowError(Exception):
    """Base class for all marshmallow-related errors."""


@dataclasses.dataclass(frozen=True)
class ValidationIssue:
    """A single, translation-independent validation issue.

    Unlike ValidationError.messages, which are human-readable and may be
    localized, an issue carries structured data that is stable across
    translations of the display messages.

    :param code: Stable machine-readable code, e.g. 'required', 'invalid'
        or 'range'. Errors raised without an explicit code are categorized
        as USER_ERROR_CODE.
    :param message: The human-readable display message (may be localized).
    :param params: Parameters associated with the issue, e.g. {'min': 1}
        for a Range failure.
    :param path: Input path stored by segment. Field names are strings and
        collection indices are ints, so index 0 and a field named '0' are
        distinguishable. An empty tuple refers to the input as a whole
        (schema-level errors). For Dict key validation failures, the path
        ends with the extra segment 'key'.
    :param stage: One of STAGE_DESERIALIZE, STAGE_VALIDATE or
        STAGE_SCHEMA_VALIDATE, or None if the error was raised outside of
        those phases (e.g. in a pre/post-load processor).
    """

    code: str
    message: str
    params: dict[str, typing.Any] = dataclasses.field(default_factory=dict)
    path: tuple[str | int, ...] = ()
    stage: str | None = None

    def __post_init__(self):
        object.__setattr__(self, "params", dict(self.params))
        object.__setattr__(self, "path", tuple(self.path))

    def _with_prefix(self, prefix: typing.Iterable[str | int]) -> ValidationIssue:
        """Return a copy of this issue with prefix prepended to its path."""
        return dataclasses.replace(self, path=tuple(prefix) + self.path)

    def _with_stage(self, stage: str) -> ValidationIssue:
        """Return a copy of this issue with the given stage."""
        return dataclasses.replace(self, stage=stage)


def _issues_from_messages(
    messages: typing.Any,
    *,
    code: str | None = None,
    params: dict[str, typing.Any] | None = None,
    stage: str | None = None,
    path: tuple[str | int, ...] = (),
) -> list[ValidationIssue]:
    """Build a list of ValidationIssue from a messages structure.

    Follows the same shapes as ValidationError.messages: a string, a list,
    or a (possibly nested) dict keyed by path segment.
    """
    resolved_code = code or USER_ERROR_CODE
    if isinstance(messages, (str, bytes)):
        return [
            ValidationIssue(
                code=resolved_code,
                message=str(messages),
                params=dict(params) if params else {},
                path=path,
                stage=stage,
            )
        ]
    if isinstance(messages, dict):
        issues: list[ValidationIssue] = []
        for key, value in messages.items():
            issues.extend(
                _issues_from_messages(
                    value,
                    code=code,
                    params=params,
                    stage=stage,
                    path=(*path, key),
                )
            )
        return issues
    if isinstance(messages, (list, tuple)):
        issues = []
        for item in messages:
            issues.extend(
                _issues_from_messages(
                    item, code=code, params=params, stage=stage, path=path
                )
            )
        return issues
    return [
        ValidationIssue(
            code=resolved_code,
            message=str(messages),
            params=dict(params) if params else {},
            path=path,
            stage=stage,
        )
    ]


class ValidationError(MarshmallowError):
    """Raised when validation fails on a field or schema.

    Validators and custom fields should raise this exception.

    :param message: An error message, list of error messages, or dict of
        error messages. If a dict, the keys are subitems and the values are error messages.
    :param field_name: Field name to store the error on.
    :param data: Raw input data.
    :param valid_data: Valid (de)serialized data.
    :param code: Optional stable, machine-readable code for the error. The
        code is stored on the error's issues and is independent of the
        (possibly localized) message text. Errors raised without a code are
        categorized as USER_ERROR_CODE.
    :param params: Optional parameters associated with the error, stored on
        the error's issues.
    :param issues: Optional explicit list of ValidationIssue. When omitted,
        issues are derived from message, code and params.

    .. versionadded:: 4.4.0
        The code, params and issues parameters.
    """

    def __init__(
        self,
        message: str | list | dict,
        field_name: str = SCHEMA,
        data: typing.Mapping[str, typing.Any]
        | typing.Iterable[typing.Mapping[str, typing.Any]]
        | None = None,
        valid_data: list[typing.Any] | dict[str, typing.Any] | None = None,
        *,
        code: str | None = None,
        params: dict[str, typing.Any] | None = None,
        issues: typing.Iterable[ValidationIssue] | None = None,
        **kwargs,
    ):
        self.messages = [message] if isinstance(message, (str, bytes)) else message
        self.field_name = field_name
        self.data = data
        self.valid_data = valid_data
        self.kwargs = kwargs
        if issues is not None:
            #: Structured, translation-independent issues for this error.
            self.issues = list(issues)
        else:
            self.issues = _issues_from_messages(
                self.messages, code=code, params=params
            )
        super().__init__(message)

    def _fill_stage(self, stage: str) -> None:
        """Set stage on all issues that do not have a stage yet."""
        self.issues = [
            issue if issue.stage is not None else issue._with_stage(stage)
            for issue in self.issues
        ]

    def normalized_messages(self):
        if self.field_name == SCHEMA and isinstance(self.messages, dict):
            return self.messages
        return {self.field_name: self.messages}

    @property
    def messages_dict(self) -> dict[str, typing.Any]:
        if not isinstance(self.messages, dict):
            raise TypeError(
                "cannot access 'messages_dict' when 'messages' is of type "
                + type(self.messages).__name__
            )
        return self.messages


class RegistryError(NameError):
    """Raised when an invalid operation is performed on the serializer
    class registry.
    """


class StringNotCollectionError(MarshmallowError, TypeError):
    """Raised when a string is passed when a list of strings is expected."""


class _FieldInstanceResolutionError(MarshmallowError, TypeError):
    """Raised when an argument is passed to a field class that cannot be resolved to a Field instance."""

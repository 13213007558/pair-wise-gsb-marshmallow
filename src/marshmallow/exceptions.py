"""Exception classes for marshmallow-related errors."""

from __future__ import annotations

import dataclasses
import typing

# Key used for schema-level validation errors
SCHEMA = "_schema"

# Code used for validation errors raised without an explicit code
USER_ERROR = "user_error"


class IssueStage:
    """Stages of the load pipeline at which a validation issue can occur."""

    #: Deserializing/converting an input value failed
    FIELD_CONVERSION = "field_conversion"
    #: A field-level validator rejected the value
    FIELD_VALIDATION = "field_validation"
    #: A schema-level check (e.g. validates_schema, unknown fields) failed
    SCHEMA_VALIDATION = "schema_validation"


@dataclasses.dataclass(frozen=True)
class Issue:
    """A single, translation-independent validation issue.

    Unlike human-readable messages, an issue carries a stable ``code``,
    structured ``params``, a ``path`` of input segments (strings for mapping
    keys, integers for collection indices), and the ``stage`` of the load
    pipeline at which it was raised. Issues are never merged: multiple
    problems at the same path result in multiple issues.
    """

    #: Stable, translation-independent error code, e.g. "required".
    #: Errors raised without an explicit code use ``USER_ERROR``.
    code: str
    #: Human-readable (possibly translated) message for display.
    message: str
    #: Structured parameters used to build the message.
    params: dict[str, typing.Any] = dataclasses.field(default_factory=dict)
    #: Input path segments. Integer segments are collection indices and are
    #: always distinguishable from string field names such as "0".
    path: tuple[str | int, ...] = ()
    #: Stage of the load pipeline, see :class:`IssueStage`.
    stage: str | None = None

    def with_path_prefix(self, prefix: tuple[str | int, ...]) -> Issue:
        """Return a copy of this issue with ``prefix`` prepended to its path."""
        if not prefix:
            return self
        return dataclasses.replace(self, path=tuple(prefix) + self.path)

    def with_stage(self, stage: str | None) -> Issue:
        """Return a copy of this issue tagged with ``stage``.

        Issues that already have a stage are returned unchanged, so the
        innermost (most specific) stage always wins.
        """
        if stage is None or self.stage is not None:
            return self
        return dataclasses.replace(self, stage=stage)

    def to_dict(self) -> dict[str, typing.Any]:
        """Return a JSON-friendly dict representation of this issue."""
        return {
            "code": self.code,
            "message": self.message,
            "params": dict(self.params),
            "path": list(self.path),
            "stage": self.stage,
        }


def _issues_from_messages(
    messages: str | list | dict,
    *,
    code: str,
    params: typing.Mapping[str, typing.Any] | None = None,
    stage: str | None = None,
    base_path: tuple[str | int, ...] = (),
) -> list[Issue]:
    """Build a list of :class:`Issue` from a messages structure.

    The structure matches the ``message`` parameter of :class:`ValidationError`:
    a string, a list of messages, or a dict of nested messages. Dict keys
    (including integer keys for collection items) become path segments.
    """
    issues: list[Issue] = []

    def walk(node: typing.Any, path: tuple[str | int, ...]) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                walk(value, (*path, key))
        elif isinstance(node, (list, tuple)):
            for item in node:
                walk(item, path)
        else:
            issues.append(
                Issue(
                    code=code,
                    message=node if isinstance(node, str) else str(node),
                    params=dict(params or {}),
                    path=path,
                    stage=stage,
                )
            )

    walk(messages, base_path)
    return issues


class MarshmallowError(Exception):
    """Base class for all marshmallow-related errors."""


class ValidationError(MarshmallowError):
    """Raised when validation fails on a field or schema.

    Validators and custom fields should raise this exception.

    :param message: An error message, list of error messages, or dict of
        error messages. If a dict, the keys are subitems and the values are error messages.
    :param field_name: Field name to store the error on.
    :param data: Raw input data.
    :param valid_data: Valid (de)serialized data.
    :param code: A stable, translation-independent error code. Defaults to
        ``USER_ERROR`` for errors raised without a code.
    :param params: Structured parameters associated with the error.
    :param issues: Explicit list of :class:`Issue` objects. Used when
        combining errors from nested structures. If omitted, issues are
        derived from ``message``, ``code`` and ``params``.
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
        params: typing.Mapping[str, typing.Any] | None = None,
        issues: typing.Iterable[Issue] | None = None,
        **kwargs,
    ):
        self.messages = [message] if isinstance(message, (str, bytes)) else message
        self.field_name = field_name
        self.data = data
        self.valid_data = valid_data
        self.code = code if code is not None else USER_ERROR
        self.params = dict(params) if params else {}
        self._issues = list(issues) if issues is not None else None
        self.kwargs = kwargs
        super().__init__(message)

    @property
    def issues(self) -> list[Issue]:
        """Structured, translation-independent issues for this error.

        Paths are relative to the input this error was raised for. The
        error raised by ``Schema.load`` carries issues with paths relative
        to the loaded data.
        """
        if self._issues is not None:
            return list(self._issues)
        return _issues_from_messages(self.messages, code=self.code, params=self.params)

    def with_stage(self, stage: str) -> ValidationError:
        """Tag all issues that do not have a stage yet with ``stage``.

        Returns ``self`` to allow re-raising the same error.
        """
        self._issues = [issue.with_stage(stage) for issue in self.issues]
        return self

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

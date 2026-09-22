"""Exception classes for marshmallow-related errors."""

from __future__ import annotations

import typing

# Key used for schema-level validation errors
SCHEMA = "_schema"


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
    """

    def __init__(
        self,
        message: str | list | dict,
        field_name: str = SCHEMA,
        data: typing.Mapping[str, typing.Any]
        | typing.Iterable[typing.Mapping[str, typing.Any]]
        | None = None,
        valid_data: list[typing.Any] | dict[str, typing.Any] | None = None,
        **kwargs,
    ):
        self.messages = [message] if isinstance(message, (str, bytes)) else message
        self.field_name = field_name
        self.data = data
        self.valid_data = valid_data
        self.kwargs = kwargs
        super().__init__(message)

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


class TraversalBudgetExceeded(MarshmallowError):
    """Raised when a ``load``/``dump`` call exhausts its traversal budget.

    This is raised when the number of visited nodes exceeds ``max_nodes``
    or the container nesting depth exceeds ``max_depth``. It is not a
    `ValidationError`, so it is never swallowed by field-level error
    handling and always aborts the traversal immediately.

    :param message: Error message.
    :param path: Path to the position where the budget was exhausted, as a
        tuple of external field names (``data_key``) and collection indices.

    .. versionadded:: 4.4.0
    """

    def __init__(self, message: str, *, path: typing.Iterable = ()):
        self.message = message
        #: Path to the position where the budget was exhausted.
        self.path = tuple(path)
        super().__init__(message)

    @property
    def messages(self) -> list | dict:
        """The error message nested by :attr:`path`, mirroring the structure
        of `ValidationError.messages <marshmallow.exceptions.ValidationError>`.
        """
        messages: list | dict = [self.message]
        for segment in reversed(self.path):
            messages = {segment: messages}
        return messages


class TraversalCycleError(TraversalBudgetExceeded):
    """Raised when a container cycle is detected along the current ancestor
    chain during a budgeted traversal.

    Only containers on the current traversal path are considered, so objects
    that are legitimately shared between sibling positions (diamond-shaped
    object graphs) are not reported as cycles.

    .. versionadded:: 4.4.0
    """


class _FieldInstanceResolutionError(MarshmallowError, TypeError):
    """Raised when an argument is passed to a field class that cannot be resolved to a Field instance."""

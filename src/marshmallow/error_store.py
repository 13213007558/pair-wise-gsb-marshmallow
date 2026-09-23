"""Utilities for storing collections of error messages.

.. warning::

    This module is treated as private API.
    Users should not need to use this module directly.
"""

from marshmallow.exceptions import (
    SCHEMA,
    USER_ERROR,
    Issue,
    _issues_from_messages,
)


class ErrorStore:
    def __init__(self):
        #: Dictionary of errors stored during serialization
        self.errors = {}
        #: List of structured issues stored during serialization
        self.issues: list[Issue] = []

    def store_error(
        self,
        messages,
        field_name=SCHEMA,
        index=None,
        *,
        issues=None,
        code=None,
        params=None,
        stage=None,
    ):
        # field error  -> store/merge error messages under field name key
        # schema error -> if string or list, store/merge under _schema key
        #              -> if dict, store/merge with other top-level keys
        messages = copy_containers(messages)
        wrapped_in_field_name = field_name != SCHEMA or not isinstance(messages, dict)
        if wrapped_in_field_name:
            messages = {field_name: messages}
        if index is not None:
            messages = {index: messages}
        self.errors = merge_errors(self.errors, messages)

        # Structured issues are appended, never merged, so multiple
        # problems at the same path are all preserved.
        prefix = ()
        if index is not None:
            prefix += (index,)
        if field_name != SCHEMA:
            prefix += (field_name,)
        if issues is None:
            # Unwrap the field_name/index layers added above. Schema-level
            # errors (stored under the _schema key) map to the root path.
            source = messages
            if index is not None:
                source = next(iter(source.values()))
            if wrapped_in_field_name:
                source = next(iter(source.values()))
            self.issues.extend(
                _issues_from_messages(
                    source,
                    code=code or USER_ERROR,
                    params=params,
                    stage=stage,
                    base_path=prefix,
                )
            )
        else:
            self.issues.extend(
                issue.with_stage(stage).with_path_prefix(prefix) for issue in issues
            )


def copy_containers(errors):
    if isinstance(errors, list):
        return [copy_containers(val) for val in errors]
    if isinstance(errors, dict):
        return {key: copy_containers(val) for key, val in errors.items()}
    return errors


def merge_errors(errors1, errors2):  # noqa: PLR0911
    """Deeply merge two error messages.

    The format of ``errors1`` and ``errors2`` matches the ``message``
    parameter of :exc:`marshmallow.exceptions.ValidationError`.
    """
    if not errors1:
        return errors2
    if not errors2:
        return errors1
    if isinstance(errors1, list):
        if isinstance(errors2, list):
            errors1.extend(errors2)
            return errors1
        if isinstance(errors2, dict):
            errors2[SCHEMA] = merge_errors(errors1, errors2.get(SCHEMA))
            return errors2
        errors1.append(errors2)
        return errors1
    if isinstance(errors1, dict):
        if isinstance(errors2, dict):
            for key, val in errors2.items():
                if key in errors1:
                    errors1[key] = merge_errors(errors1[key], val)
                else:
                    errors1[key] = val
            return errors1
        errors1[SCHEMA] = merge_errors(errors1.get(SCHEMA), errors2)
        return errors1
    if isinstance(errors2, list):
        return [errors1, *errors2]
    if isinstance(errors2, dict):
        errors2[SCHEMA] = merge_errors(errors1, errors2.get(SCHEMA))
        return errors2
    return [errors1, errors2]

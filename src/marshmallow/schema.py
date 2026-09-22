"""The `Schema <marshmallow.Schema>` class, including its metaclass and options (`class Meta <marshmallow.Schema.Meta>`)."""

# ruff: noqa: SLF001
from __future__ import annotations

import copy
import datetime as dt
import decimal
import functools
import inspect
import ipaddress
import json
import operator
import typing
import uuid
from abc import ABCMeta
from collections import defaultdict
from collections.abc import Mapping, Sequence
from itertools import zip_longest

from marshmallow import class_registry, types
from marshmallow import fields as ma_fields
from marshmallow.constants import EXCLUDE, INCLUDE, RAISE, missing
from marshmallow.decorators import (
    POST_DUMP,
    POST_LOAD,
    PRE_DUMP,
    PRE_LOAD,
    VALIDATES,
    VALIDATES_SCHEMA,
)
from marshmallow.error_store import ErrorStore
from marshmallow.exceptions import SCHEMA, StringNotCollectionError, ValidationError
from marshmallow.orderedset import OrderedSet
from marshmallow.utils import (
    get_value,
    is_collection,
    is_sequence_but_not_string,
    set_value,
)

if typing.TYPE_CHECKING:
    from marshmallow.fields import Field


def _get_fields(attrs) -> list[tuple[str, Field]]:
    """Get fields from a class

    :param attrs: Mapping of class attributes
    """
    ret = []
    for field_name, field_value in attrs.items():
        if isinstance(field_value, type) and issubclass(field_value, ma_fields.Field):
            raise TypeError(
                f'Field for "{field_name}" must be declared as a '
                "Field instance, not a class. "
                f'Did you mean "fields.{field_value.__name__}()"?'
            )
        if isinstance(field_value, ma_fields.Field):
            ret.append((field_name, field_value))
    return ret


# This function allows Schemas to inherit from non-Schema classes and ensures
#   inheritance according to the MRO
def _get_fields_by_mro(klass: SchemaMeta):
    """Collect fields from a class, following its method resolution order. The
    class itself is excluded from the search; only its parents are checked. Get
    fields from ``_declared_fields`` if available, else use ``__dict__``.

    :param klass: Class whose fields to retrieve
    """
    mro = inspect.getmro(klass)
    # Combine fields from all parents
    # functools.reduce(operator.iadd, list_of_lists) is faster than sum(list_of_lists, [])
    # Loop over mro in reverse to maintain correct order of fields
    return functools.reduce(
        operator.iadd,
        (
            _get_fields(
                getattr(base, "_declared_fields", base.__dict__),
            )
            for base in mro[:0:-1]
        ),
        [],
    )


class SchemaMeta(ABCMeta):
    """Metaclass for the Schema class. Binds the declared fields to
    a ``_declared_fields`` attribute, which is a dictionary mapping attribute
    names to field objects. Also sets the ``opts`` class attribute, which is
    the Schema class's `class Meta <marshmallow.Schema.Meta>` options.
    """

    Meta: type
    opts: typing.Any
    OPTIONS_CLASS: type
    _declared_fields: dict[str, Field]

    def __new__(
        mcs,
        name: str,
        bases: tuple[type, ...],
        attrs: dict[str, typing.Any],
    ) -> SchemaMeta:
        meta = attrs.get("Meta")
        cls_fields = _get_fields(attrs)
        # Remove fields from list of class attributes to avoid shadowing
        # Schema attributes/methods in case of name conflict
        for field_name, _ in cls_fields:
            del attrs[field_name]
        klass = super().__new__(mcs, name, bases, attrs)
        inherited_fields = _get_fields_by_mro(klass)

        meta = klass.Meta
        # Set klass.opts in __new__ rather than __init__ so that it is accessible in
        # get_declared_fields
        klass.opts = klass.OPTIONS_CLASS(meta)
        # Add fields specified in the `include` class Meta option
        cls_fields += list(klass.opts.include.items())

        # Assign _declared_fields on class
        klass._declared_fields = mcs.get_declared_fields(
            klass=klass,
            cls_fields=cls_fields,
            inherited_fields=inherited_fields,
            dict_cls=dict,
        )
        return klass

    @classmethod
    def get_declared_fields(
        mcs,  # noqa: N804
        klass: SchemaMeta,
        cls_fields: list[tuple[str, Field]],
        inherited_fields: list[tuple[str, Field]],
        dict_cls: type[dict] = dict,
    ) -> dict[str, Field]:
        """Returns a dictionary of field_name => `Field` pairs declared on the class.
        This is exposed mainly so that plugins can add additional fields, e.g. fields
        computed from `class Meta <marshmallow.Schema.Meta>` options.

        :param klass: The class object.
        :param cls_fields: The fields declared on the class, including those added
            by the ``include`` `class Meta <marshmallow.Schema.Meta>` option.
        :param inherited_fields: Inherited fields.
        :param dict_cls: dict-like class to use for dict output. Defaults to ``dict``.
        """
        return dict_cls(inherited_fields + cls_fields)

    def __init__(cls, name, bases, attrs):
        super().__init__(name, bases, attrs)
        if name and cls.opts.register:
            class_registry.register(name, cls)
        cls._hooks = cls.resolve_hooks()

    def resolve_hooks(cls) -> dict[str, list[tuple[str, bool, dict]]]:
        """Add in the decorated processors

        By doing this after constructing the class, we let standard inheritance
        do all the hard work.
        """
        mro = inspect.getmro(cls)

        hooks: dict[str, list[tuple[str, bool, dict]]] = defaultdict(list)

        for attr_name in dir(cls):
            # Need to look up the actual descriptor, not whatever might be
            # bound to the class. This needs to come from the __dict__ of the
            # declaring class.
            for parent in mro:
                try:
                    attr = parent.__dict__[attr_name]
                except KeyError:
                    continue
                else:
                    break
            else:
                # In case we didn't find the attribute and didn't break above.
                # We should never hit this - it's just here for completeness
                # to exclude the possibility of attr being undefined.
                continue

            try:
                hook_config: dict[str, list[tuple[bool, dict]]] = (
                    attr.__marshmallow_hook__
                )
            except AttributeError:
                pass
            else:
                for tag, config in hook_config.items():
                    # Use name here so we can get the bound method later, in
                    # case the processor was a descriptor or something.
                    hooks[tag].extend(
                        (attr_name, many, kwargs) for many, kwargs in config
                    )

        return hooks


class SchemaOpts:
    """Defines defaults for `marshmallow.Schema.Meta`."""

    def __init__(self, meta: type):
        self.fields = getattr(meta, "fields", ())
        if not isinstance(self.fields, (list, tuple)):
            raise ValueError("`fields` option must be a list or tuple.")
        self.exclude = getattr(meta, "exclude", ())
        if not isinstance(self.exclude, (list, tuple)):
            raise ValueError("`exclude` must be a list or tuple.")
        self.dateformat = getattr(meta, "dateformat", None)
        self.datetimeformat = getattr(meta, "datetimeformat", None)
        self.timeformat = getattr(meta, "timeformat", None)
        self.render_module = getattr(meta, "render_module", json)
        self.index_errors = getattr(meta, "index_errors", True)
        self.include = getattr(meta, "include", {})
        self.load_only = getattr(meta, "load_only", ())
        self.dump_only = getattr(meta, "dump_only", ())
        self.unknown = getattr(meta, "unknown", RAISE)
        self.register = getattr(meta, "register", True)
        self.many = getattr(meta, "many", False)


class Schema(metaclass=SchemaMeta):
    """Base schema class with which to define schemas.

    Example usage:

    .. code-block:: python

        import datetime as dt
        from dataclasses import dataclass

        from marshmallow import Schema, fields


        @dataclass
        class Album:
            title: str
            release_date: dt.date


        class AlbumSchema(Schema):
            title = fields.Str()
            release_date = fields.Date()


        album = Album("Beggars Banquet", dt.date(1968, 12, 6))
        schema = AlbumSchema()
        data = schema.dump(album)
        data  # {'release_date': '1968-12-06', 'title': 'Beggars Banquet'}

    :param only: Whitelist of the declared fields to select when
        instantiating the Schema. If None, all fields are used. Nested fields
        can be represented with dot delimiters.
    :param exclude: Blacklist of the declared fields to exclude
        when instantiating the Schema. If a field appears in both `only` and
        `exclude`, it is not used. Nested fields can be represented with dot
        delimiters.
    :param many: Should be set to `True` if ``obj`` is a collection
        so that the object will be serialized to a list.
    :param load_only: Fields to skip during serialization (write-only fields)
    :param dump_only: Fields to skip during deserialization (read-only fields)
    :param partial: Whether to ignore missing fields and not require
        any fields declared. Propagates down to ``Nested`` fields as well. If
        its value is an iterable, only missing fields listed in that iterable
        will be ignored. Use dot delimiters to specify nested fields.
    :param unknown: Whether to exclude, include, or raise an error for unknown
        fields in the data. Use `EXCLUDE`, `INCLUDE` or `RAISE`.

    .. versionchanged:: 3.0.0
        Remove ``prefix`` parameter.

    .. versionchanged:: 4.0.0
        Remove ``context`` parameter.
    """

    TYPE_MAPPING: dict[type, type[Field]] = {
        str: ma_fields.String,
        bytes: ma_fields.String,
        dt.datetime: ma_fields.DateTime,
        float: ma_fields.Float,
        bool: ma_fields.Boolean,
        tuple: ma_fields.Raw,
        list: ma_fields.Raw,
        set: ma_fields.Raw,
        int: ma_fields.Integer,
        uuid.UUID: ma_fields.UUID,
        dt.time: ma_fields.Time,
        dt.date: ma_fields.Date,
        dt.timedelta: ma_fields.TimeDelta,
        decimal.Decimal: ma_fields.Decimal,
        ipaddress.IPv4Address: ma_fields.IPv4,
        ipaddress.IPv6Address: ma_fields.IPv6,
        ipaddress.IPv4Interface: ma_fields.IPv4Interface,
        ipaddress.IPv6Interface: ma_fields.IPv6Interface,
    }
    #: Overrides for default schema-level error messages
    error_messages: dict[str, str] = {}

    _default_error_messages: dict[str, str] = {
        "type": "Invalid input type.",
        "unknown": "Unknown field.",
    }

    OPTIONS_CLASS: type = SchemaOpts

    set_class = OrderedSet
    dict_class: type[dict] = dict
    """`dict` type to return when serializing."""

    # These get set by SchemaMeta
    opts: typing.Any
    _declared_fields: dict[str, Field] = {}
    _hooks: dict[str, list[tuple[str, bool, dict]]] = {}

    class Meta:
        """Options object for a Schema.

        Example usage: ::

            from marshmallow import Schema


            class MySchema(Schema):
                class Meta:
                    fields = ("id", "email", "date_created")
                    exclude = ("password", "secret_attribute")

        .. admonition:: A note on type checking

            Type checkers will only check the attributes of the `Meta <marshmallow.Schema.Meta>`
            class if you explicitly subclass `marshmallow.Schema.Meta`.

            .. code-block:: python

                from marshmallow import Schema


                class MySchema(Schema):
                    # Not checked by type checkers
                    class Meta:
                        additional = True


                class MySchema2(Schema):
                    # Type checkers will check attributes
                    class Meta(Schema.Meta):
                        additional = True  # Incompatible types in assignment

        .. versionremoved:: 3.0.0b7 Remove ``strict``.
        .. versionadded:: 3.0.0b12 Add `unknown`.
        .. versionchanged:: 3.0.0b17 Rename ``dateformat`` to `datetimeformat`.
        .. versionadded:: 3.9.0 Add `timeformat`.
        .. versionchanged:: 3.26.0 Deprecate ``ordered``. Field order is preserved by default.
        .. versionremoved:: 4.0.0 Remove ``ordered``.
        """

        fields: typing.ClassVar[tuple[str, ...] | list[str]]
        """Fields to include in the (de)serialized result"""
        additional: typing.ClassVar[tuple[str, ...] | list[str]]
        """Fields to include in addition to the explicitly declared fields.
        `additional <marshmallow.Schema.Meta.additional>` and `fields <marshmallow.Schema.Meta.fields>`
        are mutually-exclusive options.
        """
        include: typing.ClassVar[dict[str, Field]]
        """Dictionary of additional fields to include in the schema. It is
        usually better to define fields as class variables, but you may need to
        use this option, e.g., if your fields are Python keywords.
        """
        exclude: typing.ClassVar[tuple[str, ...] | list[str]]
        """Fields to exclude in the serialized result.
        Nested fields can be represented with dot delimiters.
        """
        many: typing.ClassVar[bool]
        """Whether data should be (de)serialized as a collection by default."""
        dateformat: typing.ClassVar[str]
        """Default format for `Date <marshmallow.fields.Date>` fields."""
        datetimeformat: typing.ClassVar[str]
        """Default format for `DateTime <marshmallow.fields.DateTime>` fields."""
        timeformat: typing.ClassVar[str]
        """Default format for `Time <marshmallow.fields.Time>` fields."""

        # FIXME: Use a more constrained type here.
        # ClassVar[RenderModule] doesn't work.
        render_module: typing.Any
        """ Module to use for `loads <marshmallow.Schema.loads>` and `dumps <marshmallow.Schema.dumps>`.
        Defaults to `json` from the standard library.
        """
        index_errors: typing.ClassVar[bool]
        """If `True`, errors dictionaries will include the index of invalid items in a collection."""
        load_only: typing.ClassVar[tuple[str, ...] | list[str]]
        """Fields to exclude from serialized results"""
        dump_only: typing.ClassVar[tuple[str, ...] | list[str]]
        """Fields to exclude from deserialized results"""
        unknown: typing.ClassVar[types.UnknownOption]
        """Whether to exclude, include, or raise an error for unknown fields in the data.
        Use `EXCLUDE`, `INCLUDE` or `RAISE`.
        """
        register: typing.ClassVar[bool]
        """Whether to register the `Schema <marshmallow.Schema>` with marshmallow's internal
        class registry. Must be `True` if you intend to refer to this `Schema <marshmallow.Schema>`
        by class name in `Nested` fields. Only set this to `False` when memory
        usage is critical. Defaults to `True`.
        """

    def __init__(
        self,
        *,
        only: types.StrSequenceOrSet | None = None,
        exclude: types.StrSequenceOrSet = (),
        many: bool | None = None,
        load_only: types.StrSequenceOrSet = (),
        dump_only: types.StrSequenceOrSet = (),
        partial: bool | types.StrSequenceOrSet | None = None,
        unknown: types.UnknownOption | None = None,
    ):
        # Raise error if only or exclude is passed as string, not list of strings
        if only is not None and not is_collection(only):
            raise StringNotCollectionError('"only" should be a list of strings')
        if not is_collection(exclude):
            raise StringNotCollectionError('"exclude" should be a list of strings')
        # copy declared fields from metaclass
        self.declared_fields = copy.deepcopy(self._declared_fields)
        self.many = self.opts.many if many is None else many
        self.only = only
        self.exclude: set[typing.Any] | typing.MutableSet[typing.Any] = set(
            self.opts.exclude
        ) | set(exclude)
        self.load_only = set(load_only) or set(self.opts.load_only)
        self.dump_only = set(dump_only) or set(self.opts.dump_only)
        self.partial = partial
        self.unknown: types.UnknownOption = (
            self.opts.unknown if unknown is None else unknown
        )
        self._normalize_nested_options()
        #: Dictionary mapping field_names -> :class:`Field` objects
        self.fields: dict[str, Field] = {}
        self.load_fields: dict[str, Field] = {}
        self.dump_fields: dict[str, Field] = {}
        self._init_fields()
        messages = {}
        messages.update(self._default_error_messages)
        for cls in reversed(self.__class__.__mro__):
            messages.update(getattr(cls, "error_messages", {}))
        messages.update(self.error_messages or {})
        self.error_messages = messages

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__}(many={self.many})>"

    @classmethod
    def from_dict(
        cls,
        fields: dict[str, Field],
        *,
        name: str = "GeneratedSchema",
    ) -> type[Schema]:
        """Generate a `Schema <marshmallow.Schema>` class given a dictionary of fields.

        .. code-block:: python

            from marshmallow import Schema, fields

            PersonSchema = Schema.from_dict({"name": fields.Str()})
            print(PersonSchema().load({"name": "David"}))  # => {'name': 'David'}

        Generated schemas are not added to the class registry and therefore cannot
        be referred to by name in `Nested` fields.


        :param fields: Dictionary mapping field names to field instances.
        :param name: Optional name for the class, which will appear in
            the ``repr`` for the class.

        .. versionadded:: 3.0.0
        """
        Meta = type(
            "GeneratedMeta", (getattr(cls, "Meta", object),), {"register": False}
        )
        return type(name, (cls,), {**fields.copy(), "Meta": Meta})

    ##### Override-able methods #####

    def handle_error(
        self, error: ValidationError, data: typing.Any, *, many: bool, **kwargs
    ):
        """Custom error handler function for the schema.

        :param error: The `ValidationError` raised during (de)serialization.
        :param data: The original input data.
        :param many: Value of ``many`` on dump or load.
        :param partial: Value of ``partial`` on load.

        .. versionchanged:: 3.0.0rc9
            Receives `many` and `partial` (on deserialization) as keyword arguments.
        """

    def get_attribute(self, obj: typing.Any, attr: str, default: typing.Any):
        """Defines how to pull values from an object to serialize.

        .. versionchanged:: 3.0.0a1
            Changed position of ``obj`` and ``attr``.
        """
        return get_value(obj, attr, default)

    ##### Serialization/Deserialization API #####

    @staticmethod
    def _call_and_store(getter_func, data, *, field_name, error_store, index=None):
        """Call ``getter_func`` with ``data`` as its argument, and store any `ValidationErrors`.

        :param getter_func: Function for getting the serialized/deserialized
            value from ``data``.
        :param data: The data passed to ``getter_func``.
        :param field_name: Field name.
        :param index: Index of the item being validated, if validating a collection,
            otherwise `None`.
        """
        try:
            value = getter_func(data)
        except ValidationError as error:
            error_store.store_error(error.messages, field_name, index=index)
            # When a Nested field fails validation, the marshalled data is stored
            # on the ValidationError's valid_data attribute
            return error.valid_data or missing
        return value

    def _serialize(self, obj: typing.Any, *, many: bool = False):
        """Serialize ``obj``.

        :param obj: The object(s) to serialize.
        :param many: `True` if ``obj`` should be serialized as a collection.
        :return: A dictionary of the serialized data
        """
        if many and obj is not None:
            return [self._serialize(d, many=False) for d in obj]
        ret = self.dict_class()
        for attr_name, field_obj in self.dump_fields.items():
            value = field_obj.serialize(attr_name, obj, accessor=self.get_attribute)
            if value is missing:
                continue
            key = field_obj.data_key if field_obj.data_key is not None else attr_name
            ret[key] = value
        return ret

    def dump(self, obj: typing.Any, *, many: bool | None = None):
        """Serialize an object to native Python data types according to this
        Schema's fields.

        :param obj: The object to serialize.
        :param many: Whether to serialize `obj` as a collection. If `None`, the value
            for `self.many` is used.
        :return: Serialized data

        .. versionchanged:: 3.0.0b7
            This method returns the serialized data rather than a ``(data, errors)`` tuple.
            A :exc:`ValidationError <marshmallow.exceptions.ValidationError>` is raised
            if ``obj`` is invalid.
        .. versionchanged:: 3.0.0rc9
            Validation no longer occurs upon serialization.
        """
        many = self.many if many is None else bool(many)
        if self._hooks[PRE_DUMP]:
            processed_obj = self._invoke_dump_processors(
                PRE_DUMP, obj, many=many, original_data=obj
            )
        else:
            processed_obj = obj

        result = self._serialize(processed_obj, many=many)

        if self._hooks[POST_DUMP]:
            result = self._invoke_dump_processors(
                POST_DUMP, result, many=many, original_data=obj
            )

        return result

    def dumps(self, obj: typing.Any, *args, many: bool | None = None, **kwargs):
        """Same as :meth:`dump`, except return a JSON-encoded string.

        :param obj: The object to serialize.
        :param many: Whether to serialize `obj` as a collection. If `None`, the value
            for `self.many` is used.
        :return: A ``json`` string

        .. versionchanged:: 3.0.0b7
            This method returns the serialized data rather than a ``(data, errors)`` tuple.
            A :exc:`ValidationError <marshmallow.exceptions.ValidationError>` is raised
            if ``obj`` is invalid.
        """
        serialized = self.dump(obj, many=many)
        return self.opts.render_module.dumps(serialized, *args, **kwargs)

    def _deserialize(
        self,
        data: Mapping[str, typing.Any] | Sequence[Mapping[str, typing.Any]],
        *,
        error_store: ErrorStore,
        many: bool = False,
        partial=None,
        unknown: types.UnknownOption = RAISE,
        index=None,
        baseline: typing.Any = None,
    ) -> typing.Any | list[typing.Any]:
        """Deserialize ``data``.

        :param data: The data to deserialize.
        :param error_store: Structure to store errors.
        :param many: `True` if ``data`` should be deserialized as a collection.
        :param partial: Whether to ignore missing fields and not require
            any fields declared. Propagates down to ``Nested`` fields as well. If
            its value is an iterable, only missing fields listed in that iterable
            will be ignored. Use dot delimiters to specify nested fields.
        :param unknown: Whether to exclude, include, or raise an error for unknown
            fields in the data. Use `EXCLUDE`, `INCLUDE` or `RAISE`.
        :param index: Index of the item being deserialized (for storing errors) if
            deserializing a collection, otherwise `None`.
        :return: The deserialized data as `dict_class` instance or list of `dict_class`
        instances if `many` is `True`.
        """
        index = index if self.opts.index_errors else None
        if many:
            if not is_sequence_but_not_string(data):
                error_store.store_error([self.error_messages["type"]], index=index)
                ret_l = []
            else:
                if baseline is not None and not is_sequence_but_not_string(baseline):
                    raise TypeError(
                        "When loading many items, `baseline` must be a sequence of "
                        "deserialized values matching the input items."
                    )
                ret_l = [
                    self._deserialize(
                        d,
                        error_store=error_store,
                        many=False,
                        partial=partial,
                        unknown=unknown,
                        index=idx,
                        baseline=(
                            baseline[idx]
                            if baseline is not None and idx < len(baseline)
                            else None
                        ),
                    )
                    for idx, d in enumerate(data)
                ]
            return ret_l
        ret_d = self.dict_class()
        # Check data is a dict
        if not isinstance(data, Mapping):
            error_store.store_error([self.error_messages["type"]], index=index)
        else:
            partial_is_collection = is_collection(partial)
            for attr_name, field_obj in self.load_fields.items():
                if isinstance(field_obj, ma_fields.Derived):
                    # Derived values are computed after ordinary field
                    # deserialization and field-level validation.
                    continue
                field_name = (
                    field_obj.data_key if field_obj.data_key is not None else attr_name
                )
                raw_value = data.get(field_name, missing)
                if raw_value is missing:
                    # Ignore missing field if we're allowed to.
                    if partial is True or (
                        partial_is_collection and attr_name in partial
                    ):
                        continue
                d_kwargs = {}
                # Allow partial loading of nested schemas.
                if partial_is_collection:
                    prefix = attr_name + "."
                    len_prefix = len(prefix)
                    sub_partial = [
                        f[len_prefix:] for f in partial if f.startswith(prefix)
                    ]
                    d_kwargs["partial"] = sub_partial
                elif partial is not None:
                    d_kwargs["partial"] = partial

                def getter(
                    val, field_obj=field_obj, field_name=field_name, d_kwargs=d_kwargs
                ):
                    return field_obj.deserialize(
                        val,
                        field_name,
                        data,
                        **d_kwargs,
                    )

                value = self._call_and_store(
                    getter_func=getter,
                    data=raw_value,
                    field_name=field_name,
                    error_store=error_store,
                    index=index,
                )
                if value is not missing:
                    key = field_obj.attribute or attr_name
                    set_value(ret_d, key, value)
            if unknown != EXCLUDE:
                fields = {
                    field_obj.data_key if field_obj.data_key is not None else field_name
                    for field_name, field_obj in self.load_fields.items()
                    if not isinstance(field_obj, ma_fields.Derived)
                }
                # Derived fields never accept client input: submitted keys are
                # dropped silently (unless another declared field claims them).
                derived_input_keys = {
                    field_obj.data_key
                    if field_obj.data_key is not None
                    else field_name
                    for field_name, field_obj in self.load_fields.items()
                    if isinstance(field_obj, ma_fields.Derived)
                }
                for key in set(data) - fields - derived_input_keys:
                    value = data[key]
                    if unknown == INCLUDE:
                        ret_d[key] = value
                    elif unknown == RAISE:
                        error_store.store_error(
                            [self.error_messages["unknown"]],
                            key,
                            index,
                        )
        return ret_d

    def load(
        self,
        data: Mapping[str, typing.Any] | Sequence[Mapping[str, typing.Any]],
        *,
        many: bool | None = None,
        partial: bool | types.StrSequenceOrSet | None = None,
        unknown: types.UnknownOption | None = None,
        baseline: typing.Mapping[str, typing.Any] | typing.Sequence | None = None,
    ):
        """Deserialize a data structure to an object defined by this Schema's fields.

        :param data: The data to deserialize.
        :param many: Whether to deserialize `data` as a collection. If `None`, the
            value for `self.many` is used.
        :param partial: Whether to ignore missing fields and not require
            any fields declared. Propagates down to ``Nested`` fields as well. If
            its value is an iterable, only missing fields listed in that iterable
            will be ignored. Use dot delimiters to specify nested fields.
        :param unknown: Whether to exclude, include, or raise an error for unknown
            fields in the data. Use `EXCLUDE`, `INCLUDE` or `RAISE`.
            If `None`, the value for `self.unknown` is used.
        :return: Deserialized data

        .. versionchanged:: 3.0.0b7
            This method returns the deserialized data rather than a ``(data, errors)`` tuple.
            A :exc:`ValidationError <marshmallow.exceptions.ValidationError>` is raised
            if invalid data are passed.
        """
        return self._do_load(
            data,
            many=many,
            partial=partial,
            unknown=unknown,
            postprocess=True,
            baseline=baseline,
        )

    def loads(
        self,
        s: str | bytes | bytearray,
        /,
        *,
        many: bool | None = None,
        partial: bool | types.StrSequenceOrSet | None = None,
        unknown: types.UnknownOption | None = None,
        **kwargs,
    ):
        """Same as :meth:`load`, except it uses `marshmallow.Schema.Meta.render_module` to deserialize
        the passed string before passing data to :meth:`load`.

        :param s: A string of the data to deserialize.
        :param many: Whether to deserialize `obj` as a collection. If `None`, the
            value for `self.many` is used.
        :param partial: Whether to ignore missing fields and not require
            any fields declared. Propagates down to ``Nested`` fields as well. If
            its value is an iterable, only missing fields listed in that iterable
            will be ignored. Use dot delimiters to specify nested fields.
        :param unknown: Whether to exclude, include, or raise an error for unknown
            fields in the data. Use `EXCLUDE`, `INCLUDE` or `RAISE`.
            If `None`, the value for `self.unknown` is used.
        :return: Deserialized data

        .. versionchanged:: 3.0.0b7
            This method returns the deserialized data rather than a ``(data, errors)`` tuple.
            A :exc:`ValidationError <marshmallow.exceptions.ValidationError>` is raised
            if invalid data are passed.
        .. versionchanged:: 4.0.0
            Rename ``json_module`` parameter to ``s``.
        """
        baseline = kwargs.pop("baseline", None)
        data = self.opts.render_module.loads(s, **kwargs)
        return self.load(
            data,
            many=many,
            partial=partial,
            unknown=unknown,
            baseline=baseline,
        )

    def _run_validator(
        self,
        validator_func: types.SchemaValidator,
        output,
        *,
        original_data,
        error_store: ErrorStore,
        many: bool,
        partial: bool | types.StrSequenceOrSet | None,
        unknown: types.UnknownOption | None,
        pass_original: bool,
        index: int | None = None,
    ):
        try:
            if pass_original:  # Pass original, raw data (before unmarshalling)
                validator_func(
                    output, original_data, partial=partial, many=many, unknown=unknown
                )
            else:
                validator_func(output, partial=partial, many=many, unknown=unknown)
        except ValidationError as err:
            field_name = err.field_name
            data_key: str
            if field_name == SCHEMA:
                data_key = SCHEMA
            else:
                field_obj: Field | None = None
                try:
                    field_obj = self.fields[field_name]
                except KeyError:
                    if field_name in self.declared_fields:
                        field_obj = self.declared_fields[field_name]
                if field_obj:
                    data_key = (
                        field_obj.data_key
                        if field_obj.data_key is not None
                        else field_name
                    )
                else:
                    data_key = field_name
            error_store.store_error(err.messages, data_key, index=index)

    def validate(
        self,
        data: Mapping[str, typing.Any] | Sequence[Mapping[str, typing.Any]],
        *,
        many: bool | None = None,
        partial: bool | types.StrSequenceOrSet | None = None,
        baseline: typing.Mapping[str, typing.Any] | typing.Sequence | None = None,
    ) -> dict[str, list[str]]:
        """Validate `data` against the schema, returning a dictionary of
        validation errors.

        :param data: The data to validate.
        :param many: Whether to validate `data` as a collection. If `None`, the
            value for `self.many` is used.
        :param partial: Whether to ignore missing fields and not require
            any fields declared. Propagates down to ``Nested`` fields as well. If
            its value is an iterable, only missing fields listed in that iterable
            will be ignored. Use dot delimiters to specify nested fields.
        :return: A dictionary of validation errors.
        """
        try:
            self._do_load(
                data,
                many=many,
                partial=partial,
                postprocess=False,
                baseline=baseline,
            )
        except ValidationError as exc:
            return typing.cast("dict[str, list[str]]", exc.messages)
        return {}

    ##### Private Helpers #####

    def _do_load(
        self,
        data: (Mapping[str, typing.Any] | Sequence[Mapping[str, typing.Any]]),
        *,
        many: bool | None = None,
        partial: bool | types.StrSequenceOrSet | None = None,
        unknown: types.UnknownOption | None = None,
        postprocess: bool = True,
        baseline: typing.Mapping[str, typing.Any] | typing.Sequence | None = None,
    ):
        """Deserialize `data`, returning the deserialized result.
        This method is private API.

        :param data: The data to deserialize.
        :param many: Whether to deserialize `data` as a collection. If `None`, the
            value for `self.many` is used.
        :param partial: Whether to validate required fields. If its
            value is an iterable, only fields listed in that iterable
            will be allowed missing. If `True`, all fields will be allowed missing.
            If `None`, the value for `self.partial` is used.
        :param unknown: Whether to exclude, include, or raise an error for unknown
            fields in the data. Use `EXCLUDE`, `INCLUDE` or `RAISE`.
            If `None`, the value for `self.unknown` is used.
        :param postprocess: Whether to run post_load methods.
        :return: Deserialized data
        """
        error_store = ErrorStore()
        errors: dict[str, list[str]] = {}
        many = self.many if many is None else bool(many)
        unknown = self.unknown if unknown is None else unknown
        if partial is None:
            partial = self.partial
        # Run preprocessors
        if self._hooks[PRE_LOAD]:
            try:
                processed_data = self._invoke_load_processors(
                    PRE_LOAD,
                    data,
                    many=many,
                    original_data=data,
                    partial=partial,
                    unknown=unknown,
                )
            except ValidationError as err:
                errors = err.normalized_messages()
                result: list | dict | None = None
        else:
            processed_data = data
        if not errors:
            # Deserialize data
            result = self._deserialize(
                processed_data,
                error_store=error_store,
                many=many,
                partial=partial,
                unknown=unknown,
                baseline=baseline,
            )
            # Run field-level validation
            self._invoke_field_validators(
                error_store=error_store, data=result, many=many, baseline=baseline
            )
            # Run schema-level validation
            if self._hooks[VALIDATES_SCHEMA]:
                field_errors = bool(error_store.errors)
                self._invoke_schema_validators(
                    error_store=error_store,
                    pass_collection=True,
                    data=result,
                    original_data=data,
                    many=many,
                    partial=partial,
                    unknown=unknown,
                    field_errors=field_errors,
                )
                self._invoke_schema_validators(
                    error_store=error_store,
                    pass_collection=False,
                    data=result,
                    original_data=data,
                    many=many,
                    partial=partial,
                    unknown=unknown,
                    field_errors=field_errors,
                )
            errors = error_store.errors
            # Run post processors
            if not errors and postprocess and self._hooks[POST_LOAD]:
                try:
                    result = self._invoke_load_processors(
                        POST_LOAD,
                        result,
                        many=many,
                        original_data=data,
                        partial=partial,
                        unknown=unknown,
                    )
                except ValidationError as err:
                    errors = err.normalized_messages()
        if errors:
            exc = ValidationError(errors, data=data, valid_data=result)
            self.handle_error(exc, data, many=many, partial=partial)
            raise exc

        return result

    def _normalize_nested_options(self) -> None:
        """Apply then flatten nested schema options.
        This method is private API.
        """
        if self.only is not None:
            # Apply the only option to nested fields.
            self.__apply_nested_option("only", self.only, "intersection")
            # Remove the child field names from the only option.
            self.only = self.set_class([field.split(".", 1)[0] for field in self.only])
        if self.exclude:
            # Apply the exclude option to nested fields.
            self.__apply_nested_option("exclude", self.exclude, "union")
            # Remove the parent field names from the exclude option.
            self.exclude = self.set_class(
                [field for field in self.exclude if "." not in field]
            )

    def __apply_nested_option(self, option_name, field_names, set_operation) -> None:
        """Apply nested options to nested fields"""
        # Split nested field names on the first dot.
        nested_fields = [name.split(".", 1) for name in field_names if "." in name]
        # Partition the nested field names by parent field.
        nested_options = defaultdict(list)  # type: defaultdict
        for parent, nested_names in nested_fields:
            nested_options[parent].append(nested_names)
        # Apply the nested field options.
        for key, options in iter(nested_options.items()):
            new_options = self.set_class(options)
            original_options = getattr(self.declared_fields[key], option_name, ())
            if original_options:
                if set_operation == "union":
                    new_options |= self.set_class(original_options)
                if set_operation == "intersection":
                    new_options &= self.set_class(original_options)
            setattr(self.declared_fields[key], option_name, new_options)

    def _init_fields(self) -> None:
        """Update self.fields, self.load_fields, and self.dump_fields based on schema options.
        This method is private API.
        """
        if self.opts.fields:
            available_field_names = self.set_class(self.opts.fields)
        else:
            available_field_names = self.set_class(self.declared_fields.keys())

        invalid_fields = self.set_class()

        if self.only is not None:
            # Return only fields specified in only option
            field_names: typing.AbstractSet[typing.Any] = self.set_class(self.only)

            invalid_fields |= field_names - available_field_names
        else:
            field_names = available_field_names

        # If "exclude" option or param is specified, remove those fields.
        if self.exclude:
            # Note that this isn't available_field_names, since we want to
            # apply "only" for the actual calculation.
            field_names = field_names - self.exclude
            invalid_fields |= self.exclude - available_field_names

        if invalid_fields:
            message = f"Invalid fields for {self}: {invalid_fields}."
            raise ValueError(message)

        fields_dict = self.dict_class()
        for field_name in field_names:
            field_obj = self.declared_fields[field_name]
            self._bind_field(field_name, field_obj)
            fields_dict[field_name] = field_obj

        load_fields, dump_fields = self.dict_class(), self.dict_class()
        for field_name, field_obj in fields_dict.items():
            if not field_obj.dump_only:
                load_fields[field_name] = field_obj
            if not field_obj.load_only:
                dump_fields[field_name] = field_obj

        dump_data_keys = [
            field_obj.data_key if field_obj.data_key is not None else name
            for name, field_obj in dump_fields.items()
        ]
        if len(dump_data_keys) != len(set(dump_data_keys)):
            data_keys_duplicates = {
                x for x in dump_data_keys if dump_data_keys.count(x) > 1
            }
            raise ValueError(
                "The data_key argument for one or more fields collides "
                "with another field's name or data_key argument. "
                "Check the following field names and "
                f"data_key arguments: {list(data_keys_duplicates)}"
            )
        load_attributes = [obj.attribute or name for name, obj in load_fields.items()]
        if len(load_attributes) != len(set(load_attributes)):
            attributes_duplicates = {
                x for x in load_attributes if load_attributes.count(x) > 1
            }
            raise ValueError(
                "The attribute argument for one or more fields collides "
                "with another field's name or attribute argument. "
                "Check the following field names and "
                f"attribute arguments: {list(attributes_duplicates)}"
            )

        self.fields = fields_dict
        self.dump_fields = dump_fields
        self.load_fields = load_fields
        self.derived_order: list[str] = self._resolve_derived_fields()
        self._derived_validators: dict[str, list[typing.Callable]] = defaultdict(list)
        for hook_attr_name, _, validator_kwargs in self._hooks[VALIDATES]:
            for field_name in validator_kwargs["field_names"]:
                field_obj = self.fields.get(field_name)
                if isinstance(field_obj, ma_fields.Derived):
                    self._derived_validators[field_name].append(
                        getattr(self, hook_attr_name)
                    )

    def on_bind_field(self, field_name: str, field_obj: Field) -> None:
        """Hook to modify a field when it is bound to the `Schema <marshmallow.Schema>`.

        No-op by default.
        """
        return

    def _bind_field(self, field_name: str, field_obj: Field) -> None:
        """Bind field to the schema, setting any necessary attributes on the
        field (e.g. parent and name).

        Also set field load_only and dump_only values if field_name was
        specified in `class Meta <marshmallow.Schema.Meta>`.
        """
        if field_name in self.load_only:
            field_obj.load_only = True
        if field_name in self.dump_only:
            field_obj.dump_only = True
        field_obj._bind_to_schema(field_name, self)
        self.on_bind_field(field_name, field_obj)

    def _resolve_derived_fields(self) -> list[str]:
        """Validate the dependency graph of `Derived` fields and return the
        derived field names in dependency-first execution order.

        Unknown dependencies and dependency cycles raise `ValueError` while
        the schema is being bound, naming the concrete offending path.
        """
        # Both the declared field name and its internal attribute resolve to
        # the field, so dependencies can be written either way. Values are
        # always read from the internal (post-deserialization) data.
        internal_paths: dict[str, tuple[str, Field]] = {}
        for field_name, field_obj in self.fields.items():
            internal_paths[field_name] = (field_name, field_obj)
            internal_paths[field_obj.attribute or field_name] = (field_name, field_obj)

        derived: dict[str, ma_fields.Derived] = {
            field_name: field_obj
            for field_name, field_obj in self.load_fields.items()
            if isinstance(field_obj, ma_fields.Derived)
        }

        # Resolve each declared dependency to the internal path of a selected
        # field and decide how the compute callable receives its arguments.
        resolved_deps: dict[str, list[str]] = {}
        for field_name, derived_obj in derived.items():
            deps: list[str] = []
            for dep in derived_obj.dependencies:
                root = dep.split(".", 1)[0]
                if root not in internal_paths:
                    raise ValueError(
                        f'Derived field "{field_name}" depends on unknown path '
                        f'"{dep}": no field named "{root}" on {self}.'
                    )
                target_name, target_obj = internal_paths[root]
                if isinstance(target_obj, ma_fields.Derived) and target_name not in derived:
                    raise ValueError(
                        f'Derived field "{field_name}" depends on non-loaded '
                        f'Derived field "{target_name}" via path "{dep}".'
                    )
                if target_obj.dump_only and not isinstance(target_obj, ma_fields.Derived):
                    raise ValueError(
                        f'Derived field "{field_name}" depends on dump-only field '
                        f'"{target_name}" via path "{dep}".'
                    )
                deps.append(
                    dep.replace(root, target_obj.attribute or target_name, 1)
                )
            derived_obj._resolved_dependencies = tuple(deps)
            derived_obj._compute_call_plan = self._plan_derived_compute(
                field_name, derived_obj, internal_paths
            )
            resolved_deps[field_name] = deps

        # Stable topological sort: declaration order is preserved for fields
        # that do not depend on each other.
        order: list[str] = []
        state: dict[str, str] = {}

        def visit(node: str, chain: list[str]) -> None:
            status = state.get(node)
            if status == "done":
                return
            if status == "visiting":
                cycle = chain[chain.index(node) :] + [node]
                cycle_paths = " -> ".join(cycle)
                raise ValueError(
                    f"Circular dependency between Derived fields: {cycle_paths}."
                )
            state[node] = "visiting"
            chain.append(node)
            for dep in resolved_deps[node]:
                dep_name = dep.split(".", 1)[0]
                if dep_name in derived:
                    visit(dep_name, chain)
            chain.pop()
            state[node] = "done"
            order.append(node)

        for field_name in derived:
            visit(field_name, [])
        return order

    def _plan_derived_compute(
        self,
        field_name: str,
        derived_obj: ma_fields.Derived,
        internal_paths: dict[str, tuple[str, Field]],
    ) -> dict[str, typing.Any]:
        """Pre-resolve, at bind time, how ``compute`` is invoked at load time.

        The callable is invoked either with the full accumulated mapping as a
        single positional argument, or with each dependency as a keyword
        argument named after the dependency path's last segment.
        """
        deps = list(derived_obj._resolved_dependencies)
        leaf_names = [dep.rsplit(".", 1)[-1] for dep in deps]
        try:
            sig = inspect.signature(derived_obj.compute)
        except (TypeError, ValueError):
            sig = None

        has_var_keyword = sig is not None and any(
            param.kind is inspect.Parameter.VAR_KEYWORD
            for param in sig.parameters.values()
        )
        named_params: set[str] = set()
        positional_count = 0
        positional_only_required = 0
        if sig is not None:
            for param in sig.parameters.values():
                if param.kind in (
                    inspect.Parameter.POSITIONAL_OR_KEYWORD,
                    inspect.Parameter.KEYWORD_ONLY,
                ):
                    named_params.add(param.name)
                if param.kind in (
                    inspect.Parameter.POSITIONAL_ONLY,
                    inspect.Parameter.POSITIONAL_OR_KEYWORD,
                ):
                    positional_count += 1
                    if (
                        param.kind is inspect.Parameter.POSITIONAL_ONLY
                        and param.default is inspect.Parameter.empty
                    ):
                        positional_only_required += 1

        duplicates = {name for name in leaf_names if leaf_names.count(name) > 1}
        if duplicates and positional_count != 1:
            raise ValueError(
                f'Derived field "{field_name}" has dependencies whose path leaf '
                f"names collide: {sorted(duplicates)}. Its compute callable must "
                "accept a single mapping argument."
            )

        # Mapping mode is only selected explicitly: the single parameter is
        # positional-only or uses one of the conventional mapping names.
        # Otherwise each dependency is passed as a keyword argument named after
        # the last segment of its path, so a single dependency lambda
        # ``lambda a: ...`` is unambiguous.
        MAPPING_PARAM_NAMES = {"values", "data", "items", "context", "fields"}
        mapping_mode = False
        if positional_count == 1 and sig is not None:
            single_param = next(
                param
                for param in sig.parameters.values()
                if param.kind
                in (
                    inspect.Parameter.POSITIONAL_ONLY,
                    inspect.Parameter.POSITIONAL_OR_KEYWORD,
                )
            )
            if (
                single_param.kind is inspect.Parameter.POSITIONAL_ONLY
                or single_param.name in MAPPING_PARAM_NAMES
            ):
                mapping_mode = True

        return {
            "mapping_mode": mapping_mode,
            "leaf_names": leaf_names,
        }

    @staticmethod
    def _failed_field_paths(error_store: ErrorStore, *, index: int | None) -> set[str]:
        """Return the internal field paths that currently carry errors.

        Only the roots of error paths are needed because a dependency on a
        nested value always has the failed field as its first segment.
        Nested error mappings (lists/dicts) are traversed recursively.
        """
        errors: typing.Any = error_store.errors
        if index is not None:
            errors = errors.get(index) if isinstance(errors, dict) else None
        if not isinstance(errors, dict):
            return set()

        def walk(node: typing.Any, prefix: tuple[str, ...]) -> None:
            if isinstance(node, dict):
                for key, val in node.items():
                    if key == SCHEMA:
                        continue
                    walk(val, (*prefix, str(key)))
            else:
                failed.add(".".join(prefix))

        failed: set[str] = set()
        walk(errors, ())
        return failed

    @staticmethod
    def _data_key_to_attribute(data_key: str, load_fields: dict) -> str | None:
        for field_name, field_obj in load_fields.items():
            field_data_key = (
                field_obj.data_key if field_obj.data_key is not None else field_name
            )
            if field_data_key == data_key:
                return field_obj.attribute or field_name
        return None

    def _compute_derived_fields(
        self,
        ret_d: dict,
        *,
        error_store: ErrorStore,
        index: int | None,
        baseline: typing.Mapping[str, typing.Any] | None,
    ) -> None:
        """Compute every ready `Derived` field exactly once, in dependency
        order, and run its field-level validators.

        Fields whose dependencies are unavailable (a dependency failed or is
        omitted during partial loading) are skipped silently; independent
        branches still run. Errors from already-failed fields are never
        overwritten, since the error store merges instead of replacing.
        """
        if not self.derived_order:
            return
        if baseline is not None and not isinstance(baseline, Mapping):
            raise TypeError("`baseline` must be a mapping of deserialized values.")

        def lookup(path: str) -> typing.Any:
            value = ma_fields.get_nested_value(ret_d, path)
            if value is missing and baseline is not None:
                value = ma_fields.get_nested_value(baseline, path)
            return value

        failed = self._failed_field_paths(error_store, index=index)
        for attr_name in self.derived_order:
            field_obj = typing.cast(ma_fields.Derived, self.load_fields[attr_name])
            output_key = field_obj.attribute or attr_name
            dependencies: tuple[str, ...] = field_obj._resolved_dependencies
            if output_key in failed or any(
                dep.split(".", 1)[0] in failed for dep in dependencies
            ):
                failed.add(output_key)
                continue
            values: dict[str, typing.Any] = {}
            ready = True
            for dep in dependencies:
                dep_value = lookup(dep)
                if dep_value is missing:
                    ready = False
                    break
                set_value(values, dep, dep_value)
            if not ready:
                # Missing dependency (typically during partial loading):
                # omit this field without flagging an error.
                continue
            plan = field_obj._compute_call_plan
            data_key = (
                field_obj.data_key if field_obj.data_key is not None else attr_name
            )
            try:
                if plan["mapping_mode"]:
                    computed = field_obj.compute(
                        self._DerivedView(ret_d, baseline)
                    )
                else:
                    kwargs = {
                        leaf: ma_fields.get_nested_value(values, dep)
                        for dep, leaf in zip(
                            dependencies, plan["leaf_names"], strict=True
                        )
                    }
                    computed = field_obj.compute(**kwargs)
                # pre_load/post_load callbacks, null checks and validators
                # run here, so derived fields behave like ordinary fields.
                computed = field_obj.deserialize(
                    computed,
                    field_obj.data_key if field_obj.data_key is not None else attr_name,
                    ret_d,
                )
                if computed is missing:
                    continue
            except ValidationError as error:
                data_key = (
                    field_obj.data_key
                    if field_obj.data_key is not None
                    else attr_name
                )
                error_store.store_error(error.messages, data_key, index=index)
                failed.add(output_key)
                continue
            set_value(ret_d, output_key, computed)
            # Run any @validates hooks declared for this derived field, in
            # hook registration order. A failing hook removes the value so
            # downstream derived fields are skipped just as for a failed
            # source field.
            for validator in self._derived_validators.get(attr_name, ()):
                validated_value = self._call_and_store(
                    getter_func=functools.partial(
                        validator, data_key=data_key
                    ),
                    data=computed,
                    field_name=data_key,
                    error_store=error_store,
                    index=index,
                )
                if validated_value is missing:
                    ret_d.pop(output_key, None)
                    failed.add(output_key)
                    break

    class _DerivedView(Mapping):
        """Read-only mapping over computed data plus the caller's baseline.

        The baseline is exposed but never mutated. Keys are internal field
        paths (``attribute`` if set, otherwise the field name).
        """

        def __init__(self, data: Mapping, baseline: Mapping | None):
            self._data = data
            self._baseline = baseline

        def __getitem__(self, key):
            value = ma_fields.get_nested_value(self._data, key)
            if value is missing and self._baseline is not None:
                value = ma_fields.get_nested_value(self._baseline, key)
            if value is missing:
                raise KeyError(key)
            return value

        def __iter__(self):
            yield from self._data
            if self._baseline is not None:
                for key in self._baseline:
                    if key not in self._data:
                        yield key

        def __len__(self):
            return sum(1 for _ in self)

        def __contains__(self, key) -> bool:
            try:
                self[key]
            except KeyError:
                return False
            return True

    def _invoke_dump_processors(
        self, tag: str, data, *, many: bool, original_data=None
    ):
        # The pass_collection post-dump processors may do things like add an envelope, so
        # invoke those after invoking the non-pass_collection processors which will expect
        # to get a list of items.
        data = self._invoke_processors(
            tag,
            pass_collection=False,
            data=data,
            many=many,
            original_data=original_data,
        )
        return self._invoke_processors(
            tag, pass_collection=True, data=data, many=many, original_data=original_data
        )

    def _invoke_load_processors(
        self,
        tag: str,
        data: Mapping[str, typing.Any] | Sequence[Mapping[str, typing.Any]],
        *,
        many: bool,
        original_data,
        partial: bool | types.StrSequenceOrSet | None,
        unknown: types.UnknownOption | None,
    ):
        # This has to invert the order of the dump processors, so run the pass_collection
        # processors first.
        data = self._invoke_processors(
            tag,
            pass_collection=True,
            data=data,
            many=many,
            original_data=original_data,
            partial=partial,
            unknown=unknown,
        )
        return self._invoke_processors(
            tag,
            pass_collection=False,
            data=data,
            many=many,
            original_data=original_data,
            partial=partial,
            unknown=unknown,
        )

    def _invoke_field_validators(
        self,
        *,
        error_store: ErrorStore,
        data,
        many: bool,
        baseline=None,
    ):
        for attr_name, _, validator_kwargs in self._hooks[VALIDATES]:
            validator = getattr(self, attr_name)

            field_names = validator_kwargs["field_names"]

            for field_name in field_names:
                try:
                    field_obj = self.fields[field_name]
                except KeyError as error:
                    if field_name in self.declared_fields:
                        continue
                    raise ValueError(f'"{field_name}" field does not exist.') from error
                if isinstance(field_obj, ma_fields.Derived):
                    continue

                data_key = (
                    field_obj.data_key if field_obj.data_key is not None else field_name
                )
                do_validate = functools.partial(validator, data_key=data_key)

                if many:
                    for idx, item in enumerate(data):
                        self._run_field_validator_on_item(
                            do_validate,
                            field_obj=field_obj,
                            field_name=field_name,
                            data_key=data_key,
                            item=item,
                            error_store=error_store,
                            index=(idx if self.opts.index_errors else None),
                        )
                else:
                    self._run_field_validator_on_item(
                        do_validate,
                        field_obj=field_obj,
                        field_name=field_name,
                        data_key=data_key,
                        item=data,
                        error_store=error_store,
                        index=None,
                    )

        # Ordinary fields are fully deserialized and validated; now compute
        # derived fields (with their own validators) in dependency order.
        if many:
            for idx, item in enumerate(data):
                item_baseline = (
                    baseline[idx] if baseline is not None and idx < len(baseline) else None
                )
                self._compute_derived_fields(
                    item,
                    error_store=error_store,
                    index=(idx if self.opts.index_errors else None),
                    baseline=item_baseline,
                )
        else:
            self._compute_derived_fields(
                data,
                error_store=error_store,
                index=None,
                baseline=baseline,
            )

    def _run_field_validator_on_item(
        self,
        validator,
        *,
        field_obj: Field,
        field_name: str,
        data_key: str,
        item: dict,
        error_store: ErrorStore,
        index: int | None,
    ) -> None:
        try:
            value = item[field_obj.attribute or field_name]
        except KeyError:
            return
        validated_value = self._call_and_store(
            getter_func=validator,
            data=value,
            field_name=data_key,
            error_store=error_store,
            index=index,
        )
        if validated_value is missing:
            item.pop(field_obj.attribute or field_name, None)

    def _invoke_schema_validators(
        self,
        *,
        error_store: ErrorStore,
        pass_collection: bool,
        data,
        original_data,
        many: bool,
        partial: bool | types.StrSequenceOrSet | None,
        field_errors: bool = False,
        unknown: types.UnknownOption | None,
    ):
        for attr_name, hook_many, validator_kwargs in self._hooks[VALIDATES_SCHEMA]:
            if hook_many != pass_collection:
                continue
            validator = getattr(self, attr_name)
            if field_errors and validator_kwargs["skip_on_field_errors"]:
                continue
            pass_original = validator_kwargs.get("pass_original", False)

            if many and not pass_collection:
                for idx, (item, orig) in enumerate(
                    zip(data, original_data, strict=True)
                ):
                    self._run_validator(
                        validator,
                        item,
                        original_data=orig,
                        error_store=error_store,
                        many=many,
                        partial=partial,
                        unknown=unknown,
                        index=idx,
                        pass_original=pass_original,
                    )
            else:
                self._run_validator(
                    validator,
                    data,
                    original_data=original_data,
                    error_store=error_store,
                    many=many,
                    pass_original=pass_original,
                    partial=partial,
                    unknown=unknown,
                )

    def _invoke_processors(
        self,
        tag: str,
        *,
        pass_collection: bool,
        data: Mapping[str, typing.Any] | Sequence[Mapping[str, typing.Any]],
        many: bool,
        original_data=None,
        **kwargs,
    ):
        for attr_name, hook_many, processor_kwargs in self._hooks[tag]:
            if hook_many != pass_collection:
                continue
            # This will be a bound method.
            processor = getattr(self, attr_name)
            pass_original = processor_kwargs.get("pass_original", False)

            if many and not pass_collection:
                if pass_original:
                    data = [
                        processor(item, original, many=many, **kwargs)
                        for item, original in zip_longest(data, original_data)
                    ]
                else:
                    data = [processor(item, many=many, **kwargs) for item in data]
            elif pass_original:
                data = processor(data, original_data, many=many, **kwargs)
            else:
                data = processor(data, many=many, **kwargs)
        return data


BaseSchema = Schema  # for backwards compatibility

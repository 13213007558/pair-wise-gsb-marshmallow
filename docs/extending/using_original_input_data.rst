Using original input data
-------------------------

If you want to use the original, unprocessed input, you can add ``pass_original=True`` to
`post_load <marshmallow.decorators.post_load>` or `validates_schema <marshmallow.decorators.validates_schema>`.

.. code-block:: python

    from marshmallow import Schema, fields, post_load, ValidationError


    class MySchema(Schema):
        foo = fields.Int()
        bar = fields.Int()

        @post_load(pass_original=True)
        def add_baz_to_bar(self, data, original_data, **kwargs):
            baz = original_data.get("baz")
            if baz:
                data["bar"] = data["bar"] + baz
            return data


    schema = MySchema()
    schema.load({"foo": 1, "bar": 2, "baz": 3})
    # {'foo': 1, 'bar': 5}

Reference behavior (default)
============================

By default ``pass_original`` passes the *exact object* handed to
`load <marshmallow.Schema.load>`. No copy is made. This means a
`pre_load <marshmallow.decorators.pre_load>` hook that mutates the input in
place also changes what a later ``pass_original`` hook, `handle_error
<marshmallow.Schema.handle_error>`, or the raised
`ValidationError <marshmallow.exceptions.ValidationError>` observes:

.. code-block:: python

    class MutationSchema(Schema):
        foo = fields.Int()

        @pre_load
        def mutate(self, data, **kwargs):
            data["foo"] = data["foo"].upper()
            return data

        @post_load(pass_original=True)
        def audit(self, data, original_data, **kwargs):
            # original_data is the caller's own dict, already mutated
            assert original_data["foo"] == "BAR"
            return data

Snapshot mode
=============

Set ``snapshot_original=True`` on the schema (constructor argument or
``class Meta`` option) to capture a deep snapshot of the input *before* any
`pre_load <marshmallow.decorators.pre_load>` hook runs. Everything exposed via
``pass_original`` — collection-level and element-level hooks alike — as well as
the ``data`` of `handle_error <marshmallow.Schema.handle_error>` and the raised
`ValidationError <marshmallow.exceptions.ValidationError>` then refers to that
immutable-per-copy snapshot, while the deserialized return value and the
caller's own object are unaffected by the snapshotting:

.. code-block:: python

    class AuditSchema(Schema):
        class Meta:
            snapshot_original = True

        foo = fields.Str()

        @pre_load
        def mutate(self, data, **kwargs):
            data["deep"]["items"].append("added")
            return data

        @post_load(pass_original=True)
        def audit(self, data, original_data, **kwargs):
            # original_data is the snapshot taken at load entry
            assert original_data["deep"]["items"] == ["initial"]
            return data

    payload = {"foo": "bar", "deep": {"items": ["initial"]}}
    AuditSchema().load(payload)
    # the caller's object is still mutated in place by pre_load:
    assert payload["deep"]["items"] == ["initial", "added"]

The same applies with ``many=True``: element-level hooks receive a per-element
snapshot and collection-level hooks (``pass_collection=True``) receive a
snapshot of the whole collection, including on the failing element when an
error is raised. A `Nested <marshmallow.fields.Nested>` schema only snapshots
its own input when it opts in itself; each nesting level therefore captures
the data it actually received.

Costs and supported inputs
==========================

Snapshot mode walks the entire input once per `load <marshmallow.Schema.load>`
/ `validate <marshmallow.Schema.validate>` call — an ``O(n)`` traversal with
``O(n)`` extra memory, where ``n`` is the number of JSON values — and keeps
the snapshot alive for the duration of the call. Only JSON-compatible data is
accepted: ``dict``, ``list``, ``tuple`` (preserved as tuples), ``str``,
``int``, ``float``, ``bool`` and ``None``. Containers are rebuilt directly;
custom ``__copy__``/``__deepcopy__`` protocols are never invoked, so
snapshotting has no side effects on user objects. Unsupported values and
self-referential (cyclic) data raise
`SnapshotError <marshmallow.utils.SnapshotError>` before any hook runs. Leave
the option off (the default) to keep the zero-copy reference behavior.

.. seealso::

   The default behavior for unspecified fields can be controlled with the ``unknown`` option, see :ref:`Handling Unknown Fields <unknown>` for more information.

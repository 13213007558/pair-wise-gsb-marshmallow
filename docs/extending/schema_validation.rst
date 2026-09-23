.. _schema_validation:

Schema-level validation
=======================

You can register schema-level validation functions for a :class:`Schema` using the `marshmallow.validates_schema <marshmallow.decorators.validates_schema>` decorator. By default, schema-level validation errors will be stored on the ``_schema`` key of the errors dictionary.

.. code-block:: python

    from marshmallow import Schema, fields, validates_schema, ValidationError


    class NumberSchema(Schema):
        field_a = fields.Integer()
        field_b = fields.Integer()

        @validates_schema
        def validate_numbers(self, data, **kwargs):
            if data["field_b"] >= data["field_a"]:
                raise ValidationError("field_a must be greater than field_b")


    schema = NumberSchema()
    try:
        schema.load({"field_a": 1, "field_b": 2})
    except ValidationError as err:
        err.messages["_schema"]
    # => ["field_a must be greater than field_b"]

Storing errors on specific fields
---------------------------------

It is possible to report errors on fields and subfields using a `dict`.

When multiple schema-leval validator return errors, the error structures are merged together in the :exc:`ValidationError <marshmallow.exceptions.ValidationError>` raised at the end of the validation.

.. code-block:: python

    from marshmallow import Schema, fields, validates_schema, ValidationError


    class NumberSchema(Schema):
        field_a = fields.Integer()
        field_b = fields.Integer()
        field_c = fields.Integer()
        field_d = fields.Integer()

        @validates_schema
        def validate_lower_bound(self, data, **kwargs):
            errors = {}
            if data["field_b"] <= data["field_a"]:
                errors["field_b"] = ["field_b must be greater than field_a"]
            if data["field_c"] <= data["field_a"]:
                errors["field_c"] = ["field_c must be greater than field_a"]
            if errors:
                raise ValidationError(errors)

        @validates_schema
        def validate_upper_bound(self, data, **kwargs):
            errors = {}
            if data["field_b"] >= data["field_d"]:
                errors["field_b"] = ["field_b must be lower than field_d"]
            if data["field_c"] >= data["field_d"]:
                errors["field_c"] = ["field_c must be lower than field_d"]
            if errors:
                raise ValidationError(errors)


    schema = NumberSchema()
    try:
        schema.load({"field_a": 3, "field_b": 2, "field_c": 1, "field_d": 0})
    except ValidationError as err:
        err.messages
    # => {
    #     'field_b': [
    #         'field_b must be greater than field_a',
    #         'field_b must be lower than field_d'
    #     ],
    #     'field_c': [
    #         'field_c must be greater than field_a',
    #         'field_c must be lower than field_d'
    #     ]
    #    }

Declaring required fields
-------------------------

By default, schema-level validators are skipped whenever *any* field fails
validation (see ``skip_on_field_errors``). If a validator only depends on
specific fields, declare them with ``required_fields``. The validator then runs
as soon as all of its declared fields were successfully deserialized and passed
field-level validation, regardless of errors on unrelated fields.

.. code-block:: python

    from marshmallow import Schema, fields, validates_schema, ValidationError


    class PolicySchema(Schema):
        starts_at = fields.Date(required=True)
        ends_at = fields.Date(required=True)
        phone = fields.Str(required=True)

        @validates_schema(required_fields=("starts_at", "ends_at"))
        def validate_date_order(self, data, **kwargs):
            if data["ends_at"] < data["starts_at"]:
                raise ValidationError("ends_at must not be before starts_at")


    schema = PolicySchema()
    # The invalid phone number does not prevent the date check from running.
    schema.validate(
        {"starts_at": "2026-01-02", "ends_at": "2026-01-01", "phone": 123}
    )
    # => {"phone": ["Not a valid string."],
    #     "_schema": ["ends_at must not be before starts_at"]}

Declared field names are resolved against the schema's bound fields --
honoring inheritance, field overrides, and ``data_key``/``attribute``
mappings -- when the schema is instantiated. Declaring a field that does not
exist raises a ``ValueError`` at that point.

When loading with ``many=True``, readiness is checked per record, so a record
with invalid declared fields does not prevent the validator from running for
the other records.

A declared field that is missing because ``partial`` loading allowed it to be
omitted does not count as successfully validated, and the validator is skipped
by default. Pass ``pass_missing=True`` to invoke the validator anyway; it then
receives a ``missing_fields`` keyword argument with the declared field names
that were absent.

.. code-block:: python

    class PolicySchema(Schema):
        starts_at = fields.Date(required=True)
        ends_at = fields.Date(required=True)

        @validates_schema(required_fields=("starts_at", "ends_at"), pass_missing=True)
        def validate_date_order(self, data, missing_fields, **kwargs):
            if missing_fields:
                return  # not enough data to compare dates
            if data["ends_at"] < data["starts_at"]:
                raise ValidationError("ends_at must not be before starts_at")

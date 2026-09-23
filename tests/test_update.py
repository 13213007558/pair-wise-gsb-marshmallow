import copy
import datetime as dt

import pytest
from marshmallow import EXCLUDE, Schema, fields, post_load, validates_schema
from marshmallow.exceptions import ValidationError


class TrackedField(fields.Field):
    """Custom field that records every raw value it deserializes."""

    default_error_messages = {"invalid": "Not a valid secret."}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.deserialized = []

    def _deserialize(self, value, attr, data, **kwargs):
        self.deserialized.append(value)
        if not isinstance(value, str):
            raise self.make_error("invalid")
        return f"decrypted:{value}"

    def _serialize(self, value, attr, obj, **kwargs):
        return value


class InnerSchema(Schema):
    leaf = fields.Int(required=True)
    secret = TrackedField()
    other = fields.Str()


class EventSchema(Schema):
    starts_at = fields.Date(required=True)
    ends_at = fields.Date()
    secret = TrackedField(data_key="token", attribute="credentials")
    nested = fields.Nested(InnerSchema, allow_none=True)
    tags = fields.List(fields.Str())

    @validates_schema
    def check_date_range(self, data, **kwargs):
        starts = data.get("starts_at")
        ends = data.get("ends_at")
        if starts and ends and ends < starts:
            raise ValidationError("Must be after starts_at.", "ends_at")


@pytest.fixture
def schema():
    return EventSchema()


@pytest.fixture
def old(schema):
    return schema.load(
        {
            "starts_at": "2026-09-01",
            "token": "abc",
            "nested": {"leaf": 1, "secret": "x", "other": "keep"},
            "tags": ["a", "b"],
        }
    )


def secret_tracker(schema):
    return schema.fields["secret"].deserialized


def nested_secret_tracker(schema):
    return schema.fields["nested"].schema.fields["secret"].deserialized


class TestLoadUpdate:
    def test_merges_converted_patch_into_instance(self, schema, old):
        result, paths = schema.load_update({"ends_at": "2026-10-01"}, old)

        assert result == {
            "starts_at": dt.date(2026, 9, 1),
            "ends_at": dt.date(2026, 10, 1),
            "credentials": "decrypted:abc",
            "nested": {"leaf": 1, "secret": "decrypted:x", "other": "keep"},
            "tags": ["a", "b"],
        }
        assert paths == {"ends_at"}

    def test_missing_field_keeps_old_value_and_shares_container(self, schema, old):
        result, paths = schema.load_update({"ends_at": "2026-10-01"}, old)

        assert result["nested"] is old["nested"]
        assert result["tags"] is old["tags"]
        assert paths == {"ends_at"}

    def test_old_values_are_not_deserialized_again(self, schema, old):
        secret_tracker(schema).clear()
        nested_secret_tracker(schema).clear()

        schema.load_update({"ends_at": "2026-10-01"}, old)

        assert secret_tracker(schema) == []
        assert nested_secret_tracker(schema) == []

    def test_custom_fields_deserialize_exactly_the_submitted_values(self, schema, old):
        secret_tracker(schema).clear()
        nested_secret_tracker(schema).clear()

        result, paths = schema.load_update(
            {"token": "def", "nested": {"secret": "y"}}, old
        )

        assert secret_tracker(schema) == ["def"]
        assert nested_secret_tracker(schema) == ["y"]
        assert result["credentials"] == "decrypted:def"
        assert result["nested"]["secret"] == "decrypted:y"
        assert result["nested"]["leaf"] == 1
        assert paths == {"credentials", "nested.secret"}

    def test_nested_single_leaf_update(self, schema, old):
        result, paths = schema.load_update({"nested": {"leaf": 2}}, old)

        assert result["nested"] == {
            "leaf": 2,
            "secret": "decrypted:x",
            "other": "keep",
        }
        assert paths == {"nested.leaf"}
        # The old nested mapping is untouched
        assert old["nested"] == {"leaf": 1, "secret": "decrypted:x", "other": "keep"}

    def test_explicit_null_replaces_value(self, schema, old):
        result, paths = schema.load_update({"nested": None}, old)

        assert result["nested"] is None
        assert paths == {"nested"}

    def test_empty_mapping_leaves_nested_value_unchanged(self, schema, old):
        result, paths = schema.load_update({"nested": {}}, old)

        assert result["nested"] == {"leaf": 1, "secret": "decrypted:x", "other": "keep"}
        assert paths == {"nested"}

    def test_lists_are_replaced_wholesale(self, schema, old):
        result, paths = schema.load_update({"tags": ["c"]}, old)

        assert result["tags"] == ["c"]
        assert paths == {"tags"}

    def test_data_key_and_attribute_are_honored(self, schema, old):
        result, paths = schema.load_update({"token": "xyz"}, old)

        assert result["credentials"] == "decrypted:xyz"
        assert "token" not in result
        assert paths == {"credentials"}

    def test_schema_level_validation_runs_on_merged_data(self, schema, old):
        # ends_at is compared against the *old* starts_at from the instance
        with pytest.raises(ValidationError) as excinfo:
            schema.load_update({"ends_at": "2026-08-01"}, old)

        assert excinfo.value.messages == {"ends_at": ["Must be after starts_at."]}

    def test_field_error_keyed_by_client_data_key(self, schema, old):
        with pytest.raises(ValidationError) as excinfo:
            schema.load_update({"token": 123}, old)

        assert excinfo.value.messages == {"token": ["Not a valid secret."]}

    def test_nested_error_keyed_by_client_patch(self, schema, old):
        with pytest.raises(ValidationError) as excinfo:
            schema.load_update({"nested": {"leaf": "not-an-int"}}, old)

        assert excinfo.value.messages == {"nested": {"leaf": ["Not a valid integer."]}}

    @pytest.mark.parametrize(
        "patch",
        [
            {"tags": ["new"], "ends_at": "2026-08-01"},  # schema-level failure
            {"tags": ["new"], "token": 123},  # field-level failure
        ],
    )
    def test_failed_update_does_not_mutate_instance(self, schema, old, patch):
        snapshot = copy.deepcopy(old)
        old_tags = old["tags"]
        old_nested = old["nested"]

        with pytest.raises(ValidationError):
            schema.load_update(patch, old)

        assert old == snapshot
        assert old["tags"] is old_tags
        assert old["nested"] is old_nested

    def test_unknown_field_raises_by_default(self, schema, old):
        with pytest.raises(ValidationError) as excinfo:
            schema.load_update({"bogus": 1}, old)

        assert excinfo.value.messages == {"bogus": ["Unknown field."]}

    def test_unknown_field_excluded(self, schema, old):
        result, paths = schema.load_update({"bogus": 1}, old, unknown=EXCLUDE)

        assert result == old
        assert paths == set()

    def test_post_load_runs_on_merged_result(self, old):
        class WrappedSchema(EventSchema):
            @post_load
            def mark(self, data, **kwargs):
                return {"wrapped": data}

        schema = WrappedSchema()
        result, paths = schema.load_update({"ends_at": "2026-10-01"}, old)

        assert result["wrapped"]["ends_at"] == dt.date(2026, 10, 1)
        assert result["wrapped"]["starts_at"] == dt.date(2026, 9, 1)
        assert paths == {"ends_at"}

"""Tests for `Schema.update_load`."""

import copy
import datetime as dt

import pytest
from marshmallow import (
    EXCLUDE,
    INCLUDE,
    Schema,
    ValidationError,
    fields,
    post_load,
    validates,
    validates_schema,
)


class TrackedField(fields.Field):
    """Custom field that records every value it deserializes."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.deserialized = []

    def _deserialize(self, value, attr, data, **kwargs):
        self.deserialized.append(value)
        return f"decrypted:{value}"

    def _serialize(self, value, attr, obj, **kwargs):
        return value


class SecretSchema(Schema):
    starts_at = fields.Date(required=True)
    ends_at = fields.Date()
    secret = TrackedField()

    @validates_schema
    def check_range(self, data, **kwargs):
        if (
            "starts_at" in data
            and "ends_at" in data
            and data["ends_at"] < data["starts_at"]
        ):
            raise ValidationError(
                "ends_at must not be before starts_at", field_name="ends_at"
            )


class ChildSchema(Schema):
    leaf_a = TrackedField()
    leaf_b = TrackedField()


class ParentSchema(Schema):
    name = fields.Str()
    child = fields.Nested(ChildSchema)


@pytest.fixture
def secret_schema():
    return SecretSchema()


@pytest.fixture
def secret_field(secret_schema):
    return secret_schema.fields["secret"]


class TestUpdateLoadBasic:
    def test_merges_patch_into_old_and_returns_paths(self, secret_schema):
        old = secret_schema.load({"starts_at": "2026-09-01", "secret": "hunter2"})
        result, paths = secret_schema.update_load(old, {"ends_at": "2026-10-01"})

        assert result == {
            "starts_at": dt.date(2026, 9, 1),
            "ends_at": dt.date(2026, 10, 1),
            "secret": "decrypted:hunter2",
        }
        assert paths == {"ends_at"}

    def test_old_object_is_not_mutated_on_success(self, secret_schema):
        old = secret_schema.load({"starts_at": "2026-09-01", "secret": "hunter2"})
        snapshot = copy.deepcopy(old)
        result, _ = secret_schema.update_load(old, {"ends_at": "2026-10-01"})

        assert old == snapshot
        assert result is not old

    def test_old_values_are_not_deserialized_again(self, secret_schema, secret_field):
        old = secret_schema.load({"starts_at": "2026-09-01", "secret": "hunter2"})
        secret_field.deserialized.clear()

        secret_schema.update_load(old, {"ends_at": "2026-10-01"})

        # The old secret must not go through the decryptor again.
        assert secret_field.deserialized == []

    def test_empty_patch_returns_old_values_and_no_paths(self, secret_schema):
        old = secret_schema.load({"starts_at": "2026-09-01", "secret": "hunter2"})
        result, paths = secret_schema.update_load(old, {})

        assert result == old
        assert paths == set()

    def test_load_default_is_not_applied_to_missing_fields(self):
        class DefaultSchema(Schema):
            level = fields.Int(load_default=99)

        result, paths = DefaultSchema().update_load({"level": 1}, {})

        assert result == {"level": 1}
        assert paths == set()

    def test_non_mapping_object_raises_type_error(self, secret_schema):
        with pytest.raises(TypeError, match="mapping"):
            secret_schema.update_load([("starts_at", "x")], {})


class TestSchemaLevelValidation:
    def test_old_values_participate_in_range_check(self, secret_schema):
        old = secret_schema.load({"starts_at": "2026-09-01", "secret": "hunter2"})

        with pytest.raises(ValidationError) as excinfo:
            secret_schema.update_load(old, {"ends_at": "2026-08-01"})

        # Error location points at the client patch key.
        assert excinfo.value.messages == {
            "ends_at": ["ends_at must not be before starts_at"]
        }

    def test_range_check_passes_with_merged_values(self, secret_schema):
        old = secret_schema.load({"starts_at": "2026-09-01", "secret": "hunter2"})
        result, _ = secret_schema.update_load(old, {"ends_at": "2026-10-01"})
        assert result["ends_at"] == dt.date(2026, 10, 1)

    def test_field_validators_run_on_patch_values(self):
        class FutureSchema(Schema):
            starts_at = fields.Date()
            ends_at = fields.Date()

            @validates("ends_at")
            def check_not_ancient(self, value, **kwargs):
                if value < dt.date(2020, 1, 1):
                    raise ValidationError("ends_at is too far in the past")

        schema = FutureSchema()
        with pytest.raises(ValidationError) as excinfo:
            schema.update_load(
                {"starts_at": dt.date(2026, 9, 1)}, {"ends_at": "2019-01-01"}
            )

        assert excinfo.value.messages == {"ends_at": ["ends_at is too far in the past"]}

    def test_post_load_runs_on_merged_result(self):
        class TotalSchema(Schema):
            a = fields.Int()
            b = fields.Int()

            @post_load
            def add_total(self, data, **kwargs):
                data["total"] = data["a"] + data["b"]
                return data

        result, _ = TotalSchema().update_load({"a": 1, "b": 2}, {"b": 5})

        assert result == {"a": 1, "b": 5, "total": 6}


class TestMissingNullAndEmptyDict:
    def test_explicit_null_replaces_value(self):
        class NullableSchema(Schema):
            nickname = fields.Str(allow_none=True)
            name = fields.Str()

        result, paths = NullableSchema().update_load(
            {"nickname": "bob", "name": "Bob"}, {"nickname": None}
        )

        assert result == {"nickname": None, "name": "Bob"}
        assert paths == {"nickname"}

    def test_explicit_null_on_non_nullable_field_errors(self, secret_schema):
        old = secret_schema.load({"starts_at": "2026-09-01", "secret": "hunter2"})

        with pytest.raises(ValidationError) as excinfo:
            secret_schema.update_load(old, {"secret": None})

        assert excinfo.value.messages == {"secret": ["Field may not be null."]}

    def test_empty_dict_on_nested_field_is_a_noop_merge(self):
        schema = ParentSchema()
        old = {"name": "parent", "child": {"leaf_a": "a", "leaf_b": "b"}}

        result, paths = schema.update_load(old, {"child": {}})

        assert result == old
        assert result["child"] == {"leaf_a": "a", "leaf_b": "b"}
        # The submitted (but empty) nested patch is still reported.
        assert paths == {"child"}


class TestNestedMerge:
    def test_single_leaf_change_recurses_and_keeps_siblings(self):
        schema = ParentSchema()
        leaf_a = schema.fields["child"].schema.fields["leaf_a"]
        leaf_b = schema.fields["child"].schema.fields["leaf_b"]
        old = schema.load({"name": "parent", "child": {"leaf_a": "a", "leaf_b": "b"}})
        leaf_a.deserialized.clear()
        leaf_b.deserialized.clear()

        result, paths = schema.update_load(old, {"child": {"leaf_b": "new"}})

        assert result == {
            "name": "parent",
            "child": {"leaf_a": "decrypted:a", "leaf_b": "decrypted:new"},
        }
        assert paths == {"child.leaf_b"}
        # Only the submitted leaf was converted by the custom field.
        assert leaf_a.deserialized == []
        assert leaf_b.deserialized == ["new"]

    def test_nested_old_dict_is_not_mutated(self):
        schema = ParentSchema()
        old = {"name": "parent", "child": {"leaf_a": "a", "leaf_b": "b"}}
        child_ref = old["child"]

        result, _ = schema.update_load(old, {"child": {"leaf_b": "new"}})

        assert child_ref == {"leaf_a": "a", "leaf_b": "b"}
        assert result["child"] is not child_ref

    def test_nested_field_created_when_missing_from_old(self):
        schema = ParentSchema()
        result, paths = schema.update_load(
            {"name": "parent"}, {"child": {"leaf_a": "a"}}
        )

        assert result == {"name": "parent", "child": {"leaf_a": "decrypted:a"}}
        assert paths == {"child.leaf_a"}

    def test_nested_error_locations_use_client_keys(self):
        class DateChild(Schema):
            day = fields.Date()

        class DateParent(Schema):
            child = fields.Nested(DateChild)

        with pytest.raises(ValidationError) as excinfo:
            DateParent().update_load({}, {"child": {"day": "not-a-date"}})

        assert excinfo.value.messages == {"child": {"day": ["Not a valid date."]}}


class TestListReplace:
    def test_lists_are_replaced_wholesale(self):
        class ListSchema(Schema):
            tags = fields.List(fields.Int())

        old = {"tags": [1, 2, 3]}
        result, paths = ListSchema().update_load(old, {"tags": [4]})

        assert result == {"tags": [4]}
        assert paths == {"tags"}
        # The old list object is left alone.
        assert old == {"tags": [1, 2, 3]}

    def test_list_of_nested_is_replaced_wholesale(self):
        class ItemSchema(Schema):
            value = fields.Int()

        class BagSchema(Schema):
            items = fields.List(fields.Nested(ItemSchema))

        old = {"items": [{"value": 1}, {"value": 2}]}
        result, paths = BagSchema().update_load(old, {"items": [{"value": 9}]})

        assert result == {"items": [{"value": 9}]}
        assert paths == {"items"}


class TestDataKeyAndAttribute:
    @pytest.fixture
    def schema(self):
        class KeySchema(Schema):
            starts_at = fields.Date(data_key="startsAt", required=True)
            display = fields.Str(data_key="displayName", attribute="display_name")

        return KeySchema()

    def test_patch_uses_data_key_result_uses_attribute(self, schema):
        old = schema.load({"startsAt": "2026-09-01", "displayName": "old"})

        result, paths = schema.update_load(old, {"displayName": "new"})

        assert result == {
            "starts_at": dt.date(2026, 9, 1),
            "display_name": "new",
        }
        # Paths are reported with the client-facing data_key.
        assert paths == {"displayName"}

    def test_errors_are_keyed_by_data_key(self, schema):
        old = schema.load({"startsAt": "2026-09-01"})

        with pytest.raises(ValidationError) as excinfo:
            schema.update_load(old, {"startsAt": "not-a-date"})

        assert excinfo.value.messages == {"startsAt": ["Not a valid date."]}


class TestUnknownFields:
    def test_unknown_fields_raise_by_default(self, secret_schema):
        old = secret_schema.load({"starts_at": "2026-09-01"})

        with pytest.raises(ValidationError) as excinfo:
            secret_schema.update_load(old, {"bogus": 1})

        assert excinfo.value.messages == {"bogus": ["Unknown field."]}

    def test_unknown_fields_excluded(self, secret_schema):
        old = secret_schema.load({"starts_at": "2026-09-01"})

        result, paths = secret_schema.update_load(
            old, {"bogus": 1, "ends_at": "2026-10-01"}, unknown=EXCLUDE
        )

        assert "bogus" not in result
        assert paths == {"ends_at"}

    def test_unknown_fields_included(self, secret_schema):
        old = secret_schema.load({"starts_at": "2026-09-01"})

        result, paths = secret_schema.update_load(old, {"bogus": 1}, unknown=INCLUDE)

        assert result["bogus"] == 1
        assert paths == {"bogus"}


class TestFailureAtomicity:
    def test_failed_update_leaves_old_object_untouched(self):
        class RangeChild(Schema):
            leaf_a = TrackedField()
            leaf_b = TrackedField()

        class RangeParent(Schema):
            starts_at = fields.Date()
            ends_at = fields.Date()
            child = fields.Nested(RangeChild)
            tags = fields.List(fields.Int())

            @validates_schema
            def check_range(self, data, **kwargs):
                if (
                    "starts_at" in data
                    and "ends_at" in data
                    and data["ends_at"] < data["starts_at"]
                ):
                    raise ValidationError("bad range", field_name="ends_at")

        schema = RangeParent()
        old = {
            "starts_at": dt.date(2026, 9, 1),
            "child": {"leaf_a": "a", "leaf_b": "b"},
            "tags": [1, 2, 3],
        }
        snapshot = copy.deepcopy(old)
        child_ref = old["child"]
        tags_ref = old["tags"]

        with pytest.raises(ValidationError):
            schema.update_load(
                old,
                {"ends_at": "2026-08-01", "child": {"leaf_b": "x"}, "tags": [9]},
            )

        assert old == snapshot
        assert old["child"] is child_ref
        assert old["tags"] is tags_ref

    def test_failed_deserialization_leaves_old_object_untouched(self):
        schema = ParentSchema()
        old = {"name": "parent", "child": {"leaf_a": "a", "leaf_b": "b"}}
        snapshot = copy.deepcopy(old)

        with pytest.raises(ValidationError):
            schema.update_load(old, {"child": {"leaf_b": None}})

        assert old == snapshot

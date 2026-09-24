"""Tests for Schema.load_rows (row-by-row fault-tolerant loading)."""

import pytest

from marshmallow import (
    IndexedRows,
    Schema,
    ValidationError,
    fields,
    missing,
    post_load,
    pre_load,
    validates,
    validates_schema,
)
from marshmallow.exceptions import MarshmallowError


class SimpleSchema(Schema):
    name = fields.Str(required=True)
    age = fields.Int(required=True)


def test_load_rows_returns_objects_or_errors_in_input_order():
    constructed = []

    class UserSchema(SimpleSchema):
        @post_load
        def make_user(self, item, **kwargs):
            user = (item["name"], item["age"])
            constructed.append(user)
            return user

    schema = UserSchema()
    data = [{"name": f"user{i}", "age": i} for i in range(500)]
    data[7] = {"name": "bad", "age": "not-an-int"}
    data[301] = {"name": "incomplete"}  # missing required "age"

    result = schema.load_rows(data)

    assert result.batch_errors == []
    assert len(result.rows) == 500
    for i, row in enumerate(result.rows):
        if i == 7:
            assert isinstance(row, ValidationError)
            assert row.messages == {"age": ["Not a valid integer."]}
        elif i == 301:
            assert isinstance(row, ValidationError)
            assert row.messages == {"age": ["Missing data for required field."]}
        else:
            assert row == (f"user{i}", i)
    # failed rows never entered object construction, and every constructed
    # object corresponds to its source row
    assert len(constructed) == 498
    assert constructed == [
        (f"user{i}", i) for i in range(500) if i not in (7, 301)
    ]


def test_load_rows_runs_field_and_schema_validators_per_row():
    class UserSchema(SimpleSchema):
        @validates("age")
        def check_age(self, value, **kwargs):
            if value == 13:
                raise ValidationError("unlucky")

        @validates_schema
        def check_name(self, item, **kwargs):
            if item.get("name") == "root":
                raise ValidationError({"name": ["reserved"]})

    result = UserSchema().load_rows(
        [
            {"name": "a", "age": 1},
            {"name": "b", "age": 13},
            {"name": "root", "age": 5},
            {"name": "c", "age": 3},
        ]
    )

    assert result.batch_errors == []
    assert result.rows[0] == {"name": "a", "age": 1}
    assert isinstance(result.rows[1], ValidationError)
    assert result.rows[1].messages == {"age": ["unlucky"]}
    assert isinstance(result.rows[2], ValidationError)
    assert result.rows[2].messages == {"name": ["reserved"]}
    assert result.rows[3] == {"name": "c", "age": 3}


def test_load_rows_post_load_validation_error_marks_only_that_row():
    class UserSchema(SimpleSchema):
        @post_load
        def check(self, item, **kwargs):
            if item["name"] == "cursed":
                raise ValidationError({"name": ["cursed name"]})
            return item["name"]

    result = UserSchema().load_rows(
        [{"name": "a", "age": 1}, {"name": "cursed", "age": 2}, {"name": "b", "age": 3}]
    )

    assert result.batch_errors == []
    assert result.rows[0] == "a"
    assert isinstance(result.rows[1], ValidationError)
    assert result.rows[1].messages == {"name": ["cursed name"]}
    assert result.rows[2] == "b"


def test_load_rows_non_validation_error_propagates():
    class UserSchema(SimpleSchema):
        @post_load
        def boom(self, item, **kwargs):
            if item["name"] == "boom":
                raise ValueError("boom")
            return item

    with pytest.raises(ValueError, match="boom"):
        UserSchema().load_rows([{"name": "ok", "age": 1}, {"name": "boom", "age": 2}])


def test_load_rows_hooks_receive_many_true():
    seen = []

    class UserSchema(SimpleSchema):
        @post_load
        def record(self, item, many, **kwargs):
            seen.append(many)
            return item

    UserSchema().load_rows([{"name": "a", "age": 1}])
    assert seen == [True]


def test_load_rows_unknown_fields_are_per_row_errors():
    result = SimpleSchema().load_rows(
        [{"name": "a", "age": 1}, {"name": "b", "age": 2, "extra": 1}]
    )
    assert result.rows[0] == {"name": "a", "age": 1}
    assert isinstance(result.rows[1], ValidationError)
    assert result.rows[1].messages == {"extra": ["Unknown field."]}


def test_load_rows_collection_validator_indexed_and_batch_errors():
    class UserSchema(SimpleSchema):
        @validates_schema(pass_collection=True)
        def check_collection(self, items, **kwargs):
            errors = {}
            for i, item in enumerate(items):
                if item.get("name") == "dup":
                    errors[i] = {"name": ["duplicate"]}
            if len(items) > 2:
                errors["_schema"] = ["too many rows"]
            if errors:
                raise ValidationError(errors)

    result = UserSchema().load_rows(
        [
            {"name": "a", "age": 1},
            {"name": "dup", "age": 2},
            {"name": "c", "age": 3},
        ]
    )

    # the indexed error is attributed to its row; the rest of the batch
    # is still constructed
    assert result.rows[0] == {"name": "a", "age": 1}
    assert isinstance(result.rows[1], ValidationError)
    assert result.rows[1].messages == {"name": ["duplicate"]}
    assert result.rows[2] == {"name": "c", "age": 3}
    # the unattributable error is returned as a batch error
    assert len(result.batch_errors) == 1
    assert result.batch_errors[0].messages == {"_schema": ["too many rows"]}


def test_load_rows_collection_validator_out_of_range_index_is_batch_error():
    class UserSchema(SimpleSchema):
        @validates_schema(pass_collection=True)
        def check_collection(self, items, **kwargs):
            raise ValidationError({99: ["no such row"]})

    result = UserSchema().load_rows([{"name": "a", "age": 1}])

    assert result.rows[0] == {"name": "a", "age": 1}
    assert len(result.batch_errors) == 1
    assert result.batch_errors[0].messages == {99: ["no such row"]}


def test_load_rows_pre_load_reorder_with_index_map_preserves_provenance():
    constructed = []

    class UserSchema(SimpleSchema):
        @pre_load(pass_collection=True)
        def reverse_rows(self, data, **kwargs):
            return IndexedRows(
                data=list(reversed(data)),
                index_map=list(reversed(range(len(data)))),
            )

        @post_load
        def make_user(self, item, **kwargs):
            constructed.append(item["name"])
            return item["name"]

    data = [
        {"name": "a", "age": 1},
        {"name": "bad", "age": "not-an-int"},
        {"name": "c", "age": 3},
        {"name": "d", "age": 4},
    ]
    result = UserSchema().load_rows(data)

    # rows are reported against the original input order, not the
    # reordered processing order
    assert result.rows[0] == "a"
    assert isinstance(result.rows[1], ValidationError)
    assert result.rows[1].messages == {"age": ["Not a valid integer."]}
    assert result.rows[1].data == {"name": "bad", "age": "not-an-int"}
    assert result.rows[2] == "c"
    assert result.rows[3] == "d"
    # construction happened in processed (reversed) order, skipping the
    # failed row
    assert constructed == ["d", "c", "a"]


def test_load_rows_collection_validator_indices_follow_index_map():
    class UserSchema(SimpleSchema):
        @pre_load(pass_collection=True)
        def reverse_rows(self, data, **kwargs):
            return IndexedRows(
                data=list(reversed(data)),
                index_map=list(reversed(range(len(data)))),
            )

        @validates_schema(pass_collection=True)
        def reject_first(self, items, **kwargs):
            # "d" is at position 0 of the processed (reversed) collection
            raise ValidationError({0: {"name": ["first row rejected"]}})

    result = UserSchema().load_rows(
        [{"name": "a", "age": 1}, {"name": "d", "age": 4}]
    )

    assert result.rows[0] == {"name": "a", "age": 1}
    assert isinstance(result.rows[1], ValidationError)
    assert result.rows[1].messages == {"name": ["first row rejected"]}


def test_load_rows_pre_load_unmapped_length_change_is_rejected():
    class UserSchema(SimpleSchema):
        @pre_load(pass_collection=True)
        def drop_empty(self, data, **kwargs):
            return [row for row in data if row]

    with pytest.raises(MarshmallowError, match="IndexedRows"):
        UserSchema().load_rows([{"name": "a", "age": 1}, {}, {"name": "b", "age": 2}])


def test_load_rows_pre_load_removed_rows_are_missing():
    class UserSchema(SimpleSchema):
        @pre_load(pass_collection=True)
        def drop_middle(self, data, **kwargs):
            return IndexedRows(data=[data[0], data[2]], index_map=[0, 2])

    result = UserSchema().load_rows(
        [{"name": "a", "age": 1}, {"name": "b", "age": 2}, {"name": "c", "age": 3}]
    )

    assert result.rows[0] == {"name": "a", "age": 1}
    assert result.rows[1] is missing
    assert result.rows[2] == {"name": "c", "age": 3}


def test_load_rows_pre_load_invalid_index_map_is_rejected():
    class UserSchema(SimpleSchema):
        @pre_load(pass_collection=True)
        def bad_map(self, data, **kwargs):
            return IndexedRows(data=list(data), index_map=[0, 0])

    with pytest.raises(MarshmallowError, match="duplicate"):
        UserSchema().load_rows([{"name": "a", "age": 1}, {"name": "b", "age": 2}])


def test_load_rows_collection_pre_load_validation_error_is_batch_error():
    class UserSchema(SimpleSchema):
        @pre_load(pass_collection=True)
        def fail(self, data, **kwargs):
            raise ValidationError("bad envelope")

    result = UserSchema().load_rows([{"name": "a", "age": 1}, {"name": "b", "age": 2}])

    assert len(result.batch_errors) == 1
    assert result.batch_errors[0].messages == ["bad envelope"]
    assert all(isinstance(row, ValidationError) for row in result.rows)


def test_load_rows_collection_post_load_is_rejected():
    class UserSchema(SimpleSchema):
        @post_load(pass_collection=True)
        def envelope(self, data, **kwargs):
            return data

    with pytest.raises(MarshmallowError, match="pass_collection"):
        UserSchema().load_rows([{"name": "a", "age": 1}])


def test_load_rows_non_collection_input_is_not_treated_as_single_item():
    schema = SimpleSchema()
    with pytest.raises(ValidationError):
        schema.load_rows({"name": "a", "age": 1})
    with pytest.raises(ValidationError):
        schema.load_rows("not a collection")

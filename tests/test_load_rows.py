import pytest

from marshmallow import (
    IndexedRows,
    Schema,
    ValidationError,
    fields,
    post_load,
    pre_load,
    validates,
    validates_schema,
)
from marshmallow.exceptions import MarshmallowError


class User:
    def __init__(self, name, age):
        self.name = name
        self.age = age

    def __eq__(self, other):
        return (
            isinstance(other, User)
            and self.name == other.name
            and self.age == other.age
        )


def test_load_rows_returns_objects_and_errors_per_row():
    constructed = []

    class UserSchema(Schema):
        name = fields.Str(required=True)
        age = fields.Int(required=True)

        @post_load
        def make_user(self, data, **kwargs):
            user = User(**data)
            constructed.append(user)
            return user

    data = [{"name": f"user{i}", "age": i} for i in range(500)]
    data[7] = {"name": "bad"}  # missing required age
    data[250] = {"name": "bad", "age": "not-an-int"}

    result = UserSchema().load_rows(data)

    # the batch is reported row by row, never as a single success
    assert len(result.rows) == 500
    assert result.errors == []
    assert not result.ok
    # only valid rows entered object construction
    assert len(constructed) == 498
    for i, row in enumerate(result.rows):
        if i in (7, 250):
            assert row.value is None
            assert isinstance(row.error, ValidationError)
            assert row.error.data == data[i]
        else:
            assert row.ok
            assert row.value == User(name=f"user{i}", age=i)
    assert result.rows[7].error.messages == {
        "age": ["Missing data for required field."]
    }
    assert result.rows[250].error.messages == {"age": ["Not a valid integer."]}


def test_load_rows_all_valid():
    class UserSchema(Schema):
        name = fields.Str(required=True)

        @post_load
        def make_user(self, data, **kwargs):
            return User(age=0, **data)

    result = UserSchema().load_rows([{"name": "a"}, {"name": "b"}])
    assert result.ok
    assert result.errors == []
    assert [row.value for row in result.rows] == [
        User(name="a", age=0),
        User(name="b", age=0),
    ]


def test_post_load_validation_error_marks_only_that_row():
    class UserSchema(Schema):
        name = fields.Str(required=True)
        age = fields.Int(required=True)

        @post_load
        def make_user(self, data, **kwargs):
            if data["age"] < 0:
                raise ValidationError("age must be non-negative")
            return User(**data)

    result = UserSchema().load_rows(
        [{"name": "a", "age": 1}, {"name": "b", "age": -1}, {"name": "c", "age": 3}]
    )

    assert [row.ok for row in result.rows] == [True, False, True]
    assert result.rows[1].error.messages == {"_schema": ["age must be non-negative"]}
    assert result.rows[0].value == User(name="a", age=1)
    assert result.rows[2].value == User(name="c", age=3)


def test_unexpected_exception_in_post_load_propagates():
    class UserSchema(Schema):
        age = fields.Int(required=True)

        @post_load
        def boom(self, data, **kwargs):
            if data["age"] == 13:
                raise ValueError("boom")
            return data

    with pytest.raises(ValueError, match="boom"):
        UserSchema().load_rows([{"age": 1}, {"age": 13}])


def test_field_and_schema_validators_run_per_row():
    class UserSchema(Schema):
        age = fields.Int(required=True)

        @validates("age")
        def check_age(self, value, **kwargs):
            if value > 120:
                raise ValidationError("too old")

        @validates_schema(skip_on_field_errors=False)
        def check_teen(self, data, **kwargs):
            if data.get("age") == 13:
                raise ValidationError({"age": ["unlucky"]})

    result = UserSchema().load_rows([{"age": 30}, {"age": 200}, {"age": 13}])

    assert result.rows[0].ok
    assert result.rows[1].error.messages == {"age": ["too old"]}
    assert result.rows[2].error.messages == {"age": ["unlucky"]}


def test_pre_load_reorder_with_index_map_preserves_provenance():
    constructed = []

    class UserSchema(Schema):
        name = fields.Str(required=True)
        age = fields.Int(required=True)

        @pre_load(pass_collection=True)
        def reverse(self, data, many, **kwargs):
            indices = list(reversed(range(len(data))))
            return IndexedRows([data[i] for i in indices], indices)

        @post_load
        def make_user(self, data, **kwargs):
            user = User(**data)
            constructed.append(user)
            return user

    data = [{"name": f"user{i}", "age": i} for i in range(10)]
    data[3] = {"name": "bad"}  # invalid row

    result = UserSchema().load_rows(data)

    assert len(result.rows) == 10
    # the error is attributed to the original input position,
    # not the position after reordering
    assert result.rows[3].error is not None
    assert len(constructed) == 9
    for i, row in enumerate(result.rows):
        if i == 3:
            continue
        assert row.ok
        assert row.value == User(name=f"user{i}", age=i)


def test_pre_load_drop_without_index_map_is_rejected():
    class UserSchema(Schema):
        name = fields.Str()

        @pre_load(pass_collection=True)
        def drop_first(self, data, many, **kwargs):
            return data[1:]

    with pytest.raises(MarshmallowError, match="IndexedRows"):
        UserSchema().load_rows([{"name": "a"}, {"name": "b"}])


def test_pre_load_drop_with_index_map_leaves_dropped_rows_empty():
    class UserSchema(Schema):
        name = fields.Str()

        @pre_load(pass_collection=True)
        def drop_dupes(self, data, many, **kwargs):
            return IndexedRows([data[0], data[2]], [0, 2])

    result = UserSchema().load_rows([{"name": "a"}, {"name": "b"}, {"name": "c"}])

    assert len(result.rows) == 3
    assert result.rows[0].value == {"name": "a"}
    assert result.rows[1].value is None
    assert result.rows[1].error is None
    assert result.rows[2].value == {"name": "c"}


def test_pre_load_invalid_index_map_is_rejected():
    class UserSchema(Schema):
        name = fields.Str()

        @pre_load(pass_collection=True)
        def bad_map(self, data, many, **kwargs):
            return IndexedRows([data[0]], [5])

    with pytest.raises(MarshmallowError, match="index mapping"):
        UserSchema().load_rows([{"name": "a"}])


def test_collection_validation_errors_attributed_to_original_indices():
    constructed = []

    class UserSchema(Schema):
        name = fields.Str(required=True)

        @pre_load(pass_collection=True)
        def reverse(self, data, many, **kwargs):
            indices = list(reversed(range(len(data))))
            return IndexedRows([data[i] for i in indices], indices)

        @validates_schema(pass_collection=True)
        def check_rows(self, data, many, **kwargs):
            raise ValidationError(
                {0: {"name": ["duplicate"]}, "_schema": ["batch issue"]}
            )

        @post_load
        def make_user(self, data, **kwargs):
            user = User(age=0, **data)
            constructed.append(user)
            return user

    result = UserSchema().load_rows([{"name": "a"}, {"name": "b"}, {"name": "c"}])

    # processed position 0 maps back to original index 2 after reversing
    assert [row.ok for row in result.rows] == [True, True, False]
    assert result.rows[2].error.messages == {"name": ["duplicate"]}
    # unattributable errors become batch errors
    assert result.errors == [{"_schema": ["batch issue"]}]
    # the flagged row never entered object construction
    assert sorted(user.name for user in constructed) == ["a", "b"]


def test_collection_post_load_runs_on_successful_objects_only():
    seen = []

    class UserSchema(Schema):
        name = fields.Str(required=True)

        @post_load
        def make_user(self, data, **kwargs):
            return User(age=0, **data)

        @post_load(pass_collection=True)
        def collect(self, objs, many, **kwargs):
            seen.append(list(objs))
            return objs

    result = UserSchema().load_rows([{"name": "a"}, {}, {"name": "c"}])

    assert seen == [[User(name="a", age=0), User(name="c", age=0)]]
    assert [row.ok for row in result.rows] == [True, False, True]


def test_collection_post_load_validation_error_becomes_batch_error():
    class UserSchema(Schema):
        name = fields.Str(required=True)

        @post_load(pass_collection=True)
        def check(self, objs, many, **kwargs):
            raise ValidationError("too few rows")

    result = UserSchema().load_rows([{"name": "a"}])

    assert result.rows[0].ok
    assert result.errors == [{"_schema": ["too few rows"]}]
    assert not result.ok


def test_non_collection_input_returns_batch_error():
    class UserSchema(Schema):
        name = fields.Str()

    result = UserSchema().load_rows({"name": "a"})

    assert result.rows == []
    assert result.errors == [{"_schema": ["Invalid input type."]}]
    assert not result.ok


def test_indexed_rows_unwrapped_in_regular_load():
    class UserSchema(Schema):
        name = fields.Str()

        @pre_load(pass_collection=True)
        def reverse(self, data, many, **kwargs):
            indices = list(reversed(range(len(data))))
            return IndexedRows([data[i] for i in indices], indices)

    result = UserSchema().load([{"name": "a"}, {"name": "b"}], many=True)
    assert result == [{"name": "b"}, {"name": "a"}]


def test_load_rows_respects_unknown_and_partial():
    class UserSchema(Schema):
        name = fields.Str(required=True)

    result = UserSchema().load_rows(
        [{"name": "a", "extra": 1}, {}],
        unknown="exclude",
        partial=True,
    )
    assert result.rows[0].value == {"name": "a"}
    assert result.rows[1].value == {}


def test_load_rows_unknown_raise_marks_only_offending_row():
    class UserSchema(Schema):
        name = fields.Str(required=True)

    result = UserSchema().load_rows([{"name": "a"}, {"name": "b", "extra": 1}])
    assert result.rows[0].ok
    assert result.rows[1].error.messages == {"extra": ["Unknown field."]}

"""Tests for the ``load_aliases`` field argument."""

import pytest

from marshmallow import (
    EXCLUDE,
    INCLUDE,
    RAISE,
    Schema,
    fields,
    post_load,
    pre_load,
    validates,
    validates_schema,
)
from marshmallow.exceptions import (
    StringNotCollectionError,
    ValidationError,
)


class TestLoadAliases:
    def test_alias_is_accepted_on_load(self):
        class S(Schema):
            customer_id = fields.Int(load_aliases=("customerId",))

        assert S().load({"customerId": "42"}) == {"customer_id": 42}
        assert S().load({"customer_id": 42}) == {"customer_id": 42}

    def test_aliases_combine_with_data_key(self):
        class S(Schema):
            name = fields.Str(data_key="userName", load_aliases=("user_name",))

        assert S().load({"userName": "a"}) == {"name": "a"}
        assert S().load({"user_name": "a"}) == {"name": "a"}

    def test_dump_always_uses_primary_name(self):
        class S(Schema):
            customer_id = fields.Int(load_aliases=("customerId",))
            name = fields.Str(data_key="userName", load_aliases=("user_name",))

        assert S().dump({"customer_id": 1, "name": "a"}) == {
            "customer_id": 1,
            "userName": "a",
        }

    def test_multiple_aliases(self):
        class S(Schema):
            color = fields.Str(load_aliases=("colour", "farbe"))

        assert S().load({"colour": "red"}) == {"color": "red"}
        assert S().load({"farbe": "rot"}) == {"color": "rot"}

    def test_ambiguous_primary_and_alias_even_when_equal(self):
        class S(Schema):
            customer_id = fields.Int(load_aliases=("customerId",))

        with pytest.raises(ValidationError) as exc_info:
            S().load({"customer_id": 1, "customerId": 1})
        messages = exc_info.value.messages
        assert "customer_id" in messages
        joined = " ".join(messages["customer_id"])
        assert "customer_id" in joined
        assert "customerId" in joined

    def test_ambiguous_two_aliases(self):
        class S(Schema):
            color = fields.Str(load_aliases=("colour", "farbe"))

        with pytest.raises(ValidationError) as exc_info:
            S().load({"colour": "red", "farbe": "rot"})
        assert "color" in exc_info.value.messages

    def test_ambiguous_field_is_not_deserialized(self):
        class S(Schema):
            n = fields.Int(load_aliases=("n2",))

            @validates("n")
            def validate_n(self, value, **kwargs):
                raise AssertionError("validator must not run")

        with pytest.raises(ValidationError):
            S().load({"n": 1, "n2": 1})

    def test_consumed_alias_is_not_unknown_raise(self):
        class S(Schema):
            n = fields.Int(load_aliases=("n2",))

        assert S().load({"n2": 1}) == {"n": 1}
        with pytest.raises(ValidationError) as exc_info:
            S().load({"n2": 1, "x": 2})
        assert set(exc_info.value.messages) == {"x"}

    def test_consumed_alias_is_not_unknown_exclude(self):
        class S(Schema):
            n = fields.Int(load_aliases=("n2",))

        assert S(unknown=EXCLUDE).load({"n2": 1, "x": 2}) == {"n": 1}

    def test_consumed_alias_is_not_unknown_include(self):
        class S(Schema):
            n = fields.Int(load_aliases=("n2",))

        assert S(unknown=INCLUDE).load({"n2": 1, "x": 2}) == {
            "n": 1,
            "x": 2,
        }

    def test_field_error_points_to_submitted_alias(self):
        class S(Schema):
            n = fields.Int(load_aliases=("n2",))

        with pytest.raises(ValidationError) as exc_info:
            S().load({"n2": "notanint"})
        assert set(exc_info.value.messages) == {"n2"}
        with pytest.raises(ValidationError) as exc_info:
            S().load({"n": "notanint"})
        assert set(exc_info.value.messages) == {"n"}

    def test_required_error_points_to_submitted_name_when_alias_present_is_missing(self):
        class S(Schema):
            n = fields.Int(required=True, load_aliases=("n2",))

        with pytest.raises(ValidationError) as exc_info:
            S().load({})
        assert set(exc_info.value.messages) == {"n"}

    def test_field_validator_error_points_to_submitted_alias(self):
        class S(Schema):
            n = fields.Int(load_aliases=("n2",))

            @validates("n")
            def validate_n(self, value, data_key, **kwargs):
                raise ValidationError(f"bad {data_key}")

        with pytest.raises(ValidationError) as exc_info:
            S().load({"n2": 1})
        assert set(exc_info.value.messages) == {"n2"}
        with pytest.raises(ValidationError) as exc_info:
            S().load({"n": 1})
        assert set(exc_info.value.messages) == {"n"}

    def test_schema_validator_error_points_to_submitted_alias(self):
        class S(Schema):
            n = fields.Int(load_aliases=("n2",))

            @validates_schema
            def validate_schema(self, data, **kwargs):
                raise ValidationError("bad", field_name="n")

        with pytest.raises(ValidationError) as exc_info:
            S().load({"n2": 1})
        assert set(exc_info.value.messages) == {"n2"}

    def test_partial_still_uses_field_names_but_errors_use_submitted_key(self):
        class S(Schema):
            n = fields.Int(required=True, load_aliases=("n2",))
            m = fields.Int(required=True, load_aliases=("m2",))

        assert S().load({"n2": 1}, partial=("m",)) == {"n": 1}
        with pytest.raises(ValidationError) as exc_info:
            S().load({"n2": "bad"}, partial=("m",))
        assert set(exc_info.value.messages) == {"n2"}

    def test_alias_resolution_happens_after_pre_load(self):
        class S(Schema):
            n = fields.Int(load_aliases=("n2",))

            @pre_load
            def rename(self, data, **kwargs):
                return {"n2": data["old"]}

        assert S().load({"old": "3"}) == {"n": 3}

    def test_hook_call_counts_are_not_increased(self):
        calls = []

        class S(Schema):
            n = fields.Int(load_aliases=("n2",))

            @pre_load
            def pre(self, data, **kwargs):
                calls.append("pre")
                return data

            @post_load
            def post(self, data, **kwargs):
                calls.append("post")
                return data

            @validates("n")
            def validate_n(self, value, **kwargs):
                calls.append("validates")

        S().load({"n2": 1})
        assert calls == ["pre", "validates", "post"]

    def test_nested_list_mixed_old_and_new_names(self):
        class Inner(Schema):
            customer_id = fields.Int(load_aliases=("customerId",))

        class Outer(Schema):
            items = fields.List(fields.Nested(Inner))

        result = Outer().load(
            {"items": [{"customer_id": 1}, {"customerId": 2}]}
        )
        assert result == {"items": [{"customer_id": 1}, {"customer_id": 2}]}

    def test_many_load_per_item_key_tracking(self):
        class S(Schema):
            n = fields.Int(load_aliases=("n2",))

        result = S().load([{"n": 1}, {"n2": 2}], many=True)
        assert result == [{"n": 1}, {"n": 2}]

        with pytest.raises(ValidationError) as exc_info:
            S().load([{"n": "ok"}, {"n2": "bad"}], many=True)
        assert set(exc_info.value.messages[1]) == {"n2"}

    def test_ambiguous_in_many(self):
        class S(Schema):
            n = fields.Int(load_aliases=("n2",))

        with pytest.raises(ValidationError) as exc_info:
            S().load([{"n": 1, "n2": 1}], many=True)
        assert "n" in exc_info.value.messages[0]


class TestLoadAliasBinding:
    def test_alias_used_by_two_fields_raises_on_instantiation(self):
        class S(Schema):
            a = fields.Int(load_aliases=("x",))
            b = fields.Int(load_aliases=("x",))

        with pytest.raises(ValueError, match="load_aliases"):
            S()

    def test_alias_colliding_with_primary_name_raises(self):
        class S(Schema):
            a = fields.Int(load_aliases=("b",))
            b = fields.Int()

        with pytest.raises(ValueError, match="load_aliases"):
            S()

    def test_alias_colliding_with_foreign_data_key_raises(self):
        class S(Schema):
            a = fields.Int(load_aliases=("B",))
            b = fields.Int(data_key="B")

        with pytest.raises(ValueError, match="load_aliases"):
            S()

    def test_alias_equal_to_own_primary_name_is_allowed(self):
        class S(Schema):
            a = fields.Int(load_aliases=("a",))

        assert S().load({"a": 1}) == {"a": 1}

        class S2(Schema):
            a = fields.Int(data_key="A", load_aliases=("A",))

        assert S2().load({"A": 1}) == {"a": 1}

    def test_inherited_fields_participate_in_collision_check(self):
        class Base(Schema):
            a = fields.Int(load_aliases=("x",))

        class Child(Base):
            x = fields.Int()

        with pytest.raises(ValueError, match="load_aliases"):
            Child()

    @pytest.mark.parametrize(
        ("schema_kwargs", "payload", "expected"),
        [
            ({"only": ("a",)}, {"x": 1}, {"a": 1}),
            ({"exclude": ("b",)}, {"x": 1}, {"a": 1}),
            ({"dump_only": ("b",)}, {"x": 1}, {"a": 1}),
        ],
    )
    def test_conflicting_alias_ok_when_only_one_field_loads(
        self, schema_kwargs, payload, expected
    ):
        class S(Schema):
            a = fields.Int(load_aliases=("x",))
            b = fields.Int(load_aliases=("x",))

        assert S(**schema_kwargs).load(payload) == expected

    def test_load_only_field_keeps_alias_conflict(self):
        class S(Schema):
            a = fields.Int(load_aliases=("x",), load_only=True)
            b = fields.Int(load_aliases=("x",))

        with pytest.raises(ValueError, match="load_aliases"):
            S()

    def test_field_instances_do_not_share_alias_index(self):
        class S(Schema):
            a = fields.Int(load_aliases=("x",))
            b = fields.Int(load_aliases=("x",))

        s1 = S(only=("a",))
        s2 = S(only=("b",))
        assert s1.load({"x": 1}) == {"a": 1}
        assert s2.load({"x": 2}) == {"b": 2}
        assert s1.load({"x": 3}) == {"a": 3}
        assert s1._load_alias_index["x"] == "a"
        assert s2._load_alias_index["x"] == "b"


class TestLoadAliasesArgumentValidation:
    def test_string_aliases_are_rejected(self):
        with pytest.raises(StringNotCollectionError):
            fields.Int(load_aliases="customerId")

    def test_non_string_alias_items_are_rejected(self):
        with pytest.raises(ValueError, match="load_aliases"):
            fields.Int(load_aliases=(1,))

    def test_duplicate_aliases_on_one_field_are_deduped(self):
        class S(Schema):
            a = fields.Int(load_aliases=("x", "x"))
            b = fields.Int()

        assert S().load({"x": 1}) == {"a": 1}

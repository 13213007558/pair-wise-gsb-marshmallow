"""Tests for the opt-in ``snapshot_original`` mode.

The historical ``pass_original`` behavior passes the caller's own object, so a
``pre_load`` hook that mutates it in place changes what auditors see. These
tests pin the reference semantics of both modes and verify that snapshot mode
captures the data actually handed to ``load``.
"""

import datetime as dt
import typing

import pytest
from marshmallow import (
    EXCLUDE,
    Schema,
    SnapshotError,
    ValidationError,
    fields,
    post_load,
    pre_load,
    validates_schema,
)


class DeepMutationSchema(Schema):
    class Meta:
        unknown = EXCLUDE

    foo = fields.Int()

    @pre_load
    def mutate_in_place(self, data, **kwargs):
        data["deep"]["nested"].append(99)
        data["extra"] = "mutated"
        return data

    @post_load(pass_original=True)
    def audit(self, data, original_data, **kwargs):
        data["_audit"] = original_data
        return data


class TestSnapshotStability:
    def test_deep_list_mutation_does_not_reach_snapshot(self):
        schema = DeepMutationSchema(snapshot_original=True)
        payload = {"foo": 1, "deep": {"nested": [1, 2]}}

        result = schema.load(payload)

        # The caller's own object is still mutated in place.
        assert payload["deep"]["nested"] == [1, 2, 99]
        assert payload["extra"] == "mutated"
        # The pass_original argument is the pre-mutation snapshot.
        assert result["_audit"] == {"foo": 1, "deep": {"nested": [1, 2]}}
        assert result["_audit"] is not payload
        assert result["_audit"]["deep"] is not payload["deep"]
        # The deserialized return value only contains declared fields plus the
        # audit attachment added by the post_load hook.
        assert {k: v for k, v in result.items() if k != "_audit"} == {"foo": 1}

    def test_snapshot_is_deep_and_independent(self):
        schema = DeepMutationSchema(snapshot_original=True)
        payload = {"foo": 1, "deep": {"nested": [1, 2]}}

        result = schema.load(payload)
        snapshot = result["_audit"]

        # Mutating after load cannot reach the captured snapshot either way.
        payload["deep"]["nested"].append(100)
        assert snapshot["deep"]["nested"] == [1, 2]

    def test_legacy_mode_keeps_reference_semantics(self):
        schema = DeepMutationSchema()
        payload = {"foo": 1, "deep": {"nested": [1, 2]}}

        result = schema.load(payload)

        assert result["_audit"] is payload
        assert result["_audit"]["deep"]["nested"] == [1, 2, 99]

    def test_meta_option_enables_snapshot_mode(self):
        class MetaSchema(DeepMutationSchema):
            class Meta(DeepMutationSchema.Meta):
                snapshot_original = True

        payload = {"foo": 1, "deep": {"nested": []}}
        result = MetaSchema().load(payload)
        assert result["_audit"] is not payload
        assert result["_audit"] == {"foo": 1, "deep": {"nested": []}}

    def test_constructor_overrides_meta_option(self):
        class MetaSchema(DeepMutationSchema):
            class Meta(DeepMutationSchema.Meta):
                snapshot_original = True

        payload = {"foo": 1, "deep": {"nested": []}}
        result = MetaSchema(snapshot_original=False).load(payload)
        assert result["_audit"] is payload


class TestManySnapshots:
    def test_element_level_post_load_gets_per_element_snapshot(self):
        seen = []

        class ItemSchema(Schema):
            class Meta:
                unknown = EXCLUDE

            foo = fields.Int()

            @pre_load
            def tag(self, data, **kwargs):
                data["tag"] = "mutated"
                return data

            @post_load(pass_original=True)
            def audit(self, data, original_data, **kwargs):
                seen.append(original_data)
                return data

        schema = ItemSchema(snapshot_original=True)
        payload = [{"foo": 1}, {"foo": 2}]
        result = schema.load(payload, many=True)

        assert seen == [{"foo": 1}, {"foo": 2}]
        assert all(
            snapshot is not item for snapshot, item in zip(seen, payload, strict=True)
        )
        assert payload == [
            {"foo": 1, "tag": "mutated"},
            {"foo": 2, "tag": "mutated"},
        ]
        assert result == [{"foo": 1}, {"foo": 2}]

    def test_collection_level_post_load_gets_whole_collection_snapshot(self):
        seen = []

        class CollectionSchema(Schema):
            class Meta:
                unknown = EXCLUDE

            foo = fields.Int()

            @pre_load(pass_collection=True)
            def inject(self, data, **kwargs):
                data.append({"foo": 3})
                return data

            @post_load(pass_collection=True, pass_original=True)
            def audit(self, data, original_data, **kwargs):
                seen.append(original_data)
                return data

        schema = CollectionSchema(snapshot_original=True)
        payload = [{"foo": 1}, {"foo": 2}]
        result = schema.load(payload, many=True)

        assert seen == [[{"foo": 1}, {"foo": 2}]]
        assert seen[0] is not payload
        assert payload == [{"foo": 1}, {"foo": 2}, {"foo": 3}]
        assert len(result) == 3

    def test_element_level_validator_gets_snapshot_for_each_index(self):
        seen = []

        class ValidatorSchema(Schema):
            class Meta:
                unknown = EXCLUDE

            foo = fields.Int(required=True)

            @pre_load
            def tag(self, data, **kwargs):
                data["tag"] = "mutated"
                return data

            @validates_schema(pass_original=True, skip_on_field_errors=False)
            def audit(self, data, original_data, **kwargs):
                seen.append(original_data)

        schema = ValidatorSchema(snapshot_original=True)
        payload = [{"foo": 1}, {"bar": 2}]
        with pytest.raises(ValidationError) as exc_info:
            schema.load(payload, many=True)

        assert 1 in exc_info.value.messages
        assert seen == [{"foo": 1}, {"bar": 2}]
        assert all(
            snapshot is not item for snapshot, item in zip(seen, payload, strict=True)
        )

    def test_failing_element_error_carries_matching_snapshot(self):
        class FailingSchema(Schema):
            foo = fields.Int(required=True)

            @pre_load
            def tag(self, data, **kwargs):
                data["tag"] = "mutated"
                return data

        schema = FailingSchema(snapshot_original=True)
        payload = [{"foo": 1}, {"bar": 2}]
        with pytest.raises(ValidationError) as exc_info:
            schema.load(payload, many=True)

        assert exc_info.value.data == [{"foo": 1}, {"bar": 2}]
        assert exc_info.value.data is not payload
        assert payload == [
            {"foo": 1, "tag": "mutated"},
            {"bar": 2, "tag": "mutated"},
        ]


class TestNestedSnapshots:
    def test_each_schema_level_snapshots_its_own_input(self):
        child_seen = []

        class ChildSchema(Schema):
            class Meta:
                unknown = EXCLUDE
                snapshot_original = True

            bar = fields.Int()

            @pre_load
            def clobber(self, data, **kwargs):
                data["bar"] = 999
                return data

            @post_load(pass_original=True)
            def audit(self, data, original_data, **kwargs):
                child_seen.append(original_data)
                return data

        class ParentSchema(Schema):
            class Meta:
                unknown = EXCLUDE
                snapshot_original = True

            child = fields.Nested(ChildSchema)

            @pre_load
            def rewrite(self, data, **kwargs):
                data["child"]["bar"] = 888
                return data

            @post_load(pass_original=True)
            def audit(self, data, original_data, **kwargs):
                data["_audit"] = original_data
                return data

        payload = {"child": {"bar": 1}}
        result = ParentSchema().load(payload)

        assert result["_audit"] == {"child": {"bar": 1}}
        assert result["_audit"] is not payload
        # The child snapshots at its own entry point, i.e. after the parent's
        # pre_load, but still gets an independent copy.
        assert child_seen == [{"bar": 888}]
        assert child_seen[0] is not payload["child"]
        # Both pre_load hooks keep mutating the caller's shared graph.
        assert payload == {"child": {"bar": 999}}

    def test_nested_schema_without_flag_keeps_legacy_references(self):
        child_seen = []

        class ChildSchema(Schema):
            class Meta:
                unknown = EXCLUDE

            bar = fields.Int()

            @pre_load
            def clobber(self, data, **kwargs):
                data["bar"] = 999
                return data

            @post_load(pass_original=True)
            def audit(self, data, original_data, **kwargs):
                child_seen.append(original_data)
                return data

        class ParentSchema(Schema):
            class Meta:
                unknown = EXCLUDE
                snapshot_original = True

            child = fields.Nested(ChildSchema)

        payload = {"child": {"bar": 1}}
        ParentSchema().load(payload)

        assert child_seen[0] is payload["child"]
        assert child_seen[0] == {"bar": 999}


class TestFailurePath:
    def test_handle_error_receives_snapshot_not_caller_object(self):
        handled = []

        class ErrorSchema(Schema):
            foo = fields.Int(required=True)

            @pre_load
            def tag(self, data, **kwargs):
                data["tag"] = "mutated"
                return data

            def handle_error(self, error, data, **kwargs):
                handled.append(data)

        schema = ErrorSchema(snapshot_original=True)
        payload = {"bar": 2}
        with pytest.raises(ValidationError) as exc_info:
            schema.load(payload)

        assert handled == [{"bar": 2}]
        assert handled[0] is not payload
        assert exc_info.value.data == {"bar": 2}
        assert exc_info.value.data is not payload
        assert payload == {"bar": 2, "tag": "mutated"}

    def test_pre_load_failure_audit_uses_snapshot(self):
        class PreLoadErrorSchema(Schema):
            foo = fields.Int()

            @pre_load
            def explode(self, data, **kwargs):
                data["tag"] = "mutated"
                raise ValidationError("boom")

        schema = PreLoadErrorSchema(snapshot_original=True)
        payload = {"foo": 1}
        with pytest.raises(ValidationError) as exc_info:
            schema.load(payload)

        assert exc_info.value.data == {"foo": 1}
        assert exc_info.value.data is not payload
        assert payload == {"foo": 1, "tag": "mutated"}

    def test_validate_uses_snapshot(self):
        class OnlyValidateSchema(Schema):
            foo = fields.Int(required=True)

            @pre_load
            def tag(self, data, **kwargs):
                data["tag"] = "mutated"
                return data

        schema = OnlyValidateSchema(snapshot_original=True)
        payload = {"bar": 2}
        messages = schema.validate(payload)

        assert messages
        assert payload == {"bar": 2, "tag": "mutated"}


class TestUnsupportedInput:
    @pytest.mark.parametrize(
        "payload",
        [
            {"d": dt.date(2020, 1, 1)},
            {"s": {1, 2}},
            [1, b"bytes"],
            {"n": object()},
        ],
    )
    def test_unsupported_type_raises_snapshot_error_before_hooks(self, payload):
        hook_ran = []

        class StrictSchema(Schema):
            class Meta:
                unknown = EXCLUDE

            foo = fields.Int()

            @pre_load
            def note(self, data, **kwargs):
                hook_ran.append(data)
                return data

        schema = StrictSchema(snapshot_original=True)
        with pytest.raises(SnapshotError):
            schema.load(payload)
        assert hook_ran == []

    def test_cyclic_input_raises_snapshot_error(self):
        cyclic: dict[str, typing.Any] = {"foo": 1}
        cyclic["self"] = cyclic
        schema = DeepMutationSchema(snapshot_original=True)
        with pytest.raises(SnapshotError, match="cycle"):
            schema.load(cyclic)

    def test_unsupported_input_does_not_error_in_legacy_mode(self):
        payload = {"when": dt.date(2020, 1, 1)}
        schema = Schema.from_dict({"when": fields.Date()})()
        assert schema.load(payload) == {"when": dt.date(2020, 1, 1)}

    def test_tuples_are_supported_and_preserved(self):
        seen = []

        class TupleSchema(Schema):
            class Meta:
                unknown = EXCLUDE

            foo = fields.Int()

            @post_load(pass_original=True)
            def audit(self, data, original_data, **kwargs):
                seen.append(original_data)
                return data

        payload = {"foo": 1, "values": (1, [2, 3])}
        TupleSchema(snapshot_original=True).load(payload)
        snapshot = seen[0]
        assert snapshot == {"foo": 1, "values": (1, [2, 3])}
        assert isinstance(snapshot["values"], tuple)
        assert snapshot is not payload


class TestNoCopyProtocolSideEffects:
    def test_custom_deepcopy_is_never_invoked(self):
        class EvilDict(dict):
            def __deepcopy__(self, memo):
                raise AssertionError("deepcopy must not be called")

        class EvilList(list):
            def __deepcopy__(self, memo):
                raise AssertionError("deepcopy must not be called")

        seen = []

        class SafeSchema(Schema):
            class Meta:
                unknown = EXCLUDE

            foo = fields.Int()

            @post_load(pass_original=True)
            def audit(self, data, original_data, **kwargs):
                seen.append(original_data)
                return data

        payload = EvilDict({"foo": 1, "items": EvilList([1, 2])})
        SafeSchema(snapshot_original=True).load(payload)

        assert type(seen[0]) is dict
        assert type(seen[0]["items"]) is list
        assert seen[0] == {"foo": 1, "items": [1, 2]}

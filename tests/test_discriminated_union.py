"""Tests for fields.DiscriminatedUnion."""

import pytest

from marshmallow import EXCLUDE, Schema, fields, post_load, pre_load
from marshmallow.exceptions import ValidationError

click_calls = {"pre_load": 0, "post_load": 0}
view_calls = {"pre_load": 0, "post_load": 0}


class ClickEventSchema(Schema):
    kind = fields.Str(required=True)
    x = fields.Int(required=True)

    @pre_load
    def count_pre_load(self, data, **kwargs):
        click_calls["pre_load"] += 1
        return data

    @post_load
    def count_post_load(self, data, **kwargs):
        click_calls["post_load"] += 1
        return data


class ViewEventSchema(Schema):
    kind = fields.Str(required=True)
    url = fields.Str(required=True)

    @pre_load
    def count_pre_load(self, data, **kwargs):
        view_calls["pre_load"] += 1
        return data

    @post_load
    def count_post_load(self, data, **kwargs):
        view_calls["post_load"] += 1
        return data


@pytest.fixture(autouse=True)
def reset_counters():
    click_calls["pre_load"] = 0
    click_calls["post_load"] = 0
    view_calls["pre_load"] = 0
    view_calls["post_load"] = 0


def make_union_field(**kwargs):
    return fields.DiscriminatedUnion(
        {"click": ClickEventSchema, "view": ViewEventSchema},
        discriminator="kind",
        **kwargs,
    )


class EnvelopeSchema(Schema):
    event = make_union_field()


class EventStreamSchema(Schema):
    events = fields.List(make_union_field())


class TestBranchSelection:
    @pytest.mark.parametrize(
        ("event", "expected"),
        [
            ({"kind": "click", "x": 1}, {"kind": "click", "x": 1}),
            ({"kind": "view", "url": "https://example.com"}, {"kind": "view", "url": "https://example.com"}),
        ],
    )
    def test_load_dispatches_to_selected_branch(self, event, expected):
        assert EnvelopeSchema().load({"event": event}) == {"event": expected}

    def test_only_selected_branch_processors_run_on_load(self):
        EnvelopeSchema().load({"event": {"kind": "click", "x": 1}})
        assert click_calls == {"pre_load": 1, "post_load": 1}
        assert view_calls == {"pre_load": 0, "post_load": 0}

        EnvelopeSchema().load({"event": {"kind": "view", "url": "https://example.com"}})
        assert click_calls == {"pre_load": 1, "post_load": 1}
        assert view_calls == {"pre_load": 1, "post_load": 1}

    def test_unknown_kind_does_not_run_any_branch_processors(self):
        with pytest.raises(ValidationError):
            EnvelopeSchema().load({"event": {"kind": "hover"}})
        assert click_calls == {"pre_load": 0, "post_load": 0}
        assert view_calls == {"pre_load": 0, "post_load": 0}

    def test_unknown_kind_error_at_discriminator_path(self):
        with pytest.raises(ValidationError) as excinfo:
            EnvelopeSchema().load({"event": {"kind": "hover"}})
        assert excinfo.value.messages == {
            "event": {"kind": ["Unknown discriminator value: 'hover'."]}
        }

    def test_missing_kind_error_at_discriminator_path(self):
        with pytest.raises(ValidationError) as excinfo:
            EnvelopeSchema().load({"event": {"x": 1}})
        assert excinfo.value.messages == {
            "event": {"kind": ["Missing discriminator field: 'kind'."]}
        }

    def test_unhashable_kind_is_an_unknown_discriminator_error(self):
        with pytest.raises(ValidationError) as excinfo:
            EnvelopeSchema().load({"event": {"kind": ["click"]}})
        assert excinfo.value.messages == {
            "event": {"kind": ["Unknown discriminator value: ['click']."]}
        }

    def test_non_mapping_event_raises_type_error(self):
        with pytest.raises(ValidationError) as excinfo:
            EnvelopeSchema().load({"event": "nope"})
        assert excinfo.value.messages == {"event": ["Invalid type."]}

    def test_branch_validation_errors_are_propagated(self):
        with pytest.raises(ValidationError) as excinfo:
            EnvelopeSchema().load({"event": {"kind": "click", "x": "not-an-int"}})
        assert excinfo.value.messages == {"event": {"x": ["Not a valid integer."]}}
        # The failing branch ran its processors, the other branch did not
        assert click_calls["pre_load"] == 1
        assert view_calls == {"pre_load": 0, "post_load": 0}


class TestListOfEvents:
    def test_mixed_valid_and_unknown_kind_in_list(self):
        data = {
            "events": [
                {"kind": "click", "x": 1},
                {"kind": "hover"},
                {"kind": "view", "url": "https://example.com"},
                {"url": "https://example.com"},
            ]
        }
        with pytest.raises(ValidationError) as excinfo:
            EventStreamSchema().load(data)
        assert excinfo.value.messages == {
            "events": {
                1: {"kind": ["Unknown discriminator value: 'hover'."]},
                3: {"kind": ["Missing discriminator field: 'kind'."]},
            }
        }
        assert excinfo.value.valid_data == {
            "events": [
                {"kind": "click", "x": 1},
                {"kind": "view", "url": "https://example.com"},
            ]
        }
        # Each valid event ran only its own branch's processors
        assert click_calls == {"pre_load": 1, "post_load": 1}
        assert view_calls == {"pre_load": 1, "post_load": 1}

    def test_valid_list_loads_all_events(self):
        data = {
            "events": [
                {"kind": "click", "x": 1},
                {"kind": "view", "url": "https://example.com"},
            ]
        }
        assert EventStreamSchema().load(data) == data


class TestMany:
    class ManyEnvelopeSchema(Schema):
        events = fields.DiscriminatedUnion(
            {"click": ClickEventSchema, "view": ViewEventSchema},
            discriminator="kind",
            many=True,
        )

    def test_many_load(self):
        data = {
            "events": [
                {"kind": "click", "x": 1},
                {"kind": "view", "url": "https://example.com"},
            ]
        }
        assert self.ManyEnvelopeSchema().load(data) == data

    def test_many_errors_keep_list_indices(self):
        data = {
            "events": [
                {"kind": "click", "x": 1},
                {"kind": "hover"},
            ]
        }
        with pytest.raises(ValidationError) as excinfo:
            self.ManyEnvelopeSchema().load(data)
        assert excinfo.value.messages == {
            "events": {1: {"kind": ["Unknown discriminator value: 'hover'."]}}
        }
        assert excinfo.value.valid_data == {"events": [{"kind": "click", "x": 1}]}

    def test_many_dump(self):
        data = {
            "events": [
                {"kind": "click", "x": 1},
                {"kind": "view", "url": "https://example.com"},
            ]
        }
        assert self.ManyEnvelopeSchema().dump(data) == data


class TestInsideNested:
    class OuterSchema(Schema):
        inner = fields.Nested(EnvelopeSchema)

    def test_load_through_nested(self):
        data = {"inner": {"event": {"kind": "click", "x": 1}}}
        assert self.OuterSchema().load(data) == data

    def test_errors_propagate_through_nested(self):
        with pytest.raises(ValidationError) as excinfo:
            self.OuterSchema().load({"inner": {"event": {"kind": "hover"}}})
        assert excinfo.value.messages == {
            "inner": {"event": {"kind": ["Unknown discriminator value: 'hover'."]}}
        }


class TestDump:
    def test_dump_selects_branch_by_kind(self):
        obj = {"event": {"kind": "click", "x": 1}}
        assert EnvelopeSchema().dump(obj) == {"event": {"kind": "click", "x": 1}}

    def test_dump_with_unknown_kind_raises(self):
        with pytest.raises(ValidationError) as excinfo:
            EnvelopeSchema().dump({"event": {"kind": "hover"}})
        assert excinfo.value.messages == {
            "kind": ["Unknown discriminator value: 'hover'."]
        }

    def test_dump_with_missing_kind_raises(self):
        with pytest.raises(ValidationError) as excinfo:
            EnvelopeSchema().dump({"event": {"x": 1}})
        assert excinfo.value.messages == {
            "kind": ["Missing discriminator field: 'kind'."]
        }

    def test_dump_uses_only_selected_branch(self):
        # "url" is not a field of ClickEventSchema, so it is not dumped
        obj = {"event": {"kind": "click", "x": 1, "url": "https://example.com"}}
        assert EnvelopeSchema().dump(obj) == {"event": {"kind": "click", "x": 1}}


class TypedClickEventSchema(Schema):
    kind = fields.Str(required=True, data_key="type")
    x = fields.Int(required=True, data_key="X")


class TypedViewEventSchema(Schema):
    kind = fields.Str(required=True, data_key="type")
    url = fields.Str(required=True)


class TestExternalDiscriminatorName:
    class TypedEnvelopeSchema(Schema):
        event = fields.DiscriminatedUnion(
            {"click": TypedClickEventSchema, "view": TypedViewEventSchema},
            discriminator="kind",
        )

    def test_discriminator_key_uses_data_key(self):
        field = self.TypedEnvelopeSchema().fields["event"]
        assert field.discriminator_key == "type"

    def test_load_with_external_names(self):
        data = {"event": {"type": "click", "X": 1}}
        assert self.TypedEnvelopeSchema().load(data) == {
            "event": {"kind": "click", "x": 1}
        }

    def test_dump_with_external_names(self):
        obj = {"event": {"kind": "click", "x": 1}}
        assert self.TypedEnvelopeSchema().dump(obj) == {
            "event": {"type": "click", "X": 1}
        }

    def test_errors_use_external_discriminator_name(self):
        with pytest.raises(ValidationError) as excinfo:
            self.TypedEnvelopeSchema().load({"event": {"type": "hover"}})
        assert excinfo.value.messages == {
            "event": {"type": ["Unknown discriminator value: 'hover'."]}
        }

    def test_conflicting_data_keys_raise_value_error(self):
        class OtherViewEventSchema(Schema):
            kind = fields.Str(data_key="sort")

        class BadEnvelopeSchema(Schema):
            event = fields.DiscriminatedUnion(
                {"click": TypedClickEventSchema, "view": OtherViewEventSchema},
                discriminator="kind",
            )

        with pytest.raises(ValueError, match="Conflicting .data_key. values"):
            BadEnvelopeSchema().load({"event": {"type": "click", "X": 1}})


class SharedEventSchema(Schema):
    kind = fields.Str(required=True)
    a = fields.Int()
    b = fields.Int()


class TestBranchSchemaOptions:
    def test_only_and_exclude_are_applied_to_branches(self):
        class OnlySchema(Schema):
            event = fields.DiscriminatedUnion(
                {"shared": SharedEventSchema},
                discriminator="kind",
                only=("kind", "a"),
                exclude=("a",),
            )

        schema = OnlySchema()
        assert schema.load({"event": {"kind": "shared"}}) == {"event": {"kind": "shared"}}
        assert schema.dump({"event": {"kind": "shared", "a": 1, "b": 2}}) == {
            "event": {"kind": "shared"}
        }

    def test_shared_schema_instance_is_not_mutated(self):
        shared = SharedEventSchema()
        original_fields = set(shared.fields)
        original_exclude = set(shared.exclude)

        class FirstSchema(Schema):
            event = fields.DiscriminatedUnion(
                {"shared": shared}, discriminator="kind", exclude=("a",)
            )

        class SecondSchema(Schema):
            event = fields.DiscriminatedUnion(
                {"shared": shared}, discriminator="kind", exclude=("b",)
            )

        assert FirstSchema().load({"event": {"kind": "shared", "b": 2}}) == {
            "event": {"kind": "shared", "b": 2}
        }
        assert SecondSchema().load({"event": {"kind": "shared", "a": 1}}) == {
            "event": {"kind": "shared", "a": 1}
        }
        assert set(shared.fields) == original_fields
        assert set(shared.exclude) == original_exclude

    def test_unknown_option_is_passed_to_branches(self):
        class LenientSchema(Schema):
            event = fields.DiscriminatedUnion(
                {"click": ClickEventSchema}, discriminator="kind", unknown=EXCLUDE
            )

        assert LenientSchema().load(
            {"event": {"kind": "click", "x": 1, "extra": "drop me"}}
        ) == {"event": {"kind": "click", "x": 1}}

    def test_unknown_fields_raise_by_default(self):
        with pytest.raises(ValidationError) as excinfo:
            EnvelopeSchema().load({"event": {"kind": "click", "x": 1, "extra": 1}})
        assert excinfo.value.messages == {"event": {"extra": ["Unknown field."]}}

    def test_partial_is_propagated_to_branches(self):
        assert EnvelopeSchema().load({"event": {"kind": "click"}}, partial=True) == {
            "event": {"kind": "click"}
        }


class RecursiveEventSchema(Schema):
    kind = fields.Str(required=True)
    name = fields.Str()
    children = fields.List(
        fields.DiscriminatedUnion(
            {"event": lambda: RecursiveEventSchema}, discriminator="kind"
        ),
        load_default=list,
    )


class TestLazySchemaReferences:
    def test_recursive_events_load(self):
        data = {
            "kind": "event",
            "name": "root",
            "children": [
                {"kind": "event", "name": "child", "children": []},
            ],
        }
        assert RecursiveEventSchema().load(data) == data

    def test_recursive_events_error_indices(self):
        data = {
            "kind": "event",
            "children": [{"kind": "hover"}],
        }
        with pytest.raises(ValidationError) as excinfo:
            RecursiveEventSchema().load(data)
        assert excinfo.value.messages == {
            "children": {0: {"kind": ["Unknown discriminator value: 'hover'."]}}
        }

    def test_string_schema_reference(self):
        class StringRefSchema(Schema):
            event = fields.DiscriminatedUnion(
                {"click": "ClickEventSchema"}, discriminator="kind"
            )

        assert StringRefSchema().load({"event": {"kind": "click", "x": 1}}) == {
            "event": {"kind": "click", "x": 1}
        }

    def test_schema_from_dict(self):
        class DictRefSchema(Schema):
            event = fields.DiscriminatedUnion(
                {"raw": {"kind": fields.Str(required=True)}}, discriminator="kind"
            )

        assert DictRefSchema().load({"event": {"kind": "raw"}}) == {
            "event": {"kind": "raw"}
        }


class TestConfiguration:
    @pytest.mark.parametrize("mapping", [{}, [], None])
    def test_invalid_mapping_raises(self, mapping):
        with pytest.raises(ValueError, match="non-empty dict"):
            fields.DiscriminatedUnion(mapping)

    @pytest.mark.parametrize("discriminator", ["", None, 42])
    def test_invalid_discriminator_raises(self, discriminator):
        with pytest.raises(ValueError, match="non-empty string"):
            fields.DiscriminatedUnion({"a": Schema}, discriminator=discriminator)

    def test_invalid_branch_type_raises_on_use(self):
        class BadBranchSchema(Schema):
            event = fields.DiscriminatedUnion({"nope": 42}, discriminator="kind")

        with pytest.raises(ValueError, match="must be a"):
            BadBranchSchema().load({"event": {"kind": "nope"}})

    def test_default_discriminator_is_type(self):
        class DefaultSchema(Schema):
            event = fields.DiscriminatedUnion({"click": ClickEventSchema})

        field = DefaultSchema().fields["event"]
        assert field.discriminator == "type"
        assert field.discriminator_key == "type"

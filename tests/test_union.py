from __future__ import annotations

import pytest

from marshmallow import EXCLUDE, Schema, fields, post_dump, post_load, pre_load
from marshmallow.exceptions import ValidationError


class ProcessorCounters:
    created = 0
    updated = 0


class CreatedEventSchema(Schema):
    type = fields.Constant("created")
    name = fields.Str()

    @pre_load
    def count_pre_load(self, data, **kwargs):
        ProcessorCounters.created += 1
        return data

    @post_load
    def count_post_load(self, data, **kwargs):
        ProcessorCounters.created += 1
        return data


class UpdatedEventSchema(Schema):
    type = fields.Constant("updated")
    name = fields.Str()

    @pre_load
    def count_pre_load(self, data, **kwargs):
        ProcessorCounters.updated += 1
        return data

    @post_load
    def count_post_load(self, data, **kwargs):
        ProcessorCounters.updated += 1
        return data


class EventSchema(Schema):
    event = fields.Union(
        "type",
        {"created": CreatedEventSchema, "updated": UpdatedEventSchema},
    )


class EventListSchema(Schema):
    events = fields.List(
        fields.Union(
            "type",
            {"created": CreatedEventSchema, "updated": UpdatedEventSchema},
        )
    )


@pytest.fixture(autouse=True)
def reset_processor_counters():
    ProcessorCounters.created = 0
    ProcessorCounters.updated = 0


def test_load_uses_only_selected_schema_processors():
    result = EventSchema().load({"event": {"type": "created", "name": "task"}})

    assert result == {"event": {"type": "created", "name": "task"}}
    assert ProcessorCounters.created == 2
    assert ProcessorCounters.updated == 0


def test_list_mix_valid_and_unknown_kind_preserves_indices_and_valid_data():
    with pytest.raises(ValidationError) as exc_info:
        EventListSchema().load(
            {
                "events": [
                    {"type": "created", "name": "one"},
                    {"type": "deleted", "name": "two"},
                    {"type": "updated", "name": "three"},
                ]
            }
        )

    assert exc_info.value.messages == {"events": {1: {"type": ["Not a valid choice."]}}}
    assert exc_info.value.valid_data == {
        "events": [
            {"type": "created", "name": "one"},
            {},
            {"type": "updated", "name": "three"},
        ]
    }
    assert ProcessorCounters.created == 2
    assert ProcessorCounters.updated == 2


def test_many_true_preserves_indices():
    schema = fields.Union(
        "type",
        {"created": CreatedEventSchema, "updated": UpdatedEventSchema},
        many=True,
    )

    with pytest.raises(ValidationError) as exc_info:
        schema.deserialize(
            [
                {"type": "created", "name": "one"},
                {"name": "missing"},
            ]
        )

    assert exc_info.value.messages == {
        1: {"type": ["Missing data for required field."]}
    }
    assert exc_info.value.valid_data == [
        {"type": "created", "name": "one"},
        {},
    ]


def test_missing_and_unknown_kind_errors_are_on_discriminator_path():
    schema = fields.Union("type", {"created": CreatedEventSchema})

    for payload, message in (
        ({"name": "task"}, "Missing data for required field."),
        ({"type": "deleted"}, "Not a valid choice."),
    ):
        with pytest.raises(ValidationError) as exc_info:
            schema.deserialize(payload)
        assert exc_info.value.messages == {"type": [message]}


def test_external_discriminator_name_is_used_for_load_and_dump():
    class CreatedExternalSchema(Schema):
        kind = fields.Constant("created", data_key="eventType")
        name = fields.Str()

    class ExternalEventSchema(Schema):
        event = fields.Union("eventType", {"created": CreatedExternalSchema})

    loaded = ExternalEventSchema().load(
        {"event": {"eventType": "created", "name": "task"}}
    )
    dumped = ExternalEventSchema().dump(loaded)

    assert loaded == {"event": {"kind": "created", "name": "task"}}
    assert dumped == {"event": {"eventType": "created", "name": "task"}}


def test_dump_requires_selected_branch_kind_to_match_output():
    class ConflictingCreatedSchema(Schema):
        type = fields.Constant("created")

        @post_dump
        def change_type(self, data, **kwargs):
            data["type"] = "updated"
            return data

    schema = fields.Union("type", {"created": ConflictingCreatedSchema})

    with pytest.raises(ValidationError) as exc_info:
        schema.serialize("event", {"event": {"type": "created"}})

    assert exc_info.value.messages == {"type": ["Must be 'created', got 'updated'."]}


def test_discriminator_conflict_is_reported_early():
    class WrongConstantSchema(Schema):
        type = fields.Constant("updated")

    with pytest.raises(ValueError, match="must be 'created'"):
        fields.Union("type", {"created": WrongConstantSchema})


def test_missing_discriminator_field_is_reported_early():
    class NoDiscriminatorSchema(Schema):
        name = fields.Str()

    with pytest.raises(ValueError, match="data key 'type'"):
        fields.Union("type", {"created": NoDiscriminatorSchema})


def test_partial_unknown_only_and_exclude_are_branch_scoped():
    class PartialCreatedSchema(Schema):
        type = fields.Constant("created")
        required_name = fields.Str(required=True)
        optional_name = fields.Str()

        class Meta:
            unknown = EXCLUDE

    schema = fields.Union(
        "type", {"created": PartialCreatedSchema(exclude=("optional_name",))}
    )

    result = schema.deserialize({"type": "created", "extra": True}, partial=True)

    assert result == {"type": "created"}
    with pytest.raises(ValidationError) as exc_info:
        schema.deserialize({"type": "created"})
    assert exc_info.value.messages == {
        "required_name": ["Missing data for required field."]
    }


def test_shared_schema_instance_is_not_mutated():
    shared = CreatedEventSchema(only=("type", "name"))
    schema = fields.Union(
        "type",
        {"created": shared},
        exclude=("name",),
        unknown=EXCLUDE,
    )

    assert schema.deserialize({"type": "created", "name": "task"}) == {
        "type": "created"
    }
    assert set(shared.fields) == {"type", "name"}


def test_union_only_is_honored_for_discriminator_output():
    schema = fields.Union(
        "type",
        {"created": CreatedEventSchema},
        only=("name",),
        unknown=EXCLUDE,
    )

    assert schema.deserialize({"type": "created", "name": "task"}) == {"name": "task"}
    assert schema.serialize(
        "event", {"event": {"type": "created", "name": "task"}}
    ) == {"name": "task"}


def test_explicitly_excluding_discriminator_is_honored():
    schema = fields.Union(
        "type",
        {"created": CreatedEventSchema(exclude=("type",))},
        unknown=EXCLUDE,
    )

    loaded = schema.deserialize({"type": "created", "name": "task"})
    dumped = schema.serialize("event", {"event": {"type": "created", "name": "task"}})

    assert loaded == {"name": "task"}
    assert dumped == {"name": "task"}


def test_list_union_propagates_dotted_only():
    class DetailedCreatedSchema(Schema):
        type = fields.Constant("created")
        name = fields.Str()
        age = fields.Int()

        class Meta:
            unknown = EXCLUDE

    class EventListOnlySchema(Schema):
        events = fields.List(
            fields.Union(
                "type",
                {"created": DetailedCreatedSchema()},
                only=("type", "name"),
            )
        )

    result = EventListOnlySchema(only=("events.type",)).load(
        {"events": [{"type": "created", "name": "task", "age": 1}]}
    )

    assert result == {"events": [{"type": "created"}]}


def test_lazy_schema_references_support_recursive_events():
    class NodeSchema(Schema):
        node = fields.Union(
            "type",
            {
                "leaf": lambda: LeafNodeSchema,
                "branch": lambda: BranchNodeSchema,
            },
        )

    class LeafNodeSchema(Schema):
        type = fields.Constant("leaf")
        value = fields.Int()

    class BranchNodeSchema(Schema):
        type = fields.Constant("branch")
        children = fields.List(
            fields.Union(
                "type",
                {
                    "leaf": lambda: LeafNodeSchema,
                    "branch": lambda: BranchNodeSchema,
                },
            )
        )

    payload = {
        "node": {
            "type": "branch",
            "children": [
                {"type": "leaf", "value": 1},
                {
                    "type": "branch",
                    "children": [{"type": "leaf", "value": 2}],
                },
            ],
        }
    }

    assert NodeSchema().load(payload) == payload
    assert NodeSchema().dump(payload) == payload


def test_branch_errors_keep_nested_paths():
    class InvalidCreatedSchema(Schema):
        type = fields.Constant("created")
        count = fields.Int()

    class NestedEventSchema(Schema):
        event = fields.Nested(
            Schema.from_dict(
                {"payload": fields.Union("type", {"created": InvalidCreatedSchema})}
            )
        )

    with pytest.raises(ValidationError) as exc_info:
        NestedEventSchema().load(
            {"event": {"payload": {"type": "created", "count": "x"}}}
        )

    assert exc_info.value.messages == {
        "event": {"payload": {"count": ["Not a valid integer."]}}
    }

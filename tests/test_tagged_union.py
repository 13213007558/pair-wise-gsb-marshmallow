import pytest
from marshmallow import EXCLUDE, Schema, ValidationError, fields, post_load


class CountingSchema(Schema):
    """Schema that counts instantiations and post_load hook runs."""

    instances = 0
    post_load_calls = 0

    def __init__(self, *args, **kwargs):
        type(self).instances += 1
        super().__init__(*args, **kwargs)

    @post_load
    def _count_post_load(self, data, **kwargs):
        type(self).post_load_calls += 1
        return data


class ShellTaskSchema(CountingSchema):
    kind = fields.Str()
    command = fields.Str(required=True)


class HttpTaskSchema(CountingSchema):
    kind = fields.Str()
    url = fields.Str(required=True)


@pytest.fixture(autouse=True)
def reset_counters():
    for schema_class in (ShellTaskSchema, HttpTaskSchema):
        schema_class.instances = 0
        schema_class.post_load_calls = 0


def make_path_schema(**tagged_kwargs):
    class TaskSchema(Schema):
        payload = fields.TaggedUnion(
            {"shell": ShellTaskSchema, "http": HttpTaskSchema},
            tag_path="kind",
            **tagged_kwargs,
        )

    return TaskSchema()


def make_field_schema(**tagged_kwargs):
    class EnvelopeSchema(Schema):
        kind = fields.Str()
        payload = fields.TaggedUnion(
            {"shell": ShellTaskSchema, "http": HttpTaskSchema},
            tag_field="kind",
            **tagged_kwargs,
        )

    return EnvelopeSchema()


class TestTagSourceDeclaration:
    def test_both_tag_sources_rejected(self):
        with pytest.raises(ValueError, match="Exactly one"):
            fields.TaggedUnion(
                {"shell": ShellTaskSchema}, tag_path="kind", tag_field="kind"
            )

    def test_no_tag_source_rejected(self):
        with pytest.raises(ValueError, match="Exactly one"):
            fields.TaggedUnion({"shell": ShellTaskSchema})

    def test_empty_schemas_rejected(self):
        with pytest.raises(ValueError, match="non-empty mapping"):
            fields.TaggedUnion({}, tag_path="kind")


class TestLoadWithTagPath:
    def test_loads_selected_branch(self):
        schema = make_path_schema()
        result = schema.load(
            {"payload": {"kind": "http", "url": "https://example.com"}}
        )
        assert result == {"payload": {"kind": "http", "url": "https://example.com"}}

    def test_only_selected_branch_is_initialized(self):
        schema = make_path_schema()
        schema.load({"payload": {"kind": "http", "url": "https://example.com"}})
        assert HttpTaskSchema.instances == 1
        assert HttpTaskSchema.post_load_calls == 1
        assert ShellTaskSchema.instances == 0
        assert ShellTaskSchema.post_load_calls == 0

    def test_unknown_tag_errors_at_tag_location_and_runs_no_branch(self):
        schema = make_path_schema()
        with pytest.raises(ValidationError) as excinfo:
            schema.load({"payload": {"kind": "bogus", "url": "https://example.com"}})
        assert excinfo.value.messages == {
            "payload": {"kind": ["Unknown tag: 'bogus'."]}
        }
        assert ShellTaskSchema.instances == 0
        assert HttpTaskSchema.instances == 0
        assert ShellTaskSchema.post_load_calls == 0
        assert HttpTaskSchema.post_load_calls == 0

    def test_missing_tag_errors_at_tag_location(self):
        schema = make_path_schema()
        with pytest.raises(ValidationError) as excinfo:
            schema.load({"payload": {"url": "https://example.com"}})
        assert excinfo.value.messages == {"payload": {"kind": ["Missing tag."]}}

    def test_branch_field_errors_are_reported(self):
        schema = make_path_schema()
        with pytest.raises(ValidationError) as excinfo:
            schema.load({"payload": {"kind": "shell"}})
        assert excinfo.value.messages == {
            "payload": {"command": ["Missing data for required field."]}
        }

    def test_dotted_tag_path(self):
        class Nested(Schema):
            payload = fields.TaggedUnion(
                {"shell": ShellTaskSchema, "http": HttpTaskSchema},
                tag_path="meta.kind",
                unknown=EXCLUDE,
            )

        schema = Nested()
        result = schema.load({"payload": {"meta": {"kind": "shell"}, "command": "ls"}})
        assert result["payload"]["command"] == "ls"
        with pytest.raises(ValidationError) as excinfo:
            schema.load({"payload": {"meta": {"kind": "bogus"}}})
        assert excinfo.value.messages == {
            "payload": {"meta": {"kind": ["Unknown tag: 'bogus'."]}}
        }

    def test_partial_is_passed_to_branch(self):
        schema = make_path_schema()
        result = schema.load({"payload": {"kind": "http"}}, partial=True)
        assert result == {"payload": {"kind": "http"}}

    def test_unknown_option_is_passed_to_branch(self):
        schema = make_path_schema(unknown=EXCLUDE)
        result = schema.load(
            {"payload": {"kind": "http", "url": "https://example.com", "extra": 1}}
        )
        assert result == {"payload": {"kind": "http", "url": "https://example.com"}}

    def test_branch_data_key_is_respected(self):
        class HttpWithKey(Schema):
            kind = fields.Str()
            url = fields.Str(data_key="uri")

        class TaskSchema(Schema):
            payload = fields.TaggedUnion({"http": HttpWithKey}, tag_path="kind")

        schema = TaskSchema()
        result = schema.load({"payload": {"kind": "http", "uri": "https://x"}})
        assert result == {"payload": {"kind": "http", "url": "https://x"}}
        assert schema.dump(result) == {"payload": {"kind": "http", "uri": "https://x"}}


class TestLoadWithTagField:
    def test_loads_branch_selected_by_outer_field(self):
        schema = make_field_schema()
        result = schema.load({"kind": "shell", "payload": {"command": "ls -la"}})
        assert result == {"kind": "shell", "payload": {"command": "ls -la"}}

    def test_only_selected_branch_is_initialized(self):
        schema = make_field_schema()
        schema.load({"kind": "http", "payload": {"url": "https://example.com"}})
        assert HttpTaskSchema.instances == 1
        assert ShellTaskSchema.instances == 0
        assert ShellTaskSchema.post_load_calls == 0

    def test_unknown_tag_errors_at_tag_field_and_runs_no_branch(self):
        schema = make_field_schema()
        with pytest.raises(ValidationError) as excinfo:
            schema.load({"kind": "bogus", "payload": {"command": "ls"}})
        assert excinfo.value.messages == {
            "payload": {"kind": ["Unknown tag: 'bogus'."]}
        }
        assert ShellTaskSchema.post_load_calls == 0
        assert HttpTaskSchema.post_load_calls == 0


class TestDump:
    def test_dump_uses_declared_tag_not_trial_and_error(self):
        # The object has attributes for both branches; only the tagged
        # branch's fields may appear in the output.
        schema = make_path_schema()
        obj = {"kind": "http", "url": "https://example.com", "command": "ls"}
        assert schema.dump({"payload": obj}) == {
            "payload": {"kind": "http", "url": "https://example.com"}
        }

    def test_dump_with_tag_field(self):
        schema = make_field_schema()
        obj = {"kind": "shell", "payload": {"command": "ls", "url": "https://x"}}
        assert schema.dump(obj) == {"kind": "shell", "payload": {"command": "ls"}}

    def test_dump_unknown_tag_raises(self):
        schema = make_path_schema()
        with pytest.raises(ValidationError) as excinfo:
            schema.dump({"payload": {"kind": "bogus", "url": "https://x"}})
        assert excinfo.value.messages == {"kind": ["Unknown tag: 'bogus'."]}

    def test_dump_missing_tag_raises(self):
        schema = make_path_schema()
        with pytest.raises(ValidationError) as excinfo:
            schema.dump({"payload": {"url": "https://x"}})
        assert excinfo.value.messages == {"kind": ["Missing tag."]}


class TestCollections:
    def test_many_loads_each_item_with_its_own_branch(self):
        schema = make_path_schema(many=True)
        result = schema.load(
            {
                "payload": [
                    {"kind": "http", "url": "https://example.com"},
                    {"kind": "shell", "command": "ls"},
                ]
            }
        )
        assert result == {
            "payload": [
                {"kind": "http", "url": "https://example.com"},
                {"kind": "shell", "command": "ls"},
            ]
        }

    def test_many_errors_keep_list_index_and_branch_paths(self):
        schema = make_path_schema(many=True)
        with pytest.raises(ValidationError) as excinfo:
            schema.load(
                {
                    "payload": [
                        {"kind": "http", "url": "https://example.com"},
                        {"kind": "shell"},
                        {"kind": "bogus"},
                    ]
                }
            )
        assert excinfo.value.messages == {
            "payload": {
                1: {"command": ["Missing data for required field."]},
                2: {"kind": ["Unknown tag: 'bogus'."]},
            }
        }

    def test_many_dump(self):
        schema = make_path_schema(many=True)
        result = schema.dump(
            {
                "payload": [
                    {"kind": "http", "url": "https://example.com"},
                    {"kind": "shell", "command": "ls"},
                ]
            }
        )
        assert result == {
            "payload": [
                {"kind": "http", "url": "https://example.com"},
                {"kind": "shell", "command": "ls"},
            ]
        }

    def test_nested_in_list_field(self):
        class TaskSchema(Schema):
            payload = fields.List(
                fields.TaggedUnion(
                    {"shell": ShellTaskSchema, "http": HttpTaskSchema},
                    tag_path="kind",
                )
            )

        schema = TaskSchema()
        with pytest.raises(ValidationError) as excinfo:
            schema.load(
                {"payload": [{"kind": "http", "url": "https://x"}, {"kind": "shell"}]}
            )
        assert excinfo.value.messages == {
            "payload": {1: {"command": ["Missing data for required field."]}}
        }


class TestSchemaResolution:
    def test_lazy_schema_resolution(self):
        class TaskSchema(Schema):
            payload = fields.TaggedUnion(
                {"shell": lambda: ShellTaskSchema, "http": lambda: HttpTaskSchema},
                tag_path="kind",
            )

        result = TaskSchema().load({"payload": {"kind": "shell", "command": "ls"}})
        assert result == {"payload": {"kind": "shell", "command": "ls"}}
        assert ShellTaskSchema.instances == 1
        assert HttpTaskSchema.instances == 0

    def test_schema_instances_and_dicts(self):
        class TaskSchema(Schema):
            payload = fields.TaggedUnion(
                {
                    "shell": ShellTaskSchema(),
                    "http": {"kind": fields.Str(), "url": fields.Str(required=True)},
                },
                tag_path="kind",
            )

        schema = TaskSchema()
        assert schema.load({"payload": {"kind": "http", "url": "https://x"}}) == {
            "payload": {"kind": "http", "url": "https://x"}
        }
        assert schema.load({"payload": {"kind": "shell", "command": "ls"}}) == {
            "payload": {"kind": "shell", "command": "ls"}
        }

    def test_invalid_branch_type_raises(self):
        class TaskSchema(Schema):
            payload = fields.TaggedUnion({"oops": 42}, tag_path="kind")

        with pytest.raises(ValueError, match="must be a"):
            TaskSchema().load({"payload": {"kind": "oops"}})

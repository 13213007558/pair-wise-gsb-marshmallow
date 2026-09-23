"""Tests for structured, translation-independent validation issues.

``ValidationError.issues`` mirrors ``messages`` but carries a stable code,
params, a segmented input path, and the pipeline stage, so frontends can
reason about errors without parsing display messages.
"""

import pytest
from marshmallow import (
    Schema,
    ValidationError,
    fields,
    pre_load,
    validate,
    validates,
    validates_schema,
)
from marshmallow.exceptions import USER_ERROR, Issue, IssueStage


def load_error(schema, data) -> ValidationError:
    with pytest.raises(ValidationError) as excinfo:
        schema.load(data)
    return excinfo.value


def only_issue(schema, data) -> Issue:
    error = load_error(schema, data)
    assert len(error.issues) == 1
    return error.issues[0]


class SimpleSchema(Schema):
    name = fields.String(required=True)
    age = fields.Integer()


class TestFieldIssues:
    def test_required_issue(self):
        issue = only_issue(SimpleSchema(), {"age": 1})
        assert issue.code == "required"
        assert issue.path == ("name",)
        assert issue.stage == IssueStage.FIELD_VALIDATION
        assert issue.message == "Missing data for required field."

    def test_null_issue(self):
        issue = only_issue(SimpleSchema(), {"name": None})
        assert issue.code == "null"
        assert issue.path == ("name",)
        assert issue.stage == IssueStage.FIELD_VALIDATION

    def test_conversion_issue(self):
        issue = only_issue(SimpleSchema(), {"name": "n", "age": "not-an-int"})
        assert issue.code == "invalid"
        assert issue.path == ("age",)
        assert issue.stage == IssueStage.FIELD_CONVERSION
        assert issue.params["input"] == "not-an-int"

    def test_validator_issue(self):
        class MySchema(Schema):
            name = fields.String(validate=validate.Length(min=2))

        issue = only_issue(MySchema(), {"name": "x"})
        assert issue.code == "length"
        assert issue.params == {"min": 2, "max": None, "equal": None}
        assert issue.path == ("name",)
        assert issue.stage == IssueStage.FIELD_VALIDATION

    def test_custom_code(self):
        def check(value):
            raise ValidationError("no good", code="no_good", params={"v": value})

        class MySchema(Schema):
            name = fields.String(validate=check)

        issue = only_issue(MySchema(), {"name": "x"})
        assert issue.code == "no_good"
        assert issue.params == {"v": "x"}
        assert issue.message == "no good"

    def test_missing_code_defaults_to_user_error(self):
        def check(value):
            raise ValidationError("no good")

        class MySchema(Schema):
            name = fields.String(validate=check)

        issue = only_issue(MySchema(), {"name": "x"})
        assert issue.code == USER_ERROR

    def test_unknown_field_issue(self):
        issue = only_issue(SimpleSchema(), {"name": "n", "extra": 1})
        assert issue.code == "unknown"
        assert issue.path == ("extra",)
        assert issue.stage == IssueStage.SCHEMA_VALIDATION

    def test_schema_type_issue(self):
        issue = only_issue(SimpleSchema(), "not-a-mapping")
        assert issue.code == "type"
        assert issue.path == ()
        assert issue.stage == IssueStage.SCHEMA_VALIDATION


class TestPaths:
    def test_nested_path(self):
        class Inner(Schema):
            age = fields.Integer(required=True)

        class Outer(Schema):
            inner = fields.Nested(Inner)

        issue = only_issue(Outer(), {"inner": {"age": "x"}})
        assert issue.code == "invalid"
        assert issue.path == ("inner", "age")

    def test_list_index_is_int(self):
        class MySchema(Schema):
            items = fields.List(fields.Integer())

        issue = only_issue(MySchema(), {"items": ["x"]})
        assert issue.path == ("items", 0)
        assert isinstance(issue.path[1], int)

    def test_list_of_nested_path(self):
        class Inner(Schema):
            age = fields.Integer()

        class Outer(Schema):
            inners = fields.List(fields.Nested(Inner))

        issue = only_issue(Outer(), {"inners": [{"age": 1}, {"age": "x"}]})
        assert issue.path == ("inners", 1, "age")

    def test_field_named_zero_is_distinct_from_index_zero(self):
        class ListSchema(Schema):
            items = fields.List(fields.Integer())

        class ZeroSchema(Schema):
            zero = fields.Integer(data_key="0")

        index_issue = only_issue(ListSchema(), {"items": ["x"]})
        name_issue = only_issue(ZeroSchema(), {"0": "x"})
        assert index_issue.path == ("items", 0)
        assert name_issue.path == ("0",)
        assert isinstance(name_issue.path[0], str)
        assert name_issue.path != (0,)

    def test_many_paths_are_indexed(self):
        error = load_error(SimpleSchema(many=True), [{"name": "a"}, {"age": "x"}])
        paths = [issue.path for issue in error.issues]
        assert (1, "name") in paths
        assert (1, "age") in paths
        assert all(isinstance(path[0], int) for path in paths)

    def test_dict_key_and_value_issues(self):
        class MySchema(Schema):
            mapping = fields.Dict(keys=fields.Integer(), values=fields.Integer())

        error = load_error(MySchema(), {"mapping": {"k": "v"}})
        by_path = {issue.path: issue for issue in error.issues}
        assert by_path[("mapping", "k", "key")].code == "invalid"
        assert by_path[("mapping", "k", "value")].code == "invalid"

    def test_tuple_index_is_int(self):
        class MySchema(Schema):
            pair = fields.Tuple((fields.Integer(), fields.Integer()))

        issue = only_issue(MySchema(), {"pair": (1, "x")})
        assert issue.path == ("pair", 1)
        assert isinstance(issue.path[1], int)


class TestValidatorHooks:
    def test_multi_field_validates(self):
        class MySchema(Schema):
            a = fields.Integer()
            b = fields.Integer()

            @validates("a", "b")
            def check(self, value, data_key):
                raise ValidationError("unlucky", code="unlucky")

        error = load_error(MySchema(), {"a": 1, "b": 2})
        assert error.messages == {"a": ["unlucky"], "b": ["unlucky"]}
        by_path = {issue.path: issue for issue in error.issues}
        assert by_path[("a",)].code == "unlucky"
        assert by_path[("b",)].code == "unlucky"
        assert all(issue.stage == IssueStage.FIELD_VALIDATION for issue in error.issues)

    def test_validates_schema_root_issue(self):
        class MySchema(Schema):
            a = fields.Integer()

            @validates_schema
            def check(self, data, **kwargs):
                raise ValidationError("bad combo")

        issue = only_issue(MySchema(), {"a": 1})
        assert issue.code == USER_ERROR
        assert issue.path == ()
        assert issue.stage == IssueStage.SCHEMA_VALIDATION

    def test_validates_schema_dict_messages(self):
        class MySchema(Schema):
            a = fields.Integer()

            @validates_schema
            def check(self, data, **kwargs):
                raise ValidationError({"a": ["schema says no"]}, code="schema_no")

        issue = only_issue(MySchema(), {"a": 1})
        assert issue.code == "schema_no"
        assert issue.path == ("a",)
        assert issue.stage == IssueStage.SCHEMA_VALIDATION

    def test_pre_load_issue(self):
        class MySchema(Schema):
            a = fields.Integer()

            @pre_load
            def pre(self, data, **kwargs):
                raise ValidationError("pre failed", code="pre")

        issue = only_issue(MySchema(), {"a": 1})
        assert issue.code == "pre"
        assert issue.path == ()
        assert issue.stage == IssueStage.SCHEMA_VALIDATION

    def test_and_combines_issues(self):
        class MySchema(Schema):
            name = fields.String(
                validate=validate.And(
                    validate.Length(min=5),
                    validate.Regexp(r"^[a-z]+$"),
                )
            )

        error = load_error(MySchema(), {"name": "AB"})
        assert [issue.code for issue in error.issues] == ["length", "regexp"]
        assert all(issue.path == ("name",) for issue in error.issues)


class TestIssuesAreNotMerged:
    def test_same_path_issues_are_all_kept(self):
        class MySchema(Schema):
            x = fields.Integer(validate=validate.Range(min=10))

            @validates_schema(skip_on_field_errors=False)
            def check(self, data, **kwargs):
                raise ValidationError({"x": ["schema says no"]})

            @validates_schema(skip_on_field_errors=False)
            def check2(self, data, **kwargs):
                raise ValidationError({"x": ["schema says no again"]})

        error = load_error(MySchema(), {"x": 1})
        assert error.messages == {
            "x": [
                "Must be greater than or equal to 10.",
                "schema says no",
                "schema says no again",
            ]
        }
        assert len(error.issues) == 3
        assert [issue.path for issue in error.issues] == [("x",)] * 3
        assert [issue.code for issue in error.issues] == [
            "range",
            USER_ERROR,
            USER_ERROR,
        ]


class TestBackwardsCompatibility:
    def test_messages_normalized_messages_and_valid_data(self):
        class Inner(Schema):
            age = fields.Integer(required=True)
            name = fields.String()

        class Outer(Schema):
            inner = fields.Nested(Inner)

        error = load_error(Outer(), {"inner": {"age": "x", "name": "ok"}})
        assert error.messages == {"inner": {"age": ["Not a valid integer."]}}
        assert error.normalized_messages() == error.messages
        assert error.valid_data == {"inner": {"name": "ok"}}

    def test_validation_error_attributes(self):
        error = ValidationError("foo", field_name="bar")
        assert error.messages == ["foo"]
        assert error.normalized_messages() == {"bar": ["foo"]}
        assert error.code == USER_ERROR
        assert error.issues == [
            Issue(code=USER_ERROR, message="foo", params={}, path=(), stage=None)
        ]

    def test_issue_to_dict(self):
        issue = only_issue(SimpleSchema(), {"name": None})
        assert issue.to_dict() == {
            "code": "null",
            "message": "Field may not be null.",
            "params": {},
            "path": ["name"],
            "stage": IssueStage.FIELD_VALIDATION,
        }


def make_i18n_schema(*, zh: bool):
    """Build equivalent schemas with English or Chinese display messages."""
    if zh:
        messages: dict = {
            "required": "必填字段缺失。",
            "null": "字段不能为 null。",
            "invalid": "无效值。",
            "validator_failed": "校验失败。",
        }
        length_error = "长度不能小于 {min}。"
        schema_error = "组合不合法"
    else:
        messages = {}
        length_error = None
        schema_error = "invalid combination"

    class I18nSchema(Schema):
        name = fields.String(
            required=True,
            error_messages=messages,
            validate=validate.Length(min=2, error=length_error),
        )
        age = fields.Integer(error_messages=messages)

        @validates_schema(skip_on_field_errors=False)
        def check(self, data, **kwargs):
            raise ValidationError(schema_error)

    return I18nSchema()


class TestTranslationInvariance:
    @pytest.mark.parametrize("zh", [False, True])
    def test_display_messages_are_translatable(self, zh):
        schema = make_i18n_schema(zh=zh)
        error = load_error(schema, {"age": "x"})
        if zh:
            assert error.messages == {
                "name": ["必填字段缺失。"],
                "age": ["无效值。"],
                "_schema": ["组合不合法"],
            }
        else:
            assert error.messages == {
                "name": ["Missing data for required field."],
                "age": ["Not a valid integer."],
                "_schema": ["invalid combination"],
            }

    def test_issues_are_identical_across_translations(self):
        data = {"name": "x", "age": "not-an-int"}
        en_error = load_error(make_i18n_schema(zh=False), data)
        zh_error = load_error(make_i18n_schema(zh=True), data)

        en_fingerprints = [
            (issue.code, issue.path, issue.stage) for issue in en_error.issues
        ]
        zh_fingerprints = [
            (issue.code, issue.path, issue.stage) for issue in zh_error.issues
        ]
        assert en_fingerprints == zh_fingerprints

        # params survive translation as well
        en_params = [issue.params for issue in en_error.issues]
        zh_params = [issue.params for issue in zh_error.issues]
        assert en_params == zh_params

        # only the display messages differ
        assert [issue.message for issue in en_error.issues] != [
            issue.message for issue in zh_error.issues
        ]

    def test_length_validator_translation(self):
        en_error = load_error(make_i18n_schema(zh=False), {"name": "x"})
        zh_error = load_error(make_i18n_schema(zh=True), {"name": "x"})
        (en,) = (i for i in en_error.issues if i.path == ("name",))
        (zh,) = (i for i in zh_error.issues if i.path == ("name",))
        assert en.code == zh.code == "length"
        assert en.path == zh.path == ("name",)
        assert en.params == zh.params == {"min": 2, "max": None, "equal": None}
        assert zh.message == "长度不能小于 2。"

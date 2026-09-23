"""Tests for ValidationError.issues: structured, translation-independent errors."""

import pytest

from marshmallow import (
    RAISE,
    Schema,
    ValidationError,
    ValidationIssue,
    fields,
    validate,
    validates,
    validates_schema,
)
from marshmallow.exceptions import (
    STAGE_DESERIALIZE,
    STAGE_SCHEMA_VALIDATE,
    STAGE_VALIDATE,
    USER_ERROR_CODE,
)


def load_issues(schema, data):
    try:
        schema.load(data)
    except ValidationError as error:
        return error.issues
    raise AssertionError("ValidationError not raised")


def codes(issues):
    return [issue.code for issue in issues]


def paths(issues):
    return [issue.path for issue in issues]


class TestIssueBasics:
    def test_field_deserialize_error(self):
        class S(Schema):
            age = fields.Int()

        issues = load_issues(S(), {"age": "abc"})
        assert len(issues) == 1
        issue = issues[0]
        assert issue.code == "invalid"
        assert issue.path == ("age",)
        assert issue.stage == STAGE_DESERIALIZE
        assert "input" in issue.params
        assert issue.message

    def test_required_error(self):
        class S(Schema):
            name = fields.Str(required=True)

        issues = load_issues(S(), {})
        assert codes(issues) == ["required"]
        assert paths(issues) == [("name",)]
        assert issues[0].stage == STAGE_DESERIALIZE

    def test_null_error(self):
        class S(Schema):
            name = fields.Str()

        issues = load_issues(S(), {"name": None})
        assert codes(issues) == ["null"]

    def test_validator_error_has_code_params_and_stage(self):
        class S(Schema):
            age = fields.Int(validate=validate.Range(min=1, max=10))

        issues = load_issues(S(), {"age": 11})
        assert len(issues) == 1
        issue = issues[0]
        assert issue.code == "range"
        assert issue.params == {"input": 11, "min": 1, "max": 10}
        assert issue.path == ("age",)
        assert issue.stage == STAGE_VALIDATE

    def test_custom_validation_error_with_code(self):
        class S(Schema):
            balance = fields.Int()

            @validates("balance")
            def check_balance(self, value, **kwargs):
                if value < 0:
                    raise ValidationError(
                        "余额不能为负数",
                        code="negative_balance",
                        params={"balance": value},
                    )

        issues = load_issues(S(), {"balance": -1})
        assert len(issues) == 1
        issue = issues[0]
        assert issue.code == "negative_balance"
        assert issue.params == {"balance": -1}
        assert issue.message == "余额不能为负数"
        assert issue.stage == STAGE_VALIDATE

    def test_custom_validation_error_without_code_is_user_error(self):
        class S(Schema):
            name = fields.Str()

            @validates("name")
            def check_name(self, value, **kwargs):
                if value == "bad":
                    raise ValidationError("Nope")

        issues = load_issues(S(), {"name": "bad"})
        assert codes(issues) == [USER_ERROR_CODE]
        assert [issue.message for issue in issues] == ["Nope"]

    def test_index_zero_vs_field_named_zero(self):
        class Item(Schema):
            age = fields.Int()

        class ManySchema(Schema):
            items = fields.List(fields.Nested(Item))

        issues = load_issues(ManySchema(), {"items": [{"age": "x"}]})
        assert paths(issues) == [("items", 0, "age")]
        assert isinstance(issues[0].path[1], int)

        ZeroSchema = Schema.from_dict({"0": fields.Int()})
        issues = load_issues(ZeroSchema(), {"0": "x"})
        assert paths(issues) == [("0",)]
        assert isinstance(issues[0].path[0], str)

    def test_nested_path(self):
        class Child(Schema):
            name = fields.Str(required=True)

        class Parent(Schema):
            child = fields.Nested(Child)

        issues = load_issues(Parent(), {"child": {}})
        assert codes(issues) == ["required"]
        assert paths(issues) == [("child", "name")]
        assert issues[0].stage == STAGE_DESERIALIZE

    def test_deeply_nested_path(self):
        class Leaf(Schema):
            value = fields.Int()

        class Mid(Schema):
            leaf = fields.Nested(Leaf)

        class Root(Schema):
            mid = fields.Nested(Mid)

        issues = load_issues(Root(), {"mid": {"leaf": {"value": "bad"}}})
        assert paths(issues) == [("mid", "leaf", "value")]

    def test_list_of_scalars(self):
        class S(Schema):
            nums = fields.List(fields.Int())

        issues = load_issues(S(), {"nums": [1, "bad", 3, "also-bad"]})
        assert codes(issues) == ["invalid", "invalid"]
        assert paths(issues) == [("nums", 1), ("nums", 3)]

    def test_tuple_items(self):
        class S(Schema):
            row = fields.Tuple((fields.Int(), fields.Int()))

        issues = load_issues(S(), {"row": ["bad", "also-bad"]})
        assert paths(issues) == [("row", 0), ("row", 1)]

    def test_dict_key_and_value_validation(self):
        class S(Schema):
            mapping = fields.Dict(keys=fields.Int(), values=fields.Int())

        issues = load_issues(S(), {"mapping": {"abc": "xyz"}})
        issue_paths = set(paths(issues))
        assert ("mapping", "abc", "key") in issue_paths
        assert ("mapping", "abc") in issue_paths

    def test_dict_value_nested_path(self):
        class Inner(Schema):
            n = fields.Int()

        class S(Schema):
            mapping = fields.Dict(keys=fields.Str(), values=fields.Nested(Inner))

        issues = load_issues(S(), {"mapping": {"a": {"n": "bad"}}})
        assert paths(issues) == [("mapping", "a", "n")]

    def test_validates_schema_stage_and_root_path(self):
        class S(Schema):
            a = fields.Int()
            b = fields.Int()

            @validates_schema
            def check_sum(self, data, **kwargs):
                if data["a"] + data["b"] != 10:
                    raise ValidationError("a 加 b 必须等于 10")

        issues = load_issues(S(), {"a": 1, "b": 2})
        assert len(issues) == 1
        issue = issues[0]
        assert issue.code == USER_ERROR_CODE
        assert issue.path == ()
        assert issue.stage == STAGE_SCHEMA_VALIDATE

    def test_validates_schema_with_field_name(self):
        class S(Schema):
            password = fields.Str()
            confirm = fields.Str()

            @validates_schema
            def check(self, data, **kwargs):
                if data["password"] != data["confirm"]:
                    raise ValidationError("不匹配", field_name="confirm")

        issues = load_issues(S(), {"password": "a", "confirm": "b"})
        assert paths(issues) == [("confirm",)]
        assert issues[0].stage == STAGE_SCHEMA_VALIDATE

    def test_multi_field_validates_stage(self):
        class S(Schema):
            x = fields.Int()
            y = fields.Int()

            @validates("x")
            def check_x(self, value, **kwargs):
                raise ValidationError("x-bad", code="x_bad")

            @validates("y")
            def check_y(self, value, **kwargs):
                raise ValidationError("y-bad", code="y_bad")

        issues = load_issues(S(), {"x": 1, "y": 2})
        assert sorted(codes(issues)) == ["x_bad", "y_bad"]
        assert all(issue.stage == STAGE_VALIDATE for issue in issues)

    def test_multiple_issues_same_path_are_not_merged(self):
        def must_be_even(value):
            if value % 2 != 0:
                raise ValidationError("not even", code="not_even")

        class S(Schema):
            n = fields.Int(validate=[validate.Range(min=10), must_be_even])

        issues = load_issues(S(), {"n": 3})
        assert codes(issues) == ["range", "not_even"]
        assert paths(issues) == [("n",), ("n",)]
        assert len(issues) == 2

    def test_unknown_field_code(self):
        class S(Schema):
            a = fields.Int()

            class Meta:
                unknown = RAISE

        issues = load_issues(S(), {"a": 1, "extra": 2})
        assert codes(issues) == ["unknown"]
        assert paths(issues) == [("extra",)]
        assert issues[0].stage == STAGE_DESERIALIZE

    def test_schema_type_code(self):
        class S(Schema):
            a = fields.Int()

        issues = load_issues(S(), "not-a-dict")
        assert codes(issues) == ["type"]
        assert paths(issues) == [()]
        assert issues[0].stage == STAGE_DESERIALIZE

    def test_many_type_code(self):
        class S(Schema):
            a = fields.Int()

        issues = load_issues(S(many=True), "not-a-list")
        assert codes(issues) == ["type"]
        assert paths(issues) == [()]

    def test_many_indexed_paths(self):
        class S(Schema):
            age = fields.Int()

        issues = load_issues(S(many=True), [{"age": 1}, {"age": "bad"}])
        assert paths(issues) == [(1, "age")]

    def test_messages_and_valid_data_unchanged(self):
        class Child(Schema):
            name = fields.Str(required=True)

        class Parent(Schema):
            child = fields.Nested(Child)
            age = fields.Int(validate=validate.Range(min=0))
            tag = fields.Str()

        with pytest.raises(ValidationError) as exc_info:
            Parent().load({"child": {}, "age": -1, "tag": "ok"})
        error = exc_info.value
        assert error.messages == {
            "child": {"name": ["Missing data for required field."]},
            "age": ["Must be greater than or equal to 0."],
        }
        assert error.normalized_messages() == error.messages
        assert error.valid_data == {"tag": "ok"}

    def test_validate_api_unchanged(self):
        class S(Schema):
            age = fields.Int()

        assert S().validate({"age": "bad"}) == {"age": ["Not a valid integer."]}

    def test_builtin_validator_codes(self):
        cases = [
            (fields.Url(), "not a url", "url"),
            (fields.Email(), "not an email", "email"),
            (fields.Str(validate=validate.Regexp(r"^a+$")), "bbb", "regexp"),
            (fields.Int(validate=validate.OneOf([1, 2])), 3, "one_of"),
            (fields.Int(validate=validate.NoneOf([1, 2])), 1, "none_of"),
            (
                fields.List(fields.Int(), validate=validate.ContainsOnly([1, 2])),
                [1, 3],
                "contains_only",
            ),
            (
                fields.List(fields.Int(), validate=validate.ContainsNoneOf([1])),
                [1],
                "contains_none_of",
            ),
            (fields.Int(validate=validate.Equal(5)), 6, "equal"),
            (fields.Str(validate=validate.Length(min=3)), "a", "length"),
        ]
        for field, value, code in cases:

            class S(Schema):
                x = field

            issues = load_issues(S(), {"x": value})
            assert codes(issues) == [code], field

    def test_chinese_messages_leave_codes_and_paths_untouched(self):
        """Acceptance: display messages may be translated; issues stay stable."""
        zh_field_messages = {
            "required": "缺少必填字段",
            "null": "字段不能为 null",
            "invalid": "不是合法的整数",
            "too_large": "数值过大",
        }

        class ChildEN(Schema):
            name = fields.Str(required=True)

        class ChildZH(Schema):
            name = fields.Str(
                required=True, error_messages={"required": "缺少必填字段"}
            )

        class ParentEN(Schema):
            child = fields.Nested(ChildEN)
            age = fields.Int(validate=validate.Range(min=0))

        class ParentZH(Schema):
            child = fields.Nested(ChildZH)
            age = fields.Int(
                error_messages=zh_field_messages,
                validate=validate.Range(min=0, error="必须大于等于 {min}"),
            )

        en_issues = load_issues(ParentEN(), {"child": {}, "age": -1})
        zh_issues = load_issues(ParentZH(), {"child": {}, "age": -1})

        assert len(en_issues) == len(zh_issues) == 2
        for en, zh in zip(en_issues, zh_issues, strict=True):
            assert en.code == zh.code
            assert en.path == zh.path
            assert en.stage == zh.stage
            assert en.params == zh.params
            assert en.message != zh.message


class TestIssueObject:
    def test_validation_issue_normalizes_path(self):
        issue = ValidationIssue(code="x", message="m", path=["0", 0])
        assert issue.path == ("0", 0)
        assert issue.params == {}
        assert issue.stage is None

    def test_dict_message_derives_issues(self):
        err = ValidationError({"user": {"email": ["bad"]}})
        assert len(err.issues) == 1
        assert err.issues[0].path == ("user", "email")
        assert err.issues[0].code == USER_ERROR_CODE

    def test_explicit_issues_preserved(self):
        issue = ValidationIssue(code="c", message="m", path=("a",))
        err = ValidationError("whatever", issues=[issue])
        assert err.issues == [issue]

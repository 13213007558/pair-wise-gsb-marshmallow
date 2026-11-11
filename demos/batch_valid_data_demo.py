"""
marshmallow 4.4.0 批量 load() 部分结果语义验证
=================================================
场景：批量转账指令。业务想在捕获 ValidationError 后继续消费 exc.valid_data，
只丢掉报错字段 / 报错元素。本脚本证明这样能否绕过跨字段安全约束。

运行：.venv/bin/python demos/batch_valid_data_demo.py
"""
from __future__ import annotations

import pprint

from marshmallow import (
    Schema,
    ValidationError,
    fields,
    validates,
    validates_schema,
    validate,
)

pp = pprint.PrettyPrinter(width=100, sort_dicts=False)


def show(title: str) -> None:
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


def run(schema: Schema, payload, label: str) -> None:
    print(f"\n--- {label} ---")
    try:
        result = schema.load(payload, many=True)
        print("load() 成功，结果：")
        pp.pprint(result)
    except ValidationError as err:
        print("抛出 ValidationError")
        print("messages（错误索引即原请求下标）：")
        pp.pprint(err.messages)
        print("exc.valid_data（与输入列表同序、等长）：")
        pp.pprint(err.valid_data)


# ----------------------------------------------------------------------
# 业务 schema：
#   amount       字段类型转换 + 内置 range validator
#   account      字段级 @validates（账号必须 6 位数字字符串）
#   mfa_code     字段级校验（大额转账必须是 6 位数字）
#   @validates_schema 跨字段安全约束：amount > 10000 时 mfa_code 必填且合法
# ----------------------------------------------------------------------
class TransferSchemaDefault(Schema):
    """skip_on_field_errors 取默认值 True（当前实现的默认语义）。"""

    amount = fields.Integer(required=True, validate=validate.Range(min=1))
    account = fields.String(required=True)
    mfa_code = fields.String(required=False, allow_none=True)

    @validates("account")
    def check_account(self, value, data_key, **kwargs):
        if not (isinstance(value, str) and len(value) == 6 and value.isdigit()):
            raise ValidationError("account must be 6 digits")

    @validates("mfa_code")
    def check_mfa(self, value, data_key, **kwargs):
        if value is None:
            return
        if not (len(value) == 6 and value.isdigit()):
            raise ValidationError("mfa_code must be 6 digits")

    @validates_schema
    def high_value_requires_mfa(self, data, **kwargs):
        # 安全约束：大额转账必须带合法 MFA
        if data.get("amount", 0) > 10000 and not data.get("mfa_code"):
            raise ValidationError("amount>10000 requires mfa_code", field_name="mfa_code")


class TransferSchemaNoSkip(TransferSchemaDefault):
    """把跨字段安全校验显式改成 skip_on_field_errors=False。"""

    high_value_requires_mfa = validates_schema(
        TransferSchemaDefault.high_value_requires_mfa,
        skip_on_field_errors=False,
    )


# ----------------------------------------------------------------------
# 嵌套场景：收款人地址
# ----------------------------------------------------------------------
class AddressSchema(Schema):
    city = fields.String(required=True, validate=validate.Length(min=2))
    zip_code = fields.String(required=True, validate=validate.Length(equal=6))


class NestedTransferSchema(TransferSchemaDefault):
    payee_address = fields.Nested(AddressSchema, required=True)


# ----------------------------------------------------------------------
# pass_collection=True 的集合级安全约束：整批总额不得超限
# ----------------------------------------------------------------------
class CollectionSchema(TransferSchemaDefault):
    @validates_schema(pass_collection=True)
    def batch_total_cap(self, collection, **kwargs):
        total = sum(item.get("amount", 0) for item in collection)
        if total > 50000:
            raise ValidationError(f"batch total {total} exceeds cap 50000")


if __name__ == "__main__":
    good_payload = [
        {"amount": "100", "account": "123456", "mfa_code": None},
        {"amount": 20000, "account": "654321", "mfa_code": "111111"},
    ]

    # 场景 A：完全合法 —— valid_data 全量经过字段转换 + 全部约束
    show("场景 A：合法批次（基线，看哪些转换真正发生）")
    run(TransferSchemaDefault(), good_payload, "默认 skip_on_field_errors=True")

    # 场景 B：部分元素字段失败 + 一个元素触发跨字段安全约束
    show("场景 B：部分元素字段失败，跨字段安全约束被整体跳过（默认 True）")
    payload_b = [
        {"amount": "500", "account": "123456", "mfa_code": None},        # 合法
        {"amount": "abc", "account": "123456", "mfa_code": None},        # amount 字段失败
        {"amount": 99999, "account": "654321", "mfa_code": None},         # 应触发大额 MFA 约束
    ]
    run(TransferSchemaDefault(), payload_b, "默认 skip_on_field_errors=True")

    # 场景 C：同样的载荷，关掉 skip —— 安全约束会跑，但输入是“残缺 dict”
    show("场景 C：skip_on_field_errors=False，安全约束在残缺数据上运行")
    run(TransferSchemaNoSkip(), payload_b, "skip_on_field_errors=False")

    # 场景 D：攻击者故意打坏 mfa_code 字段，试图让它从 valid_data 消失
    show("场景 D：恶意输入——打坏受保护字段以规避跨字段约束（默认 True）")
    payload_d = [
        {"amount": 99999, "account": "654321", "mfa_code": "short"},
    ]
    run(TransferSchemaDefault(), payload_d, "默认 skip_on_field_errors=True")
    run(TransferSchemaNoSkip(), payload_d, "skip_on_field_errors=False")

    # 场景 E：嵌套错误 —— 错误路径与 valid_data 中嵌套部分结果
    show("场景 E：Nested 字段内部失败")
    payload_e = [
        {"amount": 100, "account": "123456",
         "payee_address": {"city": "Shanghai", "zip_code": "200000"}},
        {"amount": 100, "account": "123456",
         "payee_address": {"city": "X", "zip_code": "bad"}},
    ]
    run(NestedTransferSchema(), payload_e, "嵌套 schema")

    # 场景 F：pass_collection 集合级约束
    show("场景 F：pass_collection=True 的整批约束")
    payload_f = [
        {"amount": 30000, "account": "123456", "mfa_code": "111111"},
        {"amount": 30000, "account": "654321", "mfa_code": "222222"},
        {"amount": "x", "account": "123456"},  # 字段失败
    ]
    run(CollectionSchema(), payload_f, "集合级 + 字段失败混合")

    # 场景 G：index_errors=False 时错误结构变化（对照）
    show("场景 G：Meta.index_errors=False 时错误不再按下标分组")

    class NoIndexSchema(TransferSchemaDefault):
        class Meta:
            index_errors = False

    run(NoIndexSchema(), payload_b, "index_errors=False")

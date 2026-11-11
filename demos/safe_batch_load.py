"""
安全接入批量处理的参考实现（marshmallow 4.4.0，不改库语义）
=============================================================
原则：
  1. 不修改 marshmallow 的部分结果语义，不把 exc.valid_data 当成“已验证数据”；
  2. 第一次 load(many=True) 仅用于收集错误索引；
  3. 对未报错的元素逐个再跑一次 load(many=False)——无字段错误时 schema-level
     约束（含 skip_on_field_errors=True 的默认安全校验）一定会执行；
  4. 集合级（pass_collection=True）约束在干净元素集合上显式重放；
  5. @post_load 等转换只在二次验证成功后才生效，业务拿到的对象与全成功路径一致。

运行：.venv/bin/python demos/safe_batch_load.py
"""
from __future__ import annotations

from typing import Any

from marshmallow import (
    Schema,
    ValidationError,
    fields,
    validates,
    validates_schema,
    validate,
)


class TransferSchema(Schema):
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

    # 默认 skip_on_field_errors=True：逐元素二次验证时无字段错误，必然执行
    @validates_schema
    def high_value_requires_mfa(self, data, **kwargs):
        if data.get("amount", 0) > 10000 and not data.get("mfa_code"):
            raise ValidationError(
                "amount>10000 requires mfa_code", field_name="mfa_code"
            )


class BatchSchema(Schema):
    """集合级安全约束：整批总额上限。unknown=EXCLUDE 使其只做集合级校验。"""

    class Meta:
        unknown = "exclude"

    @validates_schema(pass_collection=True)
    def batch_total_cap(self, collection, **kwargs):
        total = sum(item.get("amount", 0) for item in collection)
        if total > 50000:
            raise ValidationError(f"batch total {total} exceeds cap 50000")


def _top_level_failed_indexes(messages: dict) -> set[int]:
    """many=True 且 index_errors=True（默认）时，错误字典的键即失败元素下标。"""
    return {key for key in messages if isinstance(key, int)}


def safe_batch_load(
    element_schema: Schema,
    raw_items: list[dict[str, Any]],
    collection_schema: Schema | None = None,
) -> tuple[list[Any], dict[int, Any]]:
    """返回 (可安全处理的元素列表, {原请求下标: 错误消息})。

    可安全处理的每个元素都经过完整的字段 + schema-level 验证；
    集合级约束在干净元素集合上单独重放，失败时整批拒绝（不做部分放行）。
    """
    # 第一次：many 路径，只用来定位失败下标，valid_data 不直接消费
    try:
        element_schema.load(raw_items, many=True)
        failed: dict[int, Any] = {}
        clean_indexes = list(range(len(raw_items)))
    except ValidationError as err:
        failed = {
            idx: err.messages[idx]
            for idx in _top_level_failed_indexes(err.messages)
        }
        clean_indexes = [i for i in range(len(raw_items)) if i not in failed]

    # 第二次：逐元素完整验证，保证默认 skip=True 的跨字段约束真正执行
    accepted: list[Any] = []
    for idx in clean_indexes:
        try:
            accepted.append(element_schema.load(raw_items[idx], many=False))
        except ValidationError as err:
            # 理论上不该发生（第一次已干净）；保留为防御性分支
            failed[idx] = err.messages

    # 第三次：集合级约束只在干净集合上重放；失败则整批拒绝
    if collection_schema is not None and accepted:
        try:
            collection_schema.load(accepted, many=True)
        except ValidationError as err:
            # 集合级错误没有元素下标，按整批错误返回，业务必须整批拒绝
            raise ValidationError(
                {"_batch": err.messages},
                data=raw_items,
            ) from err

    return accepted, dict(sorted(failed.items()))


if __name__ == "__main__":
    schema = TransferSchema()
    batch_schema = BatchSchema()

    print("案例 1：含字段失败 + 试图绕过 MFA 的大额元素")
    payload = [
        {"amount": "500", "account": "123456", "mfa_code": None},       # 干净
        {"amount": "abc", "account": "123456", "mfa_code": None},       # 字段失败
        {"amount": 99999, "account": "654321", "mfa_code": None},        # 必须被 MFA 约束拦下
    ]
    accepted, failed = safe_batch_load(schema, payload, batch_schema)
    print("  接受（仅下标 0，且经过完整约束）:", accepted)
    print("  拒绝下标 -> 错误:", failed)

    print("\n案例 2：恶意打坏 mfa_code 字段也无法混入")
    payload = [{"amount": 99999, "account": "654321", "mfa_code": "x"}]
    accepted, failed = safe_batch_load(schema, payload, batch_schema)
    print("  接受:", accepted)
    print("  拒绝下标 -> 错误:", failed)

    print("\n案例 3：元素都干净，但触发集合级总额上限 -> 整批拒绝")
    payload = [
        {"amount": 30000, "account": "123456", "mfa_code": "111111"},
        {"amount": 30000, "account": "654321", "mfa_code": "222222"},
    ]
    try:
        safe_batch_load(schema, payload, batch_schema)
    except ValidationError as err:
        print("  整批拒绝:", err.messages)

    print("\n案例 4：全部合法且总额不超限 -> 全量接受")
    payload = [
        {"amount": 100, "account": "123456", "mfa_code": None},
        {"amount": 20000, "account": "654321", "mfa_code": "111111"},
    ]
    accepted, failed = safe_batch_load(schema, payload, batch_schema)
    print("  接受:", accepted)
    print("  拒绝:", failed)

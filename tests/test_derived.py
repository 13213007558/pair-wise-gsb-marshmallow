from __future__ import annotations

from decimal import Decimal

import pytest

from marshmallow import (
    EXCLUDE,
    INCLUDE,
    RAISE,
    Schema,
    fields,
    post_load,
    validates,
    validates_schema,
)
from marshmallow.exceptions import ValidationError


INVOICE_LOG: list = []


class InvoiceSchema(Schema):
    """Two-level money derivation with a source field that can fail."""

    unit_price = fields.Decimal(required=True)
    quantity = fields.Integer(required=True)
    discount_rate = fields.Decimal(load_default=Decimal("0"))

    @validates("quantity")
    def _check_quantity(self, value, **kwargs):
        INVOICE_LOG.append(("validate:quantity", value))
        if value <= 0:
            raise ValidationError("Quantity must be positive.")

    subtotal = fields.Derived(
        lambda unit_price, quantity: (
            INVOICE_LOG.append(("compute:subtotal",)),
            unit_price * quantity,
        )[1],
        "unit_price",
        "quantity",
    )
    amount_due = fields.Derived(
        lambda subtotal, discount_rate: (
            INVOICE_LOG.append(("compute:amount_due",)),
            subtotal * (Decimal("1") - discount_rate),
        )[1],
        "subtotal",
        "discount_rate",
    )

    @validates("subtotal")
    def _check_subtotal(self, value, **kwargs):
        INVOICE_LOG.append(("validates:subtotal", value))

    @validates_schema
    def _check_object(self, data, **kwargs):
        INVOICE_LOG.append(("validates_schema", tuple(sorted(data))))

    @post_load
    def _post(self, data, **kwargs):
        INVOICE_LOG.append(("post_load",))
        return data


def test_two_level_money_pipeline_runs_once_and_in_order():
    INVOICE_LOG.clear()
    schema = InvoiceSchema()
    result = schema.load(
        {
            "unit_price": "12.5",
            "quantity": 2,
            "discount_rate": "0.1",
            "subtotal": 999,
            "amount_due": 999,
        }
    )
    assert result == {
        "unit_price": Decimal("12.5"),
        "quantity": 2,
        "discount_rate": Decimal("0.1"),
        "subtotal": Decimal("25.0"),
        "amount_due": Decimal("22.50"),
    }
    assert INVOICE_LOG.index(("validate:quantity", 2)) < INVOICE_LOG.index(
        ("compute:subtotal",)
    )
    assert INVOICE_LOG.index(("compute:subtotal",)) < INVOICE_LOG.index(
        ("compute:amount_due",)
    )
    assert INVOICE_LOG.count(("compute:subtotal",)) == 1
    assert INVOICE_LOG.count(("compute:amount_due",)) == 1
    assert INVOICE_LOG.index(("compute:subtotal",)) < INVOICE_LOG.index(
        ("validates:subtotal", Decimal("25.0"))
    )
    assert INVOICE_LOG.index(("compute:amount_due",)) < INVOICE_LOG.index(
        ("validates_schema", tuple(sorted(result)))
    )
    assert INVOICE_LOG[-1] == ("post_load",)


def test_failed_source_skips_dependents_but_independent_branch_continues():
    calls: list[str] = []

    def tracked(name, fn, dep):
        def compute(value):
            calls.append(name)
            return fn(value)

        return fields.Derived(compute, dep)

    class S(Schema):
        a = fields.Int()
        bad = fields.Int()
        b = fields.Int()

        @validates("bad")
        def _bad(self, value, **kwargs):
            if value == -1:
                raise ValidationError("bad source")

        from_a = tracked("from_a", lambda a: a + 1, "a")
        from_bad = tracked("from_bad", lambda bad: bad + 1, "bad")
        from_bad_chain = tracked(
            "from_bad_chain", lambda from_bad: from_bad + 1, "from_bad"
        )
        independent = tracked("independent", lambda b: b * 10, "b")

    schema = S()
    with pytest.raises(ValidationError) as exc:
        schema.load({"a": 1, "bad": -1, "b": 3})
    assert exc.value.messages == {"bad": ["bad source"]}
    assert exc.value.valid_data == {
        "a": 1,
        "bad": -1,
        "b": 3,
        "from_a": 2,
        "independent": 30,
    }
    assert calls == ["from_a", "independent"]


def test_unready_derived_fields_are_not_computed_on_error():
    INVOICE_LOG.clear()
    schema = InvoiceSchema()
    with pytest.raises(ValidationError) as exc:
        schema.load({"unit_price": "12.5", "quantity": 0})
    assert "quantity" in exc.value.messages
    assert "subtotal" not in exc.value.messages
    assert "amount_due" not in exc.value.messages
    assert ("compute:subtotal",) not in INVOICE_LOG
    assert ("compute:amount_due",) not in INVOICE_LOG
    assert not any(
        event[0] == "validates_schema" for event in INVOICE_LOG
    )


def test_client_cannot_submit_derived_values():
    schema = InvoiceSchema()
    result = schema.load(
        {"unit_price": "1", "quantity": 1, "subtotal": 42, "amount_due": 42}
    )
    assert result["subtotal"] == Decimal("1")
    assert result["amount_due"] == Decimal("1")


def test_derived_keys_interact_with_unknown_options():
    result = InvoiceSchema(unknown=EXCLUDE).load(
        {"unit_price": "1", "quantity": 1, "amount_due": 5}
    )
    assert result["amount_due"] == Decimal("1")

    result = InvoiceSchema(unknown=INCLUDE).load(
        {"unit_price": "1", "quantity": 1, "amount_due": 5, "extra": 7}
    )
    assert result["extra"] == 7
    assert result["amount_due"] == Decimal("1")

    with pytest.raises(ValidationError) as exc:
        InvoiceSchema(unknown=RAISE).load(
            {"unit_price": "1", "quantity": 1, "extra": 7}
        )
    assert "extra" in exc.value.messages


def test_unknown_dependency_raises_at_bind_time_with_path():
    class S(Schema):
        a = fields.Int()
        c = fields.Derived(lambda nope: nope, "nope")

    with pytest.raises(ValueError, match='unknown path "nope"'):
        S()


def test_circular_dependency_raises_at_bind_time_with_path():
    class S(Schema):
        a = fields.Derived(lambda b: b, "b")
        b = fields.Derived(lambda c: c, "c")
        c = fields.Derived(lambda a: a, "a")

    with pytest.raises(ValueError) as exc:
        S()
    message = str(exc.value)
    assert "Circular dependency" in message
    assert "a" in message and "b" in message and "c" in message


def test_self_dependency_raises_at_bind_time():
    class S(Schema):
        b = fields.Derived(lambda b: b, "b")

    with pytest.raises(ValueError, match="Circular dependency"):
        S()


def test_unrelated_fields_keep_declaration_order():
    events: list[str] = []

    class S(Schema):
        a = fields.Derived(lambda: events.append("a") or 1)
        source = fields.Int()
        b = fields.Derived(
            lambda source: events.append("b") or source, "source"
        )
        c = fields.Derived(lambda: events.append("c") or 3)

    S().load({"source": 9})
    assert events == ["a", "b", "c"]


def test_partial_loading_omits_derived_fields_with_missing_dependencies():
    schema = InvoiceSchema()
    assert schema.load({"unit_price": "10"}, partial=True) == {
        "unit_price": Decimal("10")
    }
    result = schema.load(
        {"quantity": 2, "discount_rate": "0"}, partial=("unit_price",)
    )
    assert "subtotal" not in result
    assert "amount_due" not in result


def test_partial_with_baseline_computes_without_mutating_baseline():
    baseline = {"unit_price": Decimal("10"), "quantity": 2}
    schema = InvoiceSchema()
    result = schema.load(
        {"discount_rate": "0.2"}, partial=True, baseline=baseline
    )
    assert result == {
        "discount_rate": Decimal("0.2"),
        "subtotal": Decimal("20"),
        "amount_due": Decimal("16.0"),
    }
    assert baseline == {"unit_price": Decimal("10"), "quantity": 2}


def test_baseline_values_are_not_emitted_into_output():
    class S(Schema):
        a = fields.Int()
        b = fields.Derived(lambda a: a + 1, "a")

    result = S().load({}, partial=True, baseline={"a": 1, "baseline_only": 99})
    assert result == {"b": 2}


def test_inherited_derived_field_can_be_overridden():
    class Base(Schema):
        a = fields.Int()
        doubled = fields.Derived(lambda a: a * 2, "a")

    class Child(Base):
        doubled = fields.Derived(lambda a: a + 100, "a")

    assert Child().load({"a": 1}) == {"a": 1, "doubled": 101}


def test_data_key_and_attribute_mapping():
    class S(Schema):
        price_field = fields.Decimal(data_key="unitPrice", attribute="price")
        qty_field = fields.Int(data_key="qty", attribute="qty_value")
        subtotal = fields.Derived(
            lambda price, qty_value: price * qty_value, "price", "qty_value"
        )

    result = S().load({"unitPrice": "3", "qty": 4, "subtotal": 1})
    assert result == {
        "price": Decimal("3"),
        "qty_value": 4,
        "subtotal": Decimal("12"),
    }

    class S2(Schema):
        price_field = fields.Decimal(data_key="unitPrice", attribute="price")
        qty_field = fields.Int(data_key="qty", attribute="qty_value")
        subtotal = fields.Derived(
            lambda price_field, qty_field: price_field * qty_field,
            "price_field",
            "qty_field",
        )

    assert S2().load({"unitPrice": "3", "qty": 4})["subtotal"] == Decimal("12")


def test_many_items_derive_independently_with_indexed_errors():
    schema = InvoiceSchema()
    result = schema.load(
        [
            {"unit_price": "2", "quantity": 3},
            {"unit_price": "4", "quantity": 5},
        ],
        many=True,
    )
    assert result[0]["subtotal"] == Decimal("6")
    assert result[1]["amount_due"] == Decimal("20")

    with pytest.raises(ValidationError) as exc:
        schema.load(
            [
                {"unit_price": "2", "quantity": 3},
                {"unit_price": "4", "quantity": 0},
            ],
            many=True,
        )
    assert "quantity" in exc.value.messages[1]
    assert "subtotal" not in exc.value.messages[1]
    assert "amount_due" not in exc.value.messages[1]
    assert exc.value.valid_data[0]["amount_due"] == Decimal("6")


def test_many_baseline_sequence_and_type_check():
    schema = InvoiceSchema()
    with pytest.raises(TypeError):
        schema.load(
            [{"unit_price": "2", "quantity": 3}],
            many=True,
            baseline={"unit_price": Decimal("1"), "quantity": 1},
        )
    result = schema.load(
        [{"discount_rate": "0.5"}],
        many=True,
        partial=True,
        baseline=[{"unit_price": Decimal("10"), "quantity": 4}],
    )
    assert result[0]["subtotal"] == Decimal("40")
    assert result[0]["amount_due"] == Decimal("20.0")


def test_derived_field_validators_run_and_cascade():
    class S(Schema):
        a = fields.Int()

        @validates("derived")
        def _check_derived(self, value, **kwargs):
            if value > 10:
                raise ValidationError("too big")

        derived = fields.Derived(lambda a: a * 100, "a")
        chained = fields.Derived(lambda derived: derived + 1, "derived")

    with pytest.raises(ValidationError) as exc:
        S().load({"a": 1})
    assert exc.value.messages == {"derived": ["too big"]}
    assert exc.value.valid_data == {"a": 1}


def test_derived_field_inline_validate_and_allow_none():
    class S(Schema):
        a = fields.Int()
        d = fields.Derived(
            lambda a: a, "a", validate=lambda v: v != 0 or v / v
        )
        nullable = fields.Derived(lambda: None, allow_none=True)

    assert S().load({"a": 5}) == {"a": 5, "d": 5, "nullable": None}
    with pytest.raises(ValidationError):
        S().load({"a": 0})


def test_mapping_style_compute_receives_accumulated_values():
    class S(Schema):
        a = fields.Int()
        b = fields.Int()
        total = fields.Derived(lambda values: values["a"] + values["b"], "a", "b")
        tagged = fields.Derived(
            lambda values: values["total"] * 2, "total"
        )

    result = S().load({"a": 2, "b": 3})
    assert result["total"] == 5
    assert result["tagged"] == 10


def test_dotted_dependency_paths():
    class S(Schema):
        order = fields.Dict()
        amount = fields.Derived(lambda order: order["price"] * order["qty"], "order")
        deep = fields.Derived(
            lambda order: order["price"], "order.price"
        )

    result = S().load({"order": {"price": Decimal("4"), "qty": 3}})
    assert result["amount"] == Decimal("12")
    assert result["deep"] == Decimal("4")

    with pytest.raises(ValidationError):
        S().load({"order": {"qty": 3}})


def test_derived_compute_error_is_stored_under_data_key_and_merged():
    class S(Schema):
        a = fields.Int(validate=lambda v: v or (_ for _ in ()).throw(ValidationError("src")))
        d = fields.Derived(
            lambda a: (_ for _ in ()).throw(ValidationError("derived")), "a"
        )

    # Source failure suppresses derived computation.
    with pytest.raises(ValidationError) as exc:
        S().load({"a": 0})
    assert list(exc.value.messages) == ["a"]

    # A compute failure merges under its own key without touching source errors.
    class S2(Schema):
        a = fields.Int()

        @validates("a")
        def _a(self, value, **kwargs):
            raise ValidationError("source-msg")

        d = fields.Derived(
            lambda a: (_ for _ in ()).throw(ValidationError("derived-msg")), "a"
        )

    with pytest.raises(ValidationError) as exc:
        S2().load({"a": 0})
    assert exc.value.messages["a"] == ["source-msg"]
    assert "d" not in exc.value.messages


def test_excluded_derived_dependency_raises_at_bind_time():
    class S(Schema):
        a = fields.Int()
        b = fields.Derived(lambda a: a, "a")

    with pytest.raises(ValueError, match="unknown path"):
        S(exclude=("a",))


def test_dump_roundtrip_includes_derived_value():
    schema = InvoiceSchema()
    loaded = schema.load({"unit_price": "3", "quantity": 2})
    dumped = schema.dump(loaded)
    assert dumped["subtotal"] == Decimal("6")
    assert dumped["amount_due"] == Decimal("6")

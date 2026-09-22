"""Tests for declarative derived fields (marshmallow.fields.Derived)."""

from decimal import Decimal
from types import SimpleNamespace

import pytest

from marshmallow import (
    EXCLUDE,
    Schema,
    ValidationError,
    fields,
    post_load,
    validate,
    validates_schema,
)


def make_order_schema(log, seen_by_validator, seen_by_post_load):
    """Build an order schema with a two-level money computation (subtotal,
    then discounted total) and traced compute functions.
    """

    def traced(name, func):
        def compute(values):
            log.append(name)
            return func(values)

        return compute

    class OrderSchema(Schema):
        unit_price = fields.Decimal()
        quantity = fields.Int(validate=validate.Range(min=1))
        discount = fields.Decimal()
        note = fields.Str()
        # Declared before its own dependency to prove that execution order
        # follows the dependency graph, not the declaration order.
        total = fields.Derived(
            depends_on=("subtotal", "discount"),
            compute=traced(
                "total", lambda values: values["subtotal"] * (1 - values["discount"])
            ),
        )
        subtotal = fields.Derived(
            depends_on=("unit_price", "quantity"),
            compute=traced(
                "subtotal", lambda values: values["unit_price"] * values["quantity"]
            ),
        )
        # Independent branch, unrelated to the money computation.
        summary = fields.Derived(
            depends_on=("note",),
            compute=traced("summary", lambda values: values["note"].upper()),
        )

        @validates_schema
        def check_totals(self, data, **kwargs):
            seen_by_validator.append(dict(data))
            if data["total"] > data["subtotal"]:
                raise ValidationError("total must not exceed subtotal")

        @post_load
        def record(self, data, **kwargs):
            seen_by_post_load.append(dict(data))
            return data

    return OrderSchema


@pytest.fixture
def order():
    log: list = []
    seen_by_validator: list = []
    seen_by_post_load: list = []
    schema_cls = make_order_schema(log, seen_by_validator, seen_by_post_load)
    return SimpleNamespace(
        schema_cls=schema_cls,
        log=log,
        seen_by_validator=seen_by_validator,
        seen_by_post_load=seen_by_post_load,
    )


VALID_ORDER = {
    "unit_price": "2.50",
    "quantity": 4,
    "discount": "0.1",
    "note": "gift",
}


class TestDerivedComputation:
    def test_two_level_money_computation(self, order):
        result = order.schema_cls().load(VALID_ORDER)
        assert result["subtotal"] == Decimal("10.00")
        assert result["total"] == Decimal("9.000")
        assert result["summary"] == "GIFT"

    def test_execution_order_follows_dependencies(self, order):
        order.schema_cls().load(VALID_ORDER)
        # "total" is declared before "subtotal" but depends on it, so it is
        # computed after it; the unrelated "summary" keeps declaration order.
        assert order.log == ["subtotal", "total", "summary"]

    def test_each_derived_field_is_computed_only_once(self, order):
        order.schema_cls().load(VALID_ORDER)
        assert order.log.count("subtotal") == 1
        assert order.log.count("total") == 1
        assert order.log.count("summary") == 1

    def test_schema_validator_sees_derived_values(self, order):
        order.schema_cls().load(VALID_ORDER)
        assert len(order.seen_by_validator) == 1
        seen = order.seen_by_validator[0]
        assert seen["subtotal"] == Decimal("10.00")
        assert seen["total"] == Decimal("9.000")

    def test_post_load_sees_derived_values(self, order):
        order.schema_cls().load(VALID_ORDER)
        assert len(order.seen_by_post_load) == 1
        assert order.seen_by_post_load[0]["total"] == Decimal("9.000")

    def test_schema_validator_can_act_on_derived_values(self, order):
        # A negative discount makes total exceed subtotal.
        schema = order.schema_cls()
        with pytest.raises(ValidationError) as excinfo:
            schema.load({**VALID_ORDER, "discount": "-0.5"})
        assert excinfo.value.messages == {"_schema": ["total must not exceed subtotal"]}

    def test_failing_source_field_skips_dependent_computations(self, order):
        # quantity=0 fails the field-level Range validator, so nothing that
        # depends on it may be computed.
        schema = order.schema_cls()
        with pytest.raises(ValidationError) as excinfo:
            schema.load({**VALID_ORDER, "quantity": 0})
        assert "quantity" in excinfo.value.messages
        # The unready computations did not happen; the independent branch did.
        assert order.log == ["summary"]
        valid_data = excinfo.value.valid_data
        assert isinstance(valid_data, dict)
        assert "subtotal" not in valid_data
        assert "total" not in valid_data
        assert valid_data["summary"] == "GIFT"

    def test_undeserializable_source_field_skips_dependent_computations(self, order):
        schema = order.schema_cls()
        with pytest.raises(ValidationError) as excinfo:
            schema.load({**VALID_ORDER, "quantity": "not-a-number"})
        assert excinfo.value.messages["quantity"] == ["Not a valid integer."]
        assert order.log == ["summary"]

    def test_derived_error_does_not_overwrite_existing_field_errors(self, order):
        def boom(values):
            raise ValidationError("cannot compute surcharge")

        class SurchargeSchema(Schema):
            quantity = fields.Int()
            unit_price = fields.Decimal()
            surcharge = fields.Derived(depends_on=("unit_price",), compute=boom)

        schema = SurchargeSchema()
        with pytest.raises(ValidationError) as excinfo:
            schema.load({"quantity": "not-a-number", "unit_price": "2.00"})
        assert excinfo.value.messages == {
            "quantity": ["Not a valid integer."],
            "surcharge": ["cannot compute surcharge"],
        }

    def test_derived_field_validators_run_on_computed_value(self):
        class LimitedSchema(Schema):
            factor = fields.Int()
            doubled = fields.Derived(
                depends_on=("factor",),
                compute=lambda values: values["factor"] * 2,
                validate=validate.Range(max=10),
            )

        assert LimitedSchema().load({"factor": 5})["doubled"] == 10
        with pytest.raises(ValidationError) as excinfo:
            LimitedSchema().load({"factor": 6})
        assert "doubled" in excinfo.value.messages


class TestClientCannotSubmitDerivedValues:
    def test_submitted_derived_value_is_rejected(self, order):
        schema = order.schema_cls()
        with pytest.raises(ValidationError) as excinfo:
            schema.load({**VALID_ORDER, "subtotal": "999"})
        assert excinfo.value.messages == {"subtotal": ["Unknown field."]}

    def test_submitted_derived_value_is_ignored_with_unknown_exclude(self, order):
        schema = order.schema_cls()
        result = schema.load({**VALID_ORDER, "subtotal": "999"}, unknown=EXCLUDE)
        # The computed value is used, not the submitted one.
        assert result["subtotal"] == Decimal("10.00")


class TestBindTimeValidation:
    def test_unknown_dependency_reports_the_path(self):
        class UnknownDepSchema(Schema):
            price = fields.Int()
            total = fields.Derived(
                depends_on=("price", "missing.path"), compute=lambda values: 1
            )

        with pytest.raises(
            ValueError, match='depends on unknown field path "missing.path"'
        ):
            UnknownDepSchema()

    def test_dependency_cycle_reports_the_path(self):
        class CyclicSchema(Schema):
            first = fields.Derived(depends_on=("second",), compute=lambda values: 1)
            second = fields.Derived(depends_on=("first",), compute=lambda values: 2)

        with pytest.raises(ValueError) as excinfo:
            CyclicSchema()
        assert '"first" -> "second" -> "first"' in str(excinfo.value)

    def test_self_dependency_is_a_cycle(self):
        class SelfCyclicSchema(Schema):
            loop = fields.Derived(depends_on=("loop",), compute=lambda values: 1)

        with pytest.raises(ValueError, match="Cycle detected"):
            SelfCyclicSchema()

    def test_load_only_is_rejected(self):
        with pytest.raises(ValueError, match="load_only"):
            fields.Derived(depends_on=(), compute=lambda values: 1, load_only=True)

    def test_non_callable_compute_is_rejected(self):
        with pytest.raises(TypeError):
            fields.Derived(depends_on=(), compute=42)  # type: ignore[arg-type]

    def test_non_string_dependency_is_rejected(self):
        with pytest.raises(TypeError):
            fields.Derived(depends_on=(42,), compute=lambda values: 1)  # type: ignore[arg-type]


class TestPartialLoading:
    class SettingsSchema(Schema):
        retries = fields.Int(load_default=9)
        timeout = fields.Int()
        retries_and_timeout = fields.Derived(
            depends_on=("retries", "timeout"),
            compute=lambda values: values["retries"] + values["timeout"],
        )

    def test_missing_dependencies_omit_derived_field(self):
        # The load_default of the missing field must not be stolen to make
        # the object complete during a partial load.
        result = self.SettingsSchema().load({"timeout": 30}, partial=True)
        assert result == {"timeout": 30}

    def test_load_default_is_used_when_not_partial(self):
        result = self.SettingsSchema().load({"timeout": 30})
        assert result == {"retries": 9, "timeout": 30, "retries_and_timeout": 39}

    def test_baseline_supplies_missing_dependencies(self):
        result = self.SettingsSchema().load(
            {"timeout": 30}, partial=True, derived_baseline={"retries": 5}
        )
        assert result == {"timeout": 30, "retries_and_timeout": 35}

    def test_baseline_is_not_modified(self):
        baseline = {"retries": 5}
        self.SettingsSchema().load(
            {"timeout": 30}, partial=True, derived_baseline=baseline
        )
        assert baseline == {"retries": 5}

    def test_baseline_can_supply_a_previously_derived_value(self):
        class ChainedSchema(Schema):
            price = fields.Int()
            quantity = fields.Int()
            subtotal = fields.Derived(
                depends_on=("price", "quantity"),
                compute=lambda values: values["price"] * values["quantity"],
            )
            total = fields.Derived(
                depends_on=("subtotal",),
                compute=lambda values: values["subtotal"] * 2,
            )

        result = ChainedSchema().load(
            {}, partial=True, derived_baseline={"subtotal": 21}
        )
        assert result == {"total": 42}


class BaseDoubledSchema(Schema):
    factor = fields.Int()
    doubled = fields.Derived(
        depends_on=("factor",), compute=lambda values: values["factor"] * 2
    )


class TestInheritance:
    def test_subclass_overrides_derived_compute(self):
        class ChildSchema(BaseDoubledSchema):
            doubled = fields.Derived(
                depends_on=("factor",), compute=lambda values: values["factor"] * 3
            )

        assert ChildSchema().load({"factor": 2})["doubled"] == 6

    def test_subclass_overrides_derived_with_plain_field(self):
        class ChildSchema(BaseDoubledSchema):
            doubled = fields.Int()  # type: ignore[assignment]

        schema = ChildSchema()
        assert schema.derived_fields == {}
        assert schema.load({"factor": 2, "doubled": 10}) == {
            "factor": 2,
            "doubled": 10,
        }

    def test_subclass_overrides_a_dependency_source_field(self):
        class BaseOrderSchema(Schema):
            price = fields.Decimal()
            quantity = fields.Int()
            total = fields.Derived(
                depends_on=("price", "quantity"),
                compute=lambda values: values["price"] * values["quantity"],
            )

        class StrictOrderSchema(BaseOrderSchema):
            quantity = fields.Int(validate=validate.Range(max=5))

        schema = StrictOrderSchema()
        assert schema.load({"price": "2.00", "quantity": 3})["total"] == Decimal("6.00")
        with pytest.raises(ValidationError) as excinfo:
            schema.load({"price": "2.00", "quantity": 10})
        assert "quantity" in excinfo.value.messages
        valid_data = excinfo.value.valid_data
        assert isinstance(valid_data, dict)
        assert "total" not in valid_data


class TestDataKeyAndAttributeMapping:
    class MappedSchema(Schema):
        unit_price = fields.Decimal(data_key="unitPrice", attribute="price_internal")
        quantity = fields.Int(data_key="qty")
        subtotal = fields.Derived(
            depends_on=("price_internal", "quantity"),
            compute=lambda values: values["price_internal"] * values["quantity"],
            data_key="subTotal",
        )

    def test_dependencies_use_internal_paths(self):
        result = self.MappedSchema().load({"unitPrice": "2.00", "qty": 3})
        assert result == {
            "price_internal": Decimal("2.00"),
            "quantity": 3,
            "subtotal": Decimal("6.00"),
        }

    def test_dump_uses_data_key(self):
        schema = self.MappedSchema()
        result = schema.load({"unitPrice": "2.00", "qty": 3})
        dumped = schema.dump(result)
        assert dumped["subTotal"] == Decimal("6.00")
        assert "subtotal" not in dumped

    def test_derived_errors_are_reported_under_data_key(self):
        def boom(values):
            raise ValidationError("cannot compute")

        class ErrorSchema(Schema):
            factor = fields.Int()
            doubled = fields.Derived(
                depends_on=("factor",),
                compute=boom,
                data_key="doubledValue",
            )

        with pytest.raises(ValidationError) as excinfo:
            ErrorSchema().load({"factor": 1})
        assert excinfo.value.messages == {"doubledValue": ["cannot compute"]}


class TestMany:
    def test_each_item_is_computed(self, order):
        schema = order.schema_cls(many=True)
        result = schema.load([VALID_ORDER, {**VALID_ORDER, "quantity": 1}])
        assert [item["subtotal"] for item in result] == [
            Decimal("10.00"),
            Decimal("2.50"),
        ]

    def test_item_errors_are_indexed_and_other_items_are_computed(self, order):
        schema = order.schema_cls(many=True)
        with pytest.raises(ValidationError) as excinfo:
            schema.load(
                [
                    VALID_ORDER,
                    {**VALID_ORDER, "quantity": 0},
                    {**VALID_ORDER, "quantity": 2},
                ]
            )
        assert set(excinfo.value.messages) == {1}
        assert "quantity" in excinfo.value.messages[1]
        valid_data = excinfo.value.valid_data
        assert valid_data[0]["subtotal"] == Decimal("10.00")
        assert "subtotal" not in valid_data[1]
        assert valid_data[2]["subtotal"] == Decimal("5.00")

    def test_many_with_per_item_baselines(self):
        class PairSchema(Schema):
            left = fields.Int()
            right = fields.Int()
            total = fields.Derived(
                depends_on=("left", "right"),
                compute=lambda values: values["left"] + values["right"],
            )

        baselines = [{"left": 10}, {"left": 20}]
        result = PairSchema().load(
            [{"right": 1}, {"right": 2}],
            many=True,
            partial=True,
            derived_baseline=baselines,
        )
        assert result == [
            {"right": 1, "total": 11},
            {"right": 2, "total": 22},
        ]
        assert baselines == [{"left": 10}, {"left": 20}]


class TestDump:
    def test_derived_value_is_serialized(self, order):
        schema = order.schema_cls()
        result = schema.load(VALID_ORDER)
        dumped = schema.dump(result)
        assert dumped["subtotal"] == Decimal("10.00")
        assert dumped["total"] == Decimal("9.000")

    def test_missing_derived_value_is_omitted_on_dump(self, order):
        assert order.schema_cls().dump({}) == {}

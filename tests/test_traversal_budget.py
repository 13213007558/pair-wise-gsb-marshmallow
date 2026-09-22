"""Tests for per-call traversal budgets on load and dump."""

from __future__ import annotations

import asyncio
import threading
from typing import Any

import pytest

from marshmallow import (
    EXCLUDE,
    INCLUDE,
    RAISE,
    CircularReferenceError,
    Schema,
    TraversalBudget,
    TraversalBudgetExceededError,
    ValidationError,
    current_budget,
    fields,
    post_load,
)


class InnerSchema(Schema):
    value = fields.Integer(data_key="v")


class CompositeSchema(Schema):
    name = fields.String(data_key="n")
    numbers = fields.List(fields.Integer())
    pair = fields.Tuple((fields.String(), fields.Integer()))
    children = fields.List(fields.Nested(InnerSchema))
    lookup = fields.Dict(keys=fields.String(), values=fields.Integer())


COMPOSITE_DATA = {
    "n": "root",
    "numbers": [1, 2, 3],
    "pair": ("a", 2),
    "children": [{"v": 10}, {"v": 20}],
    "lookup": {"x": 1, "y": 2},
}

# root + name + numbers container + 3 ints + pair + 2 members
# + 2 nested mappings + 2 nested ints + lookup + 2 dict values
COMPOSITE_NODES = 17


class TestBudgetDisabledByDefault:
    def test_load_without_budget_unchanged(self):
        result = CompositeSchema().load(COMPOSITE_DATA)
        assert result["name"] == "root"

    def test_dump_without_budget_unchanged(self):
        loaded = CompositeSchema().load(COMPOSITE_DATA)
        result = CompositeSchema().dump(loaded)
        assert result["n"] == "root"
        assert result["numbers"] == [1, 2, 3]

    def test_current_budget_is_none_without_budget(self):
        assert current_budget() is None

    def test_budget_errors_are_not_validation_errors(self):
        assert not issubclass(TraversalBudgetExceededError, ValidationError)
        assert not issubclass(CircularReferenceError, ValidationError)

    def test_non_container_root_type_error_is_validation_error(self):
        class Flat(Schema):
            a = fields.Integer()

        budget = TraversalBudget()
        with pytest.raises(ValidationError):
            Flat().load(42, budget=budget)
        # A scalar that fails the type check is not entered as a container.
        assert budget.nodes_visited == 0


class TestNodeCounting:
    def test_load_counts_root_containers_and_scalars(self):
        budget = TraversalBudget()
        CompositeSchema().load(COMPOSITE_DATA, budget=budget)
        assert budget.nodes_visited == COMPOSITE_NODES
        assert budget.depth == 0

    def test_dump_counts_root_containers_and_scalars(self):
        loaded = CompositeSchema().load(COMPOSITE_DATA)
        budget = TraversalBudget()
        CompositeSchema().dump(loaded, budget=budget)
        assert budget.nodes_visited == COMPOSITE_NODES

    def test_many_items_share_single_budget(self):
        shared = TraversalBudget()
        CompositeSchema().load(
            [COMPOSITE_DATA, COMPOSITE_DATA], many=True, budget=shared
        )
        assert shared.nodes_visited == 1 + 2 * COMPOSITE_NODES

    def test_each_many_item_is_direct_child(self):
        budget = TraversalBudget()
        InnerSchema().load([{"v": 1}, {"v": 2}], many=True, budget=budget)
        # many list + 2 item mappings + 2 integers
        assert budget.nodes_visited == 5

    def test_nested_field_shares_budget(self):
        class Outer(Schema):
            child = fields.Nested(InnerSchema)

        budget = TraversalBudget()
        Outer().load({"child": {"v": 3}}, budget=budget)
        assert budget.nodes_visited == 3

    def test_nested_many_shares_budget_with_indices(self):
        class Outer(Schema):
            children = fields.Nested(InnerSchema, many=True)

        budget = TraversalBudget()
        Outer().load({"children": [{"v": 1}, {"v": 2}]}, budget=budget)
        assert budget.nodes_visited == 1 + 1 + 2 + 2

    def test_empty_containers_still_count(self):
        class EmptySchema(Schema):
            items = fields.List(fields.Integer())
            table = fields.Dict()

        budget = TraversalBudget()
        EmptySchema().load({"items": [], "table": {}}, budget=budget)
        assert budget.nodes_visited == 3

    def test_missing_fields_are_not_visited(self):
        class PartialSchema(Schema):
            a = fields.Integer(required=True)

        budget = TraversalBudget()
        with pytest.raises(ValidationError):
            PartialSchema().load({}, budget=budget)
        assert budget.nodes_visited == 1

    def test_scalar_does_not_increase_depth(self):
        class Flat(Schema):
            a = fields.Integer()

        budget = TraversalBudget()
        Flat().load({"a": 1}, budget=budget)
        assert budget.depth == 0

    def test_none_container_value_counts_as_scalar_on_load(self):
        class Holder(Schema):
            items = fields.List(fields.Integer(), allow_none=True)

        budget = TraversalBudget()
        Holder().load({"items": None}, budget=budget)
        # root mapping + the null leaf (no list container entered)
        assert budget.nodes_visited == 2

    def test_none_container_value_counts_as_scalar_on_dump(self):
        class Holder(Schema):
            items = fields.List(fields.Integer(), allow_none=True)

        budget = TraversalBudget()
        Holder().dump({"items": None}, budget=budget)
        assert budget.nodes_visited == 2

    @pytest.mark.parametrize("unknown", (RAISE, INCLUDE))
    def test_unknown_field_values_are_visited(self, unknown):
        class Holder(Schema):
            a = fields.Integer()

        data = {"a": 1, "x": 9, "y": 8}
        budget = TraversalBudget()
        try:
            Holder(unknown=unknown).load(data, budget=budget)
        except ValidationError:
            pass
        # root mapping + declared scalar + both unknown values
        assert budget.nodes_visited == 4

    def test_excluded_unknown_fields_are_not_visited(self):
        class Holder(Schema):
            a = fields.Integer()

        budget = TraversalBudget()
        Holder(unknown=EXCLUDE).load({"a": 1, "x": 9, "y": 8}, budget=budget)
        # root mapping + declared scalar only
        assert budget.nodes_visited == 2


class RecursiveSchema(Schema):
    n = fields.String(data_key="n")
    children = fields.List(fields.Nested(lambda: RecursiveSchema()))


def recursive_payload(depth: int) -> dict:
    root: dict[str, Any] = {"n": "0", "children": []}
    node = root
    for level in range(1, depth):
        child: dict[str, Any] = {"n": str(level), "children": []}
        node["children"] = [child]
        node = child
    return root


class TestDepth:
    def test_root_container_is_depth_one(self):
        class Flat(Schema):
            a = fields.Integer()

        budget = TraversalBudget(max_depth=1)
        Flat().load({"a": 1}, budget=budget)
        assert budget.depth == 0

    def test_depth_counts_only_container_boundaries(self):
        payload = recursive_payload(3)
        # Spine: map(1), list(2), map(3), list(4), map(5), list(6)
        budget = TraversalBudget(max_depth=6)
        RecursiveSchema().load(payload, budget=budget)
        assert budget.depth == 0

        budget = TraversalBudget(max_depth=5)
        with pytest.raises(TraversalBudgetExceededError) as exc_info:
            RecursiveSchema().load(recursive_payload(3), budget=budget)
        error = exc_info.value
        assert error.kind == "depth"
        assert error.limit == 5
        assert error.used == 6
        assert error.path_string == "children.0.children.0.children"


class TestNodeBudgetExceeded:
    def test_error_reports_external_field_name(self):
        class Named(Schema):
            external_name = fields.Integer(data_key="ext")

        budget = TraversalBudget(max_nodes=1)
        with pytest.raises(TraversalBudgetExceededError) as exc_info:
            Named().load({"ext": 1}, budget=budget)
        error = exc_info.value
        assert error.kind == "nodes"
        assert error.path == ("ext",)
        assert error.path_string == "ext"
        assert error.limit == 1
        assert error.used == 2

    def test_error_reports_actual_index(self):
        class ListSchema(Schema):
            numbers = fields.List(fields.Integer())

        # root + numbers container + 3 visited scalars = 5; 4th scalar fails
        budget = TraversalBudget(max_nodes=5)
        with pytest.raises(TraversalBudgetExceededError) as exc_info:
            ListSchema().load({"numbers": [1, 2, 3, 4]}, budget=budget)
        error = exc_info.value
        assert error.path == ("numbers", 3)
        assert error.path_string == "numbers.3"

    def test_stops_iterating_remaining_iterator_elements(self):
        advanced = threading.Event()

        def values():
            yield 1
            yield 2
            advanced.set()
            yield 3

        class ListSchema(Schema):
            numbers = fields.List(fields.Integer())

        budget = TraversalBudget(max_nodes=3)
        with pytest.raises(TraversalBudgetExceededError):
            ListSchema().load({"numbers": values()}, budget=budget)
        assert not advanced.is_set()

    def test_stops_reading_remaining_object_attributes(self):
        class StopSchema(Schema):
            first = fields.Integer()
            second = fields.Integer()

        seen = []

        class ExplodingMapping(dict):
            def get(self, key, default=None):
                seen.append(key)
                return super().get(key, default)

            def __getitem__(self, key):
                seen.append(key)
                return super().__getitem__(key)

        budget = TraversalBudget(max_nodes=1)
        with pytest.raises(TraversalBudgetExceededError):
            StopSchema().load(ExplodingMapping(first=1, second=2), budget=budget)
        assert seen == ["first"]


class TestCycleDetection:
    def test_self_referential_list_is_cycle_on_load(self):
        cycle: dict[str, Any] = {"n": "root", "children": []}
        cycle["children"].append(cycle)
        with pytest.raises(CircularReferenceError) as exc_info:
            RecursiveSchema().load(cycle, budget=TraversalBudget())
        error = exc_info.value
        assert error.path == ("children", 0)
        assert error.path_string == "children.0"
        assert error.value is cycle

    def test_self_referential_mapping_is_cycle(self):
        class DictCycle(Schema):
            table = fields.Dict(
                keys=fields.String(),
                values=fields.Nested(lambda: DictCycle()),
            )

        cycle: dict[str, Any] = {}
        cycle["loop"] = cycle
        with pytest.raises(CircularReferenceError) as exc_info:
            DictCycle().load({"table": cycle}, budget=TraversalBudget())
        assert exc_info.value.path_string == "table.loop"

    def test_self_referential_tuple_is_cycle(self):
        class RecTuple(Schema):
            edge = fields.Tuple(
                (fields.String(), fields.List(fields.Nested(lambda: RecTuple())))
            )

        cycle: list[Any] = ["a", []]
        cycle[1].append({"edge": cycle})
        with pytest.raises(CircularReferenceError) as exc_info:
            RecTuple().load({"edge": cycle}, budget=TraversalBudget())
        assert exc_info.value.path_string == "edge.1.0.edge"

    def test_cycle_detected_on_dump(self):
        class Node:
            def __init__(self, name):
                self.n = name
                self.children = []

        root = Node("root")
        root.children.append(root)
        with pytest.raises(CircularReferenceError) as exc_info:
            RecursiveSchema().dump(root, budget=TraversalBudget())
        assert exc_info.value.path_string == "children.0"

    def test_diamond_shared_reference_is_not_a_cycle(self):
        shared = {"n": "shared", "children": []}
        diamond = {"n": "root", "children": [shared, shared]}
        budget = TraversalBudget()
        result = RecursiveSchema().load(diamond, budget=budget)
        assert result["children"][0]["n"] == "shared"
        assert result["children"][1]["n"] == "shared"
        # root + list + 2 * (mapping + n scalar + empty children list)
        assert budget.nodes_visited == 9

    def test_diamond_shared_reference_dump_is_not_a_cycle(self):
        class Node:
            def __init__(self, name):
                self.n = name
                self.children = []

        shared = Node("shared")
        root = Node("root")
        root.children = [shared, shared]
        budget = TraversalBudget()
        result = RecursiveSchema().dump(root, budget=budget)
        assert result["children"] == [
            {"n": "shared", "children": []},
            {"n": "shared", "children": []},
        ]

    def test_exhausted_budget_keeps_counter(self):
        budget = TraversalBudget(max_nodes=10)
        with pytest.raises(TraversalBudgetExceededError):
            CompositeSchema().load(COMPOSITE_DATA, budget=budget)
        assert budget.nodes_visited == 11


class TestCustomFieldParticipation:
    def test_custom_field_charges_internal_work(self):
        class ExpandingField(fields.Field):
            def _deserialize(self, value, attr, data, **kwargs):
                budget = current_budget()
                assert budget is not None
                for part in str(value).split("."):
                    budget.visit_node(f"{attr}::{part}")
                return value

        class CustomSchema(Schema):
            dotted = ExpandingField(data_key="d")

        # root + field scalar + first internal part fill the budget (3),
        # so the second internal part fails at the reported path.
        budget = TraversalBudget(max_nodes=3)
        with pytest.raises(TraversalBudgetExceededError) as exc_info:
            CustomSchema().load({"d": "a.b.c"}, budget=budget)
        assert exc_info.value.path == ("d::b",)

    def test_independent_top_level_schema_starts_without_budget(self):
        seen = []

        class ProbeField(fields.Field):
            def _deserialize(self, value, attr, data, **kwargs):
                seen.append(current_budget())
                return value

        class Other(Schema):
            probe = ProbeField()

        class Host(Schema):
            value = fields.Integer()

            @post_load
            def call_other(self, data, **kwargs):
                # No budget passed: the nested top-level call gets no budget
                # and its custom field must not see the host's counters.
                Other().load({"probe": "x"})
                return data

        budget = TraversalBudget()
        Host().load({"value": 1}, budget=budget)
        assert seen == [None]

    def test_independent_top_level_schema_can_share_budget_explicitly(self):
        seen = []

        class ProbeField(fields.Field):
            def _deserialize(self, value, attr, data, **kwargs):
                seen.append(current_budget())
                return value

        class Other(Schema):
            probe = ProbeField()

        class Host(Schema):
            value = fields.Integer()

            @post_load
            def call_other(self, data, **kwargs):
                budget = current_budget()
                Other().load({"probe": "x"}, budget=budget)
                return data

        budget = TraversalBudget()
        Host().load({"value": 1}, budget=budget)
        assert seen == [budget]


class TestIsolation:
    def test_budget_is_per_call_not_per_schema(self):
        schema = CompositeSchema()
        first = TraversalBudget()
        second = TraversalBudget()
        schema.load(COMPOSITE_DATA, budget=first)
        schema.load(COMPOSITE_DATA, budget=second)
        assert first.nodes_visited == COMPOSITE_NODES
        assert second.nodes_visited == COMPOSITE_NODES

    def test_threads_reusing_schema_do_not_share_counters(self):
        schema = CompositeSchema()

        def worker():
            for _ in range(10):
                schema.load(COMPOSITE_DATA, budget=TraversalBudget())

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

    def test_thread_with_tiny_budget_reports_its_own_path(self):
        schema = CompositeSchema()
        stop = threading.Event()

        def unbounded():
            while not stop.is_set():
                schema.load(COMPOSITE_DATA, budget=TraversalBudget())

        thread = threading.Thread(target=unbounded)
        thread.start()
        try:
            tiny = TraversalBudget(max_nodes=1)
            with pytest.raises(TraversalBudgetExceededError) as exc_info:
                schema.load(COMPOSITE_DATA, budget=tiny)
            assert exc_info.value.path[0] in {"n", "numbers"}
        finally:
            stop.set()
            thread.join()

    def test_async_tasks_get_independent_counters(self):
        schema = CompositeSchema()

        async def load_with_own_budget():
            budget = TraversalBudget()
            schema.load(COMPOSITE_DATA, budget=budget)
            return budget.nodes_visited

        async def run():
            return await asyncio.gather(*[load_with_own_budget() for _ in range(5)])

        results = asyncio.run(run())
        assert results == [COMPOSITE_NODES] * 5

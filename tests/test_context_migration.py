"""Regression tests for examples/context_migration_example.py.

Covers the guarantees promised by the migration guide:
nested override restoration (including on error), isolation of a shared
schema between interleaved async tasks, and explicit failure (or a
documented conservative fallback) when no context is set.
"""

import asyncio

import pytest

from examples.context_migration_example import (
    ACCOUNT,
    FULL_VIEW,
    PUBLIC_VIEW,
    AccountSchema,
    MaskedByDefault,
    ViewContext,
    override_view,
)
from marshmallow import Schema


@pytest.fixture()
def schema():
    return AccountSchema()


class TestNestedOverride:
    def test_inner_override_restores_outer_view(self, schema):
        with ViewContext(FULL_VIEW):
            assert schema.dump(ACCOUNT)["profile"]["email"] == "ada@example.com"
            with override_view(allowed=frozenset({"name"})):
                dumped = schema.dump(ACCOUNT)
                assert dumped["name"] == "Ada"
                assert dumped["profile"]["email"] == "<hidden>"
                assert dumped["tags"] == ["<hidden>", "<hidden>"]
            # Outer view is restored after the inner override exits.
            dumped = schema.dump(ACCOUNT)
            assert dumped["profile"]["email"] == "ada@example.com"
            assert dumped["tags"] == ["admin", "owner"]

    def test_override_restored_when_field_raises(self, schema):
        account = {**ACCOUNT, "fragile": "x"}
        with ViewContext(FULL_VIEW):
            with override_view(allowed=frozenset()):
                with pytest.raises(RuntimeError, match="boom"):
                    schema.dump(account)
            # The exception did not leak the narrowed override.
            assert ViewContext.get() is FULL_VIEW
            assert schema.dump(ACCOUNT)["profile"]["phone"] == "555-0100"

    def test_override_does_not_mutate_outer_view(self, schema):
        with ViewContext(FULL_VIEW):
            with override_view(mask="REDACTED"):
                assert ViewContext.get()["mask"] == "REDACTED"
            assert FULL_VIEW["mask"] == "<hidden>"


class TestAsyncIsolation:
    @staticmethod
    async def _dump_between_events(view, schema, entered, other_entered):
        with ViewContext(view):
            entered.set()
            # Ensure both tasks are inside their own context before either
            # dumps, so the dumps are genuinely interleaved.
            await other_entered.wait()
            return schema.dump(ACCOUNT)

    def test_interleaved_tasks_do_not_see_each_others_view(self):
        async def run():
            shared_schema = AccountSchema()
            entered_a, entered_b = asyncio.Event(), asyncio.Event()
            task_a = asyncio.create_task(
                self._dump_between_events(FULL_VIEW, shared_schema, entered_a, entered_b)
            )
            task_b = asyncio.create_task(
                self._dump_between_events(PUBLIC_VIEW, shared_schema, entered_b, entered_a)
            )
            return await asyncio.gather(task_a, task_b)

        full_result, public_result = asyncio.run(run())
        assert full_result["profile"]["email"] == "ada@example.com"
        assert full_result["tags"] == ["admin", "owner"]
        assert public_result["profile"]["email"] == "<hidden>"
        assert public_result["tags"] == ["<hidden>", "<hidden>"]
        assert public_result["name"] == "Ada"


class TestMissingContext:
    def test_missing_context_fails_explicitly(self, schema):
        with pytest.raises(LookupError):
            schema.dump(ACCOUNT)

    def test_context_does_not_leak_after_block_exits(self, schema):
        with ViewContext(FULL_VIEW):
            assert schema.dump(ACCOUNT)["name"] == "Ada"
        # A later request must not reuse the previous request's view.
        with pytest.raises(LookupError):
            schema.dump(ACCOUNT)

    def test_conservative_fallback_masks_everything(self):
        class SafeSchema(Schema):
            email = MaskedByDefault()

        # No context set: documented deny-by-default behavior, no exception.
        assert SafeSchema().dump({"email": "ada@example.com"}) == {"email": "***"}


class TestNestedAndListFields:
    def test_nested_schema_and_list_inherit_view(self, schema):
        with ViewContext(PUBLIC_VIEW):
            dumped = schema.dump(ACCOUNT)
        assert dumped["profile"] == {"email": "<hidden>", "phone": "<hidden>"}
        assert dumped["tags"] == ["<hidden>", "<hidden>"]
        assert dumped["id"] == 1  # non-authorized fields are unaffected

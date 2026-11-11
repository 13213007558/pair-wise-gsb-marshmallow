"""Migration example: per-request authorization views without ``schema.context``.

marshmallow 3 allowed custom fields to read per-request configuration from
``self.parent.context`` (or ``self.context`` in ``Method`` fields). That
attribute was removed in marshmallow 4. This example shows the equivalent
pattern built on the experimental
`Context <marshmallow.experimental.context.Context>` API, which is backed by
`contextvars.ContextVar` and therefore isolates concurrent async requests
sharing the same schema instances.

Old vs. new correspondence
--------------------------

===================================================  ======================================================
marshmallow 3 (removed)                              marshmallow 4 (this example)
===================================================  ======================================================
``Schema(context={...})`` / ``schema.context = ...`` ``with ViewContext({...}):`` around ``dump``/``load``
``self.parent.context["view"]`` in a custom field    ``ViewContext.get()`` inside ``_serialize``
``self.context`` in a ``Method`` field               ``ViewContext.get()`` inside the method
mutating ``schema.context`` for a nested override    nested ``with ViewContext(...)`` / ``override_view``
"no context set" silently reused the last value      ``ContextVar`` raises ``LookupError`` when unset;
                                                     or pass an explicit deny-by-default value to
                                                     ``ViewContext.get(default)``
===================================================  ======================================================

Failure policy when context is missing
--------------------------------------

``Authorized`` calls ``ViewContext.get()`` with no default, so a missing
context raises ``LookupError`` — an explicit failure, never a stale value
from a previous request. ``MaskedByDefault`` demonstrates the documented
conservative alternative: ``ViewContext.get(LOCKED_DOWN_VIEW)`` falls back to
a view that allows nothing, so missing context masks everything instead of
crashing. Pick one policy per project; do not silently reuse old values.

Out of scope (not guaranteed by the library)
--------------------------------------------

* Threads: ``contextvars`` do not propagate into ``threading.Thread`` or
  executors automatically. Re-enter ``ViewContext`` inside the worker, or
  run it via ``contextvars.copy_context().run``.
* Background tasks: an ``asyncio.Task`` copies the context at *creation*
  time. Tasks spawned before entering ``ViewContext`` will not see it;
  enter the context inside the task body instead.
* Multiprocessing: no propagation at all; set the context in each process.
* The context dict is shared by reference — never mutate it in place.
  Use ``override_view`` to derive a modified copy.
* The ``Context`` API is experimental in marshmallow 4.x and may change.

Run the tests for this example with::

    pytest tests/test_context_migration.py
"""

from __future__ import annotations

import asyncio
import contextlib
import typing

from marshmallow import Schema, fields
from marshmallow.experimental.context import Context


class View(typing.TypedDict):
    """Per-request authorization view: which fields may be shown, and how
    to mask the rest."""

    allowed: frozenset[str]
    mask: str


ViewContext = Context[View]

# Conservative fallback for callers that prefer "deny by default" over
# raising when no context is set. Allows nothing, masks everything.
LOCKED_DOWN_VIEW: View = {"allowed": frozenset(), "mask": "***"}


class Authorized(fields.String):
    """String field that masks itself unless the current view allows it.

    Fails explicitly (``LookupError``) when no view context is set, so a
    request can never inherit the previous request's authorization.
    """

    def _serialize(self, value, attr, obj, **kwargs):
        view = ViewContext.get()
        if attr not in view["allowed"]:
            return view["mask"]
        return super()._serialize(value, attr, obj, **kwargs)


class MaskedByDefault(fields.String):
    """Conservative variant: missing context masks instead of raising."""

    def _serialize(self, value, attr, obj, **kwargs):
        view = ViewContext.get(LOCKED_DOWN_VIEW)
        if attr not in view["allowed"]:
            return view["mask"]
        return super()._serialize(value, attr, obj, **kwargs)


class AuthorizedList(fields.List):
    """List field masked item-by-item unless the view allows the field."""

    def _serialize(self, value, attr, obj, **kwargs):
        view = ViewContext.get()
        if attr not in view["allowed"]:
            return [view["mask"] for _ in value or []]
        return super()._serialize(value, attr, obj, **kwargs)


class Exploding(fields.Field):
    """Field that always raises, to prove context overrides are restored
    even when serialization fails mid-flight."""

    def _serialize(self, value, attr, obj, **kwargs):
        raise RuntimeError(f"boom while serializing {attr!r}")


class ProfileSchema(Schema):
    email = Authorized()
    phone = Authorized()


class AccountSchema(Schema):
    id = fields.Integer()
    name = Authorized()
    profile = fields.Nested(ProfileSchema)
    tags = AuthorizedList(fields.String())
    fragile = Exploding(required=False)


@contextlib.contextmanager
def override_view(**changes: typing.Any) -> typing.Iterator[None]:
    """Temporarily override parts of the current view.

    Builds a *new* view dict (never mutates in place) and restores the
    enclosing view on exit, even if the body raises.
    """
    current = ViewContext.get()
    with ViewContext({**current, **changes}):
        yield


FULL_VIEW: View = {
    "allowed": frozenset({"name", "email", "phone", "tags"}),
    "mask": "<hidden>",
}
PUBLIC_VIEW: View = {"allowed": frozenset({"name"}), "mask": "<hidden>"}

ACCOUNT = {
    "id": 1,
    "name": "Ada",
    "profile": {"email": "ada@example.com", "phone": "555-0100"},
    "tags": ["admin", "owner"],
}


def demo_nested_override() -> None:
    schema = AccountSchema()
    with ViewContext(FULL_VIEW):
        print("full view:      ", schema.dump(ACCOUNT))
        with override_view(allowed=frozenset({"name"})):
            # Inner override narrows the view; nested schema and list
            # field see the narrowed view too.
            print("narrowed inner: ", schema.dump(ACCOUNT))
        # Exiting the inner override restores the outer FULL_VIEW.
        print("restored outer: ", schema.dump(ACCOUNT))


def demo_override_restored_on_error() -> None:
    schema = AccountSchema()
    account = {**ACCOUNT, "fragile": "x"}
    with ViewContext(FULL_VIEW):
        with override_view(allowed=frozenset()):
            try:
                schema.dump(account)
            except RuntimeError as error:
                print("inner dump failed as expected:", error)
        # The failing dump did not leak the narrowed override.
        assert ViewContext.get() is FULL_VIEW
        print("outer view intact after error:", schema.dump(ACCOUNT)["profile"])


async def dump_as(view: View, schema: AccountSchema, barrier: asyncio.Event) -> dict:
    with ViewContext(view):
        # Yield so both tasks are inside their own context before dumping.
        barrier.set()
        await asyncio.sleep(0)
        return schema.dump(ACCOUNT)


async def demo_async_isolation() -> None:
    # One shared schema instance, two interleaved requests.
    shared_schema = AccountSchema()
    first_barrier, second_barrier = asyncio.Event(), asyncio.Event()
    full_task = asyncio.create_task(dump_as(FULL_VIEW, shared_schema, first_barrier))
    public_task = asyncio.create_task(dump_as(PUBLIC_VIEW, shared_schema, second_barrier))
    full_result, public_result = await asyncio.gather(full_task, public_task)
    print("task A (full):  ", full_result)
    print("task B (public):", public_result)
    assert full_result["profile"]["email"] == "ada@example.com"
    assert public_result["profile"]["email"] == "<hidden>"


if __name__ == "__main__":
    demo_nested_override()
    demo_override_restored_on_error()
    asyncio.run(demo_async_isolation())

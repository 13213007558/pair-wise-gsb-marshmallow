"""Optional per-call traversal budgets for `Schema.load <marshmallow.Schema.load>`
and `Schema.dump <marshmallow.Schema.dump>`.

Pass a :class:`TraversalBudget` to a single ``load``/``dump`` call to cap the
number of nodes that are visited and the nesting depth of containers while
marshmallow walks the input::

    from marshmallow import (
        Schema,
        fields,
        TraversalBudget,
        TraversalBudgetExceededError,
    )


    class MySchema(Schema):
        items = fields.List(fields.Nested(lambda: MySchema()))


    budget = TraversalBudget(max_nodes=10_000, max_depth=100)
    try:
        MySchema().load(data, budget=budget)
    except TraversalBudgetExceededError as error:
        print(error.path_string)  # e.g. "items.0.items.3"

The budget is shared by every `Nested`, `List`, `Tuple`, and `Dict` field
encountered during that single call (including the items traversed for
``many=True``). It is neither stored on the schema nor on its fields, so the
same schema can be reused concurrently from multiple threads or asynchronous
tasks without the counters bleeding between calls.

Circular references are detected along the current ancestor chain only: a
shared object reached through two different, non-ancestral paths (a diamond)
is traversed twice and billed twice without being reported as a cycle.
"""

from __future__ import annotations

import contextlib
import contextvars
import typing

from marshmallow.exceptions import MarshmallowError

__all__ = [
    "CircularReferenceError",
    "TraversalBudget",
    "TraversalBudgetExceededError",
    "TraversalError",
    "current_budget",
]

#: Holds the budget governing the (de)serialization in progress, if any.
#: A new token is set (and reset) for every top-level ``load``/``dump`` call,
#: which keeps concurrent threads and asynchronous tasks independent.
_current_budget: contextvars.ContextVar[TraversalBudget | None] = (
    contextvars.ContextVar("marshmallow_traversal_budget", default=None)
)

# External field names give ``str``, sequence positions give ``int`` and
# mapping keys may be any hashable object.
PathSegment = typing.Any


class _Budget(typing.Protocol):  # noqa: PYI046
    """Structural type implemented by `TraversalBudget` and `_NullBudget`."""

    def visit_node(self, key: PathSegment | None = None) -> None: ...

    @contextlib.contextmanager
    def visit_container(
        self, value: typing.Any, key: PathSegment | None = None
    ) -> typing.Iterator[None]: ...

    @contextlib.contextmanager
    def activate(self) -> typing.Iterator[typing.Any]: ...


def format_path(path: typing.Iterable[PathSegment]) -> str:
    """Render a traversal path as a dotted string.

    Segments are external field names or actual container indices/keys.
    """
    return ".".join(str(segment) for segment in path)


def current_budget() -> TraversalBudget | None:
    """Return the `TraversalBudget` governing the (de)serialization in progress.

    This is the public hook for custom fields whose internal work is not
    visible to marshmallow's built-in container traversal::

        class CustomField(fields.Field):
            def _deserialize(self, value, attr, data, **kwargs):
                budget = current_budget()
                if budget is not None:
                    budget.visit_node("custom_subnode")
                ...

    Returns `None` when no budget was passed to the enclosing call or when
    another top-level schema is being (de)serialized independently.
    """
    return _current_budget.get()


class TraversalError(MarshmallowError):
    """Base class for errors raised while enforcing a `TraversalBudget`."""


class TraversalBudgetExceededError(TraversalError):
    """Raised when a traversal budget is exhausted.

    :param kind: ``"nodes"`` or ``"depth"`` for the budget that was exhausted.
    :param limit: The configured limit that was exceeded.
    :param used: The counter value when the limit was exceeded.
    :param path: Path (external field names and actual indices/keys) of the
        node at which the budget was exhausted.
    """

    def __init__(
        self,
        kind: typing.Literal["nodes", "depth"],
        *,
        limit: int,
        used: int,
        path: typing.Iterable[PathSegment],
    ):
        self.kind: typing.Literal["nodes", "depth"] = kind
        self.limit: int = limit
        self.used: int = used
        self.path: tuple[PathSegment, ...] = tuple(path)
        self.path_string: str = format_path(self.path)
        if kind == "depth":
            message = (
                f"Maximum container depth of {limit} exceeded at "
                f"{self.path_string or '<root>'}."
            )
        else:
            message = (
                f"Maximum number of visited nodes ({limit}) exceeded at "
                f"{self.path_string or '<root>'}."
            )
        super().__init__(message)


class CircularReferenceError(TraversalError):
    """Raised when a container references one of its own ancestors.

    :param path: Path (external field names and actual indices/keys) of the
        edge that closes the cycle.
    :param value: The container value that was already on the ancestor chain.
    """

    def __init__(self, *, path: typing.Iterable[PathSegment], value: typing.Any):
        self.path: tuple[PathSegment, ...] = tuple(path)
        self.path_string: str = format_path(self.path)
        self.value = value
        super().__init__(
            f"Circular reference detected at {self.path_string or '<root>'}."
        )


class TraversalBudget:
    """Per-call budget limiting traversal during one ``load`` or ``dump``.

    A *node* is any value marshmallow actually visits during the call:
    container values (mappings, sequences, and nested objects) and scalar
    leaves alike. Empty containers count as a single node. Values that are
    absent (missing fields) are not visited. Dict entries are billed as one
    node per visited entry value; transforming the keys does not count.

    *Depth* counts container boundaries only. The root object of a call is
    depth ``1``; scalar leaves do not add depth.

    :param max_nodes: Maximum number of nodes that may be visited. `None`
        (the default) disables the node budget.
    :param max_depth: Maximum nesting depth of containers. `None` (the
        default) disables the depth budget.
    """

    def __init__(
        self,
        *,
        max_nodes: int | None = None,
        max_depth: int | None = None,
    ):
        if max_nodes is not None and max_nodes < 1:
            raise ValueError('"max_nodes" must be a positive integer or None.')
        if max_depth is not None and max_depth < 1:
            raise ValueError('"max_depth" must be a positive integer or None.')
        self.max_nodes: int | None = max_nodes
        self.max_depth: int | None = max_depth
        self.nodes_visited: int = 0
        self.depth: int = 0
        self.path: tuple[PathSegment, ...] = ()
        self._path: list[PathSegment] = []
        self._ancestor_id_set: set[int] = set()

    def visit_node(self, key: PathSegment | None = None) -> None:
        """Account for one visited scalar node.

        Custom fields can call this to make otherwise invisible work count
        against the same budget. Raises `TraversalBudgetExceededError` when the
        node budget is exhausted.

        :param key: External field name or actual index/key of the node
            relative to its parent.
        """
        if key is not None:
            self._path.append(key)
        try:
            self.nodes_visited += 1
            if self.max_nodes is not None and self.nodes_visited > self.max_nodes:
                raise TraversalBudgetExceededError(
                    "nodes",
                    limit=self.max_nodes,
                    used=self.nodes_visited,
                    path=self._path,
                )
        finally:
            if key is not None:
                self._path.pop()

    @contextlib.contextmanager
    def visit_container(
        self,
        value: typing.Any,
        key: PathSegment | None = None,
    ) -> typing.Iterator[None]:
        """Account for entering a container value and track the ancestor chain.

        The node is billed and the depth increased on entry. If ``value`` is
        already being traversed in an enclosing `visit_container` block,
        `CircularReferenceError` is raised. Shared (diamond) references are
        not cycles and are simply billed again.

        :param value: The container being entered.
        :param key: External field name or actual index/key of the container
            relative to its parent.
        """
        if key is not None:
            self._path.append(key)
        self.path = tuple(self._path)
        self.nodes_visited += 1
        self.depth += 1
        try:
            if self.max_nodes is not None and self.nodes_visited > self.max_nodes:
                raise TraversalBudgetExceededError(
                    "nodes",
                    limit=self.max_nodes,
                    used=self.nodes_visited,
                    path=self._path,
                )
            if self.max_depth is not None and self.depth > self.max_depth:
                raise TraversalBudgetExceededError(
                    "depth",
                    limit=self.max_depth,
                    used=self.depth,
                    path=self._path,
                )
            value_id = id(value)
            if value_id in self._ancestor_id_set:
                raise CircularReferenceError(
                    path=self._path,
                    value=value,
                )
            self._ancestor_id_set.add(value_id)
            try:
                yield
            finally:
                self._ancestor_id_set.discard(value_id)
        finally:
            self.depth -= 1
            self.path = tuple(self._path)
            if key is not None:
                self._path.pop()

    @contextlib.contextmanager
    def activate(self) -> typing.Iterator[TraversalBudget]:
        """Make this budget the current budget for the duration of the block."""
        token = _current_budget.set(self)
        try:
            yield self
        finally:
            _current_budget.reset(token)


class _NullBudget:
    """No-op budget used when no budget was passed to a call.

    All methods are intentionally cheap so that budget-unaware calls keep
    their existing behavior and performance characteristics.
    """

    __slots__ = ()

    max_nodes: typing.ClassVar[int | None] = None
    max_depth: typing.ClassVar[int | None] = None
    nodes_visited: typing.ClassVar[int] = 0
    depth: typing.ClassVar[int] = 0
    path: typing.ClassVar[tuple[PathSegment, ...]] = ()

    def visit_node(self, key: PathSegment | None = None) -> None:
        return None

    @contextlib.contextmanager
    def visit_container(
        self,
        value: typing.Any,
        key: PathSegment | None = None,
    ) -> typing.Iterator[None]:
        yield

    @contextlib.contextmanager
    def activate(self) -> typing.Iterator[None]:
        # Explicitly hide any enclosing budget: an independent top-level
        # schema (de)serialization starts with no budget.
        token = _current_budget.set(None)
        try:
            yield
        finally:
            _current_budget.reset(token)


_NO_BUDGET = _NullBudget()

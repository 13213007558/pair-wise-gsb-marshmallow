"""Optional traversal budgets for `Schema.load <marshmallow.Schema.load>` and
`Schema.dump <marshmallow.Schema.dump>` calls.

A traversal budget bounds the work a single load/dump call may perform on
untrusted or pathological object graphs:

- ``max_depth`` limits container nesting. Only container boundaries count;
  the root container is depth 1. Scalars never add depth.
- ``max_nodes`` limits the number of visited nodes. Every visited value
  counts: the root object, each attribute or item pulled from a container,
  each mapping key and value, and each scalar. Empty containers count, too.

The budget is shared by one top-level call. `Nested <marshmallow.fields.Nested>`,
`List <marshmallow.fields.List>`, `Tuple <marshmallow.fields.Tuple>` and
`Dict <marshmallow.fields.Dict>` fields (including ``many=True`` collections)
all draw from the same budget rather than getting one each.

While a budget is active, container cycles are detected along the current
ancestor chain. Objects shared between sibling positions (diamond-shaped
graphs) are not cycles; every visit to them still counts against the budget.

Budget state is stored in a :class:`contextvars.ContextVar`, so concurrent
threads and async tasks reusing the same `Schema <marshmallow.Schema>` never
share counters.

Custom fields can participate in the ambient budget with the module-level
helpers `current_budget`, `visit`, `container` and `segment`::

    from marshmallow import budget


    class Itemized(fields.Field):
        def _serialize(self, value, attr, obj, **kwargs):
            with budget.container(value):
                result = []
                for index, item in enumerate(value):
                    with budget.segment(index):
                        budget.visit()
                        result.append(item)
                return result

Work a custom field does internally is not charged automatically.

.. versionadded:: 4.4.0
"""

from __future__ import annotations

import contextlib
import contextvars
import typing

from marshmallow.exceptions import TraversalBudgetExceeded, TraversalCycleError

__all__ = [
    "TraversalBudget",
    "container",
    "current_budget",
    "segment",
    "visit",
]

#: The budget active for the current load/dump call, if any.
_current_budget: contextvars.ContextVar[TraversalBudget | None] = (
    contextvars.ContextVar("marshmallow_current_budget", default=None)
)

_NO_BUDGET = contextlib.nullcontext()

# Scalar values are never tracked for cycle detection: they cannot contain
# other objects, and singletons (``None``, small ints, interned strings)
# would produce false positives.
_SCALAR_TYPES = (str, bytes, bytearray, bool, int, float, complex)


def _is_trackable(obj: typing.Any) -> bool:
    return obj is not None and not isinstance(obj, _SCALAR_TYPES)


class TraversalBudget:
    """A traversal budget shared by one load/dump call.

    :param max_depth: Maximum container nesting depth. The root container
        is depth 1. ``None`` means unlimited.
    :param max_nodes: Maximum number of visited nodes. ``None`` means
        unlimited.

    At least one of ``max_depth`` or ``max_nodes`` must be given.

    A budget can be passed to `Schema.load <marshmallow.Schema.load>` or
    `Schema.dump <marshmallow.Schema.dump>` with the ``budget`` parameter,
    or activated manually as a context manager::

        budget = TraversalBudget(max_nodes=1000)
        with budget:
            schema.dump(obj)  # charged against ``budget``
        print(budget.nodes)

    .. versionadded:: 4.4.0
    """

    def __init__(
        self,
        *,
        max_depth: int | None = None,
        max_nodes: int | None = None,
    ):
        if max_depth is None and max_nodes is None:
            raise ValueError(
                "At least one of max_depth or max_nodes must be set."
            )
        for name, value in (("max_depth", max_depth), ("max_nodes", max_nodes)):
            if value is not None and (
                not isinstance(value, int) or isinstance(value, bool) or value < 1
            ):
                raise ValueError(f"{name} must be a positive integer or None.")
        self.max_depth = max_depth
        self.max_nodes = max_nodes
        #: Number of nodes visited so far.
        self.nodes = 0
        #: Current container nesting depth.
        self.depth = 0
        self._ancestors: list[int] = []
        self._path: list = []
        self._token: contextvars.Token | None = None

    def __repr__(self) -> str:
        return (
            f"<{self.__class__.__name__}(max_depth={self.max_depth}, "
            f"max_nodes={self.max_nodes}, nodes={self.nodes}, "
            f"depth={self.depth})>"
        )

    def __enter__(self) -> TraversalBudget:
        self._token = _current_budget.set(self)
        return self

    def __exit__(self, *exc_info) -> None:
        if self._token is not None:
            _current_budget.reset(self._token)
            self._token = None

    @property
    def path(self) -> tuple:
        """Path of the container currently being traversed."""
        return tuple(self._path)

    def visit(self, count: int = 1) -> None:
        """Charge ``count`` visited nodes to the budget.

        :raise TraversalBudgetExceeded: If the node budget is exhausted.
        """
        self.nodes += count
        if self.max_nodes is not None and self.nodes > self.max_nodes:
            raise TraversalBudgetExceeded(
                "Traversal node budget exceeded: "
                f"visited {self.nodes} nodes (limit {self.max_nodes}).",
                path=self._path,
            )

    @contextlib.contextmanager
    def segment(self, key) -> typing.Iterator[None]:
        """Context manager recording a path segment (field name or index)."""
        self._path.append(key)
        try:
            yield
        finally:
            self._path.pop()

    @contextlib.contextmanager
    def container(self, obj: typing.Any = None) -> typing.Iterator[None]:
        """Context manager entering a container.

        Increases the current depth and registers ``obj`` on the ancestor
        chain for cycle detection while the context is active.

        :param obj: The container being entered. Scalars and ``None`` are
            not tracked for cycles.
        :raise TraversalBudgetExceeded: If entering exceeds ``max_depth``.
        :raise TraversalCycleError: If ``obj`` is already on the current
            ancestor chain.
        """
        track = _is_trackable(obj)
        if track and id(obj) in self._ancestors:
            raise TraversalCycleError(
                "Cycle detected while traversing "
                f"{obj.__class__.__name__} object.",
                path=self._path,
            )
        self.depth += 1
        try:
            if self.max_depth is not None and self.depth > self.max_depth:
                raise TraversalBudgetExceeded(
                    "Traversal depth budget exceeded: "
                    f"depth {self.depth} (limit {self.max_depth}).",
                    path=self._path,
                )
            if track:
                self._ancestors.append(id(obj))
            try:
                yield
            finally:
                if track:
                    self._ancestors.pop()
        finally:
            self.depth -= 1


def current_budget() -> TraversalBudget | None:
    """Return the budget active for the current load/dump call, if any."""
    return _current_budget.get()


def resolve(
    budget: TraversalBudget | None,
    max_depth: int | None,
    max_nodes: int | None,
) -> contextlib.AbstractContextManager:
    """Return a context manager activating the requested budget, or a no-op
    context manager when no budget was requested.
    """
    if budget is not None:
        if max_depth is not None or max_nodes is not None:
            raise ValueError(
                "Pass either budget or max_depth/max_nodes, not both."
            )
        if not isinstance(budget, TraversalBudget):
            raise TypeError(
                f"budget must be a TraversalBudget, not {budget.__class__.__name__}.",
            )
        return budget
    if max_depth is None and max_nodes is None:
        return _NO_BUDGET
    return TraversalBudget(max_depth=max_depth, max_nodes=max_nodes)


def visit(count: int = 1) -> None:
    """Charge ``count`` visited nodes to the ambient budget, if any."""
    budget = _current_budget.get()
    if budget is not None:
        budget.visit(count)


def container(obj: typing.Any = None) -> contextlib.AbstractContextManager:
    """Enter a container in the ambient budget, if any.

    Returns a no-op context manager when no budget is active.
    """
    budget = _current_budget.get()
    if budget is None:
        return _NO_BUDGET
    return budget.container(obj)


def segment(key) -> contextlib.AbstractContextManager:
    """Record a path segment in the ambient budget, if any.

    Returns a no-op context manager when no budget is active.
    """
    budget = _current_budget.get()
    if budget is None:
        return _NO_BUDGET
    return budget.segment(key)

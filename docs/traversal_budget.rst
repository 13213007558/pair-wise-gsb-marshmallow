Traversal budgets
=================

Untrusted input passed to `Schema.load <marshmallow.Schema.load>` (or arbitrary
objects passed to `Schema.dump <marshmallow.Schema.dump>`) may be deeply
nested, extremely wide, or self-referential. A *traversal budget* lets a
single call cap how much work marshmallow does while walking that input.

Pass a `TraversalBudget <marshmallow.TraversalBudget>` to the call:

.. code-block:: python

    from marshmallow import Schema, fields, TraversalBudget, TraversalBudgetExceededError

    class NodeSchema(Schema):
        name = fields.String()
        children = fields.List(fields.Nested(lambda: NodeSchema()))

    budget = TraversalBudget(max_nodes=10_000, max_depth=100)
    try:
        NodeSchema().load(data, budget=budget)
    except TraversalBudgetExceededError as error:
        error.kind         # "nodes" or "depth"
        error.path         # ("children", 0, "children", 3)
        error.path_string  # "children.0.children.3"

`Schema.dump <marshmallow.Schema.dump>`,
`Schema.loads <marshmallow.Schema.loads>`,
`Schema.dumps <marshmallow.Schema.dumps>`, and
`Schema.validate <marshmallow.Schema.validate>` accept the same argument.

What is counted
---------------

A *node* is every value actually visited during the call: container values
(mappings, sequences, and objects handled by
`Nested <marshmallow.fields.Nested>`) and scalar leaves alike. Empty
containers count as one node; missing fields are not visited. For mappings,
each visited entry value counts as a node (key transformation does not).

*Depth* counts container boundaries only. The root object of a call is depth
``1``; entering a list, tuple, mapping, or nested object adds one level and
scalars never add depth.

The budget is shared across the entire call: every
`Nested <marshmallow.fields.Nested>`, `List <marshmallow.fields.List>`,
`Tuple <marshmallow.fields.Tuple>`, and `Dict <marshmallow.fields.Dict>` field
draws from it, and all items of a ``many=True`` call share it too. Budgets are
per call - they are never stored on a schema or field, so reusing the same
schema from threads or asynchronous tasks never mixes counters.

When a limit is reached, marshmallow stops immediately: remaining object
attributes are not read and remaining iterator elements are not consumed.
`TraversalBudgetExceededError <marshmallow.TraversalBudgetExceededError>`
carries ``kind``, ``limit``, ``used``, ``path`` and ``path_string``. Path
segments use external field names (``data_key``) and the actual container
indices and mapping keys.

Circular references
-------------------

Cycles are detected along the current ancestor chain. A container that
contains itself (directly or indirectly) raises
`CircularReferenceError <marshmallow.CircularReferenceError>`:

.. code-block:: python

    data = {"name": "root", "children": []}
    data["children"].append(data)
    NodeSchema().load(data, budget=TraversalBudget())
    # raises CircularReferenceError, path == ("children", 0)

The same object reached through two different *sibling* paths (a diamond or
shared reference) is not a cycle: it is traversed again and each visit counts
against the budget.

Custom fields
-------------

Work performed entirely inside a custom field's
`_serialize <marshmallow.fields.Field._serialize>` /
`_deserialize <marshmallow.fields.Field._deserialize>` is not counted
automatically. Use `current_budget <marshmallow.current_budget>` to take part
in the same budget explicitly:

.. code-block:: python

    from marshmallow import fields, current_budget

    class ExpandingField(fields.Field):
        def _deserialize(self, value, attr, data, **kwargs):
            budget = current_budget()
            if budget is not None:
                for part in value.split("."):
                    budget.visit_node(part)
            return value

``current_budget()`` returns `None` when the enclosing call has no budget.
Calling another top-level schema without passing ``budget=`` starts an
independent, unbudgeted traversal; pass the same budget explicitly if the two
calls should share one.

When no budget is passed, marshmallow keeps its default behavior and there is
no traversal overhead.

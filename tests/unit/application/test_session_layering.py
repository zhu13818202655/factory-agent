"""Guard the layer contract of the session mixin cascade.

``SessionService`` composes its behaviour from a chain of mixins over one shared
``self``. Two properties hold that design together, and neither is visible to
the type checker:

- no member is declared by two layers, because C3 resolves such a name to the
  leftmost class in the MRO and silently turns the other declaration into dead
  code;
- a layer only uses members of the layers it inherits from, so the ordering
  stated in the module docstrings stays a fact instead of a comment.

Both properties are asserted against the real class objects and the real source
text, so moving a method between layers or adding a colliding name fails here
rather than at runtime.
"""

import ast
import inspect
from collections import defaultdict
from pathlib import Path
from typing import Any

from factory_agent.application.session import SessionService


def _layers() -> tuple[type[Any], ...]:
    """Session-package classes on the service MRO, base layer first."""
    return tuple(
        layer
        for layer in reversed(SessionService.__mro__)
        if layer.__module__.startswith("factory_agent.application.session")
    )


def _layer_source(layer: type[Any]) -> ast.Module:
    return ast.parse(Path(inspect.getfile(layer)).read_text())


def _declared_members(layer: type[Any]) -> frozenset[str]:
    """Member names the layer declares, including attributes assigned to ``self``."""
    names = {name for name in vars(layer) if not name.startswith("__")}
    for node in ast.walk(_layer_source(layer)):
        if not isinstance(node, ast.ClassDef) or node.name != layer.__name__:
            continue
        for statement in ast.walk(node):
            if isinstance(statement, ast.Assign):
                targets = list(statement.targets)
            elif isinstance(statement, ast.AnnAssign):
                targets = [statement.target]
            else:
                continue
            for target in targets:
                if (
                    isinstance(target, ast.Attribute)
                    and not target.attr.startswith("__")
                    and isinstance(target.value, ast.Name)
                    and target.value.id == "self"
                ):
                    names.add(target.attr)
    return frozenset(names)


def _self_references(layer: type[Any]) -> frozenset[str]:
    """Names the layer reads through ``self``."""
    return frozenset(
        node.attr
        for node in ast.walk(_layer_source(layer))
        if isinstance(node, ast.Attribute)
        and not node.attr.startswith("__")
        and isinstance(node.value, ast.Name)
        and node.value.id == "self"
    )


def _owners() -> dict[str, list[str]]:
    owners: dict[str, list[str]] = defaultdict(list)
    for layer in _layers():
        for name in _declared_members(layer):
            owners[name].append(layer.__name__)
    return owners


def test_no_member_is_declared_by_two_layers() -> None:
    shadowed = {name: where for name, where in _owners().items() if len(where) > 1}
    assert not shadowed, (
        "A member declared by two mixins resolves to the leftmost class in the MRO, "
        f"silently discarding the other declaration: {shadowed}"
    )


def test_layers_only_use_members_of_the_layers_they_inherit_from() -> None:
    owners = _owners()
    offenders: list[tuple[str, str, list[str]]] = []
    for layer in _layers():
        inherited = {base.__name__ for base in layer.__mro__}
        for name in sorted(_self_references(layer)):
            declaring = owners.get(name)
            if declaring is None:
                offenders.append((layer.__name__, name, ["undeclared"]))
            elif not inherited.intersection(declaring):
                offenders.append((layer.__name__, name, declaring))
    assert not offenders, (
        "A layer may only reach members it inherits, so a base, sibling, or upper "
        "layer reference has to move down instead: "
        f"{[(layer, name, where) for layer, name, where in offenders]}"
    )

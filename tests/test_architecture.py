"""Architectural invariants, enforced mechanically.

Portability to other platforms is a design goal, and it survives only if the
dependency direction holds. Documenting the rule is not enough — a rule that
is not checked is a rule that erodes. These tests are the check.

The invariant: core is platform-agnostic. It never imports platform code.
Adapters depend on core, not the other way round.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "opendinov3"


def _imported_modules(path: pathlib.Path) -> set[str]:
    """Every module name imported by a source file, statically."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                names.add(node.module)
            # A relative import such as `from ..platform import x` has
            # module="platform" with level>0; record the resolved-ish form so
            # the check below still sees it.
            if node.level and node.module:
                names.add(f"{'.' * node.level}{node.module}")
    return names


def _core_files() -> list[pathlib.Path]:
    return sorted((SRC / "core").rglob("*.py"))


def test_core_package_exists() -> None:
    assert _core_files(), f"no core modules found under {SRC / 'core'}"


@pytest.mark.parametrize("path", _core_files(), ids=lambda p: p.name)
def test_core_does_not_import_platform(path: pathlib.Path) -> None:
    """core must not depend on any platform adapter.

    If this fails, adding a new platform later will require changing core,
    which is exactly the outcome the layering exists to prevent.
    """
    offenders = {
        name
        for name in _imported_modules(path)
        if "platform" in name.split(".") or name.endswith(".platform")
    }
    assert not offenders, (
        f"{path.name} imports platform code: {sorted(offenders)}. "
        "Dependencies point from platform to core, never the reverse."
    )


@pytest.mark.parametrize("path", _core_files(), ids=lambda p: p.name)
def test_core_avoids_scheduler_and_site_specific_modules(
    path: pathlib.Path,
) -> None:
    """core must not reach for a scheduler or a shell either.

    Importing subprocess in core is how platform coupling usually starts: a
    single qsub call, then another. Adapters may do this; core may not.
    """
    forbidden = {"subprocess", "shutil.which"}
    offenders = forbidden & _imported_modules(path)
    assert not offenders, (
        f"{path.name} imports {sorted(offenders)}. Process execution belongs "
        "in a platform adapter, not in core."
    )

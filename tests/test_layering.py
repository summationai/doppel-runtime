"""The package's layers, enforced. A layer may import only the layers listed for it.

types   plain data                      -> nothing of ours
core    pure decisions                  -> types
ports   interfaces to the outside       -> types
store   persistence                     -> types
roles   events wired to the above       -> types, core, ports, store
(top)   app, cli, config                -> anything
"""

import ast
from pathlib import Path

import pytest

PKG = Path(__file__).resolve().parents[1] / "src" / "doppel_runtime"
ALLOWED = {
    "types": {"types"},
    "core": {"types", "core"},
    "ports": {"types", "ports"},
    "store": {"types", "store"},
    "roles": {"types", "core", "ports", "store", "roles"},
}


def our_imports(path: Path) -> set[str]:
    """The top-level layer of every `doppel_runtime.<layer>` this file imports."""
    layers = set()
    for node in ast.walk(ast.parse(path.read_text())):
        names = []
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            names = [node.module]
        for name in names:
            parts = name.split(".")
            if parts[0] == "doppel_runtime" and len(parts) > 1:
                layers.add(parts[1])
            elif parts[0] == "doppel_runtime":
                layers.add("(top)")
    return layers


@pytest.mark.parametrize("layer", sorted(ALLOWED))
def test_layer_imports_only_what_it_may(layer):
    for path in sorted((PKG / layer).rglob("*.py")):
        bad = our_imports(path) - ALLOWED[layer]
        assert not bad, (
            f"{path.relative_to(PKG)} imports {sorted(bad)}; {layer} may import only {sorted(ALLOWED[layer])}"
        )


def test_every_package_module_is_in_a_layer():
    top = {p.stem for p in PKG.glob("*.py")} | {p.name for p in PKG.iterdir() if p.is_dir() and p.name != "__pycache__"}
    assert top - set(ALLOWED) == {"__init__", "__main__", "app", "cli", "config"}

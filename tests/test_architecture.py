"""The architecture, enforced: which component may import which, and the control plane's own layers.

    src/shared/          doppel_shared           -> nothing of ours
    src/gateway/         doppel_gateway          -> shared
    src/model-provider/  doppel_model_provider   -> shared
    src/sandbox-runtime/ doppel_sandbox          -> shared        (it runs in the pod: never the runtime)
    src/control-plane/   doppel_control_plane    -> shared, gateway, model-provider  (the process)

Inside the control plane, `core` and `store` import only `doppel_shared` and themselves.
"""

import ast
import tomllib
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
COMPONENTS = {
    "shared": "doppel_shared",
    "gateway": "doppel_gateway",
    "model-provider": "doppel_model_provider",
    "sandbox-runtime": "doppel_sandbox",
    "control-plane": "doppel_control_plane",
}
ALLOWED = {
    "doppel_shared": set(),
    "doppel_gateway": {"doppel_shared"},
    "doppel_model_provider": {"doppel_shared"},
    "doppel_sandbox": {"doppel_shared"},
    "doppel_control_plane": {"doppel_shared", "doppel_gateway", "doppel_model_provider"},
}
CONTROL_PLANE_LAYERS = {"core": {"core"}, "store": {"store"}}  # besides doppel_shared
OURS = set(COMPONENTS.values())


def imports(path: Path) -> set[str]:
    names = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            names |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module)
    return names


def files(component: str) -> list[Path]:
    return sorted(p for p in (SRC / component).rglob("*.py") if "__pycache__" not in p.parts)


@pytest.mark.parametrize("component", sorted(COMPONENTS))
def test_component_imports_only_what_it_may(component):
    me = COMPONENTS[component]
    for path in files(component):
        if "tests" in path.relative_to(SRC / component).parts:
            continue  # tests may use the shared fakes and whatever they test
        ours = {n.split(".")[0] for n in imports(path)} & OURS
        bad = ours - ALLOWED[me] - {me}
        assert not bad, f"{path.relative_to(SRC)} imports {sorted(bad)}; {me} may import only {sorted(ALLOWED[me])}"


@pytest.mark.parametrize("layer", sorted(CONTROL_PLANE_LAYERS))
def test_control_plane_core_and_store_stay_pure(layer):
    root = SRC / "control-plane" / "doppel_control_plane" / layer
    for path in sorted(root.rglob("*.py")):
        for name in imports(path):
            parts = name.split(".")
            if parts[0] in OURS - {"doppel_shared", "doppel_control_plane"}:
                pytest.fail(f"{path.relative_to(SRC)} imports {name}")
            if parts[0] == "doppel_control_plane" and (len(parts) < 2 or parts[1] not in CONTROL_PLANE_LAYERS[layer]):
                pytest.fail(f"{path.relative_to(SRC)} imports {name}; {layer} may use only doppel_shared and itself")


def test_each_component_declares_what_it_imports():
    """A component's pyproject lists every component it imports, so it installs on its own."""
    for component, package in COMPONENTS.items():
        deps = tomllib.loads((SRC / component / "pyproject.toml").read_text())["project"]["dependencies"]
        declared = {d.split(">")[0].split("=")[0].strip().replace("-", "_") for d in deps}
        used = set().union(*({n.split(".")[0] for n in imports(p)} for p in files(component)
                             if "tests" not in p.relative_to(SRC / component).parts)) & OURS - {package}  # fmt: skip
        assert used <= declared, f"{component} imports {sorted(used - declared)} without declaring it"


def test_src_holds_exactly_the_components():
    assert {p.name for p in SRC.iterdir() if p.is_dir()} == set(COMPONENTS)

import ast
from pathlib import Path

SOURCE_ROOT = Path(__file__).parents[1] / "src" / "passagen"


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            modules.add(node.module)
    return modules


def test_root_contains_only_package_entrypoint() -> None:
    assert {path.name for path in SOURCE_ROOT.glob("*.py")} == {"__init__.py"}


def test_http_clients_are_confined_to_external_package() -> None:
    offenders = [
        path.relative_to(SOURCE_ROOT)
        for path in SOURCE_ROOT.rglob("*.py")
        if "httpx" in _imports(path) and not path.is_relative_to(SOURCE_ROOT / "external")
    ]
    assert offenders == []


def test_core_does_not_depend_on_application_frameworks() -> None:
    forbidden = ("fastapi", "rich", "typer", "passagen_cli", "passagen_web")
    offenders = [
        path.relative_to(SOURCE_ROOT)
        for path in SOURCE_ROOT.rglob("*.py")
        if any(module.startswith(forbidden) for module in _imports(path))
    ]
    assert offenders == []


def test_application_layers_do_not_import_external_adapters() -> None:
    offenders: list[Path] = []
    for package in ("stages",):
        for path in (SOURCE_ROOT / package).rglob("*.py"):
            if any(module.startswith("passagen.external") for module in _imports(path)):
                offenders.append(path.relative_to(SOURCE_ROOT))
    assert offenders == []


def test_external_does_not_depend_on_application_layers() -> None:
    forbidden = ("passagen.providers", "passagen.stages", "passagen.storage")
    offenders = [
        path.relative_to(SOURCE_ROOT)
        for path in (SOURCE_ROOT / "external").rglob("*.py")
        if any(module.startswith(forbidden) for module in _imports(path))
    ]
    assert offenders == []


def test_external_does_not_parse_local_files() -> None:
    offenders = [
        path.relative_to(SOURCE_ROOT)
        for path in (SOURCE_ROOT / "external").rglob("*.py")
        if "pymupdf" in _imports(path)
    ]
    assert offenders == []


def test_parsing_does_not_depend_on_application_or_external_layers() -> None:
    forbidden = ("passagen.external", "passagen.providers", "passagen.stages")
    offenders = [
        path.relative_to(SOURCE_ROOT)
        for path in (SOURCE_ROOT / "parsing").rglob("*.py")
        if any(module.startswith(forbidden) for module in _imports(path))
    ]
    assert offenders == []

"""Static checks: workflow modules must not call LLMs, write files, or do external I/O."""

from __future__ import annotations

import ast
from pathlib import Path

WORKFLOW_DIR = Path(__file__).resolve().parents[1] / "src" / "dev_pipeline" / "workflows"

FORBIDDEN_MODULES = frozenset(
    {
        "httpx",
        "openai",
        "anthropic",
        "requests",
        "aiohttp",
        "subprocess",
        "socket",
        "urllib",
        "urllib.request",
        "pathlib",
        "dev_pipeline.config_root",
        "dev_pipeline.worker",
        "dev_pipeline.cli",
        "dev_pipeline.budget_config",
    }
)
FORBIDDEN_CALLS = frozenset(
    {
        "open",
        "write_text",
        "write_bytes",
        "mkdir",
        "rmdir",
        "unlink",
        "touch",
        "urlopen",
        "urlretrieve",
        "Popen",
        "run",
        "system",
        "sleep",
        "time",
    }
)
FORBIDDEN_ATTR_ROOTS = {
    "datetime": frozenset({"now", "utcnow", "today"}),
    "time": frozenset({"time", "sleep", "monotonic"}),
    "uuid": frozenset({"uuid4", "uuid1"}),
    "random": frozenset({"random", "randint", "choice"}),
    "Path": frozenset({"write_text", "write_bytes", "mkdir", "touch", "open"}),
    "pathlib": frozenset({"Path"}),
}


def _module_files() -> list[Path]:
    return sorted(path for path in WORKFLOW_DIR.glob("*.py") if path.name != "__init__.py")


def test_workflow_package_exists() -> None:
    assert WORKFLOW_DIR.is_dir()
    assert (WORKFLOW_DIR / "parent.py").is_file()
    assert (WORKFLOW_DIR / "ticket.py").is_file()


def test_workflow_modules_have_no_forbidden_imports_or_io() -> None:
    violations: list[str] = []
    for path in _module_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name in FORBIDDEN_MODULES or alias.name.split(".")[0] in {
                        "httpx",
                        "openai",
                        "anthropic",
                        "requests",
                        "aiohttp",
                        "subprocess",
                        "socket",
                        "pathlib",
                    }:
                        violations.append(f"{path.name}:{node.lineno} import {alias.name}")
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if module in FORBIDDEN_MODULES or module.split(".")[:2] == ["urllib", "request"]:
                    violations.append(f"{path.name}:{node.lineno} from {module} import ...")
                if module == "dev_pipeline.config_root":
                    violations.append(f"{path.name}:{node.lineno} imports config_root")
            elif isinstance(node, ast.Call):
                name = _call_name(node.func)
                if name in FORBIDDEN_CALLS and not _is_workflow_api(node.func):
                    # `run` is too generic; only flag subprocess.run-style or open().
                    if name in {"run", "time", "system"}:
                        continue
                    if name == "open":
                        violations.append(f"{path.name}:{node.lineno} call open()")
                    elif name in {"write_text", "write_bytes", "mkdir", "urlopen", "Popen"}:
                        violations.append(f"{path.name}:{node.lineno} call {name}()")
            elif isinstance(node, ast.Attribute):
                root = _root_name(node)
                attr = node.attr
                if root in FORBIDDEN_ATTR_ROOTS and attr in FORBIDDEN_ATTR_ROOTS[root]:
                    if not _is_workflow_api(node):
                        violations.append(f"{path.name}:{node.lineno} {root}.{attr}")
    assert violations == [], "Workflow 模块含有非法 IO/LLM/非确定性调用:\n" + "\n".join(
        violations
    )


def test_workflow_source_does_not_mention_llm_clients() -> None:
    banned = ("openai", "anthropic", "httpx", "ChatCompletion", "CURSOR_API_KEY")
    for path in _module_files():
        text = path.read_text(encoding="utf-8")
        for token in banned:
            assert token not in text, f"{path.name} contains {token}"


def _call_name(func: ast.expr) -> str:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _root_name(node: ast.expr) -> str:
    current = node
    while isinstance(current, ast.Attribute):
        current = current.value
    if isinstance(current, ast.Name):
        return current.id
    return ""


def _is_workflow_api(node: ast.expr) -> bool:
    return _root_name(node) == "workflow"

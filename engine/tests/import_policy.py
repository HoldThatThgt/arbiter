import ast
import sys
import sysconfig
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional


PY39_STDLIB_FALLBACK = {
    "__future__",
    "argparse",
    "ast",
    "dataclasses",
    "os",
    "pathlib",
    "subprocess",
    "sys",
    "sysconfig",
    "tempfile",
    "textwrap",
    "typing",
    "unittest",
}

OPTIONAL_EXTRA_IMPORTS = {
    ("runs/scan.py", "tree_sitter"),
}


@dataclass(frozen=True)
class ImportViolation:
    path: Path
    line: int
    module: str
    detail: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.module}: {self.detail}"


def find_import_violations(root: Path) -> list[ImportViolation]:
    root = root.resolve()
    stdlib = _stdlib_roots()
    violations: list[ImportViolation] = []

    for path in sorted(root.rglob("*.py")):
        relpath = path.relative_to(root)
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(relpath))
        except SyntaxError as exc:
            violations.append(
                ImportViolation(relpath, exc.lineno or 1, "<syntax>", exc.msg)
            )
            continue

        visitor = _ImportVisitor(relpath, root.name, stdlib)
        visitor.visit(tree)
        violations.extend(visitor.violations)

    return violations


def _stdlib_roots() -> set[str]:
    roots = set(getattr(sys, "stdlib_module_names", ()))
    if roots:
        return roots

    roots = set(sys.builtin_module_names)
    roots.update(PY39_STDLIB_FALLBACK)
    stdlib_path = Path(sysconfig.get_paths()["stdlib"])
    roots.update(_top_level_modules(stdlib_path))
    return roots


def _top_level_modules(path: Path) -> Iterable[str]:
    for child in path.iterdir():
        if child.name.startswith("_"):
            continue
        if child.suffix == ".py":
            yield child.stem
        elif (child / "__init__.py").exists():
            yield child.name


class _ImportVisitor(ast.NodeVisitor):
    def __init__(self, path: Path, package_root: str, stdlib: set[str]) -> None:
        self.path = path
        self.package_root = package_root
        self.stdlib = stdlib
        self.violations: list[ImportViolation] = []
        self._import_guard_depth = 0

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self._check(node.lineno, alias.name.split(".", 1)[0])

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.level:
            return
        if node.module:
            self._check(node.lineno, node.module.split(".", 1)[0])

    def visit_Call(self, node: ast.Call) -> None:
        root = _dynamic_import_root(node)
        if root is not None:
            self._check(node.lineno, root)
        self.generic_visit(node)

    def visit_Try(self, node: ast.Try) -> None:
        guarded = _catches_import_error(node.handlers)
        if guarded:
            self._import_guard_depth += 1
        for child in node.body:
            self.visit(child)
        if guarded:
            self._import_guard_depth -= 1

        for child in node.orelse + node.finalbody:
            self.visit(child)
        for handler in node.handlers:
            for child in handler.body:
                self.visit(child)

    def _check(self, line: int, root: str) -> None:
        if root == self.package_root or root in self.stdlib:
            return
        if (self.path.as_posix(), root) in OPTIONAL_EXTRA_IMPORTS:
            if self._import_guard_depth:
                return
            self.violations.append(
                ImportViolation(
                    self.path,
                    line,
                    root,
                    "optional extra import must be guarded by ImportError",
                )
            )
            return
        self.violations.append(
            ImportViolation(self.path, line, root, "not in stdlib or package")
        )


def _catches_import_error(handlers: list[ast.ExceptHandler]) -> bool:
    return any(_handler_catches(handler.type) for handler in handlers)


def _handler_catches(node: Optional[ast.expr]) -> bool:
    if isinstance(node, ast.Name):
        return node.id in {"ImportError", "ModuleNotFoundError"}
    if isinstance(node, ast.Tuple):
        return any(_handler_catches(elt) for elt in node.elts)
    return False


def _dynamic_import_root(node: ast.Call) -> Optional[str]:
    if not node.args:
        return None
    if not _is_dynamic_import_call(node.func):
        return None

    module = node.args[0]
    if not isinstance(module, ast.Constant) or not isinstance(module.value, str):
        return None
    return module.value.split(".", 1)[0]


def _is_dynamic_import_call(node: ast.expr) -> bool:
    if isinstance(node, ast.Name):
        return node.id == "__import__"
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "import_module"
        and isinstance(node.value, ast.Name)
        and node.value.id == "importlib"
    )

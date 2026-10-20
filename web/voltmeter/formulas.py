"""Formula files: line 1 = unit, line 2 = expression in terms of ``V`` (volts)."""
from __future__ import annotations

import ast
import logging
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict

log = logging.getLogger(__name__)

_FUNCS: Dict[str, Callable[..., float]] = {
    "sqrt": math.sqrt, "log": math.log, "ln": math.log, "log10": math.log10,
    "log2": math.log2, "exp": math.exp, "abs": abs, "sin": math.sin,
    "cos": math.cos, "tan": math.tan, "asin": math.asin, "acos": math.acos,
    "atan": math.atan, "pow": math.pow, "min": min, "max": max, "round": round,
}
_CONSTS = {"pi": math.pi, "e": math.e}
_BIN_OPS = (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Pow, ast.Mod, ast.FloorDiv)
_UNARY_OPS = (ast.UAdd, ast.USub)


class FormulaError(ValueError):
    pass


def _validate(node: ast.AST) -> None:
    """Whitelist the AST so file contents can never run arbitrary code."""
    if isinstance(node, ast.Expression):
        _validate(node.body)
    elif isinstance(node, ast.BinOp) and isinstance(node.op, _BIN_OPS):
        _validate(node.left)
        _validate(node.right)
    elif isinstance(node, ast.UnaryOp) and isinstance(node.op, _UNARY_OPS):
        _validate(node.operand)
    elif isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) \
            and not isinstance(node.value, bool):
        # Floats only: int ** int could otherwise hang the device.
        node.value = float(node.value)
    elif isinstance(node, ast.Name) and (node.id == "V" or node.id in _CONSTS
                                         or node.id in _FUNCS):
        pass
    elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
            and node.func.id in _FUNCS and not node.keywords:
        for arg in node.args:
            _validate(arg)
    else:
        raise FormulaError(f"unsupported syntax: {type(node).__name__}")


@dataclass(frozen=True)
class Formula:
    name: str
    unit: str
    expression: str
    _code: object = None

    @classmethod
    def parse(cls, name: str, text: str) -> "Formula":
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        if len(lines) < 2:
            raise FormulaError("need a unit line and an expression line")
        unit, expr = lines[0], lines[1]
        try:
            tree = ast.parse(expr, mode="eval")
            _validate(tree)
            code = compile(tree, f"<formula {name}>", "eval")
        except SyntaxError as exc:
            raise FormulaError(f"bad expression: {exc.msg}") from exc
        return cls(name, unit, expr, code)

    def __call__(self, volts: float) -> float:
        env = {"__builtins__": {}, **_FUNCS, **_CONSTS, "V": volts}
        try:
            return float(eval(self._code, env))  # noqa: S307 - AST is whitelisted
        except (ArithmeticError, ValueError, TypeError):
            return math.nan


RAW = Formula.parse("Raw", "V\nV")


def load_formulas(folder: Path) -> Dict[str, Formula]:
    """Load every ``*.txt`` in folder. Invalid files are skipped. ``Raw`` is always present."""
    result = {RAW.name: RAW}
    if folder.is_dir():
        for path in sorted(folder.glob("*.txt")):
            if path.name.startswith("."):
                continue
            try:
                f = Formula.parse(path.stem, path.read_text(encoding="utf-8"))
            except (FormulaError, OSError, UnicodeDecodeError) as exc:
                log.warning("skipping formula %s: %s", path, exc)
                continue
            result[f.name] = f
    return result

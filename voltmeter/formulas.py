"""Formula files: line 1 = unit, line 2 = expression in terms of ``V`` (volts)."""
from __future__ import annotations

import ast
import logging
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Optional

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

    def sql(self, column: str = "volts") -> Optional[str]:
        """The expression as a SQL float8 expression over ``column``, or None if it can't be translated.

        Domain and overflow errors give NULL (like NaN here) instead of failing the query.
        """
        try:
            return _to_sql(ast.parse(self.expression, mode="eval").body, column)
        except FormulaError:
            return None

    def __call__(self, volts: float) -> float:
        env = {"__builtins__": {}, **_FUNCS, **_CONSTS, "V": volts}
        try:
            return float(eval(self._code, env))  # noqa: S307 - AST is whitelisted
        except (ArithmeticError, ValueError, TypeError):
            return math.nan


def _lit(value: float) -> str:
    if not math.isfinite(value):
        raise FormulaError("non-finite constant")
    return f"({value!r})::float8"


def _guard(cond: str, expr: str) -> str:
    return f"(CASE WHEN {cond} THEN {expr} END)"


def _pow(a: str, b: str) -> str:
    return (f"(CASE WHEN {a} > 0 THEN {_guard(f'abs({b}) * abs(ln({a})) < 700', f'power({a}, {b})')} "
            f"WHEN {a} = 0 AND {b} > 0 THEN 0::float8 END)")


def _call(name: str, a: list) -> str:
    n = len(a)
    one = {
        "sqrt": lambda x: _guard(f"{x} >= 0", f"sqrt({x})"),
        "log": lambda x: _guard(f"{x} > 0", f"ln({x})"),
        "ln": lambda x: _guard(f"{x} > 0", f"ln({x})"),
        "log10": lambda x: _guard(f"{x} > 0", f"log({x})"),
        "log2": lambda x: _guard(f"{x} > 0", f"ln({x}) / ln(2::float8)"),
        "exp": lambda x: _guard(f"{x} < 700", f"exp({x})"),
        "abs": lambda x: f"abs({x})", "sin": lambda x: f"sin({x})", "cos": lambda x: f"cos({x})",
        "tan": lambda x: f"tan({x})", "atan": lambda x: f"atan({x})", "round": lambda x: f"round({x})",
        "asin": lambda x: _guard(f"abs({x}) <= 1", f"asin({x})"),
        "acos": lambda x: _guard(f"abs({x}) <= 1", f"acos({x})"),
    }
    if name in one and n == 1:
        return one[name](a[0])
    if name == "pow" and n == 2:
        return _pow(a[0], a[1])
    if name in ("min", "max") and n >= 2:
        return f"{'LEAST' if name == 'min' else 'GREATEST'}({', '.join(a)})"
    raise FormulaError(f"{name}() with {n} arguments has no SQL translation")


def _to_sql(node: ast.AST, col: str) -> str:
    if isinstance(node, ast.Constant):
        return _lit(float(node.value))
    if isinstance(node, ast.Name):
        if node.id == "V":
            return col
        if node.id in _CONSTS:
            return _lit(_CONSTS[node.id])
        raise FormulaError(f"bare name {node.id}")
    if isinstance(node, ast.UnaryOp):
        x = _to_sql(node.operand, col)
        return f"(-{x})" if isinstance(node.op, ast.USub) else x
    if isinstance(node, ast.BinOp):
        a, b = _to_sql(node.left, col), _to_sql(node.right, col)
        if isinstance(node.op, (ast.Add, ast.Sub, ast.Mult)):
            op = {ast.Add: "+", ast.Sub: "-", ast.Mult: "*"}[type(node.op)]
            return f"({a} {op} {b})"
        if isinstance(node.op, ast.Div):
            return f"({a} / NULLIF({b}, 0))"
        if isinstance(node.op, ast.Pow):
            return _pow(a, b)
        raise FormulaError("% and // have no SQL translation")
    if isinstance(node, ast.Call):
        return _call(node.func.id, [_to_sql(arg, col) for arg in node.args])
    raise FormulaError(f"unsupported syntax: {type(node).__name__}")


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

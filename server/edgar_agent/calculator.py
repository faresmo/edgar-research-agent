"""A small, safe calculator for the agent.

Arithmetic is never done in the model's head: it sends an expression here.
Only numbers, + - * / ** %, brackets, named variables and a few functions are
allowed. There is no way to run other Python code through it.
"""

from __future__ import annotations

import ast
import math
import operator
import re
from typing import Any, Dict, Optional


class CalculationError(ValueError):
    pass


def _cagr(start: float, end: float, years: float) -> float:
    """Compound annual growth rate, as a fraction (0.226 for 22.6%)."""
    if start <= 0 or years <= 0:
        raise CalculationError("cagr needs a positive start value and a positive number of years")
    return (end / start) ** (1.0 / years) - 1.0


def _pct_change(old: float, new: float) -> float:
    if old == 0:
        raise CalculationError("pct_change from zero is undefined")
    return (new - old) / abs(old)


def _bps(fraction: float) -> float:
    """A fraction as basis points: 0.0141 -> 141."""
    return fraction * 10000.0


def _avg(*values: float) -> float:
    if not values:
        raise CalculationError("avg of nothing")
    return sum(values) / len(values)


def _ufcf(ebit, da, tax_rate, capex, nwc_change_cf, sbc=0.0, other_noncash=0.0):
    """Unlevered free cash flow by the catalogue's mechanics (CFA FCFF): EBIT x (1 - t) + D&A + non-cash
    charges (SBC is ADDED BACK) - capex + the cash-flow statement's change-in-NWC line used with its sign
    (negative = outflow). Pass capex as a positive number."""
    return ebit * (1 - tax_rate) + da + sbc + other_noncash - abs(capex) + nwc_change_cf


def _npv(rate, cashflows):
    """Present value of cash flows at t = 1..n, end-of-year discounting (no mid-year convention)."""
    return sum(cf / (1 + rate) ** t for t, cf in enumerate(cashflows, start=1))


def _pv(value, rate, years):
    return value / (1 + rate) ** years


def _terminal_exit(final_year_ebitda, multiple):
    return final_year_ebitda * multiple


def _terminal_growth(final_year_fcf, rate, growth):
    if rate <= growth:
        raise CalculationError("terminal growth must be below the discount rate")
    return final_year_fcf * (1 + growth) / (rate - growth)


def _dcf_ev(ufcfs, rate, terminal_value):
    """Enterprise value = NPV of the projected UFCFs (t = 1..n) + terminal value discounted n full years."""
    return _npv(rate, ufcfs) + _pv(terminal_value, rate, len(ufcfs))


FUNCTIONS = {
    "abs": abs, "round": round, "min": min, "max": max, "sum": lambda *v: sum(v),
    "avg": _avg, "cagr": _cagr, "pct_change": _pct_change, "bps": _bps,
    "sqrt": math.sqrt, "log": math.log,
    # valuation mechanics as the metric catalogue defines them (docs/METRIC_DEFINITIONS.md): use these instead of
    # writing the discounting or the FCF build-up by hand, so the convention is the same in every answer
    "ufcf": _ufcf, "npv": _npv, "pv": _pv, "terminal_exit": _terminal_exit, "terminal_growth": _terminal_growth, "dcf_ev": _dcf_ev,
}
OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
       ast.Pow: operator.pow, ast.Mod: operator.mod, ast.USub: operator.neg, ast.UAdd: operator.pos}


def calculate(expression: str, variables: Optional[Dict[str, float]] = None) -> Dict[str, Any]:
    """Evaluate `expression`. Returns {"expression", "result", "variables"}.

    Thousands separators are fine ("391,035"); write percentages as fractions
    (0.2262, not 22.62%) and dollar amounts without the sign.
    """
    text = (expression or "").strip()
    if not text:
        raise CalculationError("expression is empty")
    names = {k: float(v) for k, v in (variables or {}).items()}
    # Drop thousands separators ("391,035") but keep the commas between arguments.
    cleaned = re.sub(r"(?<=\d),(?=\d{3}(?!\d))", "", text)
    try:
        tree = ast.parse(cleaned, mode="eval")
    except SyntaxError as exc:
        raise CalculationError(f"could not parse {text!r}: {exc.msg}") from None
    try:
        result = _eval(tree.body, names)
    except ZeroDivisionError:
        raise CalculationError("division by zero") from None
    _check_number(result)
    return {"expression": text, "result": result, "variables": names}


MAX_LIST = 400  # elements; a 5- or 30-year model is tiny, this stops runaway comprehensions
MAX_STEPS = 200  # assignments in a script

_LIST_FUNCTIONS = {"len": len, "sum": sum, "min": min, "max": max,
                   "zip": lambda *seqs: list(zip(*seqs)), "range": lambda *a: list(range(*(int(x) for x in a))),
                   "list": list, "avg": lambda seq: sum(seq) / len(seq) if len(seq) else _avg()}


def _check_number(value: Any) -> Any:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CalculationError("expression did not produce a number")
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        raise CalculationError("result is not a finite number")
    return value


def _eval(node: ast.AST, names: Dict[str, Any]) -> Any:
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise CalculationError("only numbers are allowed")
        return node.value
    if isinstance(node, ast.Name):
        if node.id in names:
            return names[node.id]
        raise CalculationError(f"unknown name {node.id!r}; define it first or pass it in variables")
    if isinstance(node, ast.BinOp) and type(node.op) in OPS:
        left, right = _eval(node.left, names), _eval(node.right, names)
        if isinstance(left, list) or isinstance(right, list):
            raise CalculationError("arithmetic works on numbers; use a comprehension for lists, e.g. [x * 2 for x in xs]")
        return OPS[type(node.op)](left, right)
    if isinstance(node, ast.UnaryOp) and type(node.op) in OPS:
        return OPS[type(node.op)](_eval(node.operand, names))
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.keywords:
            raise CalculationError("only these functions are allowed: " + ", ".join(sorted(FUNCTIONS | _LIST_FUNCTIONS)))
        args = [_eval(a, names) for a in node.args]
        fname = node.func.id
        if fname in ("sum", "min", "max", "avg") and len(args) == 1 and isinstance(args[0], list):
            return _LIST_FUNCTIONS[fname](args[0])
        if fname in FUNCTIONS:
            return FUNCTIONS[fname](*args)
        if fname in _LIST_FUNCTIONS:
            out = _LIST_FUNCTIONS[fname](*args)
            if isinstance(out, list) and len(out) > MAX_LIST:
                raise CalculationError(f"lists are limited to {MAX_LIST} elements")
            return out
        raise CalculationError("only these functions are allowed: " + ", ".join(sorted(FUNCTIONS | _LIST_FUNCTIONS)))
    # --- script-form additions: lists, indexing, comprehensions, tuple unpacking in comprehensions
    if isinstance(node, (ast.List, ast.Tuple)):
        items = [_eval(e, names) for e in node.elts]
        if len(items) > MAX_LIST:
            raise CalculationError(f"lists are limited to {MAX_LIST} elements")
        return items
    if isinstance(node, ast.Subscript):
        seq = _eval(node.value, names)
        if not isinstance(seq, list):
            raise CalculationError("only lists can be indexed")
        if isinstance(node.slice, ast.Slice):
            lo = _eval(node.slice.lower, names) if node.slice.lower else None
            hi = _eval(node.slice.upper, names) if node.slice.upper else None
            return seq[int(lo) if lo is not None else None:int(hi) if hi is not None else None]
        idx = _eval(node.slice, names)
        try:
            return seq[int(idx)]
        except IndexError:
            raise CalculationError("list index out of range") from None
    if isinstance(node, ast.ListComp):
        if len(node.generators) != 1 or node.generators[0].is_async:
            raise CalculationError("one 'for' per comprehension")
        gen = node.generators[0]
        source = _eval(gen.iter, names)
        if not isinstance(source, list):
            raise CalculationError("comprehensions iterate over lists")
        out = []
        for item in source:
            local = dict(names)
            _bind(gen.target, item, local)
            if all(_eval(cond, local) for cond in gen.ifs):
                out.append(_eval(node.elt, local))
        return out
    if isinstance(node, ast.Compare) and len(node.ops) == 1:
        left, right = _eval(node.left, names), _eval(node.comparators[0], names)
        op = type(node.ops[0])
        table = {ast.Lt: operator.lt, ast.LtE: operator.le, ast.Gt: operator.gt, ast.GtE: operator.ge, ast.Eq: operator.eq, ast.NotEq: operator.ne}
        if op in table:
            return table[op](left, right)
    if isinstance(node, ast.IfExp):
        return _eval(node.body, names) if _eval(node.test, names) else _eval(node.orelse, names)
    raise CalculationError(f"not allowed in a calculation: {type(node).__name__}")


def _bind(target: ast.AST, value: Any, names: Dict[str, Any]) -> None:
    if isinstance(target, ast.Name):
        names[target.id] = value
    elif isinstance(target, (ast.Tuple, ast.List)):
        if not isinstance(value, (list, tuple)) or len(value) != len(target.elts):
            raise CalculationError("cannot unpack: lengths differ")
        for t, v in zip(target.elts, value):
            _bind(t, v, names)
    else:
        raise CalculationError("only simple names can be assigned")


def run_script(script: str, variables: Optional[Dict[str, float]] = None) -> Dict[str, Any]:
    """Evaluate a short script: one assignment per line (`name = expression`), with
    numbers, lists, list comprehensions and the calculator's functions. Returns every
    name defined, in order, so each intermediate value can be recorded. The last
    assignment is the result. Comments after '#' are allowed. Nothing else: no imports,
    loops, attribute access, strings or calls outside the allowed functions."""
    text = (script or "").strip()
    if not text:
        raise CalculationError("script is empty")
    cleaned = re.sub(r"(?<=\d),(?=\d{3}(?!\d))", "", text)
    try:
        tree = ast.parse(cleaned, mode="exec")
    except SyntaxError as exc:
        raise CalculationError(f"could not parse the script at line {exc.lineno}: {exc.msg}") from None
    if len(tree.body) > MAX_STEPS:
        raise CalculationError(f"scripts are limited to {MAX_STEPS} steps")
    names: Dict[str, Any] = {k: float(v) for k, v in (variables or {}).items()}
    defined: Dict[str, Any] = {}
    for i, stmt in enumerate(tree.body, start=1):
        if isinstance(stmt, ast.Expr) and i == len(tree.body):  # a bare final expression is the result
            stmt = ast.Assign(targets=[ast.Name(id="result", ctx=ast.Store())], value=stmt.value)
        if not isinstance(stmt, ast.Assign) or len(stmt.targets) != 1:
            raise CalculationError(f"line {i}: only assignments like 'name = expression' are allowed")
        try:
            value = _eval(stmt.value, names)
        except ZeroDivisionError:
            raise CalculationError(f"line {i}: division by zero") from None
        except CalculationError as exc:
            raise CalculationError(f"line {i}: {exc}") from None
        if isinstance(value, list):
            for v in value:
                _check_number(v)
        else:
            _check_number(value)
        _bind(stmt.targets[0], value, names)
        for t in ([stmt.targets[0]] if isinstance(stmt.targets[0], ast.Name) else stmt.targets[0].elts):
            if isinstance(t, ast.Name):
                defined[t.id] = names[t.id]
    if not defined:
        raise CalculationError("the script defined nothing")
    last = list(defined)[-1]
    return {"script": text, "values": defined, "result": defined[last], "result_name": last}

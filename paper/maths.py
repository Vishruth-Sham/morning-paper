"""Do the Maths: exercises are stored as inputs + formula steps and recomputed here,
so the arithmetic shown to the reader is always calculated, never written by hand."""
from __future__ import annotations

import ast
import math
import operator
import re

_OPS = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
    ast.Pow: operator.pow, ast.USub: operator.neg, ast.UAdd: operator.pos,
}
_FUNCS = {"ceil": math.ceil, "floor": math.floor, "round": round, "max": max, "min": min}


class MathsError(ValueError):
    pass


def safe_eval(expr: str, env: dict) -> float:
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as e:
        raise MathsError(f"Bad expression {expr!r}") from e

    def ev(node):
        if isinstance(node, ast.Expression):
            return ev(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return node.value
        if isinstance(node, ast.Name):
            if node.id not in env:
                raise MathsError(f"Unknown name {node.id!r} in {expr!r}")
            return env[node.id]
        if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
            return _OPS[type(node.op)](ev(node.left), ev(node.right))
        if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
            return _OPS[type(node.op)](ev(node.operand))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _FUNCS:
            return _FUNCS[node.func.id](*[ev(a) for a in node.args])
        raise MathsError(f"Unsupported syntax in {expr!r}")

    return ev(tree)


def compute(exercise: dict) -> dict:
    """Fill in `value` for every step. Raises MathsError if a step cannot be computed."""
    env = {}
    shown = {}
    for i in exercise.get("inputs", []):
        env[i["key"]] = float(i["value"])
        shown[i["key"]] = fmt(float(i["value"]), i.get("unit", ""))
    for s in exercise.get("steps", []):
        val = safe_eval(s["expr"], env)
        if isinstance(val, float) and (math.isnan(val) or math.isinf(val)):
            raise MathsError(f"Step {s['key']} is not a finite number")
        s["shown"] = readable(s["expr"], shown)
        env[s["key"]] = val
        s["value"] = val
        shown[s["key"]] = fmt(val, s.get("unit", ""))
    ans_key = exercise.get("answer_key")
    if ans_key and ans_key not in env:
        raise MathsError(f"Answer key {ans_key!r} is not a step")
    exercise["answer_value"] = env.get(ans_key)
    return exercise


def readable(expr: str, shown: dict) -> str:
    """Turn `price_usd * usd_inr` into `$400 × 93.29 ₹ per $` for the reader."""
    out = re.sub(r"\b(ceil|floor|round|min|max)\(", lambda m: {"ceil": "round up(", "floor": "round down(", "round": "round(",
                                                               "min": "lower of(", "max": "higher of("}[m.group(1)], expr)
    out = re.sub(r"\b([A-Za-z_][A-Za-z0-9_]*)\b", lambda m: shown.get(m.group(1), m.group(1)), out)
    out = out.replace("*", "×").replace("/", "÷").replace("  ", " ")
    return re.sub(r"(\d[\d.,]*%) ÷ 100\b", r"\1", out)


def fmt(value: float, unit: str = "") -> str:
    if value is None:
        return ""
    if unit in ("₹", "INR", "inr", "rupees"):
        return "₹" + indian(value)
    if unit in ("$", "USD", "usd"):
        return "$" + (f"{value:,.0f}" if abs(value) >= 100 else f"{value:,.2f}".rstrip("0").rstrip("."))
    if unit == "%":
        return f"{value:.0f}%" if float(value).is_integer() else f"{value:.1f}%"
    num = f"{value:,.0f}" if abs(value) >= 100 or float(value).is_integer() else f"{value:,.2f}".rstrip("0").rstrip(".")
    return f"{num} {unit}".strip()


def indian(value: float) -> str:
    """Format in the Indian numbering system: 1,00,000."""
    neg = value < 0
    n = int(round(abs(value)))
    s = str(n)
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        s = ",".join(parts) + "," + tail
    return ("-" if neg else "") + s


def fallback_exercise(business: str, price_usd: float | None, target_inr: int, usd_inr: float,
                      price_note: str = "assumption") -> dict:
    """Template exercise used when no language model is available."""
    price = price_usd or 50.0
    return {
        "question": f"How many paying customers would {business} need to clear ₹{indian(target_inr)} a month in operating profit?",
        "inputs": [
            {"key": "price_usd", "label": "Price per customer per month", "value": price, "unit": "$",
             "kind": price_note, "note": "" if price_usd else "Not reported in the case study; a placeholder to edit."},
            {"key": "usd_inr", "label": "Exchange rate", "value": usd_inr, "unit": "₹ per $", "kind": "assumption", "note": ""},
            {"key": "delivery_share", "label": "Delivery cost as a share of price", "value": 0.4, "unit": "",
             "kind": "assumption", "note": "Staff time, tools and payment fees per customer."},
            {"key": "fixed_inr", "label": "Fixed monthly costs", "value": 40000, "unit": "₹", "kind": "assumption",
             "note": "Software, accounting, a little marketing."},
            {"key": "target_inr", "label": "Target operating profit", "value": target_inr, "unit": "₹", "kind": "goal", "note": ""},
        ],
        "steps": [
            {"key": "price_inr", "label": "Price in rupees", "expr": "price_usd * usd_inr", "unit": "₹"},
            {"key": "margin_inr", "label": "Contribution per customer after delivery", "expr": "price_inr * (1 - delivery_share)", "unit": "₹"},
            {"key": "needed_exact", "label": "Customers to cover fixed costs and target", "expr": "(fixed_inr + target_inr) / margin_inr", "unit": "customers"},
            {"key": "needed", "label": "Rounded up", "expr": "ceil(needed_exact)", "unit": "customers"},
        ],
        "answer_key": "needed",
        "answer_template": "About {needed}.",
    }

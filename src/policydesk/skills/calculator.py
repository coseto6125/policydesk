"""
The calculator the model calls instead of doing arithmetic itself.

A model that computes 日額 2,000 × 住院 4 日 in its head is a model that can be wrong
by a digit and confident about it, and an insurance figure nobody can re-derive is the
failure this project exists to remove. So the model produces the expression and this
tool evaluates it.

Evaluation walks a parsed AST against an allow-list. Anything the allow-list does not
name — an attribute, a call to something unlisted, a name at all — raises rather than
evaluates, so an expression is arithmetic or it is an error, never a side effect.

Decimal throughout, because money in binary floating point is wrong in ways that
surface as a one-dollar discrepancy in a claim total and cost an afternoon to find.

Follows the calculator in raccoon-ai-platform, including its two hard-won details:
floored division and floored modulo, so negative operands behave the way Python's
integers do rather than the way Decimal truncates by default.
"""

import ast
import math
import operator
from decimal import Decimal, DivisionByZero, InvalidOperation
from typing import Any

from msgspec import Struct

_OPS: dict[type, Any] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    # Decimal truncates toward zero; Python's // floors. Match Python, or a negative
    # operand silently disagrees with every other // in the codebase.
    ast.FloorDiv: lambda a, b: Decimal(math.floor(a / b)),
    ast.Pow: lambda a, b: Decimal(1) if a == 0 and b == 0 else operator.pow(a, b),
    # Same reason: (-1) % 9 is 8 here, as it is for Python ints.
    ast.Mod: lambda a, b: ((a % b) + b) % b,
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}

_FUNCS: dict[str, Any] = {
    "abs": abs,
    "min": min,
    "max": max,
    "round": lambda x, n=Decimal(0): Decimal(x).quantize(Decimal(10) ** -int(n)),
}
# min and max are what a per-year cap looks like in an expression: a benefit written
# "門診手術醫療保險金的給付以十次為限" becomes min(次數, 10) × 倍數 × 日額.


class Computed(Struct, frozen=True):
    """
    One evaluated expression.

    `basis` is the expression as the model wrote it, kept so a reviewer re-derives the
    figure without reading code. It is what `Money.basis` carries onto the screen.
    """

    amount: int
    basis: str


class CalculationError(ValueError):
    """The expression could not be evaluated, and the caller must not fall back."""


def _eval(node: ast.AST) -> Decimal:
    """
    Evaluate one AST node against the allow-list.

    Args:
        node: A node from a parsed expression.

    Returns:
        Its value.

    Raises:
        CalculationError: The node is outside the allow-list, or the arithmetic failed.

    """
    match node:
        case ast.Expression():
            return _eval(node.body)
        case ast.Constant(value=bool()):
            msg = "a boolean is not an amount"
            raise CalculationError(msg)
        case ast.Constant(value=int() | float() | str() as v):
            try:
                return Decimal(str(v))
            except InvalidOperation as exc:
                raise CalculationError(f"{v!r} is not a number") from exc
        case ast.BinOp(op=op) if type(op) in _OPS:
            try:
                return _OPS[type(op)](_eval(node.left), _eval(node.right))
            except (DivisionByZero, ZeroDivisionError) as exc:
                raise CalculationError("division by zero") from exc
            except (InvalidOperation, OverflowError) as exc:
                raise CalculationError(f"arithmetic failed: {exc}") from exc
        case ast.UnaryOp(op=op) if type(op) in _OPS:
            return _OPS[type(op)](_eval(node.operand))
        case ast.Call(func=ast.Name(id=name), args=args, keywords=[]) if name in _FUNCS:
            return Decimal(_FUNCS[name](*(_eval(a) for a in args)))
        case _:
            msg = f"{type(node).__name__} is not allowed in a calculation"
            raise CalculationError(msg)


def calculate(expression: str) -> Computed:
    """
    Evaluate an arithmetic expression and return it with its own working.

    Args:
        expression: Arithmetic over numbers, using + - * / // % ** and abs/min/max/round.
            Names, attributes and any other call raise instead of evaluating.

    Returns:
        The amount in whole TWD, alongside the expression that produced it.

    Raises:
        CalculationError: The expression is not parseable, or steps outside the
            allow-list, or the arithmetic fails. The caller states that it cannot
            compute the figure; it never guesses one.

    """
    if not (expression := expression.strip()):
        msg = "empty expression"
        raise CalculationError(msg)
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise CalculationError(f"cannot parse {expression!r}") from exc

    value = _eval(tree)
    # Premiums and benefits are settled in whole TWD, and rounding at the end rather
    # than per step keeps a multi-term expression off by nothing.
    return Computed(amount=int(value.quantize(Decimal(1))), basis=expression)


TOOL_SCHEMA = {
    "type": "function",
    "name": "calculate",
    "description": (
        "Evaluate an arithmetic expression over insurance figures and return the amount. "
        "Use this for every figure. Write the expression with the numbers already looked "
        "up from the policy, for example '2000 * 4' for a 4-night stay at a 2,000 daily "
        "benefit, or 'min(12, 10) * 3 * 2000' where a benefit is capped at ten occurrences."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "expression": {
                "type": "string",
                "description": "Arithmetic over numbers only. Supports + - * / // % ** and abs, min, max, round.",
            }
        },
        "required": ["expression"],
        "additionalProperties": False,
    },
}

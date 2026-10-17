"""A tiny evaluator for the formulas the Excel export writes, so tests can check that the
workbook computes the same numbers as Python. Supports numbers, cell refs and ranges (also
on other sheets), + - * /, parentheses and SUM, AVERAGE, SUMPRODUCT."""

import re
from typing import Any

from openpyxl.utils import column_index_from_string, get_column_letter
from openpyxl.workbook.workbook import Workbook

_TOKEN = re.compile(
    r"\s*(?:"
    r"(?P<string>\"[^\"]*\")"
    r"|(?P<cmp><>|<=|>=|=|<|>)"
    r"|(?P<number>\d+(?:\.\d+)?)"
    r"|(?P<ref>(?:'(?P<qsheet>[^']+)'!|(?P<sheet>[A-Za-z_][\w]*)!)?"
    r"\$?(?P<col>[A-Z]{1,3})\$?(?P<row>\d+)(?::\$?(?P<col2>[A-Z]{1,3})\$?(?P<row2>\d+))?)"
    r"|(?P<func>[A-Z]+)\("
    r"|(?P<op>[-+*/(),])"
    r")"
)


_DIV0 = object()  # marker for a division by zero inside a function argument


class FormulaEvaluator:
    def __init__(self, workbook: Workbook) -> None:
        self.wb = workbook

    def value(self, sheet: str, cell: str) -> Any:
        raw = self.wb[sheet][cell].value
        if isinstance(raw, str) and raw.startswith("="):
            return self._evaluate(raw[1:], sheet)
        if raw is None or raw == "":
            return 0.0
        if isinstance(raw, str):
            return raw
        return float(raw)

    def _tokens(self, text: str) -> list[tuple[str, Any]]:
        out: list[tuple[str, Any]] = []
        pos = 0
        while pos < len(text):
            m = _TOKEN.match(text, pos)
            if not m or m.end() == pos:
                raise ValueError(f"cannot parse {text[pos:]!r}")
            pos = m.end()
            if m.group("string") is not None:
                out.append(("str", m.group("string")[1:-1]))
            elif m.group("cmp"):
                out.append(("cmp", m.group("cmp")))
            elif m.group("number"):
                out.append(("num", float(m.group("number"))))
            elif m.group("ref"):
                out.append(("ref", m.groupdict()))
            elif m.group("func"):
                out.append(("func", m.group("func")))
            else:
                out.append(("op", m.group("op")))
        return out

    def _evaluate(self, text: str, sheet: str) -> float:
        tokens = self._tokens(text)
        result, pos = self._expr(tokens, 0, sheet)
        if pos != len(tokens):
            raise ValueError(f"trailing tokens in {text!r}")
        if isinstance(result, list):
            raise ValueError(f"range used as a value in {text!r}")
        return result

    @staticmethod
    def _skip(tokens: list[tuple[str, Any]], pos: int) -> int:
        """Position of the comma or closing parenthesis that ends the argument at `pos`."""
        depth = 0
        while True:
            kind, payload = tokens[pos]
            if kind == "func" or (kind == "op" and payload == "("):
                depth += 1
            elif kind == "op" and payload == ")":
                if depth == 0:
                    return pos
                depth -= 1
            elif kind == "op" and payload == "," and depth == 0:
                return pos
            pos += 1

    def _cells(self, ref: dict[str, Any], sheet: str) -> list[float]:
        target = ref["qsheet"] or ref["sheet"] or sheet
        c1, r1 = column_index_from_string(ref["col"]), int(ref["row"])
        c2 = column_index_from_string(ref["col2"]) if ref["col2"] else c1
        r2 = int(ref["row2"]) if ref["row2"] else r1
        return [
            self.value(target, f"{get_column_letter(c)}{r}")
            for r in range(r1, r2 + 1)
            for c in range(c1, c2 + 1)
        ]

    def _expr(self, tokens: list[tuple[str, Any]], pos: int, sheet: str) -> tuple[Any, int]:
        value, pos = self._sum(tokens, pos, sheet)
        if pos < len(tokens) and tokens[pos][0] == "cmp":
            op = tokens[pos][1]
            right, pos = self._sum(tokens, pos + 1, sheet)
            checks = {
                "=": value == right,
                "<>": value != right,
                "<": value < right,
                ">": value > right,
                "<=": value <= right,
                ">=": value >= right,
            }
            return checks[op], pos
        return value, pos

    def _sum(self, tokens: list[tuple[str, Any]], pos: int, sheet: str) -> tuple[Any, int]:
        value, pos = self._term(tokens, pos, sheet)
        while pos < len(tokens) and tokens[pos] in (("op", "+"), ("op", "-")):
            op = tokens[pos][1]
            right, pos = self._term(tokens, pos + 1, sheet)
            value = value + right if op == "+" else value - right
        return value, pos

    def _term(self, tokens: list[tuple[str, Any]], pos: int, sheet: str) -> tuple[Any, int]:
        value, pos = self._factor(tokens, pos, sheet)
        while pos < len(tokens) and tokens[pos] in (("op", "*"), ("op", "/")):
            op = tokens[pos][1]
            right, pos = self._factor(tokens, pos + 1, sheet)
            value = value * right if op == "*" else value / right
        return value, pos

    def _factor(self, tokens: list[tuple[str, Any]], pos: int, sheet: str) -> tuple[Any, int]:
        kind, payload = tokens[pos]
        if kind == "op" and payload == "-":
            value, pos = self._factor(tokens, pos + 1, sheet)
            return -value, pos
        if kind == "op" and payload == "(":
            value, pos = self._expr(tokens, pos + 1, sheet)
            return value, pos + 1  # closing parenthesis
        if kind in ("num", "str"):
            return payload, pos + 1
        if kind == "ref":
            cells = self._cells(payload, sheet)
            return (cells if payload["col2"] else cells[0]), pos + 1
        if kind == "func":
            args: list[Any] = []
            pos += 1
            while tokens[pos] != ("op", ")"):
                start = pos
                try:
                    arg, pos = self._expr(tokens, pos, sheet)
                except ZeroDivisionError:
                    arg, pos = _DIV0, self._skip(tokens, start)
                args.append(arg)
                if tokens[pos] == ("op", ","):
                    pos += 1
            pos += 1
            if payload == "IFERROR":
                return (args[1] if args[0] is _DIV0 else args[0]), pos
            if any(a is _DIV0 for a in args):
                raise ZeroDivisionError
            if payload == "IF":
                return (args[1] if args[0] else args[2]), pos
            if payload == "ABS":
                return abs(args[0]), pos
            flat = [x for a in args for x in (a if isinstance(a, list) else [a])]
            if payload == "SUM":
                return sum(flat), pos
            if payload == "AVERAGE":
                return sum(flat) / len(flat), pos
            if payload == "SUMPRODUCT":
                first, second = args
                return sum(a * b for a, b in zip(first, second, strict=True)), pos
            raise ValueError(f"unsupported function {payload}")
        raise ValueError(f"unexpected token {payload!r}")

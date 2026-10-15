"""A tiny evaluator for the formulas the Excel export writes, so tests can check that the
workbook computes the same numbers as Python. Supports numbers, cell refs and ranges (also
on other sheets), + - * /, parentheses and SUM, AVERAGE, SUMPRODUCT."""

import re
from typing import Any

from openpyxl.utils import column_index_from_string, get_column_letter
from openpyxl.workbook.workbook import Workbook

_TOKEN = re.compile(
    r"\s*(?:"
    r"(?P<number>\d+(?:\.\d+)?)"
    r"|(?P<ref>(?:'(?P<qsheet>[^']+)'!|(?P<sheet>[A-Za-z_][\w]*)!)?"
    r"\$?(?P<col>[A-Z]{1,3})\$?(?P<row>\d+)(?::\$?(?P<col2>[A-Z]{1,3})\$?(?P<row2>\d+))?)"
    r"|(?P<func>[A-Z]+)\("
    r"|(?P<op>[-+*/(),])"
    r")"
)


class FormulaEvaluator:
    def __init__(self, workbook: Workbook) -> None:
        self.wb = workbook

    def value(self, sheet: str, cell: str) -> float:
        raw = self.wb[sheet][cell].value
        if isinstance(raw, str) and raw.startswith("="):
            return self._evaluate(raw[1:], sheet)
        if raw is None or raw == "":
            return 0.0
        return float(raw)

    def _tokens(self, text: str) -> list[tuple[str, Any]]:
        out: list[tuple[str, Any]] = []
        pos = 0
        while pos < len(text):
            m = _TOKEN.match(text, pos)
            if not m or m.end() == pos:
                raise ValueError(f"cannot parse {text[pos:]!r}")
            pos = m.end()
            if m.group("number"):
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
        if kind == "num":
            return payload, pos + 1
        if kind == "ref":
            cells = self._cells(payload, sheet)
            return (cells if payload["col2"] else cells[0]), pos + 1
        if kind == "func":
            args: list[Any] = []
            pos += 1
            while tokens[pos] != ("op", ")"):
                arg, pos = self._expr(tokens, pos, sheet)
                args.append(arg)
                if tokens[pos] == ("op", ","):
                    pos += 1
            pos += 1
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

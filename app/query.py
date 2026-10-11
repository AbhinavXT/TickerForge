"""A query language for finding stocks.

    rsi(14) > 70 and ema(20) crosses_above ema(50)
    close > highest(252) and volume > 2 * avg_volume(20)
    rs > 70 and mos_pct > 20 and adx(14) > 25

The scanner answers fixed questions well. This answers the ones nobody thought
of in advance — which, in practice, is most of them, because a good screen is
usually two or three ordinary conditions combined in a way no menu offers.

Why a parser and not eval()
---------------------------
`eval` on a string is a remote code execution hole wearing a convenience
costume, and this app has an HTTP endpoint and a phone command. A hand-written
recursive-descent parser is perhaps two hundred lines and cannot execute
anything — the worst a malicious query can do is fail to parse.

It also buys error messages that point at a column instead of raising
`NameError: name 'rsi' is not defined`, which tells a user nothing about a
query language they're meeting for the first time.

How evaluation works
--------------------
Numeric expressions evaluate to a **series** — one value per bar — and
comparisons collapse that to a single true/false by reading the **last closed
bar**. Cross operators read the last two. That's the only stateful part, and
it's the reason `crosses_above` means "crossed on the most recent bar" rather
than "has ever crossed".

Series are computed lazily and cached per symbol, so `ema(20) > ema(50) and
ema(20) > close` computes ema(20) once.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from . import indicators as ind

# ----------------------------------------------------------------- tokenizer
TOKEN = re.compile(r"""
    (?P<ws>\s+)
  | (?P<number>\d+\.?\d*(?:[eE][+-]?\d+)?)
  | (?P<string>"[^"\n]*"|'[^'\n]*')
  | (?P<op><=|>=|==|!=|<|>|\+|-|\*|/)
  | (?P<punct>[(),])
  | (?P<ident>[A-Za-z_][A-Za-z_0-9]*)
""", re.X)

KEYWORDS = {"and", "or", "not", "crosses_above", "crosses_below",
            "contains",
            "above", "below", "crossing_above", "crossing_below",
            # presentation clauses — see Plan, below
            "sort", "order", "by", "top", "limit", "show", "select",
            "asc", "desc"}

# spellings people reach for, mapped to the canonical operator
ALIASES = {"crossing_above": "crosses_above", "crossing_below": "crosses_below",
           "above": ">", "below": "<",
           "order": "sort", "limit": "top", "select": "show"}


# Nothing anyone types by hand comes close to these. They exist because the
# parser is reachable from an HTTP endpoint and a phone message, and both
# recursive descent and a left-nested AST turn a long enough input into a
# RecursionError — which is a 500 to the caller and, deep in an interpreter,
# not always a clean one.
MAX_TEXT = 4000
MAX_TOKENS = 400
MAX_DEPTH = 40


class QueryError(ValueError):
    """A query that can't be parsed or evaluated. Carries a column."""

    def __init__(self, message: str, col: int | None = None):
        self.col = col
        super().__init__(message if col is None
                         else f"{message} (at column {col + 1})")


class QueryDataError(QueryError):
    """The query is fine; the stored candles for THIS symbol are not.

    Separate from QueryError because the two want opposite handling in a
    universe scan. A misspelt field is wrong for every symbol and must stop
    the run loudly. A symbol whose candles are missing a high is wrong for
    one symbol, and stopping the run would trade 1,999 good answers for one
    bad row. Sharing a class meant the scan had to guess which it was.
    """


@dataclass(frozen=True)
class Tok:
    kind: str          # number | op | punct | ident | keyword | end
    value: str
    col: int


def tokenize(text: str) -> list[Tok]:
    if len(text) > MAX_TEXT:
        raise QueryError(
            f"that's {len(text)} characters — queries are capped at "
            f"{MAX_TEXT}", MAX_TEXT)
    out, i = [], 0
    while i < len(text):
        m = TOKEN.match(text, i)
        if not m:
            raise QueryError(f"don't understand {text[i]!r}", i)
        i = m.end()
        kind = m.lastgroup
        if kind == "ws":
            continue
        val = m.group()
        if kind == "ident" and val.lower() in KEYWORDS:
            kind, val = "keyword", ALIASES.get(val.lower(), val.lower())
        out.append(Tok(kind, val, m.start()))
        if len(out) > MAX_TOKENS:
            # `1 + 1 + 1 + …` never recurses in the parser — parse_sum loops —
            # but it builds a left-nested tree one level deep per term, and
            # validate(), render() and evaluation all walk it recursively.
            # The depth guard below can't see this one, so it's caught here.
            raise QueryError(
                f"more than {MAX_TOKENS} terms — that isn't a screen",
                m.start())
    out.append(Tok("end", "", len(text)))
    return out


# --------------------------------------------------------------------- AST
@dataclass(frozen=True)
class Num:
    value: float


@dataclass(frozen=True)
class Str:
    """A name, not a number. Only ever a benchmark: `index("NIFTY BANK", …)`.

    Kept as its own node rather than an identifier so a bare `"NIFTY BANK"`
    sitting where an expression belongs fails with something readable instead
    of being looked up in the field catalogue.
    """
    value: str


@dataclass(frozen=True)
class Call:
    name: str
    args: tuple
    col: int


@dataclass(frozen=True)
class Binary:
    op: str
    left: object
    right: object
    col: int


@dataclass(frozen=True)
class Not:
    operand: object


@dataclass(frozen=True)
class Plan:
    """A condition plus how to present what it finds.

    The clauses are part of the LANGUAGE rather than parameters on run(),
    because a saved query and a phone command are each one string. Put `sort`
    in the HTTP layer and `/query` from the phone can never rank anything, and
    a saved screen can never remember how it wanted to be ordered.

    `condition` alone is what every other caller — backtest, to_positions,
    explain — wants, and those clauses are meaningless to all of them.
    """
    condition: object
    sort: object | None = None
    descending: bool = True
    top: int | None = None
    show: tuple = ()


CLAUSES = ("sort", "top", "show")


# ------------------------------------------------------------------ parser
COMPARISONS = {"<", ">", "<=", ">=", "==", "!=",
               "crosses_above", "crosses_below", "contains"}

# Fields whose value is a name rather than a number. They compare only with a
# quoted string, and only with ==, != or contains — `sector > 5` is refused
# rather than coerced into something.
STRING_FIELDS = {"sector", "industry"}
STRING_OPS = {"==", "!=", "contains"}


class Parser:
    """Recursive descent. Grammar, loosest binding first:

        or   := and ("or" and)*
        and  := not ("and" not)*
        not  := "not" not | comparison
        comp := sum (COMPOP sum)?
        sum  := product (("+"|"-") product)*
        prod := unary (("*"|"/") unary)*
        unary:= "-" unary | primary
        prim := NUMBER | IDENT ["(" args ")"] | "(" or ")"
    """

    def __init__(self, tokens: list[Tok]):
        self.toks = tokens
        self.i = 0
        self.depth = 0

    def _descend(self) -> None:
        """Count one level of genuine recursion.

        Only three productions can recurse without consuming a bracket:
        parse_or (through a sub-expression or a function argument), parse_not
        and parse_unary. Guarding those covers every path that can grow the
        Python stack, and leaves the iterative ones — and/or/sum/product
        chains — free to be as long as MAX_TOKENS allows.
        """
        self.depth += 1
        if self.depth > MAX_DEPTH:
            raise QueryError(
                f"nested more than {MAX_DEPTH} deep — check for unbalanced "
                f"brackets", self.peek().col)

    def peek(self) -> Tok:
        return self.toks[self.i]

    def next(self) -> Tok:
        t = self.toks[self.i]
        self.i += 1
        return t

    def accept(self, kind: str, value: str | None = None) -> Tok | None:
        t = self.peek()
        if t.kind == kind and (value is None or t.value == value):
            return self.next()
        return None

    def parse(self):
        node = self.parse_or()
        t = self.peek()
        if t.kind == "keyword" and t.value in CLAUSES:
            # Reachable from backtest(), to_positions() and the alert checker,
            # none of which can honour a ranking. Dropping it silently would
            # run a query the user believes is sorted and truncated.
            raise QueryError(
                f"'{t.value}' only means something when screening — a "
                f"backtest or an alert has nothing to rank", t.col)
        if t.kind != "end":
            raise QueryError(f"unexpected {t.value!r}", t.col)
        return node

    def parse_plan(self) -> Plan:
        cond = self.parse_or()
        seen: set[str] = set()
        sort = show = None
        desc, top = True, None

        while True:
            t = self.peek()
            if not (t.kind == "keyword" and t.value in CLAUSES):
                break
            self.next()
            if t.value in seen:
                # `sort by rs sort by pe` almost certainly means the first was
                # forgotten about. Keeping one of them is a coin flip.
                raise QueryError(f"'{t.value}' given twice", t.col)
            seen.add(t.value)

            if t.value == "sort":
                self.accept("keyword", "by")          # optional noise word
                sort = self.parse_sum()
                if self.accept("keyword", "asc"):
                    desc = False
                elif self.accept("keyword", "desc"):
                    desc = True
            elif t.value == "top":
                n = self.peek()
                self.next()
                if n.kind != "number":
                    raise QueryError("top takes a count, e.g. top 20", n.col)
                top = float(n.value)
                if top != int(top) or top < 1:
                    raise QueryError(
                        f"top takes a whole number of rows, not {n.value}",
                        n.col)
                top = int(top)
            else:                                     # show
                cols = [self.parse_sum()]
                while self.accept("punct", ","):
                    cols.append(self.parse_sum())
                show = tuple(cols)

        if self.peek().kind != "end":
            raise QueryError(f"unexpected {self.peek().value!r}",
                             self.peek().col)
        return Plan(cond, sort, desc, top, show or ())

    def parse_or(self):
        self._descend()
        try:
            node = self.parse_and()
            while self.accept("keyword", "or"):
                node = Binary("or", node, self.parse_and(), self.peek().col)
            return node
        finally:
            self.depth -= 1

    def parse_and(self):
        node = self.parse_not()
        while self.accept("keyword", "and"):
            node = Binary("and", node, self.parse_not(), self.peek().col)
        return node

    def parse_not(self):
        if self.accept("keyword", "not"):
            self._descend()
            try:
                return Not(self.parse_not())
            finally:
                self.depth -= 1
        return self.parse_comparison()

    # Only the ordering operators chain. `a == b == c` is a different idea
    # (equality of three things) and `a crosses_above b < c` isn't one at all.
    CHAINABLE = {"<", "<=", ">", ">="}

    def parse_comparison(self):
        left = self.parse_sum()
        t = self.peek()
        if not ((t.kind == "op" and t.value in COMPARISONS)
                or (t.kind == "keyword" and t.value in COMPARISONS)):
            return left
        self.next()
        middle = self.parse_sum()
        node = Binary(t.value, left, middle, t.col)

        nxt = self.peek()
        if not (t.value in self.CHAINABLE
                and ((nxt.kind == "op" and nxt.value in self.CHAINABLE)
                     or (nxt.kind == "keyword"
                         and nxt.value in self.CHAINABLE))):
            return node

        # `20 < rsi(14) < 30` — the band, written the way it's meant.
        self.next()
        if ("<" in t.value) != ("<" in nxt.value):
            # `20 < x > 30` reads like a band and is satisfiable by anything
            # above 30. Nobody types it deliberately.
            raise QueryError(
                f"'{t.value}' and '{nxt.value}' point opposite ways — a chain "
                f"like this reads as a range, so both have to face the same "
                f"direction", nxt.col)
        right = self.parse_sum()
        # The middle term appears in both halves. It's evaluated once: _num is
        # memoised per node, and both halves hold the same node object.
        return Binary("and", node, Binary(nxt.value, middle, right, nxt.col),
                      nxt.col)

    def parse_sum(self):
        node = self.parse_product()
        while True:
            t = self.peek()
            if t.kind == "op" and t.value in ("+", "-"):
                self.next()
                node = Binary(t.value, node, self.parse_product(), t.col)
            else:
                return node

    def parse_product(self):
        node = self.parse_unary()
        while True:
            t = self.peek()
            if t.kind == "op" and t.value in ("*", "/"):
                self.next()
                node = Binary(t.value, node, self.parse_unary(), t.col)
            else:
                return node

    def parse_unary(self):
        t = self.peek()
        if t.kind == "op" and t.value == "-":
            self.next()
            self._descend()
            try:
                return Binary("-", Num(0.0), self.parse_unary(), t.col)
            finally:
                self.depth -= 1
        return self.parse_primary()

    def parse_primary(self):
        t = self.next()
        if t.kind == "number":
            return Num(float(t.value))
        if t.kind == "string":
            return Str(t.value[1:-1])
        if t.kind == "punct" and t.value == "(":
            node = self.parse_or()
            if not self.accept("punct", ")"):
                raise QueryError("missing )", self.peek().col)
            return node
        if t.kind == "ident":
            args = []
            if self.accept("punct", "("):
                if not self.accept("punct", ")"):
                    while True:
                        # parse_or, not parse_sum: history operators take a
                        # CONDITION as an argument — `within(5, rsi > 70)`.
                        args.append(self.parse_or())
                        if self.accept("punct", ","):
                            continue
                        if self.accept("punct", ")"):
                            break
                        raise QueryError("missing ) or ,", self.peek().col)
            return Call(t.value.lower(), tuple(args), t.col)
        raise QueryError(f"unexpected {t.value or 'end of query'!r}", t.col)


def plan(text: str) -> Plan:
    """Parse a full screen: a condition plus how to rank and present it.

    `parse` is the same thing without the clauses, kept separate because every
    other caller wants a bare condition and would have to reach past a wrapper
    to get one.
    """
    if not (text or "").strip():
        raise QueryError("empty query")
    p = Parser(tokenize(text)).parse_plan()
    validate(p.condition)
    # Clause expressions are validated too — a typo in `sort by rsii(14)` must
    # fail here, not sort two thousand rows by None and look like a tie.
    for node in ((p.sort,) if p.sort is not None else ()) + p.show:
        validate(node)
        _require_number(node)
    return p


def _require_number(node) -> None:
    """A clause ranks or displays a NUMBER. `sort by rsi(14) > 70` sorts by a
    yes/no, which is a way of saying you meant to write a filter."""
    if isinstance(node, Str) or isinstance(node, Not) or (
            isinstance(node, Binary)
            and node.op in COMPARISONS | {"and", "or"}):
        raise QueryError(
            f"'{render(node)}' is a condition, not something to rank by — "
            f"move it before the clause", _col_of(node))


def _col_of(node) -> int | None:
    return node.col if isinstance(node, (Call, Binary)) else None


def parse(text: str):
    """Parse AND validate. A typo must fail here, not on the 40th symbol.

    Deferring field checks to evaluation would mean `rsii(14) > 70` parses
    cleanly, the live checker in the editor says it's fine, and the mistake
    only surfaces once you run it — which is exactly when it's least useful.
    """
    if not (text or "").strip():
        raise QueryError("empty query")
    node = Parser(tokenize(text)).parse()
    validate(node)
    return node


def validate(node) -> None:
    """Check every field name and argument count against the catalogue."""
    if isinstance(node, Call) and node.name in HISTORY:
        arity, takes_condition, _help = HISTORY[node.name]
        if len(node.args) != arity:
            raise QueryError(
                f"{node.name} takes {arity} argument"
                f"{'' if arity == 1 else 's'} — {HISTORY[node.name][2]}",
                node.col)
        for a in node.args:
            validate(a)
        # the window argument must be constant, or the lookback changes bar to
        # bar and nothing means anything
        if node.name in ("within",):
            _check_window(int(_const(node.args[0], node.col)), node.col)
        elif arity == 2:
            _check_window(int(_const(node.args[1], node.col)), node.col)
        return
    if isinstance(node, Call) and node.name in STATEMENT_CALLS:
        _validate_statement(node)
        return
    if isinstance(node, Call) and node.name in STATEMENT_ALIASES:
        if node.args:
            raise QueryError(
                f"{node.name} takes no arguments — for an earlier period use "
                f'fin("{STATEMENT_ALIASES[node.name][1]}", 1)', node.col)
        return
    if isinstance(node, Call) and node.name in CROSS:
        _validate_cross(node)
        return
    if isinstance(node, Call) and node.name == BENCH:
        _validate_index(node)
        return
    if isinstance(node, Call) and node.name in TIMEFRAMES:
        if len(node.args) != 1:
            raise QueryError(
                f"{node.name}() wraps exactly one expression, "
                f"e.g. {node.name}(sma(20))", node.col)
        inner = node.args[0]
        if isinstance(inner, Call) and inner.name in TIMEFRAMES:
            # weekly(monthly(x)) has no meaning — you can't resample a
            # resampling and get anything a chart would show you.
            raise QueryError("timeframes can't be nested", node.col)
        if isinstance(inner, Call) and inner.name == BENCH:
            # weekly(index(close)) would align daily index bars onto
            # week-start stamps and answer a question nobody asked. The other
            # order is well defined, so point at it.
            raise QueryError(
                f"put index() outside the timeframe: "
                f"index({node.name}(…)), not {node.name}(index(…))",
                node.col)
        validate(inner)
        return
    if isinstance(node, Call):
        spec = FIELDS.get(node.name)
        if spec is None:
            near = _suggest(node.name)
            raise QueryError(
                f"unknown field '{node.name}'"
                + (f" — did you mean {near}?" if near else ""), node.col)
        want = spec[0]
        supplied = node.args or tuple(
            Num(float(v)) for v in DEFAULT_ARGS.get(node.name, []))
        if len(supplied) != want:
            raise QueryError(
                f"{node.name} takes {want} argument"
                f"{'' if want == 1 else 's'}, got {len(node.args)}", node.col)
        # Periods and multipliers are checked HERE, not at evaluation. A
        # nonsense window that survives to evaluation produces an empty
        # result set rather than an error, and an empty result set is the one
        # answer a screen is allowed to give for a legitimate reason.
        _check_field_args(node.name, supplied, node.col)
    elif isinstance(node, Binary) and node.op in COMPARISONS:
        pair = _string_operands(node)
        if pair is not None:
            if node.op not in STRING_OPS:
                raise QueryError(
                    f"'{pair[0]}' is a name — compare it with ==, != or "
                    f"contains, not {node.op}", node.col)
            return
        for side in (node.left, node.right):
            if isinstance(side, Call) and side.name in STRING_FIELDS:
                raise QueryError(
                    f"'{side.name}' is a name — compare it with a quoted "
                    f'value, e.g. {side.name} contains "bank"', node.col)
        validate(node.left)
        validate(node.right)
    elif isinstance(node, Str):
        raise QueryError(
            f'"{node.value}" is a name, not a value — the only place for one '
            f'is the first argument to index()')
    elif isinstance(node, Not):
        validate(node.operand)
    elif isinstance(node, Binary):
        validate(node.left)
        validate(node.right)


# --------------------------------------------------------------- history
# Operators that look BACKWARDS from the bar being evaluated. All of them are
# only expressible because evaluation carries a bar index — with the last bar
# hardcoded there is no "five bars ago" to ask about.
#
# arity, whether the first argument is a CONDITION rather than a number
HISTORY = {
    "within": (2, True, "within(n, condition) — true on any of the last n bars"),
    "bars_since": (1, True, "bars_since(condition) — how long since it last held"),
    "streak": (1, True, "streak(condition) — consecutive bars it has held"),
    "ago": (2, False, "ago(expr, n) — the value n bars back"),
    "max": (2, False, "max(expr, n) — highest over the last n bars"),
    "min": (2, False, "min(expr, n) — lowest over the last n bars"),
    "avg": (2, False, "avg(expr, n) — mean over the last n bars"),
    "sum": (2, False, "sum(expr, n) — total over the last n bars"),
}

MAX_LOOKBACK = 2000        # a typo like within(999999, …) shouldn't hang


def _history(node: Call, ctx: Context, i: int):
    name = node.name

    if name == "within":
        n = int(_const(node.args[0], node.col))
        _check_window(n, node.col)
        return any(_truth(node.args[1], ctx, j)
                   for j in range(max(0, i - n + 1), i + 1))

    if name == "bars_since":
        # None when it has never been true, which every comparison reads as
        # "no" — `bars_since(x) < 5` must not match a stock where x never
        # happened at all.
        for back in range(0, min(i + 1, MAX_LOOKBACK)):
            if _truth(node.args[0], ctx, i - back):
                return back
        return None

    if name == "streak":
        n = 0
        j = i
        while j >= 0 and _truth(node.args[0], ctx, j):
            n += 1
            j -= 1
            if n >= MAX_LOOKBACK:
                break
        return n

    # --- the numeric ones
    series = _num(node.args[0], ctx)
    n = int(_const(node.args[1], node.col))
    _check_window(n, node.col)

    if name == "ago":
        return _at(series, i - n)

    window = [_at(series, j) for j in range(max(0, i - n + 1), i + 1)]
    vals = [v for v in window if v is not None]
    if not vals:
        return None
    return {"max": max, "min": min,
            "avg": lambda v: sum(v) / len(v),
            "sum": sum}[name](vals)


def _fold(node):
    """A constant expression's value, or None if it isn't constant.

    `sma(10 * 2)` is a reasonable thing to type, and refusing it because the
    argument isn't a bare literal is the kind of rule that makes a language
    feel hostile. Anything referring to a series folds to None, which is what
    makes it a *series* rather than a period.
    """
    if isinstance(node, Num):
        return node.value
    if isinstance(node, Binary) and node.op in ("+", "-", "*", "/"):
        a, b = _fold(node.left), _fold(node.right)
        if a is None or b is None:
            return None
        if node.op == "/":
            return a / b if b else None
        return {"+": a + b, "-": a - b, "*": a * b}[node.op]
    return None


def _const(node, col: int) -> float:
    v = _fold(node)
    if v is None:
        raise QueryError("this argument has to be a plain number", col)
    return v


def _check_window(n: float, col: int, what: str = "a window") -> None:
    if n != int(n):
        raise QueryError(f"{what} is a whole number of bars, not {n}", col)
    if n < 1:
        raise QueryError(f"{what} has to be at least 1 bar", col)
    if n > MAX_LOOKBACK:
        raise QueryError(
            f"{what} is capped at {MAX_LOOKBACK} bars", col)


# Which argument means what, so the checks below can say something useful.
# Position 0 is always a lookback in bars; position 1, where it exists, is a
# band or channel multiplier.
ARG_KINDS: dict[str, tuple[str, ...]] = {
    "sma": ("window",), "ema": ("window",), "rsi": ("window",),
    "adx": ("window",), "atr": ("window",), "atr_pct": ("window",),
    "highest": ("window",), "lowest": ("window",), "avg_volume": ("window",),
    "change_pct": ("window",),
    "stoch": ("window",), "stoch_d": ("window",), "willr": ("window",),
    "cci": ("window",), "mfi": ("window",), "cmf": ("window",),
    "stddev": ("window",), "vwap": ("window",),
    "dist_from_high": ("window",), "dist_from_low": ("window",),
    "keltner_upper": ("window", "mult"), "keltner_lower": ("window", "mult"),
    "bb_upper": ("window", "mult"), "bb_lower": ("window", "mult"),
    "bb_width": ("window", "mult"),
    "supertrend": ("window", "mult"), "supertrend_dir": ("window", "mult"),
}

MAX_MULT = 100.0


def _check_field_args(name: str, args: tuple, col: int) -> list[float]:
    """Fold and sanity-check a field's arguments, at PARSE time.

    `sma(-5)` used to produce a series of None, so every comparison against it
    was false and the screen returned nothing — for ever, silently, and
    indistinguishably from a screen that genuinely had no matches. That is the
    worst failure mode available to a research tool, and it is the same one
    the `highest()` comment further down already warns about; this closes the
    remaining half of it.
    """
    kinds = ARG_KINDS.get(name, ())
    vals: list[float] = []
    for i, a in enumerate(args):
        v = _fold(a)
        if v is None:
            # Explicit isinstance rather than getattr: a module that parses
            # untrusted input has no business reaching attributes by name,
            # and its own safety test asserts as much.
            at = a.col if isinstance(a, (Call, Binary)) else col
            raise QueryError(
                f"{name}() takes a number of bars, not an expression — "
                f"write something like {name}(20)", at)
        kind = kinds[i] if i < len(kinds) else None
        if kind == "window":
            _check_window(v, col, f"{name}()'s period")
        elif kind == "mult":
            if not 0 < v <= MAX_MULT:
                raise QueryError(
                    f"{name}()'s multiplier has to be above 0 and at most "
                    f"{MAX_MULT:g}, not {v:g}", col)
        vals.append(v)
    return vals


# ------------------------------------------------------------- timeframes
# Wrappers, not fields: `weekly(rsi(14))` switches the CONTEXT its argument is
# evaluated in, then hands back a weekly-length series. Comparisons then read
# the last element of each side, so `weekly(close) > sma(200)` compares the
# latest weekly close against the latest daily 200-DMA — each at its own most
# recent bar, which is what you'd read off two charts side by side.
TIMEFRAMES = {"weekly": "week", "monthly": "month"}


def _period_key(ts: int, period: str) -> int:
    """Start-of-period stamp. Mirrors weekKey/monthKey in the chart.

    utcfromtimestamp on purpose: candles.get already shifted these into IST,
    so reading them back as UTC reconstructs the IST wall-clock date. Using
    localtime here would shift them a second time.
    """
    from datetime import datetime, timedelta, timezone

    d = datetime.fromtimestamp(ts, tz=timezone.utc)
    if period == "month":
        start = datetime(d.year, d.month, 1)
    else:
        start = datetime(d.year, d.month, d.day) - timedelta(days=d.weekday())
    return int(start.replace(tzinfo=timezone.utc).timestamp())


def resample_map(candles: list[dict], period: str) -> tuple[list[dict], list[int]]:
    """Resampled bars, plus for each daily bar the index of the last COMPLETED
    period bar as of that day.

    This mapping is what makes higher timeframes usable at an arbitrary bar,
    and getting it wrong is the single easiest way to build a backtest that
    can see the future.

    The temptation is to map each day to the period bar it belongs to. That
    bar's high, low and close are computed from the whole week — including the
    days AFTER the one being evaluated. On Tuesday you would be reading
    Friday's close. Every weekly strategy would look extraordinary and none of
    it would be real.

    So a day maps to the last period that had already finished. Mid-week,
    `weekly(close)` is last Friday's close. That's the same choice TradingView
    makes when lookahead is off, and it costs at most a few days of staleness
    on a series that only moves once a week anyway.
    """
    bars = resample(candles, period)
    if not bars:
        return [], [-1] * len(candles)

    ends: list[int] = []          # last daily index of each period bar
    cur_key = None
    gi = -1
    group_of: list[int] = []
    for i, b in enumerate(candles):
        k = _period_key(b["time"], period)
        if k != cur_key:
            cur_key = k
            gi += 1
            ends.append(i)
        else:
            ends[gi] = i
        group_of.append(gi)

    # A period counts as finished only once a LATER one has started. Without
    # that clause the final, still-forming period looks complete simply
    # because it is the last thing in the data — which puts the lookahead back
    # at the most important bar of all, the one you are about to trade.
    last_group = gi
    completed = [
        group_of[i] if (i == ends[group_of[i]] and group_of[i] < last_group)
        else group_of[i] - 1
        for i in range(len(candles))
    ]
    return bars, completed


def resample(candles: list[dict], period: str) -> list[dict]:
    """Daily bars into weekly or monthly ones.

    First open, highest high, lowest low, LAST close, summed volume. The last
    close is the one that matters — a weekly bar closes where the week ended,
    not where any day inside it did.

    The final group may be a partial period, exactly as the weekly chart shows
    a week in progress. That means `weekly(sma(20))` moves during the week,
    which is how every charting package behaves and what you'd see if you
    looked.
    """
    if candles and "time" not in candles[0]:
        raise QueryError(
            "these candles carry no timestamp, so they can't be grouped into "
            "weeks or months")
    out: list[dict] = []
    cur = None
    for b in candles:
        k = _period_key(b["time"], period)
        if cur is None or cur["time"] != k:
            if cur:
                out.append(cur)
            cur = {"time": k, "open": b["open"], "high": b["high"],
                   "low": b["low"], "close": b["close"],
                   "volume": b.get("volume") or 0}
        else:
            cur["high"] = max(cur["high"], b["high"])
            cur["low"] = min(cur["low"], b["low"])
            cur["close"] = b["close"]
            cur["volume"] += b.get("volume") or 0
    if cur:
        out.append(cur)
    return out


# ------------------------------------------------------------- benchmarks
# `index(expr)` evaluates expr on the benchmark's OWN bars and projects the
# result back onto this symbol's dates.
#
# It's a wrapper rather than a field for the same reason weekly() is: it
# switches the context its argument runs in. `index(sma(200))` is the index's
# 200-day average, computed from index bars — not the stock's average of
# something.
BENCH = "index"

# The one rs.py already ranks against. Two ideas of "the index" in one app is a
# bug waiting to be argued about in a support thread with yourself; a test
# asserts these stay equal.
DEFAULT_BENCHMARK = "NIFTY 50"


def _index_args(node: Call) -> tuple[str, object]:
    """(benchmark name, inner expression) for an index() call."""
    if len(node.args) == 1:
        return DEFAULT_BENCHMARK, node.args[0]
    if len(node.args) == 2 and isinstance(node.args[0], Str):
        return node.args[0].value, node.args[1]
    raise QueryError(
        'index() takes an expression, optionally after a benchmark name — '
        'index(close) or index("NIFTY BANK", close)', node.col)


def _validate_index(node: Call) -> None:
    name, inner = _index_args(node)
    if not name.strip():
        raise QueryError("that benchmark has no name", node.col)

    def check(n):
        if isinstance(n, Call):
            if n.name == BENCH:
                # index(index(x)) has no meaning: the inner one would be
                # asking what the index's index was doing.
                raise QueryError("index() can't be nested", n.col)
            if n.name in SCALAR_FIELDS:
                # Scalars are per-COMPANY and ride along on the context. Left
                # alone, index(roe) would silently hand back the stock's ROE
                # under a name that says otherwise.
                raise QueryError(
                    f"an index has no {n.name} — fundamentals are per "
                    f"company", n.col)
            for a in n.args:
                check(a)
        elif isinstance(n, Not):
            check(n.operand)
        elif isinstance(n, Binary):
            check(n.left)
            check(n.right)

    check(inner)
    validate(inner)


def bench_map(stock: list[dict], index: list[dict]) -> list[int]:
    """For each stock bar, the index bar in force on that date.

    The last index bar at or before the stock bar's timestamp — never a later
    one. This is the same discipline as resample_map's "last completed
    period", and it exists for the same reason: reading the index from a day
    the stock hasn't reached yet makes every index-filtered backtest look
    better than it was, and nothing in the output would say so.

    Aligning by POSITION instead is the tempting shortcut and the real trap.
    It produces a plausible number for every bar of every symbol, and it is
    wrong the moment either series is missing a day — a suspension, a late
    listing, a holiday one of them observed. Which is to say: often, silently,
    and in the flattering direction.

    -1 where the stock predates the benchmark's first bar. Back-filling the
    earliest value there would invent a market that wasn't quoted yet.
    """
    out: list[int] = []
    j, n = -1, len(index)
    for b in stock:
        t = b["time"]
        while j + 1 < n and index[j + 1]["time"] <= t:
            j += 1
        out.append(j)
    return out


def _benchmarks_used(node) -> set:
    """Which benchmarks a query mentions — so a scan loads each one once, and
    a query mentioning none loads nothing."""
    found: set = set()

    def walk(n):
        if isinstance(n, Call):
            if n.name == BENCH:
                try:
                    found.add(_index_args(n)[0])
                except QueryError:
                    pass
            for a in n.args:
                walk(a)
        elif isinstance(n, Not):
            walk(n.operand)
        elif isinstance(n, Binary):
            walk(n.left)
            walk(n.right)

    walk(node)
    return found


def _load_benchmarks(names, days: int) -> dict:
    """Stored candles for each benchmark. Absent stays absent — the caller
    turns that into an error naming the backfill, not into silence."""
    from . import candles, instruments

    out: dict = {}
    for name in names:
        inst = instruments.lookup(name)
        if not inst:
            continue
        try:
            bars = candles.get(inst["instrument_token"], "day", days)
        except Exception:                            # noqa: BLE001
            continue
        if bars:
            out[name] = bars
    return out


# --------------------------------------------------------- cross-sectional
# Operators whose answer depends on the OTHER symbols. Every other operator
# reads one company's bars; these can't be answered until the whole field has
# been measured, which is why run() makes a separate pass for them and why
# they're refused anywhere there is no field.
CROSS = {
    "percentile": "percentile(expr) — 0–100 against everything scanned",
    "rank": "rank(expr) — 1 is the largest",
}


def _validate_cross(node: Call) -> None:
    if len(node.args) != 1:
        raise QueryError(
            f"{node.name}() ranks exactly one expression, "
            f"e.g. {node.name}(change_pct(63))", node.col)
    inner = node.args[0]

    def check(n):
        if isinstance(n, Call):
            if n.name in CROSS:
                # percentile(rank(x)) ranks a ranking: the inner one has
                # already flattened the field to 1..N, so the outer one just
                # reproduces it with different numbers on the axis.
                raise QueryError(
                    f"{node.name}() can't rank another ranking", n.col)
            if n.name in SCALAR_FIELDS:
                # This is a cost refusal, not a semantic one. Ranking on a
                # fundamental means statements for the WHOLE universe, since
                # the population is everything scanned — 2,000 requests to
                # somebody else's website, which is the exact bill the
                # two-stage design exists to avoid.
                raise QueryError(
                    f"{node.name}({n.name}) would need a statements fetch for "
                    f"every company in the universe, not just the matches — "
                    f"filter on {n.name} instead of ranking by it", n.col)
            for a in n.args:
                check(a)
        elif isinstance(n, Not):
            check(n.operand)
        elif isinstance(n, Binary):
            check(n.left)
            check(n.right)

    check(inner)
    validate(inner)
    _require_number(inner)


def _cross_nodes(node) -> list:
    """Every cross-sectional call in an expression, de-duplicated by text so
    a query naming the same one twice measures the field once."""
    found: dict[str, object] = {}

    def walk(n):
        if isinstance(n, Call):
            if n.name in CROSS:
                found.setdefault(render(n), n)
            for a in n.args:
                walk(a)
        elif isinstance(n, Not):
            walk(n.operand)
        elif isinstance(n, Binary):
            walk(n.left)
            walk(n.right)

    walk(node)
    return list(found.values())


def _needs_cross(node) -> bool:
    return bool(_cross_nodes(node))


def _percentiles(scores: dict) -> dict:
    """0–100, ties at the midpoint. One implementation of the convention,
    shared with `rs`, so RS 85 on the ranking page and `percentile(...) > 85`
    in a query mean the same thing."""
    from . import rs as rs_mod
    return rs_mod.percentile_ranks(scores)


def _ranks(scores: dict) -> dict:
    """1 for the largest. Ties share the better position and consume the
    places behind them — 1, 1, 3 — which is how ranks have always worked and
    what stops two identical stocks being told one of them is second.

    Unrankable stays None rather than last: no history is not the same fact as
    worst in the field.
    """
    import bisect

    # Negated once, not once per symbol. Rebuilding it inside the loop is O(n)
    # per lookup and turns a 2,000-name universe into four million operations
    # — the same quadratic shape the evaluator was already fixed for, and just
    # as invisible until the universe grows.
    descending = sorted(-v for v in scores.values() if v is not None)
    out: dict = {}
    for symbol, value in scores.items():
        if value is None:
            out[symbol] = None
            continue
        # How many are strictly greater, +1.
        out[symbol] = bisect.bisect_left(descending, -value) + 1
    return out


# ------------------------------------------------------------- statements
# The financial statements as fields. 57 row labels across five tables were
# already stored per company and reachable only through a dozen hand-mapped
# ratios; these are the substrate underneath those.
#
# `fin` and friends take a row LABEL as a string rather than an identifier
# because the labels are screener's, they drift, and a row that appears next
# quarter should be queryable without a code change here. The named aliases
# further down cover the ones worth typing daily.
STATEMENT_CALLS = {
    "fin": (1, 2, "fin(\"row\") / fin(\"row\", n) — annual, 0 is the last "
                  "completed year, TTM excluded"),
    "ttm": (1, 1, "ttm(\"row\") — the trailing-twelve-month column"),
    "q": (1, 2, "q(\"row\") / q(\"row\", n) — quarterly, 0 is the latest"),
    "growth": (2, 2, "growth(\"row\", years) — annual CAGR %"),
    "yoy": (1, 1, "yoy(\"row\") — latest quarter against four back, %"),
}

# Screener labels the same line differently by sector: a manufacturer reports
# Sales, a bank reports Revenue. Without this a query about sales would
# quietly exclude every financial in the universe.
_ROW_SYNONYMS = {
    "sales": ("sales", "revenue"),
    "revenue": ("revenue", "sales"),
}

# Named shorthands, defined AS accessor lookups rather than beside them —
# one path to get wrong, one set of tests. Names that already exist as
# derived scalars (roe, roce, opm) are deliberately not redefined here.
STATEMENT_ALIASES: dict[str, tuple[str, str]] = {
    # profit & loss
    "sales": ("fin", "Sales"),
    "expenses": ("fin", "Expenses"),
    "operating_profit": ("fin", "Operating Profit"),
    "other_income": ("fin", "Other Income"),
    "interest_expense": ("fin", "Interest"),
    "depreciation": ("fin", "Depreciation"),
    "pbt": ("fin", "Profit before tax"),
    "tax_pct": ("fin", "Tax %"),
    "net_profit": ("fin", "Net Profit"),
    "eps": ("fin", "EPS in Rs"),
    "dividend_payout": ("fin", "Dividend Payout %"),
    # balance sheet
    "equity_capital": ("fin", "Equity Capital"),
    "reserves": ("fin", "Reserves"),
    "borrowings": ("fin", "Borrowings"),
    "fixed_assets": ("fin", "Fixed Assets"),
    "cwip": ("fin", "CWIP"),
    "investments": ("fin", "Investments"),
    "total_assets": ("fin", "Total Assets"),
    # working capital
    "debtor_days": ("fin", "Debtor Days"),
    "inventory_days": ("fin", "Inventory Days"),
    "days_payable": ("fin", "Days Payable"),
    "cash_conversion_cycle": ("fin", "Cash Conversion Cycle"),
    "working_capital_days": ("fin", "Working Capital Days"),
    # cash flow
    "cfo": ("fin", "Cash from Operating Activity"),
    "cfi": ("fin", "Cash from Investing Activity"),
    "cff": ("fin", "Cash from Financing Activity"),
    "net_cash_flow": ("fin", "Net Cash Flow"),
    "fcf": ("fin", "Free Cash Flow"),
    # latest quarter
    "q_sales": ("q", "Sales"),
    "q_net_profit": ("q", "Net Profit"),
    # shareholding — reported quarterly, in its own screener section
    "promoter_holding": ("q", "Promoters"),
    "fii_holding": ("q", "FIIs"),
    "dii_holding": ("q", "DIIs"),
    "public_holding": ("q", "Public"),
    "govt_holding": ("q", "Government"),
    "shareholders": ("q", "No. of Shareholders"),
}


def _statement_label(node: Call) -> tuple[str, str, int]:
    """(kind, label, period) for a statement call or a named alias."""
    if node.name in STATEMENT_ALIASES:
        kind, label = STATEMENT_ALIASES[node.name]
        return kind, label, 0
    label_node = node.args[0]
    if not isinstance(label_node, Str):
        raise QueryError(
            f'{node.name}() takes a row label in quotes, e.g. '
            f'{node.name}("free cash flow")', node.col)
    period = 0
    if len(node.args) > 1:
        period = int(_const(node.args[1], node.col))
    return node.name, label_node.value, period


def _validate_statement(node: Call) -> None:
    lo, hi, _help = STATEMENT_CALLS[node.name]
    if not lo <= len(node.args) <= hi:
        raise QueryError(
            f"{node.name}() takes {lo}"
            f"{'' if lo == hi else f' or {hi}'} argument"
            f"{'' if hi == 1 else 's'}, got {len(node.args)}", node.col)
    if not isinstance(node.args[0], Str):
        raise QueryError(
            f'{node.name}() takes a row label in quotes, e.g. '
            f'{node.name}("free cash flow")', node.col)
    if not node.args[0].value.strip():
        raise QueryError(f"{node.name}() needs a row name", node.col)
    if len(node.args) > 1:
        n = _const(node.args[1], node.col)
        if n != int(n):
            raise QueryError(
                f"{node.name}() counts whole periods, not {n:g}", node.col)
        if node.name == "growth":
            if n < 1:
                raise QueryError(
                    "growth() spans at least one year", node.col)
        elif n < 0:
            # Negative would be the future. Storage ascends and the language
            # descends; letting a sign slip through would read forward.
            raise QueryError(
                f"{node.name}() counts backwards from the latest, so the "
                f"period can't be negative", node.col)


def _statement_calls(node) -> list:
    """Every statement accessor and named alias in an expression."""
    found: dict[str, object] = {}

    def walk(n):
        if isinstance(n, Call):
            if n.name in STATEMENT_CALLS or n.name in STATEMENT_ALIASES:
                found.setdefault(render(n), n)
            for a in n.args:
                walk(a)
        elif isinstance(n, Not):
            walk(n.operand)
        elif isinstance(n, Binary):
            walk(n.left)
            walk(n.right)

    walk(node)
    return list(found.values())


def _resolve_statement_rows(*nodes) -> list:
    """Which (statement, row) pairs a query needs, resolved once.

    Resolved here rather than per symbol so that a label matching nothing
    fails before the scan starts. Per symbol it would be indistinguishable
    from a company with no statements filed — which is to say, from an empty
    screen with no explanation.
    """
    from . import fundamentals

    calls: list = []
    for node in nodes:
        if node is None:
            continue
        calls += [c for c in _statement_calls(node)
                  if render(c) not in {render(x) for x in calls}]
    if not calls:
        return []

    catalogue = fundamentals.row_catalogue()
    pairs: list = []
    for c in calls:
        kind, label, _period = _statement_label(c)
        stmts = (fundamentals.QUARTERLY if kind in ("q", "yoy")
                 else fundamentals.ANNUAL)
        hits: list = []
        for synonym in _ROW_SYNONYMS.get(label.strip().lower(), (label,)):
            hits += fundamentals.resolve_row(synonym, stmts, catalogue)
        if not hits:
            known = sorted({r for s in stmts for r in catalogue.get(s, [])})
            close = _suggest_rows(label, known)
            raise QueryError(
                f'no statement row matching "{label}"'
                + (f" — did you mean {close}?" if close else
                   " (nothing stored yet — run `fundamentals fetch` first)"))
        pairs += [h for h in hits if h not in pairs]
    return pairs


def _suggest_rows(label: str, known: list[str]) -> str:
    want = set(label.lower().split())
    scored = [(len(want & set(r.lower().split())), r) for r in known]
    best = [r for n, r in sorted(scored, reverse=True) if n][:3]
    return ", ".join(f'"{r}"' for r in best)


def _statement_value(node: Call, store: dict):
    """One number from this symbol's statements, or None.

    Absent stays absent all the way through: a company that hasn't filed is
    not a company with zero sales, and `fin("free cash flow") > 0` must not
    match one.
    """
    from . import fundamentals

    kind, label, period = _statement_label(node)
    stmts = (fundamentals.QUARTERLY if kind in ("q", "yoy")
             else fundamentals.ANNUAL)

    found = None
    for synonym in _ROW_SYNONYMS.get(label.strip().lower(), (label,)):
        want = synonym.strip().lower()
        for (statement, row), payload in store.items():
            if statement in stmts and (row.lower() == want
                                       or row.lower().startswith(want)
                                       or want in row.lower()):
                found = payload
                break
        if found:
            break
    if not found:
        return None

    periods, values = found
    if kind in ("fin", "growth") or (kind == "ttm"):
        # The annual table carries a trailing TTM column. Counted as a year it
        # would make fin("sales", 1) a TTM-versus-fiscal-year comparison and
        # make three-year growth span something other than three years.
        annual = [v for p, v in zip(periods, values)
                  if str(p).strip().upper() != fundamentals.TTM_PERIOD]
        if kind == "ttm":
            for p, v in zip(periods, values):
                if str(p).strip().upper() == fundamentals.TTM_PERIOD:
                    return v
            return None
        if kind == "growth":
            years = int(_const(node.args[1], node.col))
            return fundamentals._cagr(annual, years)
        return _period_back(annual, period)

    if kind == "yoy":
        latest, back = _period_back(values, 0), _period_back(values, 4)
        if latest is None or back is None or back <= 0:
            # A growth rate out of a loss is arithmetic, not information.
            return None
        return round((latest / back - 1) * 100, 1)

    return _period_back(values, period)


def _period_back(values: list, n: int):
    """n periods back from the LATEST. Storage ascends; the language does not,
    because an unindexed query reading the oldest column would return a
    perfectly plausible screen built on eleven-year-old figures."""
    if n < 0 or n >= len(values):
        return None
    return values[len(values) - 1 - n]


# ------------------------------------------------------------------- fields
# name -> (arity, help). Arity None means "any number of args".
FIELDS: dict[str, tuple[int, str]] = {
    # --- raw price
    "close": (0, "closing price"),
    "open": (0, "opening price"),
    "high": (0, "bar high"),
    "low": (0, "bar low"),
    "volume": (0, "bar volume"),
    "price": (0, "alias for close"),
    # --- moving averages
    "sma": (1, "sma(period)"),
    "ema": (1, "ema(period)"),
    # --- oscillators
    "rsi": (1, "rsi(period), default 14"),
    "adx": (1, "adx(period) — trend STRENGTH, never direction"),
    "atr": (1, "atr(period) — the volatility unit"),
    "atr_pct": (1, "atr as a % of price, so it compares across stocks"),
    "macd": (0, "macd line (12/26)"),
    "macd_signal": (0, "macd signal line"),
    "macd_hist": (0, "macd histogram"),
    # --- bands and channels
    "bb_upper": (2, "bb_upper(period, mult)"),
    "bb_lower": (2, "bb_lower(period, mult)"),
    "bb_width": (2, "band width as a % of the middle band"),
    "highest": (1, "highest high over N bars"),
    "lowest": (1, "lowest low over N bars"),
    # --- volume
    "avg_volume": (1, "average volume over N bars"),
    # --- derived
    "change_pct": (1, "% change over N bars"),

    "industry_pe": (0, "median P/E of this company's industry"),
    "sector_pe": (0, "median P/E of this company's sector"),
    "sector": (0, "sector name — compare with == != or contains"),
    "industry": (0, "industry name — compare with == != or contains"),

    # oscillators and volume — all inclusive of the current bar, unlike
    # highest()/lowest(), which exclude it so a breakout test isn't trivially
    # true. See _hh_ll in indicators.py.
    "stoch": (1, "stochastic %K, 0-100"),
    "stoch_d": (1, "stochastic %D — the smoothed %K"),
    "willr": (1, "Williams %R, -100 to 0"),
    "cci": (1, "Commodity Channel Index"),
    "mfi": (1, "Money Flow Index, 0-100 — RSI weighted by volume"),
    "cmf": (1, "Chaikin Money Flow, -1 to +1"),
    "obv": (0, "on-balance volume, cumulative"),
    "stddev": (1, "standard deviation of closes (population, as bollinger)"),
    "vwap": (1, "volume-weighted average price over N bars"),
    "keltner_upper": (2, "EMA + mult x ATR"),
    "keltner_lower": (2, "EMA - mult x ATR"),
    "prev_close": (0, "yesterday's close"),
    "gap_pct": (0, "% between today's open and yesterday's close"),
    "dist_from_high": (1, "% below the N-bar high, 0 at a new high"),
    "dist_from_low": (1, "% above the N-bar low, 0 at a new low"),
    "supertrend": (2, "supertrend(period, mult) line"),
    "supertrend_dir": (2, "+1 uptrend, -1 downtrend"),
    # --- per-symbol scalars, injected rather than computed from candles.
    # One value per company, broadcast so they compare like any other field.
    "rs": (0, "relative strength percentile vs Nifty"),
    "mos_pct": (0, "margin of safety % against your own valuation"),
    "fair_value": (0, "your composite fair value"),
    "n_models": (0, "how many valuation models produced a number"),
    # profitability
    "roe": (0, "return on equity % (ours, from the statements)"),
    "roce": (0, "return on capital employed % (ours: EBIT / equity+debt)"),
    "roce_screener": (0, "screener's own ROCE, for comparison"),
    "opm": (0, "operating margin %, latest full year"),
    # balance sheet
    "debt_equity": (0, "debt / equity — above ~1 needs a reason"),
    "interest_cover": (0, "EBIT / interest — under 3 is fragile"),
    # growth
    "sales_growth3": (0, "sales CAGR over 3 years %"),
    "sales_growth5": (0, "sales CAGR over 5 years %"),
    "profit_growth3": (0, "net profit CAGR over 3 years %"),
    "profit_growth5": (0, "net profit CAGR over 5 years %"),
    # valuation multiples
    "pe": (0, "price / earnings"),
    "pb": (0, "price / book"),
    "book_value": (0, "book value per share"),
    "div_yield": (0, "dividend yield %"),
    "earnings_yield": (0, "inverse of P/E, as a % — comparable to a bond"),
    "market_cap": (0, "market cap in ₹ crore"),
    # delivery — last completed session, from NSE's bhavcopy
    "deliv_pct": (0, "% of the last session's volume taken into delivery"),
    "avg_deliv_pct": (0, "average delivery % over the prior 20 sessions"),
    "deliv_ratio": (0, "last session's delivery qty ÷ its 20-session average"),
}

# Everything the candles can't tell you. Loaded in one pass per run, and only
# when a query actually mentions one of them.
SCALAR_FIELDS = {
    "rs", "mos_pct", "fair_value", "n_models",
    "roe", "roce", "roce_screener", "opm",
    "debt_equity", "interest_cover",
    "sales_growth3", "sales_growth5", "profit_growth3", "profit_growth5",
    "pe", "pb", "book_value", "div_yield", "earnings_yield", "market_cap",
    "industry_pe", "sector_pe",
}
DELIVERY_FIELDS = {"deliv_pct", "avg_deliv_pct", "deliv_ratio"}
SCALAR_FIELDS |= DELIVERY_FIELDS
SCALAR_FIELDS |= STRING_FIELDS

# The statement shorthands are fundamentals too. Membership here is what makes
# `index(fcf)` and `percentile(fcf)` refuse for the same reasons `index(roe)`
# does — an index has no cash flow statement, and ranking the universe on one
# is a fetch per company. `_num` intercepts these names before the scalar
# lookup, so they read from the statements rather than from `scalars`.
SCALAR_FIELDS |= set(STATEMENT_ALIASES)

DEFAULT_ARGS = {"rsi": [14], "adx": [14], "atr": [14], "atr_pct": [14],
                "sma": [20], "ema": [20], "bb_upper": [20, 2],
                "bb_lower": [20, 2], "bb_width": [20, 2],
                "highest": [252], "lowest": [252], "avg_volume": [20],
                "change_pct": [1], "supertrend": [10, 3],
                "stoch": [14], "stoch_d": [14], "willr": [14],
                "cci": [20], "mfi": [14], "cmf": [20],
                "stddev": [20], "vwap": [20],
                "dist_from_high": [252], "dist_from_low": [252],
                "keltner_upper": [20, 2], "keltner_lower": [20, 2],
                "supertrend_dir": [10, 3]}


class Context:
    """Series for one symbol, computed on demand and cached.

    Lazy because a query naming three fields shouldn't pay for twenty, and
    cached because `ema(20) > ema(50) and ema(20) > close` should compute
    ema(20) once.
    """

    def __init__(self, candles: list[dict], scalars: dict | None = None,
                 bench: dict | None = None, cross: dict | None = None,
                 statements: dict | None = None):
        self.c = candles
        # This symbol's statement rows: {(statement, row): (periods, values)}
        self.statements = statements or {}
        self.scalars = scalars or {}
        self.bench = bench or {}
        # Already-computed cross-sectional values for THIS symbol, keyed by
        # the rendered expression. Absent (rather than None) means there was
        # no universe to measure against — a different fact from "no value".
        self.cross = cross if cross is not None else None
        self._cache: dict = {}
        self._tf: dict = {}
        self._bench: dict = {}
        # Keyed by id(node) and holding (node, value): the id is O(1) where
        # hashing a deep AST is O(size), and keeping the node itself in the
        # value pins a reference, so the id can't be recycled onto a
        # different node while this context is alive.
        self._nodes: dict[int, tuple] = {}

    def benchmark(self, name: str) -> tuple["Context", list[int]]:
        """A sibling context over the benchmark's bars, plus the date map.

        No scalars passed down, deliberately: an index has no balance sheet,
        and letting the company's numbers ride along would make `index(roe)`
        answer with the stock's ROE. validate() refuses that outright; this
        makes it impossible rather than merely forbidden.
        """
        if name not in self._bench:
            bars = self.bench.get(name)
            if not bars:
                raise QueryDataError(
                    f"no stored candles for {name} — backfill it first")
            self._bench[name] = (Context(bars), bench_map(self.c, bars))
        return self._bench[name]

    def align_bench(self, name: str, series: list) -> list:
        """Project a benchmark series onto this symbol's bars, by date."""
        _, mapping = self.benchmark(name)
        return [None if j < 0 or j >= len(series) else series[j]
                for j in mapping]

    def timeframe(self, period: str) -> tuple["Context", list[int]]:
        """A sibling context over resampled bars, plus the day→period map.

        Scalars carry across unchanged: `weekly(roe > 15)` is a strange thing
        to write, but ROE doesn't become a different number on a weekly chart,
        and silently dropping it would be worse than answering.
        """
        if period not in self._tf:
            bars, mapping = resample_map(self.c, period)
            self._tf[period] = (Context(bars, self.scalars), mapping)
        return self._tf[period]

    def align(self, period: str, series: list) -> list:
        """Project a higher-timeframe series back onto daily bars.

        One index space for everything, so an expression mixing daily and
        weekly can be evaluated at a single bar number.
        """
        _, mapping = self.timeframe(period)
        return [None if j < 0 or j >= len(series) else series[j]
                for j in mapping]

    # -- helpers
    def _closes(self):
        return self.column("close")

    def column(self, name: str) -> list:
        """One OHLCV column, tolerating gaps but not absence.

        The distinction matters. A single bar with a null volume is a feed
        hiccup and the surrounding bars are still worth reading, so it becomes
        None and the window functions skip it. A column that is missing from
        every bar means these candles aren't what the query thinks they are,
        and answering `False` would be a wrong answer dressed as a real one —
        so it's an error naming the symbol instead.
        """
        col = [b.get(name) for b in self.c]
        if self.c and all(v is None for v in col):
            raise QueryDataError(
                f"these candles carry no '{name}' — re-run the backfill")
        return col

    def series(self, name: str, args: list[float]) -> list:
        key = (name, tuple(args))
        if key in self._cache:
            return self._cache[key]
        try:
            out = self._compute(name, args)
        except QueryError:
            raise
        except Exception as e:                       # noqa: BLE001
            # Indicators index candle fields directly, so malformed stored
            # data surfaces here as a TypeError or a KeyError. Uncaught, that
            # aborts a 2,000-symbol scan and returns a 500 from /api/query;
            # as a QueryDataError it costs exactly the one symbol.
            raise QueryDataError(
                f"{name} could not be computed from these candles "
                f"({type(e).__name__})") from None
        self._cache[key] = out
        return out

    def _compute(self, name: str, args: list[float]) -> list:
        c, n = self.c, len(self.c)
        closes = self._closes()

        if name in ("close", "price"):
            return closes
        if name in ("open", "high", "low", "volume"):
            return self.column(name)

        if name == "sma":
            return ind.sma(closes, int(args[0]))
        if name == "ema":
            return ind.ema(closes, int(args[0]))
        if name == "rsi":
            return ind.rsi(closes, int(args[0]))
        if name == "atr":
            return ind.atr(c, int(args[0]))
        if name == "atr_pct":
            a = ind.atr(c, int(args[0]))
            return [None if (a[i] is None or not closes[i]) else
                    a[i] / closes[i] * 100 for i in range(n)]
        if name == "adx":
            return ind.adx(c, int(args[0]))
        if name in ("macd", "macd_signal", "macd_hist"):
            # (line, signal, histogram)
            line, signal, hist = ind.macd(closes)
            return {"macd": line, "macd_signal": signal,
                    "macd_hist": hist}[name]
        if name in ("bb_upper", "bb_lower", "bb_width"):
            # (upper, middle, lower)
            up, mid, lo = ind.bollinger(closes, int(args[0]), float(args[1]))
            if name == "bb_upper":
                return up
            if name == "bb_lower":
                return lo
            return [None if (up[i] is None or not mid[i]) else
                    (up[i] - lo[i]) / mid[i] * 100 for i in range(n)]
        if name in ("supertrend", "supertrend_dir"):
            # a list of {"value", "dir"} per bar
            st = ind.supertrend(c, int(args[0]), float(args[1]))
            key = "dir" if name == "supertrend_dir" else "value"
            return [b[key] for b in st]

        # highest/lowest EXCLUDE the current bar, matching the Donchian
        # convention in the backtester. Including it makes
        # `close > highest(252)` unsatisfiable — today's close can never
        # exceed a window it is itself part of — and the query would silently
        # return nothing for ever rather than erroring.
        # A gap inside the window is skipped rather than fatal — see
        # Context.column. An entirely empty window stays None, which every
        # comparison reads as "no".
        if name in ("highest", "lowest"):
            w = int(args[0])
            src = self.column("high" if name == "highest" else "low")
            pick = max if name == "highest" else min
            return [None if i < w else _reduce(pick, src[i - w:i])
                    for i in range(n)]
        if name == "avg_volume":
            w = int(args[0])
            src = self.column("volume")
            out = []
            for i in range(n):
                if i + 1 < w:
                    out.append(None)
                    continue
                vals = [v for v in src[i + 1 - w:i + 1] if v is not None]
                out.append(sum(vals) / len(vals) if vals else None)
            return out
        if name == "change_pct":
            w = int(args[0])
            return [None if i < w or not closes[i - w] else
                    (closes[i] / closes[i - w] - 1) * 100 for i in range(n)]

        if name in ("stoch", "stoch_d", "willr", "cci", "mfi", "cmf",
                    "vwap"):
            # These read candles directly rather than through column(), so
            # ask for the columns first: a high/low missing from every bar is
            # a data error, not a field that quietly reads None for ever.
            self.column("high")
            self.column("low")
            fn = {"stoch": ind.stoch, "stoch_d": ind.stoch_d,
                  "willr": ind.williams_r, "cci": ind.cci, "mfi": ind.mfi,
                  "cmf": ind.cmf, "vwap": ind.rolling_vwap}[name]
            return fn(c, int(args[0]))
        if name == "obv":
            return ind.obv(c)
        if name == "stddev":
            return ind.stddev(closes, int(args[0]))
        if name in ("keltner_upper", "keltner_lower"):
            up, _mid, lo = ind.keltner(c, int(args[0]), float(args[1]))
            return up if name == "keltner_upper" else lo
        if name == "prev_close":
            return [None] + closes[:-1]
        if name == "gap_pct":
            opens = self.column("open")
            return [None if i == 0 or not closes[i - 1] or opens[i] is None
                    else (opens[i] / closes[i - 1] - 1) * 100
                    for i in range(n)]
        if name in ("dist_from_high", "dist_from_low"):
            # Inclusive of today, unlike highest()/lowest(): a stock making a
            # new high is 0% below its high, and reporting yesterday's high
            # instead would say otherwise on exactly the day you care.
            w = int(args[0])
            src = self.column("high" if name == "dist_from_high" else "low")
            pick = max if name == "dist_from_high" else min
            out = []
            for i in range(n):
                if i + 1 < w:
                    out.append(None)
                    continue
                edge = _reduce(pick, src[i + 1 - w:i + 1])
                if edge is None or not edge:
                    out.append(None)
                    continue
                out.append(abs(closes[i] / edge - 1) * 100)
            return out

        if name in SCALAR_FIELDS:
            # Broadcast so a per-company number compares like any series.
            # Absent stays None, which every comparison reads as "no" — a
            # missing debt figure must never look like zero debt.
            return [self.scalars.get(name)] * n

        raise QueryError(f"unknown field '{name}'")


# ---------------------------------------------------------------- evaluate
def _reduce(fn, values):
    """max/min over a window, ignoring gaps. Empty stays None."""
    vals = [v for v in values if v is not None]
    return fn(vals) if vals else None


def _at(series, i: int):
    """Read a series at bar i. Out of range is None, never a wrap-around.

    Python's negative indexing would quietly turn "20 bars before bar 5" into
    a bar near the END of the series — a lookahead bug that produces
    beautiful, entirely fictional backtests.
    """
    if not isinstance(series, list):
        return series
    if i < 0 or i >= len(series):
        return None
    return series[i]


def _num(node, ctx: Context) -> list | float:
    """Evaluate a numeric node to a series (or a bare float).

    Memoised per node. `ctx.series()` already cached raw fields, so `ema(20)`
    was computed once — but anything BUILT on a field wasn't. Since evaluation
    threads a bar index, reading `sma(20) * 1.05` at every bar rebuilt the
    whole multiplied series once per bar, and `max(close * 1.0, 20)` nested
    three of those passes: 500 bars of one symbol took 27 seconds, which on a
    200-symbol universe is not a slow screen but a hung one.

    The cache lives on the Context, so it dies with the symbol and can't leak
    one company's numbers into another's.
    """
    hit = ctx._nodes.get(id(node))
    if hit is not None:
        return hit[1]
    out = _num_uncached(node, ctx)
    ctx._nodes[id(node)] = (node, out)
    return out


def _num_uncached(node, ctx: Context) -> list | float:
    if isinstance(node, Num):
        return node.value
    if isinstance(node, Str):
        raise QueryError(
            f'"{node.value}" is a name, not a number — it only belongs as the '
            f'first argument to index()')
    if isinstance(node, Call) and (node.name in STATEMENT_CALLS
                                   or node.name in STATEMENT_ALIASES):
        # Broadcast, exactly as scalars are, so a statement figure takes part
        # in arithmetic and comparison like any series. Absent stays None per
        # bar, which every comparison reads as "no".
        return [_statement_value(node, ctx.statements)] * len(ctx.c)
    if isinstance(node, Call) and node.name in CROSS:
        label = render(node)
        if ctx.cross is None:
            raise QueryError(
                f"{node.name}() compares this stock with the rest of the "
                f"universe, so it only works in a screen — there's no field "
                f"to rank against here")
        return [ctx.cross.get(label)] * len(ctx.c)
    if isinstance(node, Call) and node.name == BENCH:
        name, inner = _index_args(node)
        sub, _ = ctx.benchmark(name)
        value = _num(inner, sub)
        if not isinstance(value, list):
            return value                  # a constant is benchmark-free
        return ctx.align_bench(name, value)
    if isinstance(node, Call) and node.name in HISTORY:
        # A history operator inside an arithmetic expression is evaluated at
        # every bar, so it stays a series like any other field.
        return [_history(node, ctx, j) for j in range(len(ctx.c))]
    if isinstance(node, Call) and node.name in TIMEFRAMES:
        # Evaluate the inner expression on resampled bars, then project the
        # result back onto daily bars so everything shares one index space.
        period = TIMEFRAMES[node.name]
        sub, _ = ctx.timeframe(period)
        inner = _num(node.args[0], sub)
        if not isinstance(inner, list):
            return inner                      # a constant is timeframe-free
        return ctx.align(period, inner)
    if isinstance(node, Call):
        spec = FIELDS.get(node.name)
        if spec is None:
            near = _suggest(node.name)
            raise QueryError(
                f"unknown field '{node.name}'" + (f" — did you mean {near}?"
                                                  if near else ""), node.col)
        arity = spec[0]
        supplied = node.args or tuple(
            Num(float(v)) for v in DEFAULT_ARGS.get(node.name, []))
        if len(supplied) != arity:
            raise QueryError(
                f"{node.name} takes {arity} argument"
                f"{'' if arity == 1 else 's'}, got {len(supplied)}", node.col)
        # Same checks as validate(), repeated rather than trusted: evaluate()
        # is public and can be handed an AST that never went through parse().
        return ctx.series(node.name,
                          _check_field_args(node.name, supplied, node.col))
    if isinstance(node, Binary) and node.op in ("+", "-", "*", "/"):
        left, right = _num(node.left, ctx), _num(node.right, ctx)
        return _arith(node.op, left, right, node.col)
    raise QueryError("expected a number here")


def _arith(op, left, right, col):
    ls = isinstance(left, list)
    rs = isinstance(right, list)
    if not ls and not rs:
        if op == "/" and not right:
            return 0.0
        return {"+": left + right, "-": left - right,
                "*": left * right, "/": left / right if right else 0.0}[op]
    n = len(left) if ls else len(right)
    out = []
    for i in range(n):
        a = left[i] if ls else left
        b = right[i] if rs else right
        if a is None or b is None:
            out.append(None)
        elif op == "/":
            out.append(a / b if b else None)
        else:
            out.append({"+": a + b, "-": a - b, "*": a * b}[op])
    return out


def _truth(node, ctx: Context, i: int | None = None) -> bool:
    """Evaluate a boolean node at bar `i` (default: the last one).

    The index is threaded rather than assumed, which is what lets the same
    expression answer three different questions: does it hold NOW (screening),
    did it hold on each historical bar (backtesting), and did it hold within
    the last N bars (the history operators below).
    """
    if i is None:
        i = len(ctx.c) - 1
    if isinstance(node, Not):
        return not _truth(node.operand, ctx, i)
    if isinstance(node, Binary) and node.op == "and":
        return _truth(node.left, ctx, i) and _truth(node.right, ctx, i)
    if isinstance(node, Binary) and node.op == "or":
        return _truth(node.left, ctx, i) or _truth(node.right, ctx, i)
    if isinstance(node, Call) and node.name in HISTORY:
        return bool(_history(node, ctx, i))
    if isinstance(node, Binary) and node.op in COMPARISONS:
        return _compare(node, ctx, i)
    raise QueryError(
        "this looks like a number, not a condition — "
        "did you mean something like `rsi(14) > 70`?")


def _string_operands(node: Binary):
    """(field name, wanted text) if this is a name comparison, else None."""
    for a, b in ((node.left, node.right), (node.right, node.left)):
        if (isinstance(a, Call) and a.name in STRING_FIELDS
                and isinstance(b, Str)):
            return a.name, b.value
    return None


def _compare(node: Binary, ctx: Context, i: int | None = None) -> bool:
    if i is None:
        i = len(ctx.c) - 1

    pair = _string_operands(node)
    if pair is not None:
        field, wanted = pair
        have = ctx.scalars.get(field)
        if have is None:
            # Unclassified. Not "not a bank" — unknown, which every other
            # missing value in this engine also reads as no.
            return False
        have, wanted = str(have).lower(), wanted.lower()
        if node.op == "contains":
            return wanted in have
        return (have == wanted) if node.op == "==" else (have != wanted)

    left, right = _num(node.left, ctx), _num(node.right, ctx)

    if node.op in ("crosses_above", "crosses_below"):
        # On bar i specifically. "Has ever crossed" is a different and much
        # less useful question — every pair of series has crossed at some point.
        a_now, b_now = _at(left, i), _at(right, i)
        a_prev, b_prev = _at(left, i - 1), _at(right, i - 1)
        if i - 1 < 0:
            return False
        if None in (a_now, b_now, a_prev, b_prev):
            return False
        if node.op == "crosses_above":
            return a_prev <= b_prev and a_now > b_now
        return a_prev >= b_prev and a_now < b_now

    a, b = _at(left, i), _at(right, i)
    if a is None or b is None:
        # Not enough history to know. False, never "probably" — a screen that
        # guesses is a screen that hands you a list you can't trust.
        return False
    return {"<": a < b, ">": a > b, "<=": a <= b, ">=": a >= b,
            "==": a == b, "!=": a != b}[node.op]


def _suggest(name: str) -> str | None:
    import difflib
    hit = difflib.get_close_matches(
        name, list(FIELDS) + list(TIMEFRAMES) + list(HISTORY),
        n=1, cutoff=0.6)
    return f"'{hit[0]}'" if hit else None


# ------------------------------------------------------------------- public
def evaluate(node, candles: list[dict], scalars: dict | None = None,
             at: int | None = None, bench: dict | None = None,
             cross: dict | None = None,
             statements: dict | None = None) -> bool:
    if not candles:
        return False
    return _truth(node, Context(candles, scalars, bench, cross, statements),
                  at)


def explain(node, candles: list[dict], scalars: dict | None = None,
            bench: dict | None = None, cross: dict | None = None,
            statements: dict | None = None) -> dict:
    """Every comparison in the query with the values it actually saw.

    A screen that only says yes or no is a screen you end up debugging by
    deleting clauses one at a time. This is what turns "no results" from a
    dead end into information.
    """
    ctx = Context(candles, scalars, bench, cross,
                  statements) if candles else None
    last = len(ctx.c) - 1 if ctx else 0
    rows: list[dict] = []

    def add(n, left, right, ok, neg):
        # `pass` is the clause's contribution to the RESULT, not its own truth.
        # Under a `not`, a clause that holds is the reason the query failed;
        # printing it as a passing clause in a query that matched nothing
        # sends you looking at the wrong line.
        rows.append({"clause": ("not " if neg else "") + render(n),
                     "left": left, "right": right, "pass": ok != neg})

    def walk(n, neg=False):
        if isinstance(n, Not):
            walk(n.operand, not neg)
        elif isinstance(n, Binary) and n.op in ("and", "or"):
            walk(n.left, neg)
            walk(n.right, neg)
        elif isinstance(n, Binary) and n.op in COMPARISONS:
            try:
                left = _at(_num(n.left, ctx), last) if ctx else None
                right = _at(_num(n.right, ctx), last) if ctx else None
                ok = _compare(n, ctx) if ctx else False
            except Exception:                        # noqa: BLE001
                left = right = None
                ok = False
            add(n, left, right, ok, neg)
        elif isinstance(n, Call) and n.name in HISTORY:
            # These used to fall off the end of the walk entirely, so a query
            # built from the history operators explained only the clauses that
            # weren't the interesting part — and `--why` is reached for
            # precisely when the interesting part is what's failing.
            try:
                value = _history(n, ctx, last) if ctx else None
                ok = bool(value)
            except Exception:                        # noqa: BLE001
                value, ok = None, False
            add(n, value, None, ok, neg)
        else:
            # A shape that isn't a condition at all. Better an honest row than
            # a table that silently loses it.
            add(n, None, None, False, neg)

    walk(node)
    return {"clauses": rows}


def render(node) -> str:
    """Turn an AST back into text — for the explain table and error messages."""
    if isinstance(node, Str):
        return f'"{node.value}"'
    if isinstance(node, Num):
        v = node.value
        return str(int(v)) if v == int(v) else str(v)
    if isinstance(node, Call):
        if not node.args:
            return node.name
        return f"{node.name}({', '.join(render(a) for a in node.args)})"
    if isinstance(node, Not):
        return f"not {render(node.operand)}"
    if isinstance(node, Binary):
        return f"{render(node.left)} {node.op} {render(node.right)}"
    return "?"


def render_plan(p: Plan) -> str:
    """Canonical text for a Plan — the round-trip the editor's live check and
    the saved-query list both display."""
    out = render(p.condition)
    if p.sort is not None:
        out += f" sort by {render(p.sort)} {'desc' if p.descending else 'asc'}"
    if p.top is not None:
        out += f" top {p.top}"
    if p.show:
        out += " show " + ", ".join(render(s) for s in p.show)
    return out


def _group_of(name: str) -> str:
    """A finer label than `kind`, for the reference panel's sections.

    `kind` drives behaviour and is left alone; this is presentation. Ninety-
    nine entries under one heading called "Fields" is the same scroll the
    panel was redesigned to remove, just indented.
    """
    if name in STRING_FIELDS:
        return "Classification"
    if name in STATEMENT_ALIASES:
        return "Statement shorthands"
    if name in DELIVERY_FIELDS:
        return "Delivery"
    if name in SCALAR_FIELDS:
        return "Fundamentals"
    return "Price & volume"


def fields() -> list[dict]:
    """The catalogue, for the UI and `/query fields`."""
    out = [{"name": k, "args": v[0], "help": v[1], "kind": "field",
            "group": _group_of(k),
            "defaults": DEFAULT_ARGS.get(k, [])}
           for k, v in sorted(FIELDS.items())]
    out += [{"name": k, "args": v[0], "help": v[2], "kind": "history",
             "defaults": []} for k, v in sorted(HISTORY.items())]
    out += [{"name": k, "args": 1, "kind": "timeframe", "defaults": [],
             "help": f"{k}(expression) — evaluate on {v}ly bars, using only "
                     f"completed {v}s"}
            for k, v in sorted(TIMEFRAMES.items())]
    out += [{"name": k, "args": v[0], "kind": "statement", "defaults": [],
             "help": v[2]} for k, v in sorted(STATEMENT_CALLS.items())]
    out += [{"name": k, "args": 0, "kind": "field", "defaults": [],
             "group": "Statement shorthands",
             "help": f'{v[1]} — shorthand for {v[0]}("{v[1]}")'}
            for k, v in sorted(STATEMENT_ALIASES.items())]
    out += [{"name": k, "args": 1, "kind": "cross", "defaults": [],
             "help": v} for k, v in sorted(CROSS.items())]
    out += [{"name": "index", "args": 1, "kind": "benchmark", "defaults": [],
             "help": f"index(expression) — evaluate it on {DEFAULT_BENCHMARK} "
                     f"bars, aligned to this symbol's dates. "
                     f'index("NIFTY BANK", close) for another benchmark'}]
    out += [
        {"name": "sort by EXPR [asc|desc]", "args": 0, "defaults": [],
         "kind": "clause",
         "help": "rank the results — highest first unless you say asc"},
        {"name": "top N", "args": 0, "defaults": [], "kind": "clause",
         "help": "keep the best N, applied AFTER the sort"},
        {"name": "show EXPR, EXPR", "args": 0, "defaults": [],
         "kind": "clause",
         "help": "extra columns in the results table"},
    ]
    return out


# Grouped so the reference panel can show them as sections rather than
# one scroll. EXAMPLES below is derived from this, not maintained
# beside it — two lists of the same thing drift within a month.
EXAMPLE_GROUPS = [
    ('Trend & momentum', [
        ("rsi(14) > 70", "overbought right now"),
        ("ema(20) crosses_above ema(50)", "a golden cross, on the latest bar"),
        ("close > highest(252)", "a new 52-week high"),
        ("volume > 2 * avg_volume(20)", "twice the usual volume"),
        ("close > sma(200) and rsi(2) < 10",
         "a sharp dip inside a long uptrend"),
        ("bb_width(20, 2) < 4 and adx(14) > 20",
         "a squeeze in a market that can still trend"),
        ("rs > 70 and mos_pct > 20", "strong AND cheap"),
        ("supertrend_dir(10, 3) > 0 and change_pct(5) > 5",
         "trending up and moving"),
        ("not (close > sma(50))", "below the 50-day"),
    ]),
    ('Multi-timeframe', [
        ("weekly(close) > weekly(sma(20))", "above the 20-week average"),
        ("weekly(rsi(14)) > 55 and rsi(14) < 40",
         "weekly trend intact, daily pulled back — the classic entry"),
        ("monthly(close) > monthly(sma(6))", "above the 6-month average"),
        ("weekly(ema(5)) crosses_above weekly(ema(20))",
         "a weekly momentum turn, which is rare and slow"),
    ]),
    ('Fundamentals', [
        ("roe > 18 and debt_equity < 0.5", "quality without leverage"),
        ("sales_growth3 > 15 and profit_growth3 > 15",
         "growing top AND bottom line — margins holding"),
        ("interest_cover > 5 and debt_equity < 1", "can service what it owes"),
        ("earnings_yield > 8 and roe > 15", "cheap on earnings, and earns well"),
        ("roe > 20 and pe < 25 and rs > 60",
         "quality, not expensive, and already working"),
    ]),
    ('Delivery', [
        ("deliv_ratio > 2 and change_pct(1) > 0 and volume > 1.5 * avg_volume(20)",
         "accumulation — up, on volume, and the volume stayed"),
        ("deliv_pct > 60 and deliv_pct > avg_deliv_pct + 15",
         "an unusually high share held overnight"),
        ("deliv_ratio > 3 and change_pct(1) < 0",
         "heavy delivery on a down day — distribution, or a strong hand"),
    ]),
    ('History', [
        ("within(5, rsi(14) crosses_above 70)",
         "went overbought at some point this week"),
        ("bars_since(close > sma(200)) > 60",
         "below the 200-day for three months — a real downtrend, not a dip"),
        ("streak(close > open) > 4", "five straight up days"),
        ("close > ago(close, 20) * 1.1", "up 10% in a month"),
        ("volume > max(volume, 60)", "the heaviest volume in three months"),
        ("avg(close, 5) > avg(close, 20)",
         "short average above long — a smoother crossover"),
        ("within(10, close > highest(252)) and close > sma(50)",
         "broke out in the last fortnight and hasn't given it back"),
    ]),
    ('Against the index', [
        ("index(close) > index(sma(200))",
         "the tape itself is above its 200-day — a regime gate, not a stock pick"),
        ("close / index(close) > ago(close / index(close), 60)",
         "the relative-strength line is rising, not just the price"),
        ("change_pct(63) > index(change_pct(63)) and close > sma(50)",
         "beat the index over a quarter and still in an uptrend"),
        ('index("NIFTY BANK", close) > index("NIFTY BANK", sma(50))',
         "banks trending — for deciding whether to look at banks at all"),
    ]),
    ('Against its industry', [
        ("pe < industry_pe * 0.8 and roe > 15",
         "cheaper than its industry without being worse than it"),
        ('industry contains "bank" and pb < 1.5 and roe > 14',
         "the classic bank screen, on the industry label itself"),
        ("pe < sector_pe and mos_pct > 20",
         "cheap against the sector and against your own valuation"),
    ]),
    ('Ownership & ranges', [
        ("q(\"promoters\") < q(\"promoters\", 2) and fii_holding > 10",
         "promoters trimming while foreign money adds — worth a second look"),
        ("promoter_holding > 50 and debt_equity < 0.3",
         "founder-controlled and unlevered"),
        ("20 < rsi(14) < 30 and close > sma(200)",
         "oversold but not broken — a band, written as one"),
    ]),
    ('Oscillators & bars', [
        ("stoch(14) < 20 and close > sma(200) and cmf(20) > 0",
         "oversold inside an uptrend, with money still flowing in"),
        ("dist_from_high(252) < 5 and volume > 2 * avg_volume(20)",
         "within 5% of the 52-week high on real volume"),
        ("gap_pct > 3 and close > open",
         "gapped up and held it — the gap that didn't fill"),
        ("close > vwap(20) and obv > ago(obv, 20)",
         "above the 20-day VWAP with on-balance volume confirming"),
    ]),
    ('From the statements', [
        ('growth("sales", 3) > 15 and fcf > 0 and debtor_days < 60',
         "compounding, cash-generative, and collecting on time"),
        ('q("net profit") > q("net profit", 4) and q("sales") > q("sales", 4)',
         "both lines up year-on-year in the latest quarter"),
        ('ttm("sales") > fin("sales") * 1.1',
         "the trailing year is already 10% ahead of the last full one"),
        ("cfo > net_profit and debt_equity < 0.5",
         "profits arriving as cash, with a clean balance sheet"),
    ]),
    ('Against the universe', [
        ("percentile(change_pct(63)) > 80 and close > sma(200)",
         "top fifth of the universe on a quarter's move, in an uptrend"),
        ("percentile(change_pct(20) - index(change_pct(20))) > 70",
         "relative strength, defined here rather than pre-computed"),
        ("rank(close / sma(200)) <= 25",
         "the 25 names furthest above their own 200-day"),
    ]),
    ('Ranking & columns', [
        ("close > sma(200) sort by change_pct(20) top 20",
         "the twenty strongest names in an uptrend, not the first twenty"),
        ("rs > 60 sort by pe asc top 15 show roe, debt_equity",
         "cheapest of the strong, with the quality numbers beside them"),
        ("close > highest(252) show atr_pct(14), rs",
         "fresh 52-week highs, with volatility and strength as columns"),
        ("roe > 15 and mos_pct > 0 sort by mos_pct top 25",
         "quality on sale, deepest discount first"),
    ]),
]

EXAMPLES = [pair for _title, items in EXAMPLE_GROUPS for pair in items]

# --------------------------------------------------------------- the runner
def run(text: str, symbols: list[str], days: int = 420,
        with_scalars: bool = True, limit: int = 200,
        explain_misses: int = 0, fetch_missing: bool = False,
        progress=None) -> dict:
    """Evaluate one query across many symbols.

    Two stages when the query allows it. The price half runs over everything
    from stored candles; the fundamentals half runs only over what survived.
    On a whole-market screen that is the difference between 2,000 requests to
    screener.in and eighty.

    Parses ONCE and reuses the AST — 2,000 symbols would otherwise re-parse
    the same string 2,000 times, and a parse error would surface as 2,000
    identical failures instead of one clear message before any work started.
    """
    from . import candles, instruments

    p = plan(text)                         # fails fast, before any I/O
    node = p.condition
    cheap, costly = split_by_cost(node)
    if not with_scalars:
        cheap, costly = conjuncts(node), []

    # A price-only CONDITION with `sort by roe` still needs fundamentals, or
    # every row sorts by None and the ranking silently becomes a tie.
    clause_nodes = ((p.sort,) if p.sort is not None else ()) + p.show
    clauses_costly = with_scalars and any(_needs_scalars(n)
                                          for n in clause_nodes)

    # Once for the whole run, not once per symbol: a 200-name universe would
    # otherwise read the same index candles 200 times. A query naming no
    # benchmark loads none.
    wanted = _benchmarks_used(node).union(
        *[_benchmarks_used(n) for n in clause_nodes]) if clause_nodes \
        else _benchmarks_used(node)
    benchmarks = _load_benchmarks(wanted, days) if wanted else {}

    # Cross-sectional operators can't be answered symbol by symbol — a
    # percentile has no value until the whole field has been measured. So they
    # get a pass of their own, before any filtering.
    cross_calls = _cross_nodes(node)
    for n in clause_nodes:
        cross_calls += [c for c in _cross_nodes(n)
                        if render(c) not in {render(x) for x in cross_calls}]
    cross, population = _measure_field(cross_calls, symbols, days, benchmarks,
                                       progress)

    # Resolved before the scan so a label matching no stored row fails once,
    # loudly, rather than looking like 2,000 companies that never filed.
    statement_rows = _resolve_statement_rows(node, *clause_nodes)

    hits, misses, errors, scanned = [], [], {}, 0
    survivors: list[tuple[str, list]] = []

    # ---- stage 1: everything the candles can answer
    for n, symbol in enumerate(symbols):
        if progress and n % 100 == 0:
            progress(n, len(symbols), "reading candles")
        inst = instruments.lookup(symbol)
        if not inst:
            errors[symbol] = "not in the instrument index"
            continue
        try:
            bars = candles.get(inst["instrument_token"], "day", days)
        except Exception as e:                       # noqa: BLE001
            errors[symbol] = str(e)[:120]
            continue
        if not bars:
            errors[symbol] = "no stored candles — backfill first"
            continue

        try:
            ok = _all_true(cheap, (bars, {}, benchmarks, cross.get(symbol),
                                   None)) if cheap else True
        except QueryDataError as e:
            # This symbol's candles, not this query. Costs one row.
            errors[symbol] = str(e)[:120]
            continue
        except QueryError as e:
            # The query itself. Wrong for every symbol, so reporting it 2,000
            # times as a per-symbol data problem would bury it.
            raise QueryError(f"{e} (evaluating {symbol})") from None
        except Exception as e:                       # noqa: BLE001
            errors[symbol] = f"{type(e).__name__}: {e}"[:120]
            continue

        scanned += 1
        if ok:
            survivors.append((symbol, bars))
        elif explain_misses and len(misses) < explain_misses:
            misses.append({"symbol": symbol, "close": bars[-1]["close"],
                           "why": explain(node, bars, {}, benchmarks,
                                          cross.get(symbol))["clauses"]})

    passed_price = len(survivors)

    # ---- stage 2: fundamentals, only for what's left
    scalars: dict[str, dict] = {}
    stmts: dict[str, dict] = {}
    if statement_rows and survivors:
        from . import fundamentals as _f
        stmts = _f.series_for([s for s, _ in survivors], statement_rows)
    if (costly or clauses_costly) and survivors:
        if fetch_missing:
            _fetch_fundamentals([s for s, _ in survivors], progress)
        if progress:
            progress(0, len(survivors), "valuing survivors")
        scalars = _load_scalars([s for s, _ in survivors], days)

    for symbol, bars in survivors:
        sc = dict(scalars.get(symbol, {}))
        if sc.get("pb") is None and sc.get("book_value"):
            # Derived here rather than stored: the price moves daily and the
            # book value doesn't, so a cached P/B goes stale silently.
            sc["pb"] = round(bars[-1]["close"] / sc["book_value"], 2)

        try:
            passes = _all_true(costly, (bars, sc, benchmarks,
                                        cross.get(symbol),
                                        stmts.get(symbol))) if costly else True
        except QueryDataError as e:
            errors[symbol] = str(e)[:120]
            continue
        except QueryError as e:
            raise QueryError(f"{e} (evaluating {symbol})") from None
        except Exception as e:                       # noqa: BLE001
            errors[symbol] = f"{type(e).__name__}: {e}"[:120]
            continue

        if not passes:
            if explain_misses and len(misses) < explain_misses:
                misses.append({"symbol": symbol, "close": bars[-1]["close"],
                               "why": explain(node, bars, sc, benchmarks,
                                              cross.get(symbol),
                                              stmts.get(symbol))["clauses"]})
            continue
        row = {"symbol": symbol, "close": bars[-1]["close"],
               "bars": len(bars),
               **{k: v for k, v in sc.items() if v is not None}}
        row.update(_clause_values(p, bars, sc, benchmarks,
                                  cross.get(symbol), stmts.get(symbol)))
        hits.append(row)

    columns = [render(s) for s in p.show]
    hits = _order(hits, p)
    limit = p.top or limit
    truncated = len(hits) > limit

    return {
        "query": text,
        "parsed": render_plan(p),
        "scanned": scanned,
        "passed_price": passed_price,
        "staged": bool(cheap and costly),
        "columns": columns,
        "sorted_by": render(p.sort) if p.sort is not None else None,
        "descending": p.descending,
        "hits": hits[:limit],
        "hit_count": len(hits),
        "misses": misses,
        "errors": errors,
        "population": population,
        "note": _run_note(len(hits), scanned, errors, passed_price,
                          bool(costly), p, truncated, population),
    }


def _clause_values(p: Plan, bars: list[dict], sc: dict,
                   bench: dict | None = None, cross: dict | None = None,
                   statements: dict | None = None) -> dict:
    """The sort key and every `show` column, read at the last closed bar.

    One Context for all of them, so a query sorting by `rsi(14)` and also
    showing it computes the series once. A column that can't be computed —
    sma(200) on a stock with 30 bars — is left absent rather than becoming
    zero, for the same reason a missing debt figure isn't no debt.
    """
    out: dict = {}
    wanted = ((p.sort,) if p.sort is not None else ()) + p.show
    if not wanted:
        return out
    ctx = Context(bars, sc, bench, cross, statements)
    last = len(bars) - 1
    for n in wanted:
        label = render(n)
        try:
            out[label] = _at(_num(n, ctx), last)
        except (QueryError, TypeError, KeyError, ZeroDivisionError):
            out[label] = None
    if p.sort is not None:
        out["_sort"] = out.get(render(p.sort))
    return out


def _order(hits: list[dict], p: Plan) -> list[dict]:
    """Rank the hits, with unknowns last in BOTH directions.

    Absent is not zero and it isn't infinity — it's "we don't know", and the
    only honest place for it is the bottom whichever way you sorted. The
    boolean-first key does that; the second element is never None, because two
    unranked rows would otherwise end up comparing None with None.

    Python's sort is stable, so ties keep scan order rather than shuffling
    between runs.
    """
    if p.sort is None:
        return hits

    def key(row):
        v = row.get("_sort")
        if v is None:
            return (1, 0.0)
        return (0, -v if p.descending else v)

    ranked = sorted(hits, key=key)
    for row in ranked:
        row.pop("_sort", None)
    return ranked


def _measure_field(calls: list, symbols: list[str], days: int,
                   benchmarks: dict, progress=None) -> tuple[dict, int]:
    """Measure every cross-sectional expression across the whole field.

    Returns per-symbol values keyed by the rendered call, plus how many
    symbols were actually measurable.

    **The population is everything scanned, not everything that survived the
    other clauses.** If it narrowed as filters were added, `percentile(x) > 80`
    would mean "top fifth of the universe" in one query and "top fifth of the
    stocks already above their 200-DMA" in another — a much weaker screen
    wearing identical syntax, with nothing in the output to tell them apart.
    That also makes the answer depend on clause composition, which is the kind
    of invisible coupling this engine keeps having to design out.

    The price is a second read of the candles: the field has to be complete
    before any symbol can be judged, and holding two thousand symbols' bars in
    memory to avoid re-reading them trades a bounded cost for an unbounded
    one. Stored candles are a local DuckDB read; the earlier stage-splitting
    argument was about requests to somebody else's website, and doesn't apply.
    Queries with no cross-sectional operator pay none of this.

    A symbol whose expression can't be computed is left out of the population
    entirely rather than counted as zero — too little history is not the same
    fact as bottom of the field, and including it would drag everyone else's
    percentile up.
    """
    if not calls:
        return {}, 0

    from . import candles, instruments

    raw: dict[str, dict] = {render(c): {} for c in calls}
    for n, symbol in enumerate(symbols):
        if progress and n % 100 == 0:
            progress(n, len(symbols), "measuring the field")
        inst = instruments.lookup(symbol)
        if not inst:
            continue
        try:
            bars = candles.get(inst["instrument_token"], "day", days)
        except Exception:                            # noqa: BLE001
            continue
        if not bars:
            continue
        # No scalars: _validate_cross has already refused them, and building
        # the context without makes it so rather than merely forbidden.
        ctx = Context(bars, None, benchmarks)
        last = len(bars) - 1
        for c in calls:
            try:
                value = _at(_num(c.args[0], ctx), last)
            except (QueryError, TypeError, KeyError, ZeroDivisionError):
                value = None
            if value is not None:
                raw[render(c)][symbol] = value

    out: dict[str, dict] = {}
    population = 0
    for c in calls:
        label = render(c)
        scores = raw[label]
        population = max(population, len(scores))
        table = (_percentiles(scores) if c.name == "percentile"
                 else _ranks(scores))
        for symbol, value in table.items():
            out.setdefault(symbol, {})[label] = value
    # Every scanned symbol needs an entry, so that a symbol absent from the
    # population still gets a context with cross values enabled — and reads
    # None for the operator rather than "there is no universe here".
    for symbol in symbols:
        out.setdefault(symbol, {})
    return out, population


def _fetch_fundamentals(symbols: list[str], progress=None) -> None:
    """Pull statements for symbols that have none.

    Only reached for survivors, and only when asked. Each one is a page
    request to somebody else's website — polite at eighty, rude at two
    thousand, which is the entire argument for the two-stage design.
    """
    from . import fundamentals

    for n, symbol in enumerate(symbols):
        if fundamentals.summary(symbol):
            continue
        if progress:
            progress(n, len(symbols), f"fetching {symbol}")
        try:
            fundamentals.fetch_and_store(symbol)
        except (Exception, SystemExit):              # noqa: BLE001
            # SystemExit deliberately: fetch_and_store exits the process when
            # config is missing, which is right for a one-shot CLI command and
            # wrong in the middle of a 2,000-symbol screen. One company with
            # no page must not end the run.
            continue


def conjuncts(node) -> list:
    """Flatten a top-level `a and b and c` into [a, b, c].

    Only top-level ANDs: anything under an `or` or a `not` stays whole,
    because you can't evaluate half of a disjunction and draw a conclusion.
    """
    if isinstance(node, Binary) and node.op == "and":
        return conjuncts(node.left) + conjuncts(node.right)
    return [node]


def split_by_cost(node) -> tuple[list, list]:
    """Partition a query into (cheap, expensive) halves.

    Cheap = answerable from stored candles. Expensive = needs fundamentals or
    valuation, which cost a scrape per company.

    This is what makes a whole-market fundamental screen possible at all.
    Loading fundamentals for 2,000 symbols means 2,000 requests to a third
    party; loading them for the 80 that already passed the price filter means
    80. AND is commutative, so evaluating the cheap half first and the
    expensive half only on survivors gives an identical answer for a fraction
    of the work.

    Returns ([], [node]) when the query can't be split — a scalar sitting
    inside an `or` has to be evaluated for everyone, because the other branch
    might be false.
    """
    parts = conjuncts(node)
    cheap = [p for p in parts if not _needs_scalars(p)]
    costly = [p for p in parts if _needs_scalars(p)]
    if not costly:
        return parts, []
    if not cheap:
        return [], parts
    return cheap, costly


def _all_true(parts: list, ctx_args, at=None) -> bool:
    candles, scalars, bench, cross, statements = ctx_args
    ctx = Context(candles, scalars, bench, cross, statements)
    return all(_truth(p, ctx, at) for p in parts)


def _needs_scalars(node) -> bool:
    """Only pay for valuation and RS if the query actually mentions them."""
    found = {"needed": False}

    # Explicit type matching rather than getattr-walking. The nodes are all
    # known dataclasses, and a module whose whole job is parsing untrusted
    # input has no business calling getattr with a computed name — even
    # safely, it is the shape of the classic sandbox escape and invites
    # someone to extend it carelessly later.
    def walk(n):
        if isinstance(n, Call):
            if (n.name in SCALAR_FIELDS or n.name in STATEMENT_CALLS
                    or n.name in STATEMENT_ALIASES):
                # Statement rows are a local SQLite read rather than a website
                # fetch, but they still belong in the second stage: no point
                # loading them for a symbol the price filter already rejected.
                found["needed"] = True
            for a in n.args:
                walk(a)
        elif isinstance(n, Not):
            walk(n.operand)
        elif isinstance(n, Binary):
            walk(n.left)
            walk(n.right)

    walk(node)
    return found["needed"]


def _load_scalars(symbols: list[str], days: int) -> dict[str, dict]:
    """Every non-price number, for every symbol, in one pass.

    Each source is wrapped separately: a broken valuation for one company must
    not cost you the fundamentals for all of them. Anything unavailable stays
    absent rather than becoming zero — `debt_equity < 0.5` should not match a
    company whose debt we simply don't know.
    """
    from . import fundamentals, rs as rs_mod, valuation

    out: dict[str, dict] = {s: {} for s in symbols}

    try:
        for row in rs_mod.rank(symbols, days=days):
            out.setdefault(row["symbol"], {})["rs"] = row.get("rs")
    except Exception:                                # noqa: BLE001
        pass

    try:
        for row in valuation.screen(symbols).get("rows", []):
            d = out.setdefault(row["symbol"], {})
            d["mos_pct"] = row.get("mos_pct")
            d["fair_value"] = row.get("composite")
            d["n_models"] = row.get("n_models")
            d["pe"] = row.get("pe")
            d["market_cap"] = row.get("market_cap")
    except Exception:                                # noqa: BLE001
        pass

    try:
        # One aggregate for the whole run. The medians are over everything
        # stored rather than over the symbols being scanned, so this is the
        # same table whatever the query — see fundamentals.group_pe().
        groups = fundamentals.group_pe()
        for symbol in symbols:
            sector, industry = groups["of"].get(symbol, (None, None))
            d = out.setdefault(symbol, {})
            if sector:
                d["sector"] = sector
                d["sector_pe"] = groups["sector"].get(sector)
            if industry:
                d["industry"] = industry
                d["industry_pe"] = groups["industry"].get(industry)
    except Exception:                                # noqa: BLE001
        pass

    try:
        from . import delivery
        for symbol, st in delivery.many(symbols).items():
            d = out.setdefault(symbol, {})
            for k in DELIVERY_FIELDS:
                if st.get(k) is not None:
                    d[k] = st[k]
    except Exception:                                # noqa: BLE001
        pass

    for symbol in symbols:
        d = out.setdefault(symbol, {})
        try:
            _fundamentals_into(d, symbol, fundamentals)
        except Exception:                            # noqa: BLE001
            continue
    return out


# screener's own labels, matched loosely because the exact wording drifts
# ("Stock P/E" vs "P/E"). Longest match first so "book value" can't be
# satisfied by a key that merely contains "value".
_SNAP_KEYS = {
    "pe": ("stock p/e", "p/e"),
    "book_value": ("book value",),
    "div_yield": ("dividend yield",),
    "market_cap": ("market cap",),
}

# what fundamentals.compute() calls them -> what a query calls them
_COMPUTED = {
    "ROE %": "roe",
    "ROCE % (ours: EBIT / eq+debt)": "roce",
    "ROCE % (screener)": "roce_screener",
    "Debt / Equity": "debt_equity",
    "Interest coverage": "interest_cover",
    "OPM % (latest FY)": "opm",
    "Sales CAGR 3y %": "sales_growth3",
    "Sales CAGR 5y %": "sales_growth5",
    "Profit CAGR 3y %": "profit_growth3",
    "Profit CAGR 5y %": "profit_growth5",
}


def _fundamentals_into(d: dict, symbol: str, fundamentals) -> None:
    # summary() already runs compute() internally, so take both from one call
    # rather than deriving the same ratios twice per symbol.
    summary = fundamentals.summary(symbol) or {}
    for label, value in (summary.get("computed") or {}).items():
        key = _COMPUTED.get(label)
        if key and value is not None:
            d[key] = value

    snap = {str(k).lower(): v for k, v in (summary.get("snapshot") or {}).items()}
    for field, patterns in _SNAP_KEYS.items():
        if d.get(field) is not None:
            continue
        for pat in patterns:
            hit = next((v for k, v in snap.items() if pat in k), None)
            if isinstance(hit, (int, float)):
                d[field] = float(hit)
                break

    if d.get("earnings_yield") is None and d.get("pe"):
        d["earnings_yield"] = round(100.0 / d["pe"], 2)


def _run_note(hits: int, scanned: int, errors: dict,
              passed_price: int = 0, had_fundamentals: bool = False,
              p: "Plan | None" = None, truncated: bool = False,
              population: int = 0) -> str:
    if not scanned:
        return ("Nothing was scanned — none of these symbols has stored "
                "candles. Run `universe backfill` first.")
    bits = [f"{hits} of {scanned} matched."]
    if had_fundamentals and passed_price != hits:
        # Where the funnel narrowed matters: a price filter that leaves three
        # candidates is a different problem from a valuation that rejects
        # nine hundred.
        bits.append(f"{passed_price} passed the price filter, "
                    f"{hits} then passed on fundamentals.")
    if population:
        # The population is the whole meaning of a percentile, and the one
        # thing about it that isn't visible in the query text.
        bits.append(f"Percentile and rank measured across the {population} "
                    f"symbols with enough history.")
    if p is not None and p.sort is not None:
        # Descending is a guess — a sensible one for `sort by rs`, less so for
        # `sort by pe`. Saying which way it went beats leaving you to infer it
        # from the rows.
        bits.append(f"Sorted by {render(p.sort)}, "
                    f"{'highest' if p.descending else 'lowest'} first.")
    elif truncated:
        # Handing back 200 of 412 in scan order is the thing ranking exists to
        # remove. Where it still happens it has to be visible.
        bits.append("These are the first rows in scan order, not the best — "
                    "add `sort by …` to choose.")
    if errors:
        bits.append(f"{len(errors)} skipped (no data).")
    if hits == 0:
        bits.append("Run with --why to see which clause is failing.")
    return " ".join(bits)


def to_positions(node, candles: list[dict], scalars: dict | None = None,
                 warmup: int = 0, bench: dict | None = None) -> list[int]:
    """1 where the expression holds, 0 where it doesn't — one per bar.

    This is the whole bridge between screening and backtesting: a query that
    tells you what's true today can, evaluated at every bar, tell you what
    would have happened. `warmup` skips the opening stretch where indicators
    are still None, which would otherwise read as a flat position and quietly
    shorten the test.
    """
    ctx = Context(candles, scalars, bench)
    return [1 if (i >= warmup and _truth(node, ctx, i)) else 0
            for i in range(len(candles))]


def backtest(text: str, symbol: str, days: int = 730,
             cost_bps: float = 10.0, capital: float = 100000.0,
             risk: dict | None = None, warmup: int = 200) -> dict:
    """Run a query as a strategy: long while it holds, flat while it doesn't.

    The point is that it goes through the SAME simulator as the built-in
    strategies — same costs, same intrabar fill rules, same metrics — so a
    query's numbers and a strategy's numbers can be put side by side.

    Scalars are deliberately NOT loaded here. `roe` and `mos_pct` are today's
    figures; applying them to a bar from 2023 asserts you knew this year's
    balance sheet two years ago, which is the most flattering lookahead
    available. A query naming one is refused rather than silently backdated.
    """
    from . import backtest as bt, candles as candle_store, instruments

    node = parse(text)
    if _needs_cross(node):
        raise QueryError(
            "percentile() and rank() can't be backtested: they measure this "
            "stock against today's universe, and applying that to a bar from "
            "two years ago assumes you knew this year's cross-section at the "
            "time. It would flatter every momentum system ever tested.")
    if _needs_scalars(node):
        raise QueryError(
            "fundamentals can't be backtested: fields like roe and mos_pct "
            "are today's figures, and applying them to a bar from two years "
            "ago would assume you knew them at the time. Drop them, or "
            "screen with them instead.")

    inst = instruments.lookup(symbol)
    if not inst:
        raise QueryError(f"unknown symbol {symbol}")
    from . import kite_auth
    candle_store.ensure(kite_auth.get_kite(), inst["instrument_token"],
                        "day", days)
    c = candle_store.get(inst["instrument_token"], "day", days)
    if len(c) < warmup + 30:
        raise QueryError(
            f"only {len(c)} bars — not enough to test after a {warmup}-bar "
            f"warm-up")

    # Benchmarks, unlike fundamentals, are safe to backtest — see the
    # docstring. Loaded for the same window as the symbol.
    wanted = _benchmarks_used(node)
    benchmarks = _load_benchmarks(wanted, days) if wanted else {}
    missing = wanted - set(benchmarks)
    if missing:
        raise QueryError(
            f"no stored candles for {', '.join(sorted(missing))} — "
            f"backfill the index before testing against it")

    pos = to_positions(node, c, None, warmup=warmup, bench=benchmarks)
    out = bt.run_positions(symbol, pos, render(node), days=days,
                           cost_bps=cost_bps, capital=capital, risk=risk,
                           candles_in=c)
    out["query"] = text
    out["bars_long"] = sum(pos)
    return out


def summary(result: dict, top: int = 20) -> str:
    """CLI / phone rendering."""
    cols = result.get("columns") or []
    lines = [f"{result['parsed']}", ""]
    for row in result["hits"][:top]:
        extra = ""
        if row.get("rs") is not None and "rs" not in cols:
            extra += f"  RS {row['rs']:.0f}"
        if row.get("mos_pct") is not None and "mos_pct" not in cols:
            extra += f"  MoS {row['mos_pct']:+.0f}%"
        # Explicit columns come last and are labelled, because on a phone a
        # bare number in a row of numbers is a guess.
        for c in cols:
            extra += f"  {c} {_fmt(row.get(c))}"
        lines.append(f"  {row['symbol']:<14} {row['close']:>9.2f}{extra}")
    if not result["hits"]:
        lines.append("  (nothing matched)")
        for m in result["misses"][:3]:
            failed = [c for c in m["why"] if not c["pass"]]
            if failed:
                c = failed[0]
                lines.append(f"    {m['symbol']}: {c['clause']} "
                             f"— saw {_fmt(c['left'])} vs {_fmt(c['right'])}")
    lines.append("")
    lines.append(result["note"])
    return "\n".join(lines)


def _fmt(v) -> str:
    if v is None:
        return "—"
    return f"{v:.2f}" if isinstance(v, float) else str(v)


# ----------------------------------------------------------- saved queries
def save(name: str, expression: str, universe: str | None = None,
         alert: bool = False) -> dict:
    """Store a query under a name. Parses first, so a broken one can't be
    saved and then fail silently every evening."""
    from . import db

    # plan(), not parse(): remembering how a screen wanted to be ranked is
    # most of the value of saving one.
    plan(expression)
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO saved_queries (name, expression, universe, alert) "
            "VALUES (?,?,?,?) ON CONFLICT(name) DO UPDATE SET "
            "expression=excluded.expression, universe=excluded.universe, "
            "alert=excluded.alert",
            [name, expression, universe, 1 if alert else 0])
    return {"name": name, "expression": expression, "universe": universe,
            "alert": alert}


def saved(name: str | None = None) -> list[dict]:
    from . import db
    import json as _json

    q = "SELECT * FROM saved_queries"
    args: list = []
    if name:
        q += " WHERE name=?"
        args.append(name)
    q += " ORDER BY name"
    with db.connect() as conn:
        rows = [dict(r) for r in conn.execute(q, args).fetchall()]
    for r in rows:
        r["alert"] = bool(r["alert"])
        try:
            r["last_hits"] = _json.loads(r["last_hits"] or "[]")
        except ValueError:
            r["last_hits"] = []
    return rows


def forget(name: str) -> bool:
    from . import db
    with db.connect() as conn:
        return conn.execute("DELETE FROM saved_queries WHERE name=?",
                            [name]).rowcount > 0


def check_alerts(push: bool = True, days: int = 420) -> dict:
    """Run every watched query and report what NEWLY entered each one.

    The diff is the point. Pushing a full result set every evening trains you
    to ignore it; pushing only the three names that just qualified is a
    message worth opening.

    Departures are tracked but not pushed. A stock dropping out of a screen is
    rarely something to act on, and every extra notification costs a little of
    the attention the useful ones need.
    """
    from . import db, notify, universe as universe_mod, watchlist
    import json as _json

    out = {"checked": 0, "entered": {}, "left": {}, "errors": {}}
    for row in saved():
        if not row["alert"]:
            continue
        out["checked"] += 1
        syms = (universe_mod.symbols(row["universe"]) if row["universe"]
                else [r["tradingsymbol"] for r in watchlist.items()])
        try:
            res = run(row["expression"], syms, days=days)
        except QueryError as e:
            out["errors"][row["name"]] = str(e)
            continue

        now = sorted(r["symbol"] for r in res["hits"])
        before = set(row["last_hits"] or [])
        entered = [s for s in now if s not in before]
        left = sorted(before - set(now))

        with db.connect() as conn:
            conn.execute(
                "UPDATE saved_queries SET last_hits=?, "
                "last_run=datetime('now','localtime') WHERE id=?",
                [_json.dumps(now), row["id"]])

        if entered:
            out["entered"][row["name"]] = entered
        if left:
            out["left"][row["name"]] = left

        # Nothing on the first run: `before` is empty, so every current hit
        # would look new and the first push would be the entire result set.
        first_run = row["last_run"] is None
        if push and entered and not first_run:
            try:
                if not notify.send(
                        f"{row['name']}\n" + ", ".join(entered)
                        + f"\n\n{row['expression']}",
                        title="Query alert", tags="mag"):
                    out["errors"][row["name"]] = "push failed: not delivered"
            except (Exception, SystemExit) as e:      # noqa: BLE001
                out["errors"][row["name"]] = f"push failed: {e}"
    return out

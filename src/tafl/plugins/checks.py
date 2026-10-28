"""Checks: stop the table, or warn, when the data is not as expected."""

from __future__ import annotations

from tafl.api import ActionError, P, action

# Each row of the setting "Checks" is one check: its kind ("check") and the fields of that kind,
# shown only for it. A new kind is a new option here, new fields with visible_if={"check": kind},
# and an entry in _RUN and _NEED; saved rows get the defaults of fields they lack, so the
# settings of existing steps stay as they are. Raise the action's version with it (its migrate
# keeps the settings as they are): an app that doesn't know the kind then says that the step
# needs a newer version, instead of calling the kind a wrong choice and dropping its fields when
# the step is edited.
_CHECKS = [("rows", "Number of rows")]
_COMPARE = [("at_least", "at least"), ("at_most", "at most"), ("exactly", "exactly"), ("between", "between")]
_LABELS = dict(_COMPARE)
_ON_FAIL = [("stop", "Stop the table"), ("warn", "Only warn")]
_NO_CHECKS = "Checks: add at least 1 row"
_PASSES = {
    "at_least": lambda n, row: n >= row.number,
    "at_most": lambda n, row: n <= row.number,
    "exactly": lambda n, row: n == row.number,
    "between": lambda n, row: row.number <= n <= row.up_to,
}


def _rows(n: int) -> str:
    """A number of rows in words: "no rows", "1 row", "1,500 rows"."""
    return "no rows" if n == 0 else "1 row" if n == 1 else f"{n:,} rows"


def _rows_need(row) -> str:
    """What a rows check asks for, e.g. "at least 1 row" or "between 10 and 100 rows"."""
    compare, number = row.compare, row.number
    if compare == "between":
        if number != row.up_to:
            return f"between {number:,} and {row.up_to:,} rows"
        compare = "exactly"
    if number == 0 and compare != "at_least":
        return "no rows"
    return f"{_LABELS[compare]} {number:,} row{'' if number == 1 else 's'}"


def _rows_check(df, rows) -> str | None:
    """The failure of the rows checks, all in one line, or None when they pass."""
    failed = [_rows_need(row) for row in rows if not _PASSES[row.compare](df.height, row)]
    if not failed:
        return None
    needs = failed[0] if len(failed) == 1 else f"{', '.join(failed[:-1])} and {failed[-1]}"
    return f"The table has {_rows(df.height)}; it must have {needs}"


_RUN = {"rows": _rows_check}  # kind -> the failure of the checks of that kind, or None
_NEED = {"rows": _rows_need}  # kind -> what a check of that kind asks for (for the summary)


def _summary(p) -> str:
    if not isinstance(p.checks, list):
        return ""
    needs = "; ".join(_NEED[row.check](row) if row.check in _NEED else "?" for row in p.checks) or "?"
    return f"Check: {needs}, else {'warn' if p.on_fail == 'warn' else 'stop'}"


def _check_settings(p, vc) -> list[str]:
    """No checks at all, or a "between" row whose numbers are the wrong way round. (The list has
    no min_items: for the summary, the engine would give an empty list one new row, so that a step
    without checks would read "Check: at least 1 row".)"""
    if isinstance(p.checks, list) and not p.checks:
        return [_NO_CHECKS]
    problems = []
    for number, row in enumerate(p.checks if isinstance(p.checks, list) else (), start=1):
        if row.get("check") != "rows" or row.get("compare") != "between":
            continue
        low, high = row.get("number"), row.get("up_to")
        if _whole(low) and _whole(high) and low > high:
            problems.append(
                f"Checks, row {number} – between {low:,} and {high:,}: the first number must not be bigger than the second"
            )
    return problems


def _whole(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


@action(
    "check.data",
    "Check data",
    category="Rows",
    params=[
        P.List(
            "checks",
            "Checks",
            fields=[
                P.Choice("check", "Check", _CHECKS),
                P.Choice("compare", "Must be", _COMPARE, visible_if={"check": "rows"}),
                P.Int("number", "Number", default=1, min=0, visible_if={"check": "rows"}),
                P.Int("up_to", "and", default=100, min=0, visible_if={"check": "rows", "compare": "between"}),
            ],
            default=({"check": "rows", "compare": "at_least", "number": 1},),
        ),
        P.Choice(
            "on_fail",
            "When a check fails",
            _ON_FAIL,
            help="Stop the table: the step shows an error, the steps after it and the tables that use "
            "this table are not calculated, and a run of the project fails. Only warn: the step shows "
            "a warning and the data goes on.",
        ),
    ],
    summary=_summary,
    checks=_check_settings,
)
def check_data(df, p, ctx):
    """Checks the data at this step: when a check fails, it stops the table or only warns. The data
    itself stays as it is.

    Number of rows: at least, at most, exactly or between two numbers; at least 1 means that the
    table must not be empty. Stop the table: the step shows an error, like a step that fails, so the
    steps after it and the tables that use this table are not calculated and their exports don't
    run; a run of the project fails. Only warn: the step and Run results show a warning, and the
    data goes on. Put the step before the exports, so that bad input never overwrites a good file.
    """
    if not p.checks:  # the engine reports this before the step runs, as the unknown kind below
        raise ActionError(_NO_CHECKS)
    for number, row in enumerate(p.checks, start=1):
        if row.check not in _RUN:
            raise ActionError(f"Checks, row {number} – Check: '{row.check}' is not one of: {', '.join(_RUN)}")
    failures = []
    for kind in dict.fromkeys(row.check for row in p.checks):  # each kind in one line, in their order
        if (failure := _RUN[kind](df, [row for row in p.checks if row.check == kind])) is not None:
            failures.append(failure)
    if not failures:
        count = len(p.checks)
        passed = "The check passed" if count == 1 else f"All {count} checks passed"
        ctx.log(f"{passed}: the table has {_rows(df.height)}")
    elif p.on_fail == "warn":
        for failure in failures:
            ctx.warn(failure)
    else:
        raise ActionError("\n".join(failures))
    return df

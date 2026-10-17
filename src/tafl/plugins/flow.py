"""Flow: end a table early when there is nothing to do."""

from __future__ import annotations

from tafl.api import P, SkipRest, action

_WHICH = [("this", "This table (the data at this step)"), ("other", "Another table")]


def _summary(p) -> str:
    if p.which == "other":
        return f"Skip the rest if {p.table or 'another table'} is empty"
    return "Skip the rest if the table is empty"


@action(
    "flow.skip_if_empty",
    "Skip the rest if a table is empty",
    category="Rows",
    params=[
        P.Choice("which", "Which table", _WHICH),
        P.TableRef("table", "Table", visible_if={"which": "other"}),
    ],
    summary=_summary,
)
def skip_if_empty(df, p, ctx):
    """Ends the table here when the table has no rows: the steps after this one are skipped, e.g.
    the exports on a day without new data, and the table's result is the data at this step. This
    is not an error: a run of the project succeeds. When the table has rows, nothing changes."""
    if p.which == "other":
        if ctx.table(p.table).height == 0:
            raise SkipRest(f"The table “{ctx.table_name(p.table)}” is empty")
    elif df.height == 0:
        raise SkipRest("The table is empty")
    return df

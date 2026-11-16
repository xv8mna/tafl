import re
import time

import pytest

from tafl.api import AddedColumn, P, ValidationContext, action
from tafl.api.params import check, is_visible, resolve, table_refs, unknown_settings

SPEC = [
    P.Choice("mode", "Mode", [("table", "Table"), ("query", "Query")]),
    P.Text("query", "Query", required=True, visible_if={"mode": "query"}),
    P.Column("column", "Column"),
    P.Columns("extra", "Extra"),
    P.TableRef("other", "Other table", required=False),
    P.List(
        "match",
        "Match",
        fields=[P.Column("left", "Left"), P.Column("right", "Right", source="other")],
    ),
]


def test_resolve_fills_defaults_and_keeps_unknown_keys():
    params = resolve(SPEC, {"column": "A", "future_setting": 1})
    assert params.mode == "table"  # first option is the default
    assert params.query == ""
    assert params.extra == []
    assert params.match == []
    assert params["future_setting"] == 1


def test_list_rows_become_params_with_defaults():
    params = resolve(SPEC, {"match": [{"left": "a"}]})
    assert params.match[0].left == "a"
    assert params.match[0].right == ""


def test_visibility_controls_validation():
    vc = ValidationContext(columns=["A"])
    assert check(SPEC, resolve(SPEC, {"column": "A"}), vc) == []
    problems = check(SPEC, resolve(SPEC, {"column": "A", "mode": "query"}), vc)
    assert problems == ["Query: required"]
    assert is_visible(SPEC[1], {"mode": "query"})


def test_missing_column_is_reported_with_suggestion():
    vc = ValidationContext(columns=["Amount", "Date"])
    problems = check(SPEC, resolve(SPEC, {"column": "amount"}), vc)
    assert problems == ["Column: column 'amount' not found (did you mean 'Amount'?)"]


def test_list_columns_of_other_table_are_checked_against_that_table():
    vc = ValidationContext(columns=["id"], table_columns={"t2": ["key"]}.get, tables={"t2": "Other"})
    params = resolve(SPEC, {"column": "id", "other": "t2", "match": [{"left": "id", "right": "nope"}]})
    assert check(SPEC, params, vc) == ["Match, row 1 – Right: column 'nope' not found"]


def test_table_refs_and_unknown_table():
    params = resolve(SPEC, {"column": "id", "other": "t9"})
    assert table_refs(SPEC, params) == ["t9"]
    vc = ValidationContext(columns=["id"], tables={"t1": "One"})
    assert "Other table: table 't9' does not exist" in check(SPEC, params, vc)


def test_int_validation():
    spec = [P.Int("n", "N", min=1)]
    vc = ValidationContext()
    assert check(spec, resolve(spec, {"n": "5"}), vc) == []
    assert check(spec, resolve(spec, {"n": 0}), vc) == ["N: must be at least 1"]
    assert check(spec, resolve(spec, {"n": "x"}), vc) == ["N: must be a whole number"]
    assert check(spec, resolve(spec, {"n": float("inf")}), vc) == ["N: must be a whole number"]  # 1e999 in a file


def test_values_of_the_wrong_kind_are_reported_and_used_nowhere():
    """A project file written by hand can hold e.g. a number where a table belongs."""
    spec = [*SPEC, P.TableRefs("tables", "Tables", required=False), P.ConnectionRef("db", "Database", required=False)]
    wrong = {"extra": 5, "other": ["t2"], "match": "left", "tables": {"t2": 1}, "db": 7}
    params = resolve(spec, {"column": "id", **wrong})
    assert {key: params[key] for key in wrong} == wrong  # kept as they are
    assert table_refs(spec, params) == []
    vc = ValidationContext(columns=["id"], tables={"t2": "Other"}, connections=["db"])
    assert check(spec, params, vc) == [
        "Extra: 5 is not a list of columns",
        'Other table: ["t2"] is not a table',
        'Match: "left" is not a list',
        'Tables: {"t2": 1} is not a list of tables',
        "Database: 7 is not a connection",
    ]
    assert resolve(spec, {"extra": [1, "B"], "tables": ("t2",)}).extra == ["1", "B"]  # names written as numbers


def test_reserved_and_duplicate_parameter_names_are_rejected():
    with pytest.raises(ValueError, match="reserved"):
        action("x.y", "X", params=[P.List("items", "Items", fields=[])])
    with pytest.raises(ValueError, match="reserved"):
        action("x.y", "X", params=[P.List("rows", "Rows", fields=[P.Text("keys")])])
    with pytest.raises(ValueError, match="duplicate"):
        action("x.y", "X", params=[P.Text("a"), P.Bool("a")])


def test_labels_default_from_keys():
    assert P.Text("new_name").label == "New name"


def test_stored_keys_that_no_setting_has():
    """They have no effect: the setting keeps its default. The closest known key is suggested."""
    assert unknown_settings(SPEC, {"column": "A", "colum": "B", "Mode": "query", "xyz": 1}) == [
        "Setting 'colum' is not known (did you mean 'column'?): it has no effect",
        "Setting 'Mode' is not known (did you mean 'mode'?): it has no effect",
        "Setting 'xyz' is not known: it has no effect",
    ]
    assert unknown_settings(SPEC, {"query": "SELECT 1", "_sheet": {"file": "a.csv"}, "_other": 1}) == []  # hidden; marks
    assert unknown_settings(SPEC, {}) == []


def test_stored_keys_that_no_field_has_in_the_rows_of_a_list():
    rows = [{"left": "a", "right": "b"}, {"left": "a", "rigth": "b", "_mark": 1}, "not a row", {"lft": "c"}]
    assert unknown_settings(SPEC, {"match": rows}) == [
        "Match, row 2: setting 'rigth' is not known (did you mean 'right'?): it has no effect",
        "Match, row 4: setting 'lft' is not known (did you mean 'left'?): it has no effect",
    ]
    assert unknown_settings(SPEC, {"match": "left"}) == []  # not a list: the check says so
    same = [{"lefft": "a"}] * 2 + [{"left": "a"}] + [{"lefft": "a"}]
    assert unknown_settings(SPEC, {"match": same}) == [
        "Match, rows 1, 2 and 4: setting 'lefft' is not known (did you mean 'left'?): it has no effect"
    ]
    nested = [P.List("groups", "Groups", fields=[P.Text("name"), P.List("parts", "Parts", fields=[P.Text("part")])])]
    assert unknown_settings(nested, {"groups": [{"name": "a"}, {"parts": [{"part": "x"}, {"prt": "y"}]}]}) == [
        "Groups, row 2, Parts, row 2: setting 'prt' is not known (did you mean 'part'?): it has no effect"
    ]


def test_a_key_in_4000_rows_is_said_once_and_fast():
    rows = [{"lefft": "a", "right": str(n)} for n in range(4000)]
    started = time.perf_counter()
    found = unknown_settings(SPEC, {"match": rows})
    assert time.perf_counter() - started < 0.2
    assert found == ["Match, rows 1, 2, 3 and 3,997 more: setting 'lefft' is not known (did you mean 'left'?): it has no effect"]
    rows = [{f"key{n}": n} for n in range(4000)]  # each row its own key: one message each
    started = time.perf_counter()
    found = unknown_settings(SPEC, {"match": rows, **{f"top{n}": n for n in range(4000)}})
    assert time.perf_counter() - started < 1 and len(found) == 8000


SOURCE = [P.Choice("source", "Read", ["file", "folder"]), P.Bool("file_column", "Add a column with the file name")]


def test_added_columns_are_declared_with_the_action():
    file_column = AddedColumn("File", "text", when={"source": "folder", "file_column": True})
    declared = action("x.y", "X", params=SOURCE, adds_columns=[file_column, AddedColumn("Size", "integer")])(lambda df, p, ctx: df)
    assert declared.adds_columns == (file_column, AddedColumn("Size", "integer", when=None))
    assert action("x.z", "Z")(lambda df, p, ctx: df).adds_columns == ()
    assert AddedColumn.TYPES == ("text", "integer", "number", "date", "datetime", "boolean")


@pytest.mark.parametrize(
    ("column", "message"),
    [
        (AddedColumn("File", "string"), "added column 'File' has the type 'string'; types: text, integer, number"),
        (AddedColumn("File", "text", when={"file_columns": True}), "'file_columns', which is not a setting of the action"),
        (AddedColumn("File", "text", when={"column": "x"}), "'column', which is not a setting"),  # a field of a list isn't one
        (AddedColumn("", "text"), "needs a name"),
        (AddedColumn("File", "text", when=["source"]), "added column 'File': 'when' must be settings and values"),
        (("File", "text"), "adds_columns takes AddedColumn entries"),
    ],
)
def test_added_columns_that_would_describe_the_table_wrongly_are_refused(column, message):
    """As for parameter names: when the action is declared. A setting that is renamed can't leave
    a declaration that names it behind."""
    params = [*SOURCE, P.List("rows", "Rows", fields=[P.Column("column", "Column")])]
    with pytest.raises(ValueError, match=re.escape(message)):
        action("x.y", "X", params=params, adds_columns=[column])

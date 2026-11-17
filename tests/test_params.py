import json
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


def test_a_setting_that_is_on_or_off_reads_texts_and_numbers_for_true_and_false():
    """As Int and Float read texts: "false", "no" and "0" are false (before, any text counted as
    true), also in the rows of a list; nothing or an empty text is the default."""
    spec = [P.Bool("header", "First row has the column names", default=True), P.List("rows", "Rows", fields=[P.Bool("flag", "Flag")])]
    vc = ValidationContext()
    read = [(True, True), (False, False), ("true", True), (" Yes ", True), ("1", True), ("FALSE", False), ("no", False)]
    read += [(" 0", False), (1, True), (0, False), (1.0, True), (0.0, False)]  # a list: 1 == 1.0 == True as keys
    for stored, value in read:
        params = resolve(spec, {"header": stored, "rows": [{"flag": stored}]})
        assert (params.header, params.rows[0].flag) == (value, value) and isinstance(params.header, bool), stored
        assert check(spec, params, vc) == [], stored
    for stored in (None, ""):
        params = resolve(spec, {"header": stored, "rows": [{"flag": stored}]})
        assert (params.header, params.rows[0].flag) == (True, False) and check(spec, params, vc) == []  # the defaults


@pytest.mark.parametrize("stored", ["maybe", " ", "on", "2", 2, -1, 1.5, [], {}, ["x"]])
def test_a_setting_that_is_on_or_off_with_another_value_stops_the_step(stored):
    """The value stays as it is, and the check says so: no guess."""
    spec = [P.Bool("header", "First row has the column names", default=True), P.List("rows", "Rows", fields=[P.Bool("flag", "Flag")])]
    params = resolve(spec, {"header": stored, "rows": [{"flag": stored}]})
    assert params.header == stored and params.rows[0].flag == stored
    shown = json.dumps(stored)
    assert check(spec, params, ValidationContext()) == [
        f"First row has the column names: {shown} is not true or false",
        f"Rows, row 1 – Flag: {shown} is not true or false",
    ]


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
        'Match, row 3: "not a row" is not a row of settings: a row with the defaults is used',
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


def test_unknown_settings_of_something_else_than_a_step(registry):
    """E.g. of a connection: the message says whose settings they are (as the changelog and the
    guide show it)."""
    where = "Connection 'Sales'"
    assert unknown_settings(registry.connectors["sqlserver"].params, {"sever": ".", "database": "x"}, where=where) == [
        "Connection 'Sales': setting 'sever' is not known (did you mean 'server'?): it has no effect"
    ]
    assert unknown_settings(SPEC, {"colum": "B", "_note": 1}, where=where) == [
        "Connection 'Sales': setting 'colum' is not known (did you mean 'column'?): it has no effect"
    ]
    assert unknown_settings(SPEC, {"match": [{"lft": "a"}]}, where=where) == [
        "Connection 'Sales', Match, row 1: setting 'lft' is not known (did you mean 'left'?): it has no effect"
    ]


def test_rows_that_are_no_object_of_settings():
    """A text, a number, null or a list where a row belongs: Tafl uses a row with the defaults
    there (as before), and says so, once for the list."""
    assert unknown_settings(SPEC, {"match": [None]}) == ["Match, row 1: null is not a row of settings: a row with the defaults is used"]
    odd = [["ab", "x"], {"left": "a"}, 5, "a" * 60]
    assert unknown_settings(SPEC, {"match": odd}) == [
        'Match, rows 1, 3 and 4: not rows of settings (the first is ["ab", "x"]): rows with the defaults are used'
    ]
    many = unknown_settings(SPEC, {"match": ["x"] * 4000})
    assert many == ['Match, rows 1, 2, 3 and 3,997 more: not rows of settings (the first is "x"): rows with the defaults are used']
    nested = [P.List("groups", "Groups", fields=[P.List("parts", "Parts", fields=[P.Text("part")])])]
    assert unknown_settings(nested, {"groups": [{"parts": [{"part": "x"}, "y"]}]}) == [
        'Groups, row 1, Parts, row 2: "y" is not a row of settings: a row with the defaults is used'
    ]
    assert resolve(SPEC, {"match": ["x"]}).match == [{"left": "", "right": ""}]  # as before


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
        (AddedColumn("File", "text", when={"source": "folders"}), "source = 'folders', which is not an option of it ('file', 'folder')"),
        (AddedColumn("File", "text", when={"source": ("file", "Folder")}), "source = 'Folder', which is not an option of it"),
        (AddedColumn("File", "text", when={"file_column": "yes"}), "added column 'File' is switched on by file_column = 'yes', which is not true or false"),
        (AddedColumn("File", "text", when={"file_column": 1}), "file_column = 1, which is not true or false"),
        (AddedColumn("File", "text", when={"level": True}), "level = True, which is not an option of it (1, 2)"),  # as JSON: true isn't 1
    ],
)
def test_added_columns_that_would_describe_the_table_wrongly_are_refused(column, message):
    """As for parameter names: when the action is declared. A setting that is renamed, or an
    option that is renamed, can't leave a declaration that names it behind."""
    params = [*SOURCE, P.Choice("level", "Level", [(1, "one"), (2, "two")]), P.List("rows", "Rows", fields=[P.Column("column", "Column")])]
    with pytest.raises(ValueError, match=re.escape(message)):
        action("x.y", "X", params=params, adds_columns=[column])


def test_the_values_that_switch_an_added_column_on():
    """Options of a choice, true or false for a Bool; the values of other kinds stay as they are."""
    params = [*SOURCE, P.Choice("level", "Level", [(1, "one"), (2, "two")]), P.Text("pattern", "File names")]
    when = {"source": ("file", "folder"), "file_column": False, "level": 2, "pattern": 5}
    declared = action("x.y", "X", params=params, adds_columns=[AddedColumn("File", "text", when=when)])(lambda df, p, ctx: df)
    assert declared.adds_columns[0].when == when


def test_every_visible_if_and_when_of_the_built_in_plugins_can_be_met(registry):
    """Each names a setting (in a list: a field of the same row) and values that it can have: an
    option of its choice, or true or false for a Bool. Else it would hide a setting, or switch an
    added column off, for good."""
    checked = []

    def can_be_met(settings, conditions, where):
        found = {setting.key: setting for setting in settings}
        for key, expected in conditions.items():
            assert key in found, (where, key)
            for value in expected if isinstance(expected, (tuple, list, set, frozenset)) else (expected,):
                if isinstance(found[key], P.Choice):
                    options = found[key].values
                    assert any(value == o and isinstance(value, bool) == isinstance(o, bool) for o in options), (where, key, value)
                if isinstance(found[key], P.Bool):
                    assert isinstance(value, bool), (where, key, value)
        checked.append(where)

    owners = [(a.id, a.params, a.adds_columns) for a in registry.actions.values()]
    owners += [(c.id, c.params, ()) for c in registry.connectors.values()]
    for owner, params, added in owners:
        for param in params:
            if param.visible_if:
                can_be_met(params, param.visible_if, f"{owner}.{param.key}")
            for field in param.fields if isinstance(param, P.List) else ():
                if field.visible_if:
                    can_be_met(param.fields, field.visible_if, f"{owner}.{param.key}.{field.key}")
        for column in added:
            if column.when:
                can_be_met(params, column.when, f"{owner} adds {column.name}")
    assert len(checked) > 40 and "rows.filter.conditions.value" in checked and "excel.import adds Sheet" in checked

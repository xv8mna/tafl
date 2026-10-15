import pytest

from etl_tool.api import P, ValidationContext, action
from etl_tool.api.params import check, is_visible, resolve, table_refs

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


def test_reserved_and_duplicate_parameter_names_are_rejected():
    with pytest.raises(ValueError, match="reserved"):
        action("x.y", "X", params=[P.List("items", "Items", fields=[])])
    with pytest.raises(ValueError, match="reserved"):
        action("x.y", "X", params=[P.List("rows", "Rows", fields=[P.Text("keys")])])
    with pytest.raises(ValueError, match="duplicate"):
        action("x.y", "X", params=[P.Text("a"), P.Bool("a")])


def test_labels_default_from_keys():
    assert P.Text("new_name").label == "New name"

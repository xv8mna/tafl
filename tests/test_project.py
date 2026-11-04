import hashlib
import json
import math
import re
import stat
import tempfile
import time

import pytest

from tafl.core import Project, ProjectError


def sample_project() -> Project:
    project = Project(name="Sales")
    project.add_connection("local", "sqlserver", server=r".\SQLEXPRESS", database="Sales")
    orders = project.add_table("Orders")
    project.add_step(orders, "csv.import", {"file": "orders.csv"})
    sort = project.add_step(orders, "rows.sort", {"by": [{"column": "Date", "order": "desc"}]})
    sort.enabled = False
    sort.note = "check later"
    summary = project.add_table("Summary")
    project.add_step(summary, "table.derive", {"table": orders.id})
    return project


def test_save_and_load_round_trip(tmp_path):
    project = sample_project()
    path = project.save(tmp_path / "sales.tafl")
    loaded = Project.load(path)
    assert loaded.to_dict() == project.to_dict()
    assert loaded.directory == tmp_path.resolve()
    assert [p.name for p in tmp_path.iterdir()] == ["sales.tafl"]  # no temp files left


def test_plain_messages_when_the_project_cant_be_saved(tmp_path, monkeypatch):
    project = sample_project()
    path = project.save(tmp_path / "sales.tafl")
    path.chmod(stat.S_IREAD)
    try:
        with pytest.raises(ProjectError, match=r"^sales.tafl is read-only, so it can't be saved$"):
            project.save(path)
    finally:
        path.chmod(stat.S_IREAD | stat.S_IWRITE)
    assert [p.name for p in tmp_path.iterdir()] == ["sales.tafl"]

    def denied(*args, **kwargs):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(tempfile, "mkstemp", denied)
    with pytest.raises(ProjectError, match=r"^No permission to save in .*sub: save the project in another folder$"):
        project.save(tmp_path / "sub" / "sales.tafl")


def test_file_is_readable_json(tmp_path):
    path = sample_project().save(tmp_path / "sales.tafl")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["format"] == "tafl-project"
    assert data["tables"][0]["steps"][1] == {
        "id": "s2",
        "action": "rows.sort",
        "version": 1,
        "enabled": False,
        "note": "check later",
        "params": {"by": [{"column": "Date", "order": "desc"}]},
    }


def test_ids_are_unique_and_tables_found_by_id_or_name():
    project = sample_project()
    assert [t.id for t in project.tables] == ["t1", "t2"]
    assert [s.id for t in project.tables for s in t.steps] == ["s1", "s2", "s3"]
    assert project.table("t2").name == "Summary"
    assert project.table("orders").id == "t1"
    with pytest.raises(ProjectError):
        project.table("nope")
    with pytest.raises(ProjectError):
        project.add_table("ORDERS")


def test_a_lookup_by_id_only_finds_no_table_by_its_name():
    """What a step holds is an id: a table whose name looks like an id is found by that name
    only where a person types a name."""
    project = sample_project()
    named = project.add_table("T7")
    assert project.find_table("t7") is named
    assert project.find_table("t7", by_name=False) is None and project.find_table("Summary", by_name=False) is None
    assert project.find_table(named.id, by_name=False) is named


def test_a_step_finds_its_table_by_the_id_or_by_a_name_not_shaped_like_an_id():
    """A step holds the id of the table it uses; a file written by hand may hold its name. A text
    shaped like an id is taken as an id: when that table is gone, a table named like the id is
    not used in its place."""
    project = sample_project()
    named = project.add_table("t7")
    assert project.used_table(named.id) is named and project.used_table("t1").name == "Orders"
    assert project.used_table("t7") is None and project.used_table("t07") is None
    assert project.used_table("summary").name == "Summary" and project.used_table("T7") is named
    assert project.used_table("Gone") is None


@pytest.mark.parametrize(
    ("action", "params"),
    [
        ("table.derive", {"table": "t2"}),
        ("table.append", {"tables": ["t1", "t2"]}),
        ("flow.skip_if_empty", {"which": "this", "table": "t2"}),  # hidden until "which" is "other"
        ("missing.plugin", {"rows": [{"other": "t2"}]}),  # an action that no plugin here provides
    ],
)
def test_a_new_table_never_gets_the_id_of_a_deleted_table_that_a_step_names(action, params):
    """A step whose table was deleted says that the table doesn't exist. A new table with that id
    (New table, Duplicate, a sheet, New table from this one) would be used by it without a word."""
    project = Project()
    first, deleted = project.add_table("First"), project.add_table("Deleted")
    project.add_step(first, action, params)
    project.tables.remove(deleted)
    assert project.add_table("New").id == "t3"
    assert project.add_table("Next").id == "t4"


@pytest.mark.parametrize("digits", [4000, 4301, 5000])
def test_new_tables_beside_a_setting_of_t_and_thousands_of_digits(digits):
    """A text setting can be "t" and thousands of digits, e.g. a code to find (Python turns no
    text of more than 4300 digits into a number). New tables still get ids, and different ones:
    the id after 4000 nines is longer than the numbers that count. Only ASCII digits count."""
    project = Project()
    first = project.add_table("First")
    project.add_step(first, "text.replace", {"replacements": [{"find": "t" + "9" * digits}, {"find": "t7"}, {"find": "t٩"}]})
    ids = [project.add_table(name).id for name in ("A", "B", "C")]
    assert ids == ([f"t{10**4000 + n}" for n in range(3)] if digits == 4000 else ["t8", "t9", "t10"])


def test_duplicate_or_missing_ids_are_repaired():
    data = sample_project().to_dict()
    data["tables"][1]["id"] = "t1"
    data["tables"][1]["steps"][0]["id"] = ""
    project = Project.from_dict(data)
    ids = [t.id for t in project.tables] + [s.id for t in project.tables for s in t.steps]
    assert len(ids) == len(set(ids)) and all(ids)


def test_new_ids_count_on_from_the_highest_number_of_their_letter():
    steps = [{"action": "a", "id": "s007"}, {"action": "b"}, {"action": "c", "id": "t12"}]
    tables = [
        {"name": "T", "id": "x9", "group": "g3", "steps": steps},
        {"name": "U", "group": "g9", "steps": [{"action": "d", "id": "s007"}, {"action": "e", "id": "g"}]},
    ]
    groups = [{"name": "A", "id": "g3"}, {"name": "B"}, {"name": "C", "id": "g3"}]
    project = Project.from_dict({"format": "tafl-project", "groups": groups, "tables": tables})
    assert [g.id for g in project.groups] == ["g3", "g4", "g5"]
    assert [(t.id, t.group) for t in project.tables] == [("x9", "g3"), ("t13", "")]
    assert [s.id for t in project.tables for s in t.steps] == ["s007", "s8", "t12", "s9", "g"]


def test_steps_without_an_id_take_about_as_long_as_steps_with_one():
    """20,000 steps without an id, a file of 332 KB, once took three minutes."""

    def best_time(steps):  # of some tries: a test run can be busy for a moment
        data = {"format": "tafl-project", "tables": [{"name": "T", "steps": steps}]}
        times = []
        for _ in range(3):
            started = time.perf_counter()
            project = Project.from_dict(data)
            times.append(time.perf_counter() - started)
        return min(times), project

    with_ids, _ = best_time([{"action": "x", "id": f"s{n}"} for n in range(20_000)])
    without_ids, project = best_time([{"action": "x"} for _ in range(20_000)])
    assert project.tables[0].steps[-1].id == "s20000"
    assert without_ids < 10 * with_ids


def test_a_value_that_a_file_can_not_hold_is_refused_and_the_file_stays(tmp_path):
    project = sample_project()
    path = project.save(tmp_path / "project.tafl")
    before = path.read_bytes()
    loop = []
    loop.append(loop)
    for value in (object(), b"bytes", loop):
        project.tables[0].filters = [{"column": "City", "values": [value]}]
        with pytest.raises(ProjectError, match="holds a value that can't be stored in its file"):
            project.save()
        assert path.read_bytes() == before and list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize("value", [math.inf, -math.inf, math.nan])
def test_a_map_position_that_is_not_a_finite_number_is_refused_and_the_file_stays(tmp_path, value):
    project = sample_project()
    project.add_group("G")
    path = project.save(tmp_path / "project.tafl")
    before = path.read_bytes()
    for item in (project.tables[0], project.groups[0]):
        item.position = (value, 0.0)
        with pytest.raises(ProjectError, match="holds a value that can't be stored in its file"):
            project.save()
        assert path.read_bytes() == before and list(tmp_path.iterdir()) == [path]
        item.position = None


@pytest.mark.parametrize("value", [math.inf, -math.inf, math.nan])
def test_a_stored_map_position_that_is_not_a_finite_number_counts_as_not_set(tmp_path, value):
    project = sample_project()
    project.add_group("G")
    project.tables[1].position = (10.0, 20.0)
    data = project.to_dict()
    data["tables"][0]["position"] = [value, 0]
    data["groups"][0]["position"] = [0, value]
    path = tmp_path / "project.tafl"
    path.write_text(json.dumps(data), encoding="utf-8")
    loaded = Project.load(path)
    assert loaded.tables[0].position is None and loaded.groups[0].position is None
    assert Project.load(loaded.save()).tables[1].position == (10.0, 20.0)


def test_invalid_files_are_rejected(tmp_path):
    other = tmp_path / "other.json"
    other.write_text('{"hello": 1}', encoding="utf-8")
    with pytest.raises(ProjectError, match="not a project file"):
        Project.load(other)
    damaged = tmp_path / "damaged.tafl"
    damaged.write_text('{"format": "tafl-project", ', encoding="utf-8")
    with pytest.raises(ProjectError, match="damaged"):
        Project.load(damaged)
    newer = tmp_path / "newer.tafl"
    newer.write_text('{"format": "tafl-project", "format_version": 99}', encoding="utf-8")
    with pytest.raises(ProjectError, match="newer"):
        Project.load(newer)


# Valid JSON, but not what a project has: what the message says, and the change of the sample
# project (or the whole content) that causes it.
WRONG_CONTENT = [
    ('it holds [{"format": "tafl-project"}] instead of a project', [{"format": "tafl-project"}]),
    ('it holds "tafl-project" instead of a project', "tafl-project"),
    ('"tables" is 5 instead of a list', {"format": "tafl-project", "tables": 5}),
    ('"tables" is {"Orders": {}} instead of a list', {"format": "tafl-project", "tables": {"Orders": {}}}),
    ('"format_version" is "new" instead of a whole number', {"format": "tafl-project", "format_version": "new"}),
    ('table 3 is "Totals" instead of an object', lambda d: d["tables"].append("Totals")),
    ('table 2 has no "name"', lambda d: d["tables"][1].pop("name")),
    ('"steps" of table "Orders" is true instead of a list', lambda d: d["tables"][0].update(steps=True)),
    ('the "filters" of table "Orders" are 7 instead of a list', lambda d: d["tables"][0].update(filters=7)),
    ('step 1 of table "Orders" is null instead of an object', lambda d: d["tables"][0]["steps"].insert(0, None)),
    ('step 2 of table "Orders" has no "action"', lambda d: d["tables"][0]["steps"][1].pop("action")),
    ('the "params" of step 1 of table "Summary" are [1, 2] instead of an object',
     lambda d: d["tables"][1]["steps"][0].update(params=[1, 2])),
    ('the "version" of step 1 of table "Summary" is "2.1" instead of a whole number',
     lambda d: d["tables"][1]["steps"][0].update(version="2.1")),
    ('connection "local" has no "type"', lambda d: d["connections"][0].pop("type")),
    ('the "settings" of connection "local" are "x" instead of an object', lambda d: d["connections"][0].update(settings="x")),
    ('group 1 is ["Group"] instead of an object', lambda d: d.update(groups=[["Group"]])),
    ('"parameters" is "Day" instead of a list', lambda d: d.update(parameters="Day")),
    ('parameter 1 has no "name"', lambda d: d.update(parameters=[{"value": "1"}])),
]  # fmt: skip


@pytest.mark.parametrize("problem, change", WRONG_CONTENT)
def test_a_project_file_with_wrong_content_says_what_is_wrong(tmp_path, problem, change):
    data = change
    if callable(change):
        data = sample_project().to_dict()
        change(data)
    path = tmp_path / "broken.tafl"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ProjectError) as caught:
        Project.load(path)
    assert str(caught.value) == f"The project file broken.tafl is damaged: {problem}"


def test_binary_or_too_deeply_nested_files_are_refused(tmp_path):
    path = tmp_path / "picture.tafl"
    path.write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\xff\xfe")
    with pytest.raises(ProjectError, match="picture.tafl can't be read: it is not UTF-8 text"):
        Project.load(path)
    deep = tmp_path / "deep.tafl"
    deep.write_text("[" * 100_000 + "]" * 100_000, encoding="utf-8")
    with pytest.raises(ProjectError, match="deep.tafl is damaged"):
        Project.load(deep)


def nested(levels: int, inner=1):
    for _ in range(levels):
        inner = [inner]
    return inner


NESTED_PLACES = [
    pytest.param(lambda data, value: data["tables"][0].update(filters=[{"column": "A", "values": value}]), id="filter values"),
    pytest.param(lambda data, value: data["tables"][0].update(filters=[{"column": value}]), id="filter column"),
    pytest.param(lambda data, value: data["tables"][0].update(filters=[{"column": "A", "options": {"x": value}}]), id="filter options"),
    pytest.param(lambda data, value: data["tables"][0]["steps"][0]["params"].update(file=value), id="step setting"),
    pytest.param(lambda data, value: data["tables"][0].update(note=value), id="note"),
    pytest.param(lambda data, value: data.update(other={"a": [{"b": value}]}), id="elsewhere"),
]


@pytest.mark.parametrize("change", NESTED_PLACES)
@pytest.mark.parametrize("levels", [150, 500, 5000])
def test_data_nested_too_deeply_is_refused(tmp_path, change, levels):
    """A copy of the project recurses into all of it, so at some hundred levels it stopped with
    a RecursionError on each calculation. No project needs more than a few levels."""
    data = sample_project().to_dict()
    change(data, "VALUE")
    path = tmp_path / "deep.tafl"
    path.write_text(json.dumps(data).replace('"VALUE"', "[" * levels + "1" + "]" * levels), encoding="utf-8")
    with pytest.raises(ProjectError) as caught:
        Project.load(path)
    assert str(caught.value) == "The project file deep.tafl is damaged: its data is nested too deeply"


def test_data_nested_a_few_levels_opens(tmp_path):
    data = sample_project().to_dict()
    data["tables"][0]["filters"] = [{"column": "A", "values": nested(50)}]
    path = tmp_path / "nested.tafl"
    path.write_text(json.dumps(data), encoding="utf-8")
    assert Project.load(path).tables[0].filters == [{"column": "A", "values": nested(50)}]


def test_a_project_file_with_a_byte_order_mark_opens(tmp_path):
    """Some editors (e.g. Notepad of older Windows versions) write UTF-8 with a BOM."""
    data = sample_project().to_dict()
    path = tmp_path / "edited.tafl"
    path.write_bytes(b"\xef\xbb\xbf" + json.dumps(data).encode("utf-8"))
    assert Project.load(path).to_dict() == data


def test_half_of_a_character_from_another_program_becomes_a_replacement_character(tmp_path):
    """Another program may write a lone surrogate ("\\ud800", e.g. from a text cut in the middle
    of a character): the rest of the text stays, and the project can be saved again."""
    data = sample_project().to_dict()
    steps = data["tables"][0]["steps"]
    steps[1]["note"] = "check \ud800 later"
    steps[0]["params"] = {"file": "orders\udc00.csv", "sheet\udbff": "a \U0001f600 b"}
    data["connections"][0]["settings"]["database"] = r"C:\ud800\Sales"  # a backslash and "ud800": no surrogate
    path = tmp_path / "other.tafl"
    path.write_text(json.dumps(data), encoding="utf-8")  # as \ud800, \udc00, \udbff and \ud83d\ude00
    project = Project.load(path)
    assert project.tables[0].steps[1].note == "check \ufffd later"
    assert project.tables[0].steps[0].params == {"file": "orders\ufffd.csv", "sheet\ufffd": "a \U0001f600 b"}
    assert project.connections[0].settings["database"] == r"C:\ud800\Sales"

    project.save()
    assert Project.load(path).to_dict() == project.to_dict()
    assert "check \ufffd later" in path.read_text(encoding="utf-8")


@pytest.mark.parametrize("half", ["\ud800", "\ud9ff", "\udbff", "\udc00"])
@pytest.mark.parametrize("case", ["lower", "upper"])
def test_each_half_is_found_alone_in_either_case(tmp_path, half, case):
    """JSON allows \\uDC00 as well as \\udc00, and a file may hold no other escape: a text cut in
    the middle of an emoji leaves only a \\ud83d."""
    data = sample_project().to_dict()
    data["tables"][0]["steps"][0]["params"]["file"] = f"orders{half}.csv"
    text = json.dumps(data)
    if case == "upper":
        text = re.sub(r"\\u(d[0-9a-f]{3})", lambda m: "\\u" + m[1].upper(), text)
    assert len(re.findall(r"(?i)\\ud[89a-f]", text)) == 1
    path = tmp_path / "half.tafl"
    path.write_text(text, encoding="utf-8")
    project = Project.load(path)
    assert project.tables[0].steps[0].params["file"] == "orders\ufffd.csv"
    project.save()


def test_deep_data_with_half_of_a_character_is_refused_as_damaged(tmp_path):
    """The replacement recurses, so it comes after the check of the nesting."""
    data = sample_project().to_dict()
    data["tables"][0]["note"] = "VALUE"
    path = tmp_path / "deep.tafl"
    path.write_text(json.dumps(data).replace('"VALUE"', "[" * 5000 + '"\\ud800"' + "]" * 5000), encoding="utf-8")
    with pytest.raises(ProjectError, match="nested too deeply"):
        Project.load(path)


def test_a_file_of_another_format_is_refused(tmp_path):
    other = tmp_path / "other.tafl"
    data = sample_project().to_dict()
    data["format"] = "something-else"
    other.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ProjectError):
        Project.load(other)


def test_the_digest_is_of_the_bytes_of_the_file(tmp_path):
    project = sample_project()
    assert project.digest == ""  # no file yet
    path = project.save(tmp_path / "sales.tafl")
    assert project.digest == hashlib.sha256(path.read_bytes()).hexdigest()
    assert Project.load(path).digest == project.digest

    path.write_bytes(b"\xef\xbb\xbf" + path.read_bytes())  # the same project in other bytes
    other = Project.load(path)
    assert other == project and other.digest == hashlib.sha256(path.read_bytes()).hexdigest() != project.digest

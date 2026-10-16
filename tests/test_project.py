import json

import pytest

from etl_tool.core import Project, ProjectError


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


def test_duplicate_or_missing_ids_are_repaired():
    data = sample_project().to_dict()
    data["tables"][1]["id"] = "t1"
    data["tables"][1]["steps"][0]["id"] = ""
    project = Project.from_dict(data)
    ids = [t.id for t in project.tables] + [s.id for t in project.tables for s in t.steps]
    assert len(ids) == len(set(ids)) and all(ids)


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


def test_a_project_file_with_a_byte_order_mark_opens(tmp_path):
    """Some editors (e.g. Notepad of older Windows versions) write UTF-8 with a BOM."""
    data = sample_project().to_dict()
    path = tmp_path / "edited.tafl"
    path.write_bytes(b"\xef\xbb\xbf" + json.dumps(data).encode("utf-8"))
    assert Project.load(path).to_dict() == data


def test_a_file_of_another_format_is_refused(tmp_path):
    other = tmp_path / "other.tafl"
    data = sample_project().to_dict()
    data["format"] = "something-else"
    other.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ProjectError):
        Project.load(other)

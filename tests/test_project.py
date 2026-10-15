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


def test_a_file_of_another_format_is_refused(tmp_path):
    other = tmp_path / "other.tafl"
    data = sample_project().to_dict()
    data["format"] = "something-else"
    other.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ProjectError):
        Project.load(other)

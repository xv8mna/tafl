"""The example projects keep working. They are built fresh by their generators, so the tests
don't depend on the files in examples/ (which may have been edited by hand). The last test
checks that the committed examples are what the generator writes."""

import importlib.util
import io
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

from tafl.api import Mode
from tafl.core import Engine, Project

ROOT = Path(__file__).resolve().parents[1]


def generator(path: Path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(("script", "tables"), [("examples/make_examples.py", 3), ("tools/make_big_project.py", 75)])
def test_example_projects_calculate_without_errors(registry, tmp_path, script, tables):
    shutil.copytree(ROOT / "examples" / "data", tmp_path / "data")
    project = Project.load(generator(ROOT / script).build_project().save(tmp_path / "example.tafl"))
    assert len(project.tables) == tables
    results = Engine(project, registry).run(mode=Mode.PREVIEW)  # exports don't write anything
    assert all(result.ok for result in results.values())


def test_the_excel_check_project_calculates(registry, tmp_path):
    shutil.copytree(ROOT / "examples" / "data", tmp_path / "data")
    make = generator(ROOT / "examples" / "make_examples.py")
    project = Project.load(make.build_excel_check().save(tmp_path / "excel-check.tafl"))
    results = Engine(project, registry).run(mode=Mode.PREVIEW)
    assert all(result.ok for result in results.values()) and results[project.tables[0].id].df.height == 161


def test_the_big_example_is_big():
    project = generator(ROOT / "tools" / "make_big_project.py").build_project()
    assert sum(len(t.steps) for t in project.tables) >= 450 and len(project.groups) == 7


def test_the_committed_examples_are_up_to_date(tmp_path):
    """examples/ holds what make_examples.py writes, so the demo opens without changes."""
    committed = {}
    for name in ("demo.tafl", "excel-check.tafl", "data/customers.xlsx", "data/orders.csv"):
        try:
            committed[name] = subprocess.run(
                ["git", "show", f"HEAD:examples/{name}"], cwd=ROOT, capture_output=True, check=True
            ).stdout
        except (OSError, subprocess.CalledProcessError):
            pytest.skip("needs the git repository")
    make = generator(ROOT / "examples" / "make_examples.py")
    (tmp_path / "data").mkdir()
    make.write_customers(tmp_path / "data" / "customers.xlsx")
    make.write_orders(tmp_path / "data" / "orders.csv")
    make.build_project().save(tmp_path / "demo.tafl")
    make.build_excel_check().save(tmp_path / "excel-check.tafl")
    for name, content in committed.items():
        written = (tmp_path / name).read_bytes()
        if name.endswith(".tafl"):  # text: git may store other line endings
            written, content = written.replace(b"\r\n", b"\n"), content.replace(b"\r\n", b"\n")
        elif name.endswith(".xlsx"):  # the zip's compression differs between Python builds
            written, content = _unzipped(written), _unzipped(content)
        assert written == content, f"examples/{name} is out of date: run examples/make_examples.py and commit it"


def _unzipped(data: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        return {name: archive.read(name) for name in archive.namelist()}

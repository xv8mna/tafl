import importlib.util
import json
from pathlib import Path

import openpyxl
import pytest

from etl_tool.cli import main
from etl_tool.core import Parameter, Project

EXAMPLES = Path(__file__).resolve().parents[1] / "examples" / "make_examples.py"


@pytest.fixture
def demo(tmp_path) -> Path:
    """The demo project from examples/, built in a temporary folder."""
    spec = importlib.util.spec_from_file_location("make_examples", EXAMPLES)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    (tmp_path / "data").mkdir()
    module.write_customers(tmp_path / "data" / "customers.xlsx")
    module.write_orders(tmp_path / "data" / "orders.csv")
    return module.build_project().save(tmp_path / "demo.tafl")


def test_run_demo_project(demo, capsys):
    assert main(["run", str(demo)]) == 0
    output = capsys.readouterr().out
    assert "Done in" in output and "Revenue by customer" in output

    revenue = openpyxl.load_workbook(demo.parent / "output" / "revenue.xlsx")["Revenue"]
    header = [c.value for c in revenue[1]]
    assert header == ["Name", "City", "Revenue", "Orders", "Last order", "Share"]
    assert revenue["C2"].number_format == "#,##0.00 €"
    assert revenue["F2"].number_format == "0.0%"
    shares = [row[5].value for row in revenue.iter_rows(min_row=2)]
    assert sum(shares) == pytest.approx(1.0)

    orders_csv = (demo.parent / "output" / "orders.csv").read_text(encoding="utf-8-sig").splitlines()
    assert orders_csv[0] == "OrderNo;CustomerNo;Date;Net;Status;Name;City;Gross;Month;Size;Order of customer"


def test_preview_run_writes_nothing(demo, capsys):
    assert main(["run", str(demo), "--preview"]) == 0
    assert "(runs only when the project is run)" in capsys.readouterr().out
    assert not (demo.parent / "output").exists()


def test_show_a_step(demo, capsys):
    assert main(["show", str(demo), "--table", "Customers", "--step", "3"]) == 0
    output = capsys.readouterr().out
    assert "6 rows × 3 columns" in output
    assert "Bäckerei Weiß" in output  # repeated spaces collapsed
    assert main(["show", str(demo), "--table", "Customers", "--step", "9"]) == 2


def test_failing_project_returns_1(demo, capsys):
    text = demo.read_text(encoding="utf-8").replace("ROUND([Net] * 1.19, 2)", "ROUND([Nett] * 1.19, 2)")
    demo.write_text(text, encoding="utf-8")
    assert main(["run", str(demo)]) == 1
    output = capsys.readouterr().out
    assert "column [Nett] not found (did you mean [Net]?)" in output
    assert "Table 'Orders' has an error" in output  # Revenue by customer depends on Orders


def test_plugins_and_missing_project(capsys, tmp_path):
    assert main(["plugins"]) == 0
    assert "calc.enumerate" in capsys.readouterr().out
    assert main(["run", str(tmp_path / "missing.tafl")]) == 2


def test_tables_that_are_off_are_skipped(demo, capsys):
    data = json.loads(demo.read_text(encoding="utf-8"))
    data["tables"][0]["enabled"] = False  # Customers: Orders uses it, and Revenue uses Orders
    demo.write_text(json.dumps(data), encoding="utf-8")
    assert main(["run", str(demo)]) == 0  # skipped tables are not errors
    output = capsys.readouterr().out
    assert "skipped: turned off" in output
    assert "skipped: uses “Customers”, which is turned off" in output
    assert "skipped: uses “Orders”, which is skipped" in output
    assert not (demo.parent / "output").exists()


def test_parameters_and_set(tmp_path, capsys):
    (tmp_path / "orders_2026-10-01.csv").write_text("Id\n1\n2\n", encoding="utf-8")
    (tmp_path / "orders_2026-10-02.csv").write_text("Id\n1\n2\n3\n", encoding="utf-8")
    project = Project(name="Daily", parameters=[Parameter("Day", "2026-10-01"), Parameter("Next", "={Day} + 1")])
    table = project.add_table("Orders")
    project.add_step(table, "csv.import", {"file": "orders_{Day}.csv"})
    project.add_step(table, "csv.export", {"file": "out_{Next}.csv"})
    path = project.save(tmp_path / "daily.tafl")

    assert main(["run", str(path)]) == 0
    output = capsys.readouterr().out
    assert "Parameters\n  {Day} = 2026-10-01\n  {Next} = 2026-10-02\n" in output
    assert (tmp_path / "out_2026-10-02.csv").read_text(encoding="utf-8-sig").splitlines() == ["Id", "1", "2"]

    assert main(["run", str(path), "--set", "day=2026-10-02"]) == 0
    assert "{Day} = 2026-10-02" in capsys.readouterr().out
    assert (tmp_path / "out_2026-10-03.csv").read_text(encoding="utf-8-sig").splitlines() == ["Id", "1", "2", "3"]

    assert main(["show", str(path), "--table", "Orders", "--set", "Day=2026-10-02", "--set", "Next==1 +"]) == 1
    output = capsys.readouterr().out
    assert "{Next} = error: the formula ends too early" in output
    assert "The parameter {Next} has an error" in output  # the export uses it: an error even in a preview

    assert main(["run", str(path), "--set", "Month=10"]) == 2
    assert "The project has no parameter 'Month' (its parameters: Day, Next)" in capsys.readouterr().err
    assert main(["run", str(path), "--set", "Day"]) == 2
    assert "--set needs NAME=VALUE, not 'Day'" in capsys.readouterr().err
    assert main(["run", str(path), "--set", "Day=2026-09-30"]) == 1  # no such file

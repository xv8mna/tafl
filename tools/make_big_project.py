"""A big project for checking the map: 75 tables, about 460 steps, 7 groups, links between groups.

    .venv\\Scripts\\python tools\\make_big_project.py            (examples\\big500.tafl)
    .venv\\Scripts\\python tools\\make_big_project.py %TEMP%\\big  (elsewhere, with a copy of the data)

Then open big500.tafl in the app (or run it with `tafl run ... --preview`).
"""

import random
import shutil
import sys
from pathlib import Path

from etl_tool.core import EXTENSION, Project, Registry

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def build_project() -> Project:
    """The project; it reads data\\orders.csv and data\\customers.xlsx next to its file."""
    registry = Registry()
    registry.load_builtin()
    actions = registry.actions
    p = Project(name="Big 500")
    rng = random.Random(7)

    def add(table, action, params):
        p.add_step(table, actions[action], params)

    def padding(table, n):
        for i in range(n):
            add(table, "calc.columns", {"columns": [{"name": f"X{len(table.steps)}_{i}", "formula": "1"}]})

    def group(name, color, tables):
        g = p.add_group(name, color)
        for t in tables:
            t.group = g.id

    sources = []
    for year in range(2019, 2027):
        t = p.add_table(f"Orders {year}")
        add(t, "csv.import", {"file": "data/orders.csv"})
        add(t, "columns.rename", {"columns": [
            {"column": "Kundennr", "new_name": "CustomerNo"}, {"column": "Netto", "new_name": "Net"},
        ]})
        padding(t, 3)
        sources.append(t)
    customers = []
    for country in ("DE", "AT", "CH", "NL"):
        t = p.add_table(f"Customers {country}")
        add(t, "excel.import", {"file": "data/customers.xlsx", "sheet": "Kunden", "header_row": 3})
        add(t, "columns.rename", {"columns": [{"column": "Kundennr", "new_name": "CustomerNo"}]})
        padding(t, 2)
        customers.append(t)
    group("Sources", "blue", sources + customers)

    topics: dict[str, list] = {}
    for name, color in (("Sales", "green"), ("Customers", "orange"), ("Products", "purple"), ("Finance", "teal"), ("Logistics", "red")):
        base = p.add_table(f"{name} base")
        add(base, "table.derive", {"table": rng.choice(sources).id})
        add(base, "table.append", {"tables": [s.id for s in rng.sample(sources, 2)]})
        add(base, "table.merge", {
            "other": rng.choice(customers).id,
            "match": [{"column": "CustomerNo", "other_column": "CustomerNo"}],
            "columns": ["Name"],
        })
        padding(base, 3)
        tables = [base]
        for i in range(9):
            t = p.add_table(f"{name} {i + 1}")
            add(t, "table.derive", {"table": rng.choice(tables).id})
            padding(t, rng.randint(4, 8))
            if topics and rng.random() < 0.35:  # uses a table of another group
                other = rng.choice(rng.choice(list(topics.values())))
                add(t, "table.merge", {
                    "other": other.id,
                    "match": [{"column": "Bestellnr", "other_column": "Bestellnr"}],
                    "columns": ["Net"],
                })
            tables.append(t)
        topics[name] = tables
        group(name, color, tables)

    exports = []
    for i in range(10):
        t = p.add_table(f"Export {i + 1}")
        add(t, "table.derive", {"table": rng.choice(rng.choice(list(topics.values()))).id})
        add(t, "calc.aggregate", {
            "group_by": ["CustomerNo"],
            "calculations": [{"function": "sum", "column": "Net", "name": "Revenue"}],
        })
        add(t, "rows.sort", {"by": [{"column": "Revenue", "order": "desc"}]})
        add(t, "csv.export", {"file": f"output/export_{i + 1}.csv"})
        exports.append(t)
    group("Exports", "grey", exports)

    for i in range(3):  # some tables without a group
        t = p.add_table(f"Check {i + 1}")
        add(t, "table.derive", {"table": rng.choice(exports).id})
        padding(t, 3)
    return p


def main() -> None:
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else EXAMPLES
    target.mkdir(parents=True, exist_ok=True)
    if target.resolve() != EXAMPLES:
        shutil.copytree(EXAMPLES / "data", target / "data", dirs_exist_ok=True)
    project = build_project()
    steps = sum(len(t.steps) for t in project.tables)
    print(project.save(target / f"big500{EXTENSION}"), len(project.tables), "tables", steps, "steps")


if __name__ == "__main__":
    main()

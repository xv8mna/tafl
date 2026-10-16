"""Creates the demo data and project in this folder.

    .venv\\Scripts\\python examples\\make_examples.py
    .venv\\Scripts\\tafl run examples\\demo.tafl
"""

from __future__ import annotations

import datetime as dt
import random
from pathlib import Path

import xlsxwriter

from tafl.core import EXTENSION, Project, Registry

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"

CUSTOMERS = [
    ("00017", "  Müller GmbH ", "Dresden"),
    ("00023", "Schröder & Söhne", "Hamburg"),
    ("00042", "Bäckerei   Weiß", "München"),
    ("00105", "Nord Logistik", "Kiel"),
    ("00230", "Café am Markt", "Köln"),
    ("01001", "Huber KG", "Wien"),
]


def write_customers(path: Path) -> None:
    """An Excel sheet the way people make them: a title, an empty row, then the table."""
    wb = xlsxwriter.Workbook(path)
    wb.set_properties({"created": dt.datetime(2026, 10, 1)})  # the same content on every run
    ws = wb.add_worksheet("Kunden")
    ws.write("A1", "Kundenliste (Stand 01.10.2026)", wb.add_format({"bold": True, "font_size": 14}))
    ws.write_row("A3", ["Kundennr", "Name", "Ort"], wb.add_format({"bold": True}))
    for row, customer in enumerate(CUSTOMERS, start=3):
        ws.write_row(row, 0, customer)
    wb.close()


def write_orders(path: Path) -> None:
    """A CSV export as German software writes it: semicolons, decimal commas, dd.mm.yyyy, ANSI."""
    rng = random.Random(42)
    lines = ["Bestellnr;Kundennr;Datum;Netto;Status"]
    start = dt.date(2026, 1, 1)
    for number in range(1, 201):
        customer = rng.choice(CUSTOMERS)[0] if number % 37 else "09999"  # a few unknown customers
        day = start + dt.timedelta(days=rng.randrange(270))
        net = f"{rng.uniform(20, 2500):.2f}".replace(".", ",")
        status = rng.choice(["offen", "geliefert", "geliefert", "geliefert", "storniert"])
        lines.append(f"{10000 + number};{customer};{day:%d.%m.%Y};{net};{status}")
    path.write_bytes(("\r\n".join(lines) + "\r\n").encode("cp1252"))


def build_project() -> Project:
    registry = Registry()
    registry.load_builtin()
    project = Project(name="Demo")

    def add(table, action_id, params):
        project.add_step(table, registry.actions[action_id], params)  # records the action's version

    customers = project.add_table("Customers")
    add(customers, "excel.import", {"file": "data/customers.xlsx", "sheet": "Kunden", "header_row": 3})
    add(customers, "columns.rename", {"columns": [
        {"column": "Kundennr", "new_name": "CustomerNo"},
        {"column": "Ort", "new_name": "City"},
    ]})
    add(customers, "text.clean", {"columns": ["Name"], "collapse_spaces": True})

    orders = project.add_table("Orders")
    add(orders, "csv.import", {"file": "data/orders.csv"})
    add(orders, "columns.rename", {"columns": [
        {"column": "Bestellnr", "new_name": "OrderNo"},
        {"column": "Kundennr", "new_name": "CustomerNo"},
        {"column": "Datum", "new_name": "Date"},
        {"column": "Netto", "new_name": "Net"},
    ]})
    add(orders, "rows.filter", {"conditions": [{"column": "Status", "operator": "!=", "value": "storniert"}]})
    add(orders, "table.merge", {
        "other": customers.id,
        "match": [{"column": "CustomerNo", "other_column": "CustomerNo"}],
        "columns": ["Name", "City"],
    })
    add(orders, "text.replace", {"columns": ["Name"], "replacements": [{"find": "", "replace": "(unknown customer)"}]})
    add(orders, "calc.columns", {"columns": [
        {"name": "Gross", "formula": "ROUND([Net] * 1.19, 2)"},
        {"name": "Month", "formula": "STRFTIME([Date], '%Y-%m')"},
        {"name": "Size", "formula": "IIF([Net] >= 1000, 'large', 'normal')"},
    ]})
    add(orders, "calc.enumerate", {
        "name": "Order of customer",
        "group_by": ["CustomerNo"],
        "order_by": [{"column": "Date"}],
    })
    add(orders, "rows.sort", {"by": [{"column": "Name"}, {"column": "Date"}]})
    add(orders, "excel.export", {
        "file": "output/orders.xlsx",
        "sheet": "Orders",
        "table_style": "Table Style Medium 2",
        "column_formats": [{"column": "Gross", "format": "#,##0.00 €"}, {"column": "Net", "format": "#,##0.00 €"}],
    })
    add(orders, "csv.export", {"file": "output/orders.csv"})

    revenue = project.add_table("Revenue by customer")
    add(revenue, "table.derive", {"table": orders.id})
    add(revenue, "calc.aggregate", {
        "group_by": ["Name", "City"],
        "calculations": [
            {"function": "sum", "column": "Gross", "name": "Revenue"},
            {"function": "count_rows", "name": "Orders"},
            {"function": "max", "column": "Date", "name": "Last order"},
        ],
    })
    add(revenue, "calc.columns", {"columns": [
        {"name": "Share", "formula": "[Revenue] / SUM([Revenue]) OVER ()"},
    ]})
    add(revenue, "rows.sort", {"by": [{"column": "Revenue", "order": "desc"}]})
    add(revenue, "excel.export", {
        "file": "output/revenue.xlsx",
        "sheet": "Revenue",
        "column_formats": [{"column": "Revenue", "format": "#,##0.00 €"}, {"column": "Share", "format": "0.0%"}],
    })
    return project


def build_excel_check() -> Project:
    """For checking "Replace only this sheet" in Excel (docs/excel-check.md): the orders above a
    limit go into sheet Data of a workbook that Excel can build a report on."""
    registry = Registry()
    registry.load_builtin()
    project = Project(name="Excel check")
    data = project.add_table("Data")
    for action_id, params in (
        ("csv.import", {"file": "data/orders.csv"}),
        ("columns.rename", {"columns": [
            {"column": "Bestellnr", "new_name": "OrderNo"},
            {"column": "Kundennr", "new_name": "CustomerNo"},
            {"column": "Datum", "new_name": "Date"},
            {"column": "Netto", "new_name": "Net"},
        ]}),
        ("rows.filter", {"conditions": [{"column": "Net", "operator": ">", "value": "500"}]}),
        ("excel.export", {"file": "output/excel-check.xlsx", "sheet": "Data", "existing": "sheet",
                          "table_style": "Table Style Medium 2"}),
    ):  # fmt: skip
        project.add_step(data, registry.actions[action_id], params)
    return project


if __name__ == "__main__":
    DATA.mkdir(exist_ok=True)
    write_customers(DATA / "customers.xlsx")
    write_orders(DATA / "orders.csv")
    path = build_project().save(HERE / f"demo{EXTENSION}")
    print(f"Created {path}")
    path = build_excel_check().save(HERE / f"excel-check{EXTENSION}")
    print(f"Created {path}")

import decimal

import openpyxl
import polars as pl
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from tafl.api import ActionError, Mode
from tafl.api.testing import StubContext

D = decimal.Decimal


def test_decimals_and_small_types_become_the_usual_ones(act, tmp_path):
    pl.DataFrame(
        {
            "Price": pl.Series([D("1.50"), None], dtype=pl.Decimal(10, 2)),
            "Count": pl.Series([D("3"), D("4")], dtype=pl.Decimal(10, 0)),
            "Small": pl.Series([1, 2], dtype=pl.Int32),
            "Rate": pl.Series([0.5, 0.25], dtype=pl.Float32),
            "Kind": pl.Series(["a", "b"], dtype=pl.Categorical),
        }
    ).write_parquet(tmp_path / "data.parquet")
    df = act("parquet.import", None, {"file": str(tmp_path / "data.parquet")})
    assert df.schema == {
        "Price": pl.Decimal(10, 2),  # exact, as in the file
        "Count": pl.Int64,
        "Small": pl.Int64,
        "Rate": pl.Float64,
        "Kind": pl.String,
    }
    assert df.to_dict(as_series=False) == {
        "Price": [D("1.50"), None],
        "Count": [3, 4],
        "Small": [1, 2],
        "Rate": [0.5, 0.25],
        "Kind": ["a", "b"],
    }


def test_decimals_keep_all_their_digits(act, tmp_path):
    pl.DataFrame(
        {
            "ID": pl.Series([D("12345678901234567890123"), D("12345678901234567890124")], dtype=pl.Decimal(30, 0)),
            "Amount": pl.Series([D("12345678901234.5678"), D("1.1000")], dtype=pl.Decimal(18, 4)),
            "Big": pl.Series([D("9223372036854775807"), D("-9223372036854775808")], dtype=pl.Decimal(19, 0)),
        }
    ).write_parquet(tmp_path / "d.parquet")
    ctx = StubContext(project_dir=tmp_path)
    df = act("parquet.import", None, {"file": "d.parquet"}, ctx)
    assert df.schema == {"ID": pl.Decimal(30, 0), "Amount": pl.Decimal(18, 4), "Big": pl.Int64}
    assert df["ID"].n_unique() == 2  # as decimal numbers both IDs would be 1.2346e22
    assert df["Amount"].to_list() == [D("12345678901234.5678"), D("1.1000")]
    assert df["Big"].to_list() == [2**63 - 1, -(2**63)]
    assert act("rows.dedupe", df, {"columns": ["ID"]}).height == 2
    assert not ctx.warnings

    act("csv.export", df, {"file": "d.csv"}, StubContext(project_dir=tmp_path, mode=Mode.RUN))
    assert (tmp_path / "d.csv").read_text(encoding="utf-8-sig").splitlines() == [
        "ID;Amount;Big",
        "12345678901234567890123;12345678901234,5678;9223372036854775807",
        "12345678901234567890124;1,1000;-9223372036854775808",
    ]


def test_decimals_go_to_excel_with_the_number_format(act, tmp_path):
    prices = pl.Series([D("1.10"), D("19.99")], dtype=pl.Decimal(10, 2))
    pl.DataFrame({"Price": prices}).write_parquet(tmp_path / "p.parquet")
    df = act("parquet.import", None, {"file": str(tmp_path / "p.parquet")})
    act("excel.export", df, {"file": str(tmp_path / "p.xlsx")}, StubContext(mode=Mode.RUN))
    sheet = openpyxl.load_workbook(tmp_path / "p.xlsx").active
    assert [sheet["A2"].value, sheet["A3"].value] == [1.1, 19.99]
    assert sheet["A2"].number_format == "#,##0.00"


def test_files_of_a_folder_get_one_type(act, tmp_path):
    for name, number in (("a", "1"), ("b", "12345678901234567890123")):  # Int64 holds only the first
        pl.DataFrame({"Nr": pl.Series([D(number)], dtype=pl.Decimal(25, 0))}).write_parquet(tmp_path / f"{name}.parquet")
    out = act("parquet.import", None, {"source": "folder", "folder": str(tmp_path), "pattern": "*.parquet"})
    assert out["Nr"].dtype == pl.Decimal(25, 0)
    assert sorted(out["Nr"].to_list()) == [D("1"), D("12345678901234567890123")]
    (tmp_path / "b.parquet").unlink()
    alone = act("parquet.import", None, {"source": "folder", "folder": str(tmp_path), "pattern": "*.parquet"})
    assert alone["Nr"].dtype == pl.Int64


def test_numbers_of_more_than_38_digits_become_text(act, tmp_path):
    long = D("1234567890" * 4 + "1")  # 41 digits
    table = pa.table(
        {
            "Long": pa.array([long, D("1"), None], type=pa.decimal256(45, 0)),
            "Short": pa.array([D("1.50"), D("-2.25"), None], type=pa.decimal256(40, 2)),
        }
    )
    pq.write_table(table, tmp_path / "wide.parquet")
    ctx = StubContext()
    df = act("parquet.import", None, {"file": str(tmp_path / "wide.parquet")}, ctx)
    assert df.schema == {"Long": pl.String, "Short": pl.Decimal(38, 2)}
    assert df.to_dict(as_series=False) == {"Long": [str(long), "1", None], "Short": [D("1.50"), D("-2.25"), None]}
    assert ctx.warnings == [f"Long becomes text: its numbers are too long to be kept exactly, e.g. '{long}'"]


def test_a_file_that_isnt_parquet(act, tmp_path):
    (tmp_path / "broken.parquet").write_text("no parquet", encoding="utf-8")
    with pytest.raises(ActionError, match=r"^Could not read broken\.parquet: "):
        act("parquet.import", None, {"file": str(tmp_path / "broken.parquet")})

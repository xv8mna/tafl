"""Importing all files of a folder (and its subfolders), several Excel sheets, Parquet files."""

import datetime as dt
import re
import subprocess
import sys
import time
from decimal import Decimal

import polars as pl
import pytest
import xlsxwriter

from tafl.api import ActionError, Mode, SkipRest, detection, dtypes, find_files, sources_fingerprint
from tafl.api.files import in_folder
from tafl.api.params import resolve
from tafl.api.testing import StubContext
from tafl.core import Engine, Project, Status


@pytest.fixture
def lake(tmp_path):
    """Daily exports in month folders, as a simple data lake."""
    jan, feb = tmp_path / "2025" / "01", tmp_path / "2025" / "02"
    jan.mkdir(parents=True)
    feb.mkdir(parents=True)
    (jan / "orders_2025-01-02.csv").write_bytes("Nr;Kunde;Netto;Datum\n1;Müller;1,5;02.01.2025\n2;Bäcker;20;02.01.2025\n".encode("cp1252"))
    (jan / "orders_2025-01-10.csv").write_text("Nr,Kunde,Netto,Datum,Notiz\n3,Huber,7,10.01.2025,eilig\n", encoding="utf-8")
    (feb / "orders_2025-02-01.csv").write_text("Nr;Kunde;Netto;Datum\n4;Weiß;3,25;01.02.2025\n", encoding="utf-8")
    (tmp_path / "readme.txt").write_text("not an export", encoding="utf-8")
    (jan / "~$orders_2025-01-02.csv").write_text("lock", encoding="utf-8")
    return tmp_path


def folder(path, **options):
    return {"source": "folder", "folder": str(path), **options}


def test_find_files_matches_names_in_natural_order(tmp_path):
    for name in ("day10.csv", "day2.csv", "Day1.CSV", "notes.txt", "~$day3.csv"):
        (tmp_path / name).write_text("x", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "day4.csv").write_text("x", encoding="utf-8")
    assert [p.name for p in find_files(tmp_path, "*.csv")] == ["Day1.CSV", "day2.csv", "day10.csv"]
    assert [p.name for p in find_files(tmp_path, "*.csv; *.txt", subfolders=True)] == [
        "Day1.CSV", "day2.csv", "day10.csv", "notes.txt", "day4.csv",
    ]


def test_all_csv_files_of_a_data_lake(act, lake):
    out = act("csv.import", None, folder(lake, pattern="orders_*.csv", subfolders=True, path_column=True))
    assert out.columns == ["File", "Path", "Nr", "Kunde", "Netto", "Datum", "Notiz"]
    assert out["File"].to_list() == ["orders_2025-01-02.csv"] * 2 + ["orders_2025-01-10.csv", "orders_2025-02-01.csv"]
    assert out["Path"][3] == str(lake.joinpath("2025", "02", "orders_2025-02-01.csv").relative_to(lake))
    assert out["Kunde"].to_list() == ["Müller", "Bäcker", "Huber", "Weiß"]  # each file's own encoding
    assert out["Netto"].to_list() == [1.5, 20.0, 7.0, 3.25]  # whole numbers in one file, decimals in the others
    assert out["Datum"][0] == dt.date(2025, 1, 2)
    assert out["Notiz"].to_list() == [None, None, "eilig", None]  # only one file has it


def test_each_file_of_a_folder_is_read_with_its_own_formats(act, tmp_path):
    """The numbers and dates of all files were detected together: 1,50 in north.csv and 1.50 in
    south.csv gave a text column, and so did dates like 31.01.2026 in one file and 2026-02-01 in
    another, without a message. Each file is detected on its own now, and the files are put
    together as Append puts tables together."""
    (tmp_path / "north.csv").write_text("Item;Qty;Price;Day;At\nA;3;1,50;31.01.2026;31.01.2026 08:30\n", encoding="utf-8")
    (tmp_path / "south.csv").write_text("Item,Qty,Price,Day,At\nB,1,1.50,2026-02-01,2026-02-01T09:15:00\n", encoding="utf-8")
    ctx = StubContext()
    out = act("csv.import", None, folder(tmp_path), ctx)
    assert out.schema == {"File": pl.String, "Item": pl.String, "Qty": pl.Int64, "Price": pl.Float64, "Day": pl.Date, "At": pl.Datetime("us")}
    rows = [("A", 3, 1.5, dt.date(2026, 1, 31), dt.datetime(2026, 1, 31, 8, 30)), ("B", 1, 1.5, dt.date(2026, 2, 1), dt.datetime(2026, 2, 1, 9, 15))]
    assert out.drop("File").rows() == rows
    assert ctx.warnings == []
    log = "2 files; encodings utf-8; delimiters comma, semicolon; decimal comma in north.csv, decimal point in south.csv"
    assert ctx.messages[-1] == ("info", log)
    # files with the same separators and other date formats (these files are looked at together first)
    dates = tmp_path / "dates"
    dates.mkdir()
    (dates / "a.csv").write_text("Item;Day;At\nA;31.01.2026;31.01.2026 08:30\n", encoding="utf-8")
    (dates / "b.csv").write_text("Item;Day;At\nB;2026-02-01;2026-02-01T09:15:00\n", encoding="utf-8")
    ctx = StubContext()
    out = act("csv.import", None, folder(dates, file_column=False), ctx)
    assert out.schema == {"Item": pl.String, "Day": pl.Date, "At": pl.Datetime("us")} and not ctx.warnings
    assert out.rows() == [(row[0], *row[3:]) for row in rows]


def test_the_log_names_the_decimal_separator_of_each_file(act, tmp_path):
    """When the files have different decimal separators, the log names the one of each file, as the
    files are read (1.000 is 1000 in a.csv, with semicolons, and 1 in b.csv); for many files their
    number and up to three names. When all files have the same one, it names that one and the
    columns with the other one. It named one separator for such files, and "decimal comma in 2
    files" for files that all have a decimal comma."""

    def log(name, files):
        (tmp_path / name).mkdir()
        for file, text in files.items():
            (tmp_path / name / file).write_text(text, encoding="utf-8")
        ctx = StubContext()
        out = act("csv.import", None, folder(tmp_path / name, file_column=False), ctx)
        return out, ctx.messages[-1][1].split("; ")[-1]

    out, decimal = log("two", {"a.csv": "Item;W\nA;1.000\n", "b.csv": "Item,W\nB,1.000\n"})
    assert out["W"].to_list() == [1000.0, 1.0] and decimal == "decimal comma in a.csv, decimal point in b.csv"
    files = {f"d{i}.csv": "Item;W\nA;1,5\n" for i in range(5)} | {"e.csv": "Item,W\nB,2.5\n"}
    assert log("many", files)[1] == "decimal comma in 5 files (d0.csv, d1.csv, d2.csv, …), decimal point in e.csv"
    out, decimal = log("same", {"a.csv": "Nr;Betrag;P\n1;2,5;0.500\n", "b.csv": "Nr;Betrag;P\n2;3,75;1,5\n"})
    assert out["P"].to_list() == [0.5, 1.5] and decimal == "decimal comma (decimal point in P)"


def test_a_column_of_other_types_in_the_files_of_a_folder(act, tmp_path):
    """A column of a date in one file and a number in another becomes text, with Append's warning;
    one that is text in a file (a code with a leading zero, n/a) stays text without a message, as
    in one file. Such a column has the text of the files: 1,50, not 1.5."""
    (tmp_path / "a.csv").write_text("Nr;When;Code;Amount;Rate\n1;31.01.2026;0815;1,50;0,5\n", encoding="utf-8")
    (tmp_path / "b.csv").write_text("Nr,When,Code,Amount,Rate\n2,7,17,n/a,0.25\n", encoding="utf-8")  # a decimal point
    ctx = StubContext()
    out = act("csv.import", None, folder(tmp_path, file_column=False), ctx)
    assert out.to_dict(as_series=False) == {
        "Nr": [1, 2], "When": ["31.01.2026", "7"], "Code": ["0815", "17"], "Amount": ["1,50", "n/a"], "Rate": [0.5, 0.25],
    }  # fmt: skip
    assert ctx.warnings == ["“When” is text: it has different types in the files (date, integer)"]


_TEXT_T = "“T” is text: it has different types in the files (date-time, integer)"


@pytest.mark.parametrize(
    "a, b, dtype, warnings",
    [
        ("Item;T\nA;2026-01-31 08:30:00.1234567891\n", "Item;T\nB;17\n", pl.String, [_TEXT_T]),
        ("Item;T\nA;1500-01-31 08:30:00.1234567\n", "Item;T\nB;17\n", pl.String, [_TEXT_T]),
        ("Item;T\nA;2026-01-31 08:30:00.1234567891\n", "Item,T\nB,17\n", pl.String, [_TEXT_T]),
        (
            "Item;T\nA;2026-01-31 08:30:00.1234567891\n",
            "Item;T\nB;2026-02-01 09:00:00\n",
            pl.Datetime("ns"),
            ["T: the digits below a nanosecond were cut, e.g. in '2026-01-31 08:30:00.1234567891'"],
        ),
    ],
    ids=["nanoseconds", "microseconds", "other-separators", "date-times"],
)
def test_no_warning_about_cut_digits_of_a_column_that_ends_as_text(act, tmp_path, a, b, dtype, warnings):
    """A date-time with digits below a nanosecond in one file and a whole number in another make the
    column text, with all its digits: the step also said that the digits below a nanosecond (or a
    microsecond) were cut. A column of date-times still says so."""
    (tmp_path / "a.csv").write_text(a, encoding="utf-8")
    (tmp_path / "b.csv").write_text(b, encoding="utf-8")
    ctx = StubContext()
    out = act("csv.import", None, folder(tmp_path, file_column=False), ctx)
    assert out.schema["T"] == dtype and ctx.warnings == warnings
    if dtype == pl.String:
        assert out["T"].to_list() == [a.split("\n")[1][2:], "17"]


@pytest.mark.parametrize("name", ["*", "^a.*$", "c12*"])
def test_names_like_patterns_in_the_files_of_a_folder(act, tmp_path, name):
    """A column named *, ^a.*$ or c12* with a date in one file and a whole number in the other gets
    Append's warning as any other column does, and so does D beside it; abc (text in a.csv) gets
    none. The names were read as patterns: * stopped the step, ^a.*$ gave abc the warning."""
    (tmp_path / "a.csv").write_text(f"{name};D;abc\n31.01.2026;31.01.2026;n/a\n", encoding="utf-8")
    (tmp_path / "b.csv").write_text(f"{name};D;abc\n17;17;1,5\n", encoding="utf-8")
    ctx = StubContext()
    out = act("csv.import", None, folder(tmp_path, file_column=False), ctx)
    assert out.to_dict(as_series=False) == {name: ["31.01.2026", "17"], "D": ["31.01.2026", "17"], "abc": ["n/a", "1,5"]}
    assert ctx.warnings == [f"“{column}” is text: it has different types in the files (date, integer)" for column in (name, "D")]


def test_long_numbers_in_one_file_of_a_folder_keep_their_digits(act, tmp_path):
    """Files with the same decimal separator are looked at together, as one file: a decimal of more
    than 15 digits in one of them makes the column exact decimals (in 0.6.4 12345678901234.568)."""
    (tmp_path / "a.csv").write_text("Nr;Wert\n1;12345678901234,5678\n", encoding="utf-8")
    (tmp_path / "b.csv").write_text("Nr;Wert\n2;1,5\n", encoding="utf-8")
    ctx = StubContext()
    out = act("csv.import", None, folder(tmp_path, file_column=False), ctx)
    assert out.schema == {"Nr": pl.Int64, "Wert": pl.Decimal(38, 4)} and not ctx.warnings
    assert out["Wert"].to_list() == [Decimal("12345678901234.5678"), Decimal("1.5")]


@pytest.mark.parametrize(
    "files",
    [
        ["Item;Price;Day\nA;1,50;31.01.2026\nB;2,75;01.02.2026\n", "Item;Price;Day;Discount\nC;   ;   ;0,5\n"],
        ['"Item";"Price";"Day"\n"A";"1,50";"31.01.2026"\n"B";"2,75";"01.02.2026"\n', '"Item";"Price";"Day";"Discount"\n"C";"";"";"0,5"\n'],
        ["Item;Price;Day\nA;1,50;31.01.2026\nB;2,75;01.02.2026\n", "Item,Price,Day,Discount\nC,   ,   ,0.5\n"],
        ["Item;Price;Day\nA;1,50;31.01.2026\n", "Item;Price;Day\nB;2,75;2026-02-01\n", "Item;Price;Day;Discount\nC;   ;   ;0,5\n"],
    ],
    ids=["spaces", "quoted", "other-separators", "other-date-formats"],
)
def test_a_column_of_only_blanks_in_one_file_of_a_folder(act, tmp_path, files):
    """A column with only blanks (spaces, "") in one file is empty there, when another file gives it
    a type, as blanks beside numbers in one file are: the file with one more column made Price and
    Day text in all files, without a message (with the same columns they were numbers and dates)."""
    for name, text in zip("abc", files):
        (tmp_path / f"{name}.csv").write_text(text, encoding="utf-8")
    ctx = StubContext()
    out = act("csv.import", None, folder(tmp_path, file_column=False), ctx)
    assert out.schema == {"Item": pl.String, "Price": pl.Float64, "Day": pl.Date, "Discount": pl.Float64} and not ctx.warnings
    assert out["Price"].to_list() == [1.5, 2.75, None] and out["Day"].null_count() == 1
    # a column with only blanks in every file stays text, with its blanks
    for name, text in zip("abc", ["Item;Note\nA;   \n", "Item,Note,Discount\nC,  ,0.5\n", "Item;Note\nD;\n"]):
        (tmp_path / f"{name}.csv").write_text(text, encoding="utf-8")
    out = act("csv.import", None, folder(tmp_path, file_column=False))
    assert out["Note"].to_list() == ["   ", "  ", None]


_LONG = "1" * 31


@pytest.mark.parametrize(
    "a, b, dtype, values, warnings",
    [
        (
            "A;12345678901234,5678\n",
            "B|1,500.25\n",
            pl.Decimal(38, 4),
            [Decimal("12345678901234.5678"), Decimal("1500.25")],
            [],
        ),
        ("A;1,5\n", "B|9007199254740993\nC|2.5\n", pl.Decimal(38, 1), [Decimal("1.5"), Decimal("9007199254740993"), Decimal("2.5")], []),
        ("A;12,345678901234567%\n", "B|1.5\n", pl.Decimal(38, 17), [Decimal("0.12345678901234567"), Decimal("1.5")], []),
        ("A;1,5\n", "B|18014398509481984\nC|1,000,000\n", pl.Float64, [1.5, 2.0**54, 1e6], []),
        (
            f"A;{_LONG},5\n",
            "B|0.1234567890123456789\n",
            pl.String,
            [f"{_LONG},5", "0.1234567890123456789"],
            [f"V stays text: its numbers are too long to be kept exactly, e.g. '{_LONG},5'"],
        ),
    ],
    ids=["exact-decimal", "beyond-2^53", "percentage", "2^54", "too-long"],
)
def test_numbers_in_files_with_other_decimal_separators_are_read_as_in_one_file(act, tmp_path, a, b, dtype, values, warnings):
    """A column of numbers in every file gets the numbers that one file of all of them gives, each
    file read with its own decimal separator: exact decimals when a decimal number would change one
    of them, else decimal numbers; beyond 38 digits text with the warning. Such a column was text
    with "(exact decimal, number)", or without a message beside a whole number beyond 2^53."""
    (tmp_path / "a.csv").write_text("Item;V\n" + a, encoding="utf-8")
    (tmp_path / "b.csv").write_text("Item|V\n" + b, encoding="utf-8")
    ctx = StubContext()
    out = act("csv.import", None, folder(tmp_path, file_column=False), ctx)
    assert out.schema["V"] == dtype and out["V"].to_list() == values
    assert ctx.warnings == warnings


def test_files_with_more_columns_are_looked_at_with_the_files_of_their_format(act, tmp_path):
    """A file with one more column is looked at together with the other files of its format, as one
    file: a decimal of more than 15 digits in a.csv makes V exact decimals in all files. Such a file
    was looked at on its own, and V became text with "(exact decimal, number)"."""
    (tmp_path / "a.csv").write_text("Item;V\nA;12345678901234,5678\n", encoding="utf-8")
    (tmp_path / "b.csv").write_text("Item;V;D\nB;1,5;0,5\n", encoding="utf-8")
    ctx = StubContext()
    out = act("csv.import", None, folder(tmp_path, file_column=False), ctx)
    assert out.schema == {"Item": pl.String, "V": pl.Decimal(38, 4), "D": pl.Float64} and not ctx.warnings
    assert out.rows() == [("A", Decimal("12345678901234.5678"), None), ("B", Decimal("1.5"), 0.5)]


def test_files_of_one_format_with_columns_of_their_own_are_looked_at_at_once(act, tmp_path, monkeypatch):
    """Files whose decimal separators agree on the columns they share are looked at together, also
    when each has a column of its own: such files were looked at one by one (300 of them took 5 s
    instead of 3 s)."""
    for i in range(30):
        (tmp_path / f"day{i:02d}.csv").write_text(f"Item;Price;c{i}\nA;1,50;{i},5\n", encoding="utf-8")
    heights, detect = [], detection._detect
    monkeypatch.setattr(detection, "_detect", lambda data, *args: heights.append(data.height) or detect(data, *args))
    out = act("csv.import", None, folder(tmp_path, file_column=False))
    assert heights == [30]
    assert out.schema["Price"] == pl.Float64 and out["c29"].to_list() == [None] * 29 + [29.5]


def test_the_decimal_separators_of_many_small_files_are_looked_at_in_parts(monkeypatch):
    """The values of small files that show a decimal separator are counted for some millions of
    values at a time (here about one million), as for each file on its own: all at once, 300 files
    of 2,000 rows and 40 columns took 600 MB more. Files with columns of their own are looked at as
    one column of their values: as one table, mostly empty, they took much memory."""
    stacked, unpivoted = [], []
    concat, unpivot = pl.concat, pl.DataFrame.unpivot
    monkeypatch.setattr(detection, "_SHOWN_VALUES", 1 << 20)
    monkeypatch.setattr(pl, "concat", lambda items, *args, **kwargs: stacked.append(len(items)) or concat(items, *args, **kwargs))
    monkeypatch.setattr(pl.DataFrame, "unpivot", lambda frame, *args, **kwargs: unpivoted.append(frame.width) or unpivot(frame, *args, **kwargs))
    dense = [pl.DataFrame({f"c{k}": [f"{n},5", "1.000", "x"][k % 3 : k % 3 + 1] * 2_000 for k in range(10)}) for n in range(60)]
    assert detection._shown_files(dense) == [detection._shown(frame) for frame in dense]
    assert stacked == [53, 7] and not unpivoted  # 20,000 values each
    sparse = [pl.DataFrame({"Item": ["A", "B"], f"own{n}": [f"{n}.5", "1,000"], "Day": ["31.01.2026", ""]}) for n in range(100)]
    assert detection._shown_files(sparse) == [detection._shown(frame) for frame in sparse]
    assert unpivoted == [3] * 100


def test_columns_of_blanks_and_of_numbers_change_each_file_once(act, tmp_path, monkeypatch):
    """Columns with only blanks in one file and numbers in the other are emptied, and numbers of
    other files set, with one change of each file: column by column, two files of 5,000 columns
    took 16 s more."""
    names = [f"c{i}" for i in range(300)]
    (tmp_path / "a.csv").write_text(";".join(names) + "\n" + ";".join(["   ", "12345678901234,5678"] * 150) + "\n", encoding="utf-8")
    (tmp_path / "b.csv").write_text(",".join(names) + "\n" + ",".join(["4.5", "2.5"] * 150) + "\n", encoding="utf-8")
    changed, with_columns = [], pl.DataFrame.with_columns
    monkeypatch.setattr(pl.DataFrame, "with_columns", lambda frame, *args, **kwargs: changed.append(frame.width) or with_columns(frame, *args, **kwargs))
    out = act("csv.import", None, folder(tmp_path, file_column=False))
    assert out.schema["c298"] == pl.Float64 and out["c298"].to_list() == [None, 4.5]
    assert out.schema["c299"] == pl.Decimal(38, 4) and out["c299"].to_list() == [Decimal("12345678901234.5678"), Decimal("2.5")]
    assert sum(width == 300 for width in changed) < 20  # a change of a whole file


def test_files_with_dates_of_two_formats_are_looked_at_by_format(act, tmp_path, monkeypatch):
    """A column with dates of two formats in the files of a folder is looked at once for the files
    of each format (by the shape of their first value), not for each file on its own: 300 files
    with 10 such columns took 4.4 s instead of 0.7 s. A file without rows or with only blanks there
    has no type there."""
    for i in range(40):
        day = "31.01.2026" if i % 2 else "2026-01-31"
        (tmp_path / f"d{i:02d}.csv").write_text(f"Item;A;B;C\nX;{day};{day};{day}\n", encoding="utf-8")
    (tmp_path / "e0.csv").write_text("Item;A;B;C\n", encoding="utf-8")
    (tmp_path / "e1.csv").write_text("Item;A;B;C\nY;   ;;\n", encoding="utf-8")
    heights, infer = [], dtypes.infer_series
    monkeypatch.setattr(dtypes, "infer_series", lambda s, *args: heights.append(s.len()) or infer(s, *args))
    out = act("csv.import", None, folder(tmp_path, file_column=False))
    assert heights == [41] * 4 + [20] * 6  # the columns of all files, then of the files of each format
    assert out.schema == {"Item": pl.String, "A": pl.Date, "B": pl.Date, "C": pl.Date}
    assert out["A"].to_list() == [dt.date(2026, 1, 31)] * 40 + [None]


def test_many_small_files_take_no_long_time(act, tmp_path):
    """Each file is detected as it would be on its own, but files with the same decimal separators
    are looked at together: one by one, 300 small files took 18 s instead of 0.7 s."""
    header = ";".join(f"c{c}" for c in range(12)) + "\n"
    rows = "".join(";".join([f"{n}", f"{n},5", f"0{n}", f"{n % 28 + 1:02d}.01.2026"] * 3) + "\n" for n in range(1, 21))
    for i in range(300):
        (tmp_path / f"day{i:03d}.csv").write_text(header + rows, encoding="utf-8")
    start = time.perf_counter()
    out = act("csv.import", None, folder(tmp_path))
    assert time.perf_counter() - start < 6
    assert out.height == 6_000 and out.schema["c1"] == pl.Float64 and out.schema["c2"] == pl.String and out.schema["c3"] == pl.Date


_WIDE_FOLDER = """
import datetime as dt
import sys
from pathlib import Path

import polars as pl
from tafl.api.testing import StubContext
from tafl.core import Registry

registry = Registry()
registry.load_builtin()
folder = Path(sys.argv[1])
names = ";".join(f"c{i}" for i in range(4_001))
(folder / "a.csv").write_text(names + "\\n" + ";".join(["1,5", "31.01.2026", "x", "7"] * 50 + [""] * 3_800 + ["1"]) + "\\n", encoding="utf-8")
(folder / "b.csv").write_text(names + "\\n" + ";".join(["2,5", "2026-02-01", "y", "n/a"] * 50 + [""] * 3_800 + ["2"]) + "\\n", encoding="utf-8")
ctx = StubContext()
out = registry.actions["csv.import"](None, {"source": "folder", "folder": str(folder), "pattern": "*.csv", "file_column": False}, ctx)
assert out.shape == (2, 4_001), out.shape
assert out.row(0)[:4] == (1.5, dt.date(2026, 1, 31), "x", "7") and out.row(1)[:4] == (2.5, dt.date(2026, 2, 1), "y", "n/a"), out.rows()
assert out.row(1)[200:202] == (None, None) and out.row(1)[-1] == 2 and out.schema["c4000"] == pl.Int64, out.row(1)[198:204]
assert not ctx.warnings, ctx.warnings[:3]
"""


def test_a_folder_of_wide_files(tmp_path):
    """Two files of 4,001 columns, 50 of them dates in another format in each file: every column is
    looked at, and a deep Polars expression would end the process. In a process of its own, so that
    such an end shows as a failed test."""
    done = subprocess.run([sys.executable, "-c", _WIDE_FOLDER, str(tmp_path)], capture_output=True, text=True, timeout=300)
    assert done.returncode == 0, f"exit code {done.returncode:#x}: {done.stderr[-800:]}"


def test_a_warning_about_a_column_of_several_files_is_given_once(act, tmp_path):
    """Files with other decimal separators are detected one by one: a warning about the same column
    is given once, with the example of the first file."""
    (tmp_path / "a.csv").write_text(f"Id;Rate\n{'1' * 39};0,5\n", encoding="utf-8")
    (tmp_path / "b.csv").write_text(f"Id,Rate\n{'2' * 39},0.25\n", encoding="utf-8")
    ctx = StubContext()
    out = act("csv.import", None, folder(tmp_path, file_column=False), ctx)
    assert out.to_dict(as_series=False) == {"Id": ["1" * 39, "2" * 39], "Rate": [0.5, 0.25]}
    assert ctx.warnings == [f"Id stays text: its numbers are too long to be kept exactly, e.g. '{'1' * 39}'"]


def test_folder_without_matching_files(act, lake):
    with pytest.raises(ActionError, match=r"No files named orders_\*\.csv in"):
        act("csv.import", None, folder(lake, pattern="orders_*.csv"))  # they are in the subfolders
    with pytest.raises(ActionError, match="Folder not found"):
        act("csv.import", None, folder(lake / "missing"))


def test_one_file_works_as_before(act, lake):
    path = lake / "2025" / "02" / "orders_2025-02-01.csv"
    out = act("csv.import", None, {"file": str(path)})
    assert out.columns == ["Nr", "Kunde", "Netto", "Datum"] and out.height == 1


def test_new_files_are_noticed(registry, lake):
    action = registry.actions["csv.import"]
    params = resolve(action.params, folder(lake, pattern="*.csv", subfolders=True))
    before = sources_fingerprint(params, StubContext())
    (lake / "2025" / "02" / "orders_2025-02-02.csv").write_text("Nr;Kunde\n5;Neu\n", encoding="utf-8")
    assert sources_fingerprint(params, StubContext()) != before


def test_old_import_steps_are_upgraded(registry):
    for action_id in ("csv.import", "excel.import"):
        action = registry.actions[action_id]
        assert action.version == 3
        assert action.upgrade({"file": "a"}, 1)["source"] == "file"
        assert action.upgrade({"source": "folder", "folder": "in"}, 2)["no_files"] == "error"
    assert registry.actions["excel.import"].upgrade({"file": "a", "sheet": "X"}, 1)["sheets"] == "one"
    parquet = registry.actions["parquet.import"]
    assert parquet.version == 2 and parquet.upgrade({"source": "folder", "folder": "in"}, 1)["no_files"] == "error"


@pytest.fixture
def workbooks(tmp_path):
    def write(name, sheets):
        book = xlsxwriter.Workbook(str(tmp_path / name))
        for sheet, rows in sheets.items():
            page = book.add_worksheet(sheet)
            for r, row in enumerate(rows):
                page.write_row(r, 0, row)
        book.close()

    write("a.xlsx", {"2025-01": [["Nr", "Netto"], [1, 10.5]], "2025-02": [["Nr", "Netto"], [2, 20]], "Summe": [["Total"], [30.5]]})
    write("b.xlsx", {"2025-03": [["Nr", "Netto", "Notiz"], [3, 7, "x"]]})
    return tmp_path


def test_excel_sheets_whose_names_match_in_all_files(act, workbooks):
    out = act("excel.import", None, folder(workbooks, sheets="matching", sheet_pattern="2025*"))
    assert out.columns == ["File", "Sheet", "Nr", "Netto", "Notiz"]
    assert out["Sheet"].to_list() == ["2025-01", "2025-02", "2025-03"]
    assert out["File"].to_list() == ["a.xlsx", "a.xlsx", "b.xlsx"]
    assert out["Nr"].to_list() == [1, 2, 3] and out.schema["Nr"] == pl.Int64  # whole numbers, all sheets


def test_all_sheets_of_one_excel_file(act, workbooks):
    out = act("excel.import", None, {"file": str(workbooks / "a.xlsx"), "sheets": "all"})
    assert out.columns == ["Sheet", "Nr", "Netto", "Total"]
    assert out["Sheet"].to_list() == ["2025-01", "2025-02", "Summe"]
    one = act("excel.import", None, {"file": str(workbooks / "a.xlsx"), "sheet": "summe"})
    assert one.columns == ["Total"]


def test_each_sheet_is_converted_before_the_sheets_are_put_together(act, tmp_path):
    """"Whole numbers as integers" and "Date-times at midnight as dates" looked at all sheets
    together, after they were put one below the other: a sheet of dates beside a sheet of numbers
    gave the text 2025-01-31 00:00:00. Each sheet is converted on its own now."""
    path = tmp_path / "book.xlsx"
    book = xlsxwriter.Workbook(str(path))
    day = book.add_format({"num_format": "dd.mm.yyyy"})
    for name, cells in (("Dates", [dt.datetime(2025, 1, 31)]), ("Numbers", [45658.5]), ("Whole", [1, 2]), ("Decimals", [2.5])):
        sheet = book.add_worksheet(name)
        sheet.write(0, 0, "x" if name in ("Dates", "Numbers") else "n")
        for row, value in enumerate(cells, 1):
            if isinstance(value, dt.datetime):
                sheet.write_datetime(row, 0, value, day)
            else:
                sheet.write(row, 0, value)
    book.close()
    ctx = StubContext()
    out = act("excel.import", None, {"file": str(path), "sheets": "all"}, ctx)
    assert out["x"].to_list() == ["2025-01-31", "45658.5", None, None, None]
    assert ctx.warnings == ["“x” is text: it has different types in the sheets (date, number)"]
    assert out.schema["n"] == pl.Float64 and out["n"].to_list() == [None, None, 1.0, 2.0, 2.5]  # as before
    whole = act("excel.import", None, {"file": str(path), "sheets": "matching", "sheet_pattern": "Whole;Dates"})
    assert whole.schema["n"] == pl.Int64 and whole.schema["x"] == pl.Date  # each sheet as it is


def test_a_sheet_missing_in_one_file_of_a_folder(act, workbooks):
    with pytest.raises(ActionError, match="Sheet '2025-01' not found in b.xlsx"):
        act("excel.import", None, folder(workbooks, sheet="2025-01"))


def test_parquet_files_of_a_folder(act, tmp_path):
    (tmp_path / "day1").mkdir()
    pl.DataFrame({"Nr": [1, 2], "Netto": [1.5, 2.5]}).write_parquet(tmp_path / "day1" / "part-0.parquet")
    pl.DataFrame({"Nr": [3], "Kunde": ["Huber"]}).write_parquet(tmp_path / "part-1.parquet")
    out = act("parquet.import", None, folder(tmp_path, subfolders=True, file_column=False, path_column=True))
    assert out.columns == ["Path", "Nr", "Netto", "Kunde"]
    assert out["Nr"].to_list() == [1, 2, 3] and out["Kunde"].to_list() == [None, None, "Huber"]
    single = act("parquet.import", None, {"file": str(tmp_path / "part-1.parquet")})
    assert single.to_dicts() == [{"Nr": 3, "Kunde": "Huber"}]


@pytest.mark.parametrize(
    ("first", "second", "expected", "dtype", "words"),
    [
        ([7], [dt.date(2025, 1, 2)], ["7", "2025-01-02"], pl.String, "integer, date"),  # was 7 and 20090
        ([dt.date(2025, 1, 2)], [True], ["2025-01-02", "true"], pl.String, "date, true/false"),  # stopped: no supertype
        (pl.Series([Decimal("2.50")], dtype=pl.Decimal(38, 2)), [1.5], ["2.50", "1.5"], pl.String, "exact decimal, number"),
        ([7], [1.5], [7.0, 1.5], pl.Float64, ""),  # a type that holds both values: no message
        (  # a date after 2262 beside nanoseconds with a time zone: microseconds (it stopped)
            [dt.date(9999, 12, 31)],
            pl.Series([dt.datetime(2025, 3, 1, 12)], dtype=pl.Datetime("ns")).dt.replace_time_zone("UTC"),
            [dt.datetime(9999, 12, 31, tzinfo=dt.UTC), dt.datetime(2025, 3, 1, 12, tzinfo=dt.UTC)],
            pl.Datetime("us", "UTC"),
            "",
        ),
    ],
)
def test_parquet_files_whose_column_has_other_types(act, tmp_path, first, second, expected, dtype, words):
    """As Append does. Before, a date beside a whole number became a number (20090), an exact
    decimal beside a decimal number a decimal number, without a message, and a date beside
    true/false stopped the step with "failed to determine supertype"."""
    pl.DataFrame({"x": first}).write_parquet(tmp_path / "a.parquet")
    pl.DataFrame({"x": second}).write_parquet(tmp_path / "b.parquet")
    ctx = StubContext()
    out = act("parquet.import", None, folder(tmp_path, file_column=False), ctx)
    assert out.schema["x"] == dtype and out["x"].to_list() == expected
    assert ctx.warnings == ([f"“x” is text: it has different types in the files ({words})"] if words else [])


def test_parquet_files_with_binary_values_beside_text(act, tmp_path):
    """Binary values that are all text become that text; one value that is no text, in any of the
    files, makes all of them hex digits. Before, each file decided for itself: "abc" beside "0xFF"."""
    for name, values in (("a", [b"abc"]), ("b", [b"\xff"]), ("c", ["t"])):
        pl.DataFrame({"v": values}).write_parquet(tmp_path / f"{name}.parquet")
    ctx = StubContext()
    out = act("parquet.import", None, folder(tmp_path, file_column=False), ctx)
    assert out["v"].to_list() == ["0x616263", "0xFF", "t"]
    assert ctx.warnings == ["“v” is text: it has different types in the files (binary, text)"]
    (tmp_path / "b.parquet").unlink()
    assert act("parquet.import", None, folder(tmp_path, file_column=False))["v"].to_list() == ["abc", "t"]


def test_parquet_files_with_categories_beside_text(act, tmp_path):
    """A dictionary column (as pandas writes its categories) is text: no warning. Before: "“Region” is
    text: it has different types in the files (text)"."""
    pl.DataFrame({"Region": pl.Series(["North", "South"], dtype=pl.Categorical)}).write_parquet(tmp_path / "a.parquet")
    pl.DataFrame({"Region": ["West"]}).write_parquet(tmp_path / "b.parquet")
    ctx = StubContext()
    out = act("parquet.import", None, folder(tmp_path, file_column=False), ctx)
    assert out.rows() == [("North",), ("South",), ("West",)] and out.schema["Region"] == pl.String and ctx.warnings == []


def test_excel_sheets_whose_column_has_other_types(act, tmp_path):
    """Excel stores numbers as decimal numbers: beside text, 5 is "5", as Unpivot writes it. Before,
    it was "5.0", without a message."""
    book = xlsxwriter.Workbook(str(tmp_path / "mixed.xlsx"))
    for sheet, nr, amount in (("A", 5, 1.5), ("B", "n/a", 2)):
        page = book.add_worksheet(sheet)
        page.write_row(0, 0, ["Nr", "Amount"])
        page.write_row(1, 0, [nr, amount])
    book.close()
    ctx = StubContext()
    out = act("excel.import", None, {"file": str(tmp_path / "mixed.xlsx"), "sheets": "all"}, ctx)
    assert out.rows() == [("A", "5", 1.5), ("B", "n/a", 2.0)] and out.schema["Nr"] == pl.String
    # each sheet is converted first: 5 is a whole number there
    assert ctx.warnings == ["“Nr” is text: it has different types in the sheets (integer, text)"]


def test_files_in_a_folder(act, lake):
    out = act("files.list", None, {"folder": str(lake), "pattern": "*.csv;*.txt", "subfolders": True})
    assert out.columns == ["File", "Path", "Folder", "Extension", "Size", "Modified", "Full path"]
    assert out["File"].to_list() == ["orders_2025-01-02.csv", "orders_2025-01-10.csv", "orders_2025-02-01.csv", "readme.txt"]
    assert out["Folder"][3] == "" and out["Extension"][3] == "txt" and out["Size"][3] == 13
    assert out.schema["Modified"] == pl.Datetime("us")
    empty = act("files.list", None, {"folder": str(lake), "pattern": "*.xlsx"})
    assert empty.height == 0 and empty.width == 7


def test_source_columns_never_replace_data_columns(act, tmp_path):
    (tmp_path / "x.csv").write_text("File;Wert\nmine;1\n", encoding="utf-8")
    out = act("csv.import", None, folder(tmp_path))
    assert out.columns == ["File (source)", "File", "Wert"] and out["File"][0] == "mine"


@pytest.mark.parametrize(
    ("action_id", "pattern"), [("csv.import", "*.csv"), ("excel.import", "*.xlsx"), ("parquet.import", "*.parquet")]
)
def test_a_folder_without_files_as_chosen(act, tmp_path, action_id, pattern):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    message = f"No files named {pattern} in {inbox}"
    with pytest.raises(ActionError, match=re.escape(message)):  # the default, as before
        act(action_id, None, folder(inbox, pattern=pattern))
    ctx = StubContext()
    empty = act(action_id, None, folder(inbox, pattern=pattern, no_files="empty", path_column=True), ctx)
    assert empty.schema == {"File": pl.String, "Path": pl.String} and empty.height == 0
    assert ctx.warnings == [f"{message}: the table is empty"]
    with pytest.raises(SkipRest) as skipped:
        act(action_id, None, folder(inbox, pattern=pattern, no_files="skip"))
    assert skipped.value.message == message and skipped.value.df.schema == {"File": pl.String}
    with pytest.raises(ActionError, match="Folder not found"):  # a folder that isn't there is still an error
        act(action_id, None, folder(tmp_path / "missing", no_files="empty"))


@pytest.mark.parametrize("no_files", ["empty", "skip"])
def test_daily_exports_on_a_day_without_files(registry, tmp_path, no_files):
    """Import the inbox, export, move the files to an archive: a run on a day without files is
    no error, and the export of the last day stays."""
    (tmp_path / "inbox" / "2026").mkdir(parents=True)
    (tmp_path / "inbox" / "2026" / "a.csv").write_text("Nr;Netto\n1;2,5\n", encoding="utf-8")
    project = Project(name="Daily")
    project.path = tmp_path / "daily.tafl"
    table = project.add_table("Daily")
    source = folder("inbox", pattern="*.csv", subfolders=True, path_column=True, no_files=no_files)
    project.add_step(table, registry.actions["csv.import"], source)
    project.add_step(table, registry.actions["csv.export"], {"file": "out/daily.csv", "skip_empty": True})
    project.add_step(table, registry.actions["files.move"], {"column": "Path", "base": "inbox", "target": "archive"})
    first = Engine(project, registry).run(mode=Mode.RUN)[table.id]
    assert first.ok and (tmp_path / "archive" / "a.csv").exists()
    exported = (tmp_path / "out" / "daily.csv").read_bytes()

    for mode in (Mode.RUN, Mode.PREVIEW):  # the inbox is empty now
        result = Engine(project, registry).run(mode=mode)[table.id]
        assert result.ok and result.df.columns == ["File", "Path"] and result.df.height == 0
        if no_files == "empty":
            assert result.steps[0].messages[0][0] == "warning"
            if mode == Mode.RUN:
                assert result.steps[1].messages == [("info", "Not exported: the table is empty")]
        else:
            assert [s.status for s in result.steps] == [Status.OK, Status.SKIPPED, Status.SKIPPED]
    assert (tmp_path / "out" / "daily.csv").read_bytes() == exported


def test_in_folder_answers_as_find_files_does(tmp_path):
    (tmp_path / "sub").mkdir()
    for name in ("a.csv", "B.CSV", "c.txt", "~$d.csv", "sub/e.csv"):
        (tmp_path / name).touch()
    paths = [p for p in tmp_path.rglob("*") if p.is_file()]
    for patterns, subfolders in (("*.csv", False), ("*.csv; *.txt", True), ("", True), ("a*", False)):
        found = find_files(tmp_path, patterns, subfolders)
        assert [p for p in paths if in_folder(p, tmp_path, patterns, subfolders)] == [p for p in paths if p in found]

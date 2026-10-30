"""The data sheet without its window: clipboard texts, edits, undo, the row of names, limits."""

import copy
import random
import time
from pathlib import Path

import polars as pl
import pytest

from tafl.api import files, unique_names
from tafl.core import sheet as module
from tafl.core.sheet import (
    BLOCK,
    FORMATS,
    Pasted,
    Sheet,
    SheetError,
    _widest,
    mark,
    parse_text,
    source_format,
    stamp_of,
)

# --- clipboard texts ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "rows", "separator"),
    [
        # as Excel and LibreOffice write it
        ("a\tb\r\n1\t2\r\n", [("a", "b"), ("1", "2")], "\t"),
        ('a\t"x\r\ny"\tb\r\nc\td\te\r\n', [("a", "x\ny", "b"), ("c", "d", "e")], "\t"),
        ('"x\ny"\r\nz\r\n', [("x\ny",), ("z",)], ""),
        ('"x\ty"\r\nz\r\n', [("x\ty",), ("z",)], ""),
        ('"""Best"" offer"\tb\r\n', [('"Best" offer', "b")], "\t"),
        ('"a\nb"\t"c""d"\n', [("a\nb", 'c"d')], "\t"),
        ('""\ta\n', [('""', "a")], "\t"),
        ('a\t""\n"b"\n', [("a", '""'), ('"b"', None)], "\t"),
        ('"a""b"\tc\n', [('a"b', "c")], "\t"),
        ('"x\ty"\tz\n', [("x\ty", "z")], "\t"),
        ('a\t"b\n\nc"\td\n', [("a", "b\n\nc", "d")], "\t"),
        # quotes from a web page or a mail are plain text
        ('"Best" offer\t12\nx\ty\n', [('"Best" offer', "12"), ("x", "y")], "\t"),
        ('"Best offer"\t12\n', [('"Best offer"', "12")], "\t"),
        ('"Best offer"\n"Other"\n', [('"Best offer"',), ('"Other"',)], ""),
        ('5" screen\t7" screen\nx\ty\n', [('5" screen', '7" screen'), ("x", "y")], "\t"),
        ('"open\tb\nc\td\n', [('"open', "b"), ("c", "d")], "\t"),
        ('a"b\tc\n"\td\n', [('a"b', "c"), ('"', "d")], "\t"),
        # CSV style: every well-quoted cell loses its quotes
        ('"a";"b;c"\n"d";"e"\n', [("a", "b;c"), ("d", "e")], ";"),
        ('"a";"b\nc"\n"d";"e"\n', [("a", "b\nc"), ("d", "e")], ";"),
        ('"Best" offer;12\n', [('"Best" offer', "12")], ";"),
        # other separators
        ("a;b\nc;d\n", [("a", "b"), ("c", "d")], ";"),
        ("a|b\nc|d\n", [("a", "b"), ("c", "d")], "|"),
        ("Müller, Hans\nMeier, Eva\n", [("Müller", " Hans"), ("Meier", " Eva")], ","),
        # a list of numbers with decimal commas is one column
        ("1,50\n2,75\n1.234,50\n", [("1,50",), ("2,75",), ("1.234,50",)], ""),
        ("1,50\n\n-2,75\n", [("1,50",), (None,), ("-2,75",)], ""),
        ("12,5\n", [("12,5",)], ""),
        # a single column, a single value
        ("abc\r\n", [("abc",)], ""),
        ("x", [("x",)], ""),
        ("00123\n00124\n", [("00123",), ("00124",)], ""),
        ("   ", [("   ",)], ""),
        # empty cells and rows
        ("a\tb\t\t\nc\td\t\t\n\t\t\t\n", [("a", "b"), ("c", "d")], "\t"),
        ("a\tb\n\t\nc\td\n", [("a", "b"), (None, None), ("c", "d")], "\t"),
        ("a\tb\n\nc\td\n", [("a", "b"), (None, None), ("c", "d")], "\t"),
        ("a\n\nb\n", [("a",), (None,), ("b",)], ""),
        ("\n\na\tb", [(None, None), (None, None), ("a", "b")], "\t"),
        ("\tb\tc\na\t\tc\n", [(None, "b", "c"), ("a", None, "c")], "\t"),
        ("a\tb\nc\td\te\tf\ng\n", [("a", "b", None, None), ("c", "d", "e", "f"), ("g", None, None, None)], "\t"),
        ("a\tb\n\n\n\n", [("a", "b")], "\t"),
        ("a\tb\n\t\nc\td\n\t\n\t\n", [("a", "b"), (None, None), ("c", "d")], "\t"),
        ("a\tb\nc\td", [("a", "b"), ("c", "d")], "\t"),
        # line ends and characters
        ("a\tb\rc\td\r", [("a", "b"), ("c", "d")], "\t"),
        ("a\x00b\tc\n", [("ab", "c")], "\t"),
        (" a \t  \n", [(" a ", "  ")], "\t"),
        ("#a\tb\n#c\td\n", [("#a", "b"), ("#c", "d")], "\t"),
        ("NA\tnull\n\\N\tNaN\n", [("NA", "null"), ("\\N", "NaN")], "\t"),
        ("€\tÄ\n\U0001f600\tß\n", [("€", "Ä"), ("\U0001f600", "ß")], "\t"),
        ("a\x1fb\nc\n", [("a\x1fb",), ("c",)], ""),
    ],
)
def test_clipboard_texts(text, rows, separator):
    pasted = parse_text(text)
    assert pasted.frame.rows() == rows
    assert pasted.separator == separator
    assert set(pasted.frame.schema.values()) == {pl.String}


@pytest.mark.parametrize(
    "value",
    ["00123", "1,50", "27.02.2026", "1E5", "1.000", "-5", "+49 30", "=1+2", "TRUE", "#N/A", "'x", "0.1", "1 234,50 €"],
)
def test_values_stay_exactly_as_pasted(value):
    assert parse_text(f"{value}\tx\n{value}\ty\n").frame.rows() == [(value, "x"), (value, "y")]


def test_a_cell_that_holds_the_private_characters_stays():
    text = '"a\nb"\t\n'
    assert parse_text(text).frame.rows() == [("a\nb", "")]


def test_trailing_columns_and_rows_go_only_when_they_have_no_value():
    assert parse_text("a\t \n").frame.rows() == [("a", " ")]
    assert parse_text("a\n \n").frame.rows() == [("a",), (" ",)]


def test_paste_as_one_column():
    pasted = parse_text("a;b\nc;d\n", one_column=True)
    assert pasted.frame.rows() == [("a;b",), ("c;d",)]
    assert pasted.separator == ""
    assert parse_text('a\tb\n"c\td"\n', one_column=True).frame.rows() == [("a\tb",), ("c\td",)]


@pytest.mark.parametrize("extra", range(10))
def test_the_decimal_comma_list_is_looked_at_only_in_whole_lines(extra):
    numbers = "\n".join(f"{i:,}".replace(",", ".") + ",50" for i in range(10_000, 30_000))
    assert 65_536 < len(numbers)
    pasted = parse_text("1" * extra + "1,50\n" + numbers)  # the look ends at another place of a line
    assert pasted.separator == "" and pasted.frame.width == 1


@pytest.mark.parametrize(
    "lines",
    [
        ["1.234,50 €", "2,75 €"],
        ["12,5 %", "7,25 %"],
        ["1,50", "n/a", "2,75"],
        ["1,50", "", "abc"],
        ["Betrag", "1,50", "2,75"],
        ["1,5 kg", "2,25 kg", "Rest"],
        ["-1,50 EUR"],
    ],
)
def test_one_column_of_values_with_a_unit_or_a_text_stays_one_column(lines):
    pasted = parse_text("\r\n".join(lines) + "\r\n")
    assert pasted.separator == ""
    assert pasted.frame.rows() == [(line or None,) for line in lines]


def test_one_column_with_a_line_break_in_a_cell_stays_one_column():
    pasted = parse_text('1,50\r\n"a\nb"\r\n2,75\r\n')
    assert pasted.frame.rows() == [("1,50",), ("a\nb",), ("2,75",)]


@pytest.mark.parametrize(
    ("text", "rows"),
    [
        ("a,b\n1,2\n", [("a", "b"), ("1", "2")]),
        ("Müller, Hans\n", [("Müller", " Hans")]),
        ("1,50,EUR\n2,75,USD\n", [("1", "50", "EUR"), ("2", "75", "USD")]),
        ("1,2,3\n4,5,6\n", [("1", "2", "3"), ("4", "5", "6")]),
        ('"1,50","x"\n"2,75","y"\n', [("1,50", "x"), ("2,75", "y")]),
        ("M100,5\nM200,12\nM300,7\n", [("M100", "5"), ("M200", "12"), ("M300", "7")]),
        ("2026-01-15,12\n2026-01-16,7\n", [("2026-01-15", "12"), ("2026-01-16", "7")]),
        ("12:30,5\n13:00,6\n", [("12:30", "5"), ("13:00", "6")]),
    ],
)
def test_other_commas_still_split(text, rows):
    pasted = parse_text(text)
    assert pasted.separator == "," and pasted.frame.rows() == rows


@pytest.mark.parametrize(
    ("text", "rows"),
    [
        ("1,50;2,75\n3,25;4,00\n", [("1,50", "2,75"), ("3,25", "4,00")]),
        ("Wert 1,5;Wert 2,5\nWert 3,5;Wert 4,5\n", [("Wert 1,5", "Wert 2,5"), ("Wert 3,5", "Wert 4,5")]),
    ],
)
def test_a_list_of_decimal_numbers_splits_at_its_semicolons(text, rows):
    pasted = parse_text(text)
    assert pasted.separator == ";" and pasted.frame.rows() == rows


def test_a_list_of_decimal_numbers_splits_at_its_pipes():
    pasted = parse_text("1,50|2,75\n3,25|4,00\n")
    assert pasted.separator == "|" and pasted.frame.rows() == [("1,50", "2,75"), ("3,25", "4,00")]


FRAME_RULER = "-" * 32
FRAMED_LIST = (
    f"{FRAME_RULER}\r\n|Artikel   |Zone|Menge     |ME |\r\n{FRAME_RULER}\r\n"
    f"|A-100     |0010|12,500    |ST |\r\n|A-200     |0010|1.000,000 |KG |\r\n{FRAME_RULER}\r\n"
)
FRAMED_ROWS = [
    ("Artikel   ", "Zone", "Menge     ", "ME "),
    ("A-100     ", "0010", "12,500    ", "ST "),
    ("A-200     ", "0010", "1.000,000 ", "KG "),
]


@pytest.mark.parametrize(
    "text",
    [
        FRAMED_LIST,
        FRAMED_LIST.replace(FRAME_RULER + "\r\n", ""),
        FRAMED_LIST.replace(FRAME_RULER, "|" + "-" * 10 + "|----|" + "-" * 10 + "|---|"),
        FRAMED_LIST.replace(FRAME_RULER, "+" + "-" * 10 + "+----+" + "-" * 10 + "+---+"),
        FRAMED_LIST.replace("\r\n|", "\r\n  |"),
    ],
)
def test_a_list_in_a_frame_loses_the_frame_and_its_rulers(text):
    pasted = parse_text(text)
    assert pasted.separator == "|" and pasted.framed
    assert pasted.frame.rows() == FRAMED_ROWS


def test_a_table_in_markdown_form():
    pasted = parse_text("| Name | Betrag |\n|---|---:|\n| Müller | 1,50 |\n")
    assert pasted.framed and pasted.frame.rows() == [(" Name ", " Betrag "), (" Müller ", " 1,50 ")]


def test_a_title_above_a_list_in_a_frame_stays():
    pasted = parse_text(f"Bestand 2026\n{FRAMED_LIST}")
    assert pasted.framed and pasted.separator == "|"
    assert pasted.frame.rows() == [("Bestand 2026", None, None, None), *FRAMED_ROWS]


def test_a_line_with_a_pipe_but_no_frame_keeps_everything_as_it_was():
    pasted = parse_text("|a|b|\n|c|d|\nx|y\n")
    assert not pasted.framed
    assert pasted.frame.rows() == [(None, "a", "b"), (None, "c", "d"), ("x", "y", None)]


def test_text_of_only_rulers_stays():
    assert parse_text("---|---\n").frame.rows() == [("---|---",)]


def test_a_list_in_a_frame_of_one_column():
    pasted = parse_text("|a|\n|b|\n")
    assert pasted.framed and pasted.separator == "" and pasted.frame.rows() == [("a",), ("b",)]


def test_paste_as_one_column_takes_a_list_in_a_frame_as_it_is():
    text = "-----\n|a|b|\n"
    assert parse_text(text, one_column=True).frame.rows() == [("-----",), ("|a|b|",)]


@pytest.mark.parametrize(
    ("text", "rows", "separator"),
    [
        (
            "Budgets 2026\r\nName;Betrag\r\nMüller;1,50\r\n",
            [("Budgets 2026", None), ("Name", "Betrag"), ("Müller", "1,50")],
            ";",
        ),
        (
            "Budgets 2026\r\n\r\nName;Betrag\r\nMüller;1,50\r\n",
            [("Budgets 2026", None), (None, None), ("Name", "Betrag"), ("Müller", "1,50")],
            ";",
        ),
        ("Liste\nName|Ort\na|b\n", [("Liste", None), ("Name", "Ort"), ("a", "b")], "|"),
        ("Liste\nName,Ort\na,b\n", [("Liste", None), ("Name", "Ort"), ("a", "b")], ","),
        ("Titel\nName\tOrt\na\tb\n", [("Titel", None), ("Name", "Ort"), ("a", "b")], "\t"),
        (
            "Hallo, hier die Liste\nName;Betrag\nMüller;1,50\nSchmidt;2,75\n",
            [("Hallo, hier die Liste", None), ("Name", "Betrag"), ("Müller", "1,50"), ("Schmidt", "2,75")],
            ";",
        ),
        (
            "A\nB\nName;Ort\na;b\nc;d\ne;f\nGruß\n",
            [("A", None), ("B", None), ("Name", "Ort"), ("a", "b"), ("c", "d"), ("e", "f"), ("Gruß", None)],
            ";",
        ),
    ],
)
def test_a_title_above_a_table(text, rows, separator):
    pasted = parse_text(text)
    assert pasted.separator == separator and pasted.frame.rows() == rows


@pytest.mark.parametrize(
    "lines",
    [
        ["Anna", "Müller, Hans", "Schmidt, Eva"],
        ["Anna", "Bert", "Müller, Hans"],
        ["Titel", "a;b"],
        ["Titel", "a;b", "c;d;e"],
        ["Name", "1,50", "2,75"],
        ["abc", "def"],
    ],
)
def test_a_list_of_single_values_with_a_separator_now_and_then_stays_one_column(lines):
    pasted = parse_text("\n".join(lines))
    assert pasted.separator == "" and pasted.frame.rows() == [(line,) for line in lines]


@pytest.mark.parametrize(
    ("text", "rows", "separator"),
    [
        (
            "Nr;Name;Betrag\n1;Müller;1,50\n2;Schmidt\tMeier;2,75\n3;Huber;3,00\n",
            [("Nr", "Name", "Betrag"), ("1", "Müller", "1,50"), ("2", "Schmidt\tMeier", "2,75"), ("3", "Huber", "3,00")],
            ";",
        ),
        ('a,b\n"c\td",e\n', [("a", "b"), ("c\td", "e")], ","),
    ],
)
def test_a_stray_tab_gives_way_to_the_separator_of_the_lines(text, rows, separator):
    pasted = parse_text(text)
    assert pasted.separator == separator and pasted.frame.rows() == rows and not pasted.few_tabs


def test_tabs_on_few_lines_without_another_separator_still_split_and_say_so():
    pasted = parse_text("abc\nd\te\nfgh\nijk\n")
    assert pasted.separator == "\t" and pasted.few_tabs
    assert pasted.frame.rows() == [("abc", None), ("d", "e"), ("fgh", None), ("ijk", None)]


@pytest.mark.parametrize(
    ("text", "rows"),
    [
        (
            "Hallo Hans, anbei die Zahlen:\nBitte prüfen.\nDanke.\n\nName\tBetrag\nMüller\t1,50\n",
            [
                ("Hallo Hans, anbei die Zahlen:", None),
                ("Bitte prüfen.", None),
                ("Danke.", None),
                (None, None),
                ("Name", "Betrag"),
                ("Müller", "1,50"),
            ],
        ),
        (
            "Bericht, Stand heute\nQuelle: ERP, Lager 10\nA\tB\n1\t2\n",
            [("Bericht, Stand heute", None), ("Quelle: ERP, Lager 10", None), ("A", "B"), ("1", "2")],
        ),
    ],
)
def test_tabs_on_few_lines_stay_when_the_other_separator_makes_no_table(text, rows):
    pasted = parse_text(text)
    assert pasted.separator == "\t" and pasted.few_tabs and pasted.frame.rows() == rows


MAIL_LIST = (
    "CustomerNo;Name;Budget\r\n00017;Müller GmbH;12.500,00\r\n00023;Schröder;8.250,50\r\n00105;Nord;1.200,00\r\n"
)
MAIL_ROWS = [
    ("CustomerNo", "Name", "Budget"),
    ("00017", "Müller GmbH", "12.500,00"),
    ("00023", "Schröder", "8.250,50"),
    ("00105", "Nord", "1.200,00"),
]


@pytest.mark.parametrize(
    ("above", "below"),
    [
        ("Hallo Hans,\r\n\r\n", ""),
        ("Hallo Hans\r\n\r\n", "\r\nViele Grüße\r\nEva\r\n"),
        ("Hallo Hans,\r\n\r\n", "\r\nViele Grüße\r\nEva\r\n"),
        ("Hallo Hans,\r\nhier die Liste,\r\nwie besprochen:\r\n\r\n", ""),
    ],
)
def test_a_table_in_a_mail(above, below):
    pasted = parse_text(above + MAIL_LIST + below)
    assert pasted.separator == ";" and pasted.frame.width == 3
    assert [row for row in pasted.frame.rows() if row[1] is not None] == MAIL_ROWS


def test_a_tab_table_in_a_mail_with_commas_in_its_numbers():
    pasted = parse_text("Hallo Hans,\r\n\r\nanbei die Liste, wie besprochen:\r\n\r\n3.933,98\tST\r\n7.103,40\tKG\r\n")
    assert pasted.separator == "\t" and pasted.few_tabs and pasted.frame.rows()[-1] == ("7.103,40", "KG")


def test_a_list_in_a_frame_with_a_line_below_it():
    pasted = parse_text("-------\r\n|a|b|\r\n|c|d|\r\n-------\r\nAnzahl Treffer: 2\r\n")
    assert pasted.framed and pasted.frame.rows() == [("a", "b"), ("c", "d"), ("Anzahl Treffer: 2", None)]


@pytest.mark.parametrize(
    ("text", "rows"),
    [
        (
            "Name;Betrag;Bemerkung\nMüller;1,50\nSchmidt;2,75;eilig\nHuber;3,25\nMeier;4,00\n",
            [
                ("Name", "Betrag", "Bemerkung"),
                ("Müller", "1,50", None),
                ("Schmidt", "2,75", "eilig"),
                ("Huber", "3,25", None),
                ("Meier", "4,00", None),
            ],
        ),
        (
            "Name;Ort;Bemerkung\nMüller,Hans;Berlin\nMeier,Eva;Hamburg;eilig\nHuber,Jo;Bonn\n",
            [
                ("Name", "Ort", "Bemerkung"),
                ("Müller,Hans", "Berlin", None),
                ("Meier,Eva", "Hamburg", "eilig"),
                ("Huber,Jo", "Bonn", None),
            ],
        ),
        ("Müller, Hans;1,50\nMeier, Eva;2,75\n", [("Müller, Hans", "1,50"), ("Meier, Eva", "2,75")]),
        ("1,50;2,75\n", [("1,50", "2,75")]),
    ],
)
def test_semicolons_win_over_commas_of_numbers_and_names(text, rows):
    pasted = parse_text(text)
    assert pasted.separator == ";" and pasted.frame.rows() == rows


@pytest.mark.parametrize(
    ("text", "separator", "rows"),
    [
        (
            'Monitor 24";199,00;Lager 1\nMonitor 27";249,00;Lager 2\n',
            ";",
            [('Monitor 24"', "199,00", "Lager 1"), ('Monitor 27"', "249,00", "Lager 2")],
        ),
        ('Rohr 1/2"|10|m\nRohr 3/4"|12|m\n', "|", [('Rohr 1/2"', "10", "m"), ('Rohr 3/4"', "12", "m")]),
        ('Preise\nMonitor 24";199\nMonitor 27";249\n', ";", [("Preise", None), ('Monitor 24"', "199"), ('Monitor 27"', "249")]),
    ],
)
def test_an_inch_mark_hides_no_separator(text, separator, rows):
    pasted = parse_text(text)
    assert pasted.separator == separator and pasted.frame.rows() == rows


@pytest.mark.parametrize(
    "lines", [["1,234.50", "2,345.00", "12.50"], ["$1,234.50", "$2,345.00"], ["1,234,567.89", "2,345,678.00"]]
)
def test_one_column_of_numbers_with_thousands_commas_stays_one_column(lines):
    pasted = parse_text("\r\n".join(lines) + "\r\n")
    assert pasted.separator == "" and pasted.frame.rows() == [(line,) for line in lines]


def test_a_date_before_a_comma_is_no_number():
    assert parse_text("26.02.2026,234\n28.08.2026,375\n").frame.rows() == [("26.02.2026", "234"), ("28.08.2026", "375")]


@pytest.mark.parametrize(
    "lines",
    [
        ["Sehr geehrte Damen und Herren,", "folgende Beträge sind offen:", "1.234,50 EUR", "2.345,00 EUR", "99,95 EUR"],
        ["Hallo Hans,", "Müller;1,50"],
        ["Hallo Hans,", "7300012345", "7300012346"],
        ["Hallo Hans, hier die Liste:", "1,50 EUR", "2,50 EUR", "3,50 EUR"],
        ["Hallo Hans,"],
        ["abc;"],
        ["x|"],
    ],
)
def test_a_comma_of_a_sentence_or_a_separator_at_the_end_splits_nothing(lines):
    pasted = parse_text("\n".join(lines))
    assert pasted.separator == "" and pasted.frame.rows() == [(line,) for line in lines]


def test_a_comma_of_a_sentence_gives_way_to_the_semicolon_of_a_single_row():
    pasted = parse_text("Müller, Hans, Dr.;1,50")
    assert pasted.separator == ";" and pasted.frame.rows() == [("Müller, Hans, Dr.", "1,50")]


@pytest.mark.parametrize(
    "lines",
    [
        ["12,50", "7,90", "12,-", "15,-", "3,20", "1.299,00"],
        ["12,-", "15,-", "12,50", "7,90", "3,20"],
        ["987.00", "1,234.50", "1,234,567", "2,000,000", "12.50", "5,000.00"],
        ["1,50", "2,50", "offen,gesperrt", "offen,storno", "3,50", "4,50", "5,50"],
        ["1,5"] * 100 + ["1,5-2,5", "3,5-4,5"] + ["2,5"] * 100,
    ],
)
def test_two_lines_that_split_don_t_cut_a_list_of_numbers(lines):
    pasted = parse_text("\n".join(lines))
    assert pasted.separator == "" and pasted.frame.rows() == [(line,) for line in lines]


@pytest.mark.parametrize(
    "lines", [["12,-", "3,-", "7,50"], ["$1,234,567", "$12,345,678"], ["(1,234,567)", "(12,345,678)"], ["1,234,567€", "2,000,000€"]]
)
def test_one_column_of_other_number_forms_stays_one_column(lines):
    pasted = parse_text("\n".join(lines))
    assert pasted.separator == "" and pasted.frame.rows() == [(line,) for line in lines]


@pytest.mark.parametrize(
    ("text", "rows"),
    [
        ('Monitor 24";199,00;Lager 1', [('Monitor 24"', "199,00", "Lager 1")]),
        ('Monitor 24",199,Lager 1', [('Monitor 24"', "199", "Lager 1")]),
        ('Rohr 1/2";10;m\nRohr 3/4";12', [('Rohr 1/2"', "10", "m"), ('Rohr 3/4"', "12", None)]),
    ],
)
def test_an_inch_mark_hides_no_separator_of_a_single_row_or_of_rows_of_all_lengths(text, rows):
    assert parse_text(text).frame.rows() == rows


@pytest.mark.parametrize(
    ("text", "separator", "rows"),
    [
        ('Nr;Text\n1;"a\tb"\n2;"c\td"', ";", [("Nr", "Text"), ("1", "a\tb"), ("2", "c\td")]),
        ('Nr,Text\n1,"a\tb"\n2,"c\td"', ",", [("Nr", "Text"), ("1", "a\tb"), ("2", "c\td")]),
        ('1;"a\tb"\n2;"c\td"', ";", [("1", "a\tb"), ("2", "c\td")]),
        ('Text;Nr\n"a\tb";1\n"c\td";2', ";", [("Text", "Nr"), ("a\tb", "1"), ("c\td", "2")]),
        ('x,"b\tc"', ",", [("x", "b\tc")]),
    ],
)
def test_a_tab_in_a_quoted_cell_of_another_separator_is_no_tab_of_the_table(text, separator, rows):
    pasted = parse_text(text)
    assert pasted.separator == separator and pasted.frame.rows() == rows and not pasted.few_tabs


@pytest.mark.parametrize(
    ("text", "first", "rows"),
    [
        ('1,"a;b"\n2,"c;d"\n3,"e;f"', ",", [("1", "a;b"), ("2", "c;d"), ("3", "e;f")]),
        ('"Müller GmbH; Berlin","1,50"\n"Nord AG; Köln","2,50"', ",", [("Müller GmbH; Berlin", "1,50"), ("Nord AG; Köln", "2,50")]),
        ('"a;b",c', ",", [("a;b", "c")]),
        ('1;"a,b"\n2;"c,d"', ";", [("1", "a,b"), ("2", "c,d")]),
        ('1|"a,b"\n2|"c,d"', "|", [("1", "a,b"), ("2", "c,d")]),
    ],
)
def test_a_separator_in_quoted_cells_of_another_one_doesn_t_win(text, first, rows):
    pasted = parse_text(text)
    assert pasted.separator == first and pasted.frame.rows() == rows


def test_a_tab_table_with_quotes_at_the_start_and_the_end_of_a_row_stays_a_tab_table():
    assert parse_text('""""\t   \t;\t "').frame.rows() == [('"', "   ", ";", ' "')]


def test_two_tables_in_a_mail_land_in_one_sheet():
    text = "Hallo Hans\n\nTabelle 1\nName;Wert\na;1\nb;2\n\nText\nnoch Text\n\nTabelle 2\nName;Wert\nc;3\nd;4\n"
    pasted = parse_text(text)
    assert pasted.separator == ";" and ("a", "1") in pasted.frame.rows() and ("d", "4") in pasted.frame.rows()


def test_a_title_per_group_doesn_t_hide_the_table():
    pasted = parse_text("Lager 10\nA;1\nB;2\nLager 20\nC;3\nD;4\nLager 30\nE;5\nF;6\n")
    assert pasted.separator == ";" and pasted.frame.rows()[:3] == [("Lager 10", None), ("A", "1"), ("B", "2")]


def test_tabs_on_most_lines_are_no_stray_tabs():
    assert not parse_text("a\tb\nc\nd\te\n").few_tabs
    assert not parse_text("a\tb;c\nd\te;f\n").few_tabs


@pytest.mark.parametrize(
    ("first", "on"),
    [
        ("Name\tBetrag", True),
        ("Name\t2026", False),
        ("00123\tMüller", False),
        ("Müller\t1,50", False),
        ("Müller\t27.02.2026", False),
        ("Müller\t2026-02-27", False),
        ("Müller\t12:30", False),
        ("Müller\t-5", False),
        ("Q1\tJahr 2026", True),
        ("Nr.\t%", True),
        ("Name\t", False),
    ],
)
def test_where_the_switch_for_the_column_names_starts(first, on):
    sheet = Sheet()
    sheet.load(parse_text(f"{first}\nb\tc\n"))
    assert sheet.header is on


def test_widest_line():
    assert _widest(b"a\tb\tc\nd\ne\tf\n", "\t") == 2
    assert _widest(b"", ";") == 0
    assert _widest(b"a\nb\n", ";") == 0
    for count in (1, 2, 3, 4, 5, 7, 8, 9, 31, 32, 33, 1000):
        assert _widest(b"x" + b";" * count + b"\ny;\n", ";") == count


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("", "The clipboard holds no text. Copy some data, then press Ctrl+V here."),
        ("\r\n\r\n", "The clipboard holds only empty cells."),
        ("\t\t\n\t\t\n", "The clipboard holds only empty cells."),
        ("\x00", "The clipboard holds only empty cells."),
    ],
)
def test_nothing_to_paste(text, message):
    with pytest.raises(SheetError) as error:
        parse_text(text)
    assert str(error.value) == message


def test_too_much_for_a_sheet(monkeypatch):
    monkeypatch.setattr(module, "MAX_CELLS", 6)
    assert parse_text("a\tb\tc\nd\te\tf\n").frame.shape == (2, 3)
    with pytest.raises(SheetError) as error:
        parse_text("a\tb\tc\nd\te\tf\ng\th\ti\n")
    assert (
        str(error.value)
        == "The clipboard holds 3 rows × 3 columns. A sheet holds at most 6 cells: put the data in a file and import it."
    )
    monkeypatch.setattr(module, "MAX_CELLS", 100)
    monkeypatch.setattr(module, "MAX_COLUMNS", 3)
    with pytest.raises(SheetError) as error:
        parse_text("a\tb\nc\td\te\tf\n")
    assert (
        str(error.value)
        == "The clipboard holds 4 columns. A sheet holds at most 3: put the data in a file and import it."
    )


def test_the_widest_line_counts_not_only_the_first():
    assert parse_text("a\tb\n" + "c\t" * 2_000 + "d\n").frame.width == 2_001


def test_many_quoted_cells_of_one_kind_are_quick():
    rows = 20_000
    text = "\n".join(f'x{i}\t"a\nb"\ty' for i in range(rows))  # a tab before each quote, no quote after a line end
    start = time.perf_counter()
    frame = parse_text(text).frame
    assert time.perf_counter() - start < 2
    assert frame.height == rows and frame[1, 1] == "a\nb"
    text = "\n".join(f'"a\nb"\tx{i}' for i in range(rows))  # and the other way round
    assert parse_text(text).frame.height == rows


def test_big_texts_with_many_quoted_cells_are_put_together_in_pieces(monkeypatch):
    monkeypatch.setattr(module, "_PROTECTED_PARTS", 4)
    frame = parse_text("\n".join(f'"a";"b{i}";c' for i in range(50))).frame
    assert frame.rows() == [("a", f"b{i}", "c") for i in range(50)]


# --- a new sheet ----------------------------------------------------------------------------------


def test_a_new_sheet():
    sheet = Sheet()
    assert (sheet.row_count, sheet.column_count) == (20, 3)
    assert sheet.names == ["Column 1", "Column 2", "Column 3"]
    assert not (sheet.header or sheet.has_values or sheet.changed or sheet.can_undo or sheet.can_redo)
    assert sheet.text(19, 2) == ""
    assert sheet.used_rows == 0
    assert sheet.frame().shape == (0, 1)


def test_spare_columns_at_the_right_are_not_written():
    sheet = Sheet()
    sheet.set_text(0, 0, "a")
    sheet.set_text(1, 0, "b")
    assert sheet.column_count == 3 and sheet.frame().columns == ["Column 1"]
    assert sheet.frame()["Column 1"].to_list() == ["a", "b"]
    sheet.set_text(5, 2, "c")  # the column in the middle stays
    assert sheet.frame().columns == ["Column 1", "Column 2", "Column 3"]
    sheet.undo()
    sheet.rename_column(2, "Amount")  # a name of its own: it stays, also with no value
    assert sheet.frame().columns == ["Column 1", "Column 2", "Amount"]
    sheet.undo()
    sheet.insert_columns(1)  # another number, also no value
    assert sheet.names == ["Column 1", "Column 4", "Column 2", "Column 3"]
    assert sheet.frame().columns == ["Column 1"]


def test_spare_columns_with_the_row_of_names():
    sheet = Sheet()
    sheet.load(parse_text("id\tname\n1\t2\n"))
    sheet.insert_columns(2)
    sheet.set_text(0, 0, "x")
    assert sheet.header and sheet.names == ["id", "name", "Column 3"]
    assert sheet.frame().columns == ["id", "name"]
    sheet.set_text(0, 2, "y")
    assert sheet.frame().columns == ["id", "name", "Column 3"]


def test_only_names_stay_without_rows():
    sheet = Sheet()
    sheet.load(parse_text("id\tname\n1\t2\n"))
    sheet.delete_rows([(0, 0)])
    assert sheet.frame().columns == ["id", "name"] and sheet.frame().height == 0


def test_from_frame_shows_a_file_as_text():
    frame = pl.DataFrame({"a": ["x", None], "b": [1.5, None], "c": [1, 2], "d": [True, None], "e": [[1], [2, 3]]})
    sheet = Sheet.from_frame(frame.with_columns(f=pl.lit(None)))
    assert sheet.names == ["a", "b", "c", "d", "e", "f"]
    assert sheet.view().rows() == [("x", "1.5", "1", "true", "[1]", None), (None, None, "2", None, "[2, 3]", None)]
    assert set(sheet.view().schema.values()) == {pl.String}
    assert not (sheet.header or sheet.changed or sheet.can_undo)


def test_from_a_frame_without_columns_or_rows():
    assert Sheet.from_frame(pl.DataFrame()).names == ["Column 1"]
    sheet = Sheet.from_frame(pl.DataFrame({"a": []}, schema={"a": pl.String}))
    assert (sheet.row_count, sheet.names) == (0, ["a"])


def test_reading_goes_by_blocks_of_each_column():
    sheet = Sheet.from_frame(
        pl.DataFrame(
            {"a": [f"r{i}" for i in range(BLOCK * 3)], "b": [None] * (BLOCK * 3)},
            schema={"a": pl.String, "b": pl.String},
        )
    )
    assert sheet.text(BLOCK * 2 + 5, 0) == f"r{BLOCK * 2 + 5}"
    assert sheet.text(BLOCK - 1, 1) == ""
    assert set(sheet._cache) == {(0, 2), (1, 0)}
    sheet.set_text(BLOCK * 2 + 5, 0, "new")
    assert sheet.text(BLOCK * 2 + 5, 0) == "new"
    assert sheet.text(BLOCK * 2 + 6, 0) == f"r{BLOCK * 2 + 6}"


def test_the_blocks_kept_are_limited(monkeypatch):
    monkeypatch.setattr(module, "_CACHED_BLOCKS", 3)
    sheet = Sheet(columns=5)
    for column in range(5):
        sheet.text(0, column)
    assert list(sheet._cache) == [(2, 0), (3, 0), (4, 0)]
    sheet.text(0, 2)
    sheet.text(0, 0)
    assert list(sheet._cache) == [(4, 0), (2, 0), (0, 0)]


def test_a_cell_holds_what_was_typed_with_one_kind_of_line_break():
    sheet = Sheet()
    sheet.set_text(0, 0, " a\r\nb\rc\nd ")
    assert sheet.text(0, 0) == " a\nb\nc\nd "
    sheet.set_text(0, 0, "   ")
    assert sheet.text(0, 0) == "   " and sheet.has_values
    sheet.set_text(0, 0, "")
    assert sheet.view()[0, 0] is None and not sheet.has_values


def test_a_call_that_changes_nothing_is_no_step():
    sheet = Sheet()
    sheet.set_text(0, 0, "")
    sheet.clear([(0, 19, 0, 2)])
    sheet.fill([(0, 19, 0, 2)], "")
    sheet.fill_down([(0, 4, 0, 1)])
    sheet.fill_right([(0, 4, 0, 1)])
    sheet.rename_column(0, "Column 1")
    sheet.set_header(False)
    sheet.delete_rows([(50, 60)])
    sheet.delete_columns([(7, 9)])
    sheet.insert_rows(0, 0)
    sheet.insert_columns(0, 0)
    assert not sheet.can_undo and not sheet.changed
    sheet.set_text(0, 0, "x")
    sheet.fill([(0, 0, 0, 0)], "x")
    sheet.set_text(0, 0, "x")
    sheet.paste(parse_text("x"), 0, 0)
    assert len(sheet._undo) == 1


def test_ranges_outside_the_sheet_are_cut_to_it():
    sheet = Sheet(columns=2, rows=2)
    sheet.fill([(-3, 9, -3, 0), (5, 9, 0, 1), (0, 0, 5, 6)], "x")
    assert sheet.view().rows() == [("x", None), ("x", None)]


def test_fill_writes_all_ranges_in_one_step():
    sheet = Sheet()
    sheet.fill([(0, 1, 0, 0), (1, 2, 0, 1)], "x")
    assert sheet.view().head(3).rows() == [("x", None, None), ("x", "x", None), ("x", "x", None)]
    sheet.undo()
    assert not sheet.has_values and not sheet.can_undo


def test_fill_down_and_right():
    sheet = Sheet.from_frame(pl.DataFrame({"a": ["1", "2", "3"], "b": ["4", None, "6"], "c": ["7", "8", "9"]}))
    sheet.fill_down([(0, 2, 1, 1)])
    assert sheet.view()["b"].to_list() == ["4", "4", "4"]
    sheet.undo()
    sheet.fill_down([(2, 2, 0, 1)])  # one row: from the row above it
    assert sheet.view().row(2) == ("2", None, "9")
    sheet.undo()
    before = sheet.view()
    sheet.fill_down([(0, 0, 0, 2)])  # the first row has nothing above it
    sheet.fill_right([(0, 2, 0, 0)])  # the first column has nothing on its left
    assert sheet.view().equals(before)
    sheet.fill_right([(0, 1, 0, 2)])
    assert sheet.view().rows() == [("1", "1", "1"), ("2", "2", "2"), ("3", "6", "9")]
    sheet.undo()
    sheet.fill_right([(1, 2, 2, 2)])  # one column: from the column on its left
    assert sheet.view().rows() == [("1", "4", "7"), ("2", None, None), ("3", "6", "6")]


def test_paste_writes_the_block_and_grows_the_sheet():
    sheet = Sheet(columns=2, rows=2)
    sheet.rename_column(1, "Column 3")
    assert sheet.paste(parse_text("a\tb\tc\nd\t\tf\ng\th\ti\n"), 1, 1) == (1, 3, 1, 3)
    assert sheet.names == ["Column 1", "Column 3", "Column 2", "Column 4"]
    assert sheet.view().rows() == [
        (None, None, None, None),
        (None, "a", "b", "c"),
        (None, "d", None, "f"),
        (None, "g", "h", "i"),
    ]
    sheet.undo()
    assert (sheet.row_count, sheet.column_count) == (2, 2)
    assert sheet.names == ["Column 1", "Column 3"]


def test_a_single_value_is_pasted_also_below_the_last_row():
    sheet = Sheet(columns=1, rows=1)
    assert sheet.paste(parse_text("x"), 1, 0) == (1, 1, 0, 0)
    assert sheet.view().rows() == [(None,), ("x",)]


def test_used_rows_says_where_the_next_block_goes():
    sheet = Sheet()
    sheet.set_text(4, 2, "x")
    assert sheet.used_rows == 5
    sheet.set_header(True)
    assert sheet.used_rows == 4


def test_insert_and_delete_rows():
    sheet = Sheet.from_frame(pl.DataFrame({"a": ["1", "2", "3", "4"], "b": ["5", "6", "7", "8"]}))
    sheet.insert_rows(1, 2)
    assert sheet.view()["a"].to_list() == ["1", None, None, "2", "3", "4"]
    sheet.insert_rows(99)
    assert sheet.row_count == 7
    sheet.delete_rows([(0, 0), (5, 2), (1, 2), (2, 3), (6, 90)])
    assert sheet.view()["a"].to_list() == ["3", "4"]
    sheet.delete_rows([(0, 1)])
    assert (sheet.row_count, sheet.column_count, sheet.frame().shape) == (0, 2, (0, 2))
    sheet.undo()
    sheet.undo()
    assert sheet.view()["a"].to_list() == ["1", None, None, "2", "3", "4", None]


def test_insert_and_delete_columns():
    sheet = Sheet(columns=2, rows=1)
    sheet.set_text(0, 1, "x")
    sheet.insert_columns(1)
    assert sheet.names == ["Column 1", "Column 3", "Column 2"]
    assert sheet.view().rows() == [(None, None, "x")]
    sheet.insert_columns(99, 2)
    assert sheet.names == ["Column 1", "Column 3", "Column 2", "Column 4", "Column 5"]
    sheet.delete_columns([(0, 0), (3, 2), (3, 4)])
    assert sheet.names == ["Column 3", "Column 2"]
    sheet.undo()
    assert sheet.column_count == 5


def test_a_sheet_keeps_one_column():
    sheet = Sheet()
    sheet.set_text(5, 0, "x")
    sheet.delete_columns([(0, 2)])
    assert (sheet.names, sheet.row_count, sheet.has_values) == (["Column 1"], 20, False)
    sheet.undo()
    assert sheet.column_count == 3 and sheet.text(5, 0) == "x"


def test_rename():
    sheet = Sheet()
    sheet.rename_column(0, "  Nr\n1\t ")
    assert sheet.names[0] == "Nr 1"
    for bad, message in (("  ", "A column needs a name"), ("Column 2", "A column named “Column 2” already exists")):
        with pytest.raises(SheetError) as error:
            sheet.rename_column(0, bad)
        assert str(error.value) == message
    assert sheet.names[0] == "Nr 1" and len(sheet._undo) == 1
    sheet.undo()
    assert sheet.names[0] == "Column 1"


def test_the_list_of_names_is_not_shared_with_old_states():
    sheet = Sheet()
    names = sheet.names
    sheet.rename_column(0, "x")
    assert names[0] == "Column 1" and sheet.names[0] == "x"


# --- the row of names ---------------------------------------------------------------------------


def test_load_sets_the_switch_from_the_first_row():
    sheet = Sheet()
    sheet.load(parse_text("Nr\tName\n1\ta\n2\tb\n"))
    assert sheet.header and sheet.names == ["Nr", "Name"]
    assert sheet.view().rows() == [("1", "a"), ("2", "b")]
    assert (sheet.row_count, sheet.column_count) == (2, 2)
    assert sheet.frame().columns == ["Nr", "Name"]
    for text in ("a\t\n1\t2\n", "a\tb\n", "Nr\n1\n2\n", "\tb\n1\t2\n"):
        sheet = Sheet()
        sheet.load(parse_text(text))
        assert not sheet.header, text
        assert sheet.names[:2] == ["Column 1", "Column 2"][: sheet.column_count]


def test_load_replaces_everything_and_is_one_step():
    sheet = Sheet()
    sheet.set_text(0, 0, "old")
    sheet.load(parse_text("a\tb\nc\td\n"))
    assert sheet.row_count == 1
    assert sheet.can_undo and not sheet.can_redo
    sheet.undo()
    assert (sheet.row_count, sheet.column_count, sheet.text(0, 0), sheet.header) == (20, 3, "old", False)


def test_the_switch_moves_no_data_in_both_directions():
    sheet = Sheet()
    sheet.load(parse_text("a\ta\t\nb\tc\td\n1\t2\t\n"))
    assert not sheet.header
    before = sheet.view().rows()
    sheet.set_header(True)
    assert sheet.names == ["a", "a (2)", "Column 3"]
    assert sheet.view().rows() == before[1:]
    sheet.set_header(False)
    assert sheet.names == ["Column 1", "Column 2", "Column 3"]
    assert sheet.view().rows() == before


def test_the_names_are_those_the_imports_read():
    many = Sheet(columns=5, rows=1)
    for column, text in enumerate([" x ", "y\nz", "   ", "\ty", "Column 1"]):
        many.set_text(0, column, text)
    many.set_header(True)
    assert many.names == ["x", "y z", "Column 3", "y", "Column 1"]
    expected = unique_names(["a", "a", "Column 3", "a"])
    many = Sheet(columns=4, rows=1)
    for column, text in enumerate(["a", "a", "", "a"]):
        many.set_text(0, column, text)
    many.set_header(True)
    assert many.names == expected


def test_the_switch_needs_a_row():
    sheet = Sheet(columns=2, rows=0)
    sheet.set_header(True)
    assert (sheet.row_count, sheet.header, sheet.names) == (0, True, ["Column 1", "Column 2"])
    sheet.set_header(False)
    assert sheet.row_count == 1
    sheet.undo()
    sheet.undo()
    assert (sheet.row_count, sheet.header) == (0, False)


def test_rename_with_the_switch_on_writes_into_the_row_of_names():
    sheet = Sheet()
    sheet.load(parse_text("a\tb\n1\t2\n"))
    sheet.rename_column(1, "c")
    assert sheet.names == ["a", "c"]
    assert sheet.view().rows() == [("1", "2")]
    sheet.set_header(False)
    assert sheet.view().rows() == [("a", "c"), ("1", "2")]
    sheet.undo()
    sheet.undo()
    assert sheet.names == ["a", "b"]
    with pytest.raises(SheetError):
        sheet.rename_column(1, "a")


def test_row_numbers_are_data_rows_while_the_switch_is_on():
    sheet = Sheet()
    sheet.load(parse_text("a\tb\n1\t2\n3\t4\n5\t6\n"))
    assert sheet.text(0, 0) == "1"
    sheet.set_text(0, 0, "x")
    sheet.fill([(1, 1, 0, 1)], "y")
    sheet.fill_down([(2, 2, 1, 1)])
    assert sheet.view().rows() == [("x", "2"), ("y", "y"), ("5", "y")]
    sheet.fill_down([(0, 0, 0, 1)])  # the row of names isn't a row above
    assert sheet.view().row(0) == ("x", "2")
    sheet.delete_rows([(0, 2)])
    assert sheet.row_count == 0 and sheet.names == ["a", "b"]
    sheet.insert_rows(0)
    assert sheet.names == ["a", "b"] and sheet.row_count == 1
    sheet.paste(parse_text("p\tq"), 3, 1)
    assert sheet.view().rows() == [(None, None, None), (None, None, None), (None, None, None), (None, "p", "q")]
    assert sheet.names == ["a", "b", "Column 3"]


def test_a_new_column_gets_its_name_from_the_rule_while_the_switch_is_on():
    sheet = Sheet()
    sheet.load(parse_text("a\tb\n1\t2\n"))
    sheet.insert_columns(1)
    assert sheet.names == ["a", "Column 2", "b"]
    sheet.rename_column(1, "c")
    sheet.set_header(False)
    assert sheet.view().row(0) == ("a", "c", "b")


# --- undo ---------------------------------------------------------------------------------------


def test_changed_follows_the_saved_state():
    sheet = Sheet()
    sheet.set_text(0, 0, "a")
    assert sheet.changed
    sheet.mark_saved()
    assert not sheet.changed
    sheet.set_text(0, 0, "b")
    assert sheet.changed
    sheet.undo()
    assert not sheet.changed
    sheet.undo()
    assert sheet.changed
    sheet.redo()
    assert not sheet.changed
    sheet.set_text(0, 1, "c")
    sheet.undo()
    assert not sheet.changed and sheet.can_redo
    sheet.set_text(0, 1, "c")
    assert not sheet.can_redo
    sheet.set_text(0, 1, "a")
    sheet.undo()
    sheet.undo()
    assert not sheet.changed


def test_a_new_edit_after_undo_has_its_own_state():
    sheet = Sheet()
    sheet.set_text(0, 0, "a")
    sheet.mark_saved()
    sheet.undo()
    sheet.set_text(0, 0, "b")
    assert sheet.changed


def test_undo_keeps_the_last_steps(monkeypatch):
    monkeypatch.setattr(module, "UNDO_STEPS", 3)
    sheet = Sheet()
    for number in range(6):
        sheet.set_text(0, 0, str(number))
    for expected in ("4", "3", "2"):
        sheet.undo()
        assert sheet.text(0, 0) == expected
    assert not sheet.can_undo
    sheet.undo()
    assert sheet.text(0, 0) == "2"
    for expected in ("3", "4", "5"):
        sheet.redo()
        assert sheet.text(0, 0) == expected
    assert not sheet.can_redo


def test_undo_keeps_steps_up_to_a_number_of_cells(monkeypatch):
    monkeypatch.setattr(module, "UNDO_CELLS", 100)
    sheet = Sheet(columns=2, rows=40)
    sheet.fill([(0, 39, 0, 0)], "a")
    sheet.fill([(0, 39, 0, 0)], "b")
    assert len(sheet._undo) == 2
    sheet.fill([(0, 39, 0, 0)], "c")
    assert len(sheet._undo) == 2
    sheet.fill([(0, 39, 0, 1)], "d")  # 80 cells: one more step has to go
    assert len(sheet._undo) == 1
    sheet.fill([(0, 39, 0, 1)], "e")
    sheet.fill([(0, 39, 0, 1)], "f")
    assert len(sheet._undo) == 1  # at least the last edit can be undone
    monkeypatch.setattr(module, "UNDO_CELLS", 10)
    sheet.fill([(0, 39, 0, 1)], "g")
    assert len(sheet._undo) == 1


def test_many_edits_of_a_column_stay_in_few_parts(monkeypatch):
    monkeypatch.setattr(module, "MAX_CHUNKS", 16)
    sheet = Sheet(columns=1, rows=1_000)
    for row in range(0, 1_000, 3):
        sheet.set_text(row, 0, str(row))
        assert sheet._columns[0].n_chunks() <= 17
    assert sheet.text(999, 0) == "999"


def test_a_failed_edit_changes_nothing(monkeypatch):
    sheet = Sheet()
    sheet.set_text(0, 0, "x")
    monkeypatch.setattr(module, "MAX_CELLS", 60)
    before = sheet.view()
    for edit in (
        lambda: sheet.insert_rows(0),
        lambda: sheet.insert_columns(0),
        lambda: sheet.paste(parse_text("a"), 20, 0),
        lambda: sheet.paste(parse_text("a"), 0, 3),
    ):
        with pytest.raises(SheetError) as error:
            edit()
        assert str(error.value) == "A sheet holds at most 60 cells."
    assert sheet.view().equals(before) and len(sheet._undo) == 1
    monkeypatch.setattr(module, "MAX_CELLS", 1_000)
    monkeypatch.setattr(module, "MAX_COLUMNS", 3)
    with pytest.raises(SheetError) as error:
        sheet.insert_columns(0)
    assert str(error.value) == "A sheet holds at most 3 columns."


# --- every edit, against a plain list ---------------------------------------------------------------


class Plain:
    """A sheet as a list of rows, the slow and obvious way."""

    def __init__(self, columns, rows):
        self.rows = [[None] * columns for _ in range(rows)]
        self.free = [f"Column {i}" for i in range(1, columns + 1)]
        self.header = False

    @property
    def width(self):
        return len(self.free)

    @property
    def offset(self):
        return 1 if self.header else 0

    @property
    def data(self):
        return self.rows[self.offset :]

    @property
    def names(self):
        if not self.header:
            return list(self.free)
        cells = [(text or "").replace("\n", " ").replace("\t", " ").strip() for text in self.rows[0]]
        return unique_names([text or f"Column {i}" for i, text in enumerate(cells, 1)])

    def everything(self):
        return [tuple(row) for row in self.rows], list(self.free), self.header

    def fresh(self, count):
        return [name for name in (f"Column {i}" for i in range(1, self.width + count + 1)) if name not in self.free][
            :count
        ]

    def write(self, ranges, value):
        for top, bottom, left, right in ranges:
            for row in self.data[top : bottom + 1]:
                row[left : right + 1] = [value] * (right - left + 1)

    def fill_down(self, ranges):
        for top, bottom, left, right in ranges:
            source, first = (top - 1, top) if top == bottom else (top, top + 1)
            if source >= 0:
                for row in self.data[first : bottom + 1]:
                    row[left : right + 1] = self.data[source][left : right + 1]

    def fill_right(self, ranges):
        for top, bottom, left, right in ranges:
            source, first = (left - 1, left) if left == right else (left, left + 1)
            if source >= 0:
                for row in self.data[top : bottom + 1]:
                    row[first : right + 1] = [row[source]] * (right - first + 1)

    def insert_rows(self, at, count):
        at = min(at, len(self.data)) + self.offset
        self.rows[at:at] = [[None] * self.width for _ in range(count)]

    def delete_rows(self, spans):
        gone = {row for first, last in spans for row in range(first, last + 1)}
        self.rows[self.offset :] = [row for number, row in enumerate(self.data) if number not in gone]

    def insert_columns(self, at, count):
        self.free[at:at] = self.fresh(count)
        for row in self.rows:
            row[at:at] = [None] * count

    def delete_columns(self, spans):
        gone = {column for first, last in spans for column in range(first, last + 1)}
        self.free = [name for number, name in enumerate(self.free) if number not in gone]
        self.rows = [[cell for number, cell in enumerate(row) if number not in gone] for row in self.rows]

    def paste(self, block, row, column):
        height, width = len(block), len(block[0])
        added = max(column + width - self.width, 0)
        if added:
            self.free += self.fresh(added)
            for line in self.rows:
                line += [None] * added
        top = row + self.offset
        while len(self.rows) < top + height:
            self.rows.append([None] * self.width)
        for r, line in enumerate(block):
            self.rows[top + r][column : column + width] = line

    def rename(self, column, name):
        if self.header:
            self.rows[0][column] = name
        else:
            self.free[column] = name

    def set_header(self, on):
        self.header = on
        if on and not self.rows:
            self.rows.append([None] * self.width)


TEXTS = [None, "x", "y", "00123", " sp ", "   ", "a\nb", 'q"r', "1,50", "ÄÖ", "\U0001f600", "z" * 40]


def block_text(rng, height, width):
    block = [[rng.choice(TEXTS) for _ in range(width)] for _ in range(height)]
    block[-1][-1] = "last"  # so that nothing at the end is trimmed
    block[0][-1] = block[0][-1] or "w"
    return block


def pasted(block):
    frame = pl.DataFrame(
        {f"c{i}": [row[i] for row in block] for i in range(len(block[0]))},
        schema={f"c{i}": pl.String for i in range(len(block[0]))},
    )
    return Pasted(frame, "\t")


def one_range(rng, plain):
    rows, columns = len(plain.data), plain.width
    top, left = rng.randrange(rows), rng.randrange(columns)
    return top, min(top + rng.randrange(6), rows - 1), left, min(left + rng.randrange(3), columns - 1)


def random_edit(rng, sheet, plain):
    """One random edit of both, of a kind that fits the shape. Returns its name."""
    rows, columns = len(plain.data), plain.width
    kinds = ["insert_rows", "insert_columns", "paste", "header"]
    if rows:
        kinds += ["set_text"] * 8 + ["fill", "fill", "clear", "fill_down", "fill_right", "delete_rows"]
    if columns > 1:
        kinds.append("delete_columns")
    if rows and not plain.header:
        kinds.append("rename")
    kind = rng.choice(kinds)
    if kind == "set_text":
        row, column, value = rng.randrange(rows), rng.randrange(columns), rng.choice(TEXTS)
        sheet.set_text(row, column, value or "")
        plain.write([(row, row, column, column)], value)
    elif kind in ("fill", "clear"):
        ranges = [one_range(rng, plain) for _ in range(rng.randint(1, 3))]
        value = rng.choice(TEXTS) if kind == "fill" else None
        sheet.fill(ranges, value or "") if kind == "fill" else sheet.clear(ranges)
        plain.write(ranges, value)
    elif kind == "fill_down":
        ranges = [one_range(rng, plain)]
        sheet.fill_down(ranges)
        plain.fill_down(ranges)
    elif kind == "fill_right":
        ranges = [one_range(rng, plain)]
        sheet.fill_right(ranges)
        plain.fill_right(ranges)
    elif kind == "insert_rows":
        at, count = rng.randint(0, rows), rng.randint(1, 3)
        sheet.insert_rows(at, count)
        plain.insert_rows(at, count)
    elif kind == "delete_rows":
        spans = []
        for _ in range(rng.randint(1, 3)):
            first = rng.randrange(rows)
            spans.append((first, min(first + rng.randrange(4), rows - 1)))
        sheet.delete_rows(spans)
        plain.delete_rows(spans)
    elif kind == "insert_columns":
        at, count = rng.randint(0, columns), rng.randint(1, 2)
        sheet.insert_columns(at, count)
        plain.insert_columns(at, count)
    elif kind == "delete_columns":
        first = rng.randrange(columns)
        span = (first, min(first + rng.randrange(2), columns - 1))
        if span != (0, columns - 1):
            sheet.delete_columns([span])
            plain.delete_columns([span])
    elif kind == "paste":
        block = block_text(rng, rng.randint(1, 4), rng.randint(1, 4))
        row, column = rng.randint(0, rows), rng.randint(0, columns)
        assert sheet.paste(pasted(block), row, column) == (
            row,
            row + len(block) - 1,
            column,
            column + len(block[0]) - 1,
        )
        plain.paste(block, row, column)
    elif kind == "rename":
        column, name = rng.randrange(columns), f"N{rng.randrange(1000)}"
        if name not in plain.names:
            sheet.rename_column(column, name)
            plain.rename(column, name)
    elif kind == "header":
        sheet.set_header(not sheet.header)
        plain.set_header(not plain.header)
    return kind


def same(sheet, plain):
    rows = [tuple(row) for row in plain.data]
    assert sheet.header == plain.header
    assert sheet.names == plain.names
    assert (sheet.row_count, sheet.column_count) == (len(rows), plain.width)
    assert sheet.view().rows() == rows
    assert sheet.view().columns == plain.names


def see_cells(rng, sheet, plain):
    """Reads some cells, so that the blocks are filled and each edit must clear them."""
    for _ in range(4):
        if plain.data:
            row, column = rng.randrange(len(plain.data)), rng.randrange(plain.width)
            assert sheet.text(row, column) == (plain.data[row][column] or "")


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_two_thousand_random_edits_against_a_plain_list(monkeypatch, seed):
    monkeypatch.setattr(module, "UNDO_STEPS", 10_000)
    monkeypatch.setattr(module, "UNDO_CELLS", 10**9)
    monkeypatch.setattr(module, "MAX_CHUNKS", 8)  # so that putting parts together is tested too
    rng = random.Random(seed)
    sheet, plain = Sheet(columns=3, rows=8), Plain(3, 8)
    history, undone = [copy.deepcopy(plain)], []
    for _ in range(2_000):
        if plain.width > 12 or len(plain.rows) > 60:  # small enough to look at every cell
            sheet, plain = Sheet(columns=3, rows=8), Plain(3, 8)
            history, undone = [copy.deepcopy(plain)], []
        steps, chance = len(sheet._undo), rng.random()
        if chance < 0.15 and steps:
            sheet.undo()
            undone.append(history.pop())
            plain = copy.deepcopy(history[-1])
        elif chance < 0.25 and undone:
            sheet.redo()
            history.append(undone.pop())
            plain = copy.deepcopy(history[-1])
        else:
            before = plain.everything()
            kind = random_edit(rng, sheet, plain)
            changed = plain.everything() != before
            assert (len(sheet._undo) == steps + 1) == changed, kind
            if changed:
                history.append(copy.deepcopy(plain))
                undone.clear()
        same(sheet, plain)
        see_cells(rng, sheet, plain)
    for earlier in reversed(history[:-1]):
        sheet.undo()
        same(sheet, earlier)
    assert not sheet.can_undo
    while sheet.can_redo:
        sheet.redo()
    same(sheet, history[-1])


# --- the mark -----------------------------------------------------------------------------------


def step(file="a.xlsx", marked="a.xlsx", **others):
    return {"source": "file", "file": file, files.SHEET_KEY: {"file": marked, "stamp": "1-2"}, **others}


def test_source_format_of_a_marked_step():
    assert source_format("excel.import", step()) is FORMATS[".xlsx"]
    assert source_format("csv.import", step("x/b.CSV", "b.csv")) is FORMATS[".csv"]
    assert source_format("csv.import", step("b.txt", "B.TXT")) is FORMATS[".txt"]
    assert source_format("parquet.import", step("b.parquet", "b.parquet")) is FORMATS[".parquet"]


@pytest.mark.parametrize(
    ("action", "params"),
    [
        ("excel.import", {"source": "file", "file": "a.xlsx"}),  # no mark
        ("excel.import", step(marked="other.xlsx")),  # another file was chosen in the step
        ("csv.import", step()),  # another action
        ("excel.import", step("a.xlsm", "a.xlsm")),  # no format of a sheet
        ("excel.import", step(source="folder")),
        ("excel.import", {"file": "a.xlsx", files.SHEET_KEY: "yes"}),
        ("excel.import", {"source": "file", "file": None, files.SHEET_KEY: {"file": "a.xlsx"}}),
    ],
)
def test_source_format_of_other_steps(action, params):
    assert source_format(action, params) is None


def test_stamp_and_mark(tmp_path):
    path = tmp_path / "a.csv"
    assert stamp_of(path) is None
    assert mark(path) == {"file": "a.csv", "stamp": None}
    path.write_text("x", encoding="utf-8")
    stamp = stamp_of(path)
    assert stamp.endswith("-1") and mark(path) == {"file": "a.csv", "stamp": stamp}
    path.write_text("xy", encoding="utf-8")
    assert stamp_of(path) != stamp


def test_the_formats_of_the_choice():
    assert list(FORMATS) == [".xlsx", ".csv", ".txt", ".parquet"]
    assert [f.label for f in FORMATS.values()] == [
        "Excel workbook (.xlsx)",
        "CSV file (.csv)",
        "Text file with tabs (.txt)",
        "Parquet file (.parquet)",
    ]
    assert all(key == f.suffix for key, f in FORMATS.items())


def test_no_qt_in_the_sheet():
    assert "PySide" not in Path(module.__file__).read_text(encoding="utf-8")


# --- speed --------------------------------------------------------------------------------------


def test_edits_of_a_big_sheet_are_quick():
    rows = 200_000
    frame = pl.DataFrame({f"c{i}": pl.repeat(f"value {i}", rows, dtype=pl.String, eager=True) for i in range(5)})
    sheet = Sheet()
    sheet.load(Pasted(frame, "\t"))
    start = time.perf_counter()
    for row in range(0, 200):
        sheet.set_text(row * 100, 2, "x")
    sheet.insert_rows(5)
    sheet.delete_rows([(10, 20)])
    sheet.fill([(0, rows - 20, 1, 1)], "y")
    for _ in range(20):
        sheet.undo()
    assert time.perf_counter() - start < 2
    assert sheet.text(18_200, 2) == "x" and sheet.text(18_300, 2) == "value 2"
